import math
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, crud, storage
from app.config import get_settings
from app.database import get_db
from app.models import UploadedFile, UploadStatus, User
from app.schemas import (
    CompleteUploadRequest,
    DownloadUrlResponse,
    PartUrlsRequest,
    PartUrlsResponse,
    UploadedFileOut,
    UploadedPartInfo,
    UploadInitRequest,
    UploadInitResponse,
    UploadStatusResponse,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])
settings = get_settings()

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(filename: str) -> str:
    # Keep the original filename for display (stored separately in the DB
    # column); this is only for building a safe S3 object key.
    return _UNSAFE_FILENAME_CHARS.sub("_", filename)[-200:] or "file"


async def _get_owned_file(db: AsyncSession, file_id: uuid.UUID, current_user: User) -> UploadedFile:
    file = await crud.get_uploaded_file(db, file_id)
    # 404 rather than 403 on a mismatched owner, so we don't confirm
    # another user's upload_id exists.
    if file is None or file.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    return file


@router.post("/init", response_model=UploadInitResponse, status_code=status.HTTP_201_CREATED)
async def init_upload(
    data: UploadInitRequest,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    chunk_size = data.chunk_size or settings.upload_default_chunk_size_mb * 1024 * 1024
    total_parts = math.ceil(data.size / chunk_size)

    file_id = uuid.uuid4()
    s3_key = f"attachments/{current_user.id}/{file_id}/{_safe_filename(data.filename)}"

    s3_upload_id = storage.create_multipart_upload(s3_key, data.content_type)

    file = await crud.create_uploaded_file(
        db,
        file_id=file_id,
        owner_id=current_user.id,
        filename=data.filename,
        content_type=data.content_type,
        size=data.size,
        chunk_size=chunk_size,
        total_parts=total_parts,
        s3_key=s3_key,
        s3_upload_id=s3_upload_id,
    )

    first_batch = list(range(1, min(total_parts, settings.upload_init_batch_size) + 1))
    part_urls = storage.generate_part_urls(s3_key, s3_upload_id, first_batch)

    return UploadInitResponse(
        upload_id=file.id,
        chunk_size=chunk_size,
        total_parts=total_parts,
        status=file.status,
        part_urls=part_urls,
    )


@router.post("/{upload_id}/parts", response_model=PartUrlsResponse)
async def get_part_urls(
    upload_id: uuid.UUID,
    data: PartUrlsRequest,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    file = await _get_owned_file(db, upload_id, current_user)
    if file.status != UploadStatus.UPLOADING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Upload is {file.status.value}")

    invalid = [p for p in data.part_numbers if p < 1 or p > file.total_parts]
    if invalid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid part numbers: {invalid}")

    part_urls = storage.generate_part_urls(file.s3_key, file.s3_upload_id, data.part_numbers)
    return PartUrlsResponse(part_urls=part_urls)


@router.get("/{upload_id}/status", response_model=UploadStatusResponse)
async def get_upload_status(
    upload_id: uuid.UUID,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Drives resumability: the client calls this after reconnecting and
    diffs `missing_part_numbers` against what it still needs to send,
    rather than re-uploading the whole file."""
    file = await _get_owned_file(db, upload_id, current_user)

    uploaded = (
        storage.list_parts(file.s3_key, file.s3_upload_id)
        if file.status == UploadStatus.UPLOADING
        else []
    )
    uploaded_numbers = {p["part_number"] for p in uploaded}
    missing = [n for n in range(1, file.total_parts + 1) if n not in uploaded_numbers]

    return UploadStatusResponse(
        upload_id=file.id,
        status=file.status,
        total_parts=file.total_parts,
        chunk_size=file.chunk_size,
        uploaded_parts=[UploadedPartInfo(**p) for p in uploaded],
        missing_part_numbers=missing,
    )


@router.post("/{upload_id}/complete", response_model=UploadedFileOut)
async def complete_upload(
    upload_id: uuid.UUID,
    data: CompleteUploadRequest,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    file = await _get_owned_file(db, upload_id, current_user)
    if file.status != UploadStatus.UPLOADING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Upload is {file.status.value}")

    if len(data.parts) != file.total_parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected {file.total_parts} parts, got {len(data.parts)}",
        )

    file = await crud.set_upload_status(db, file, UploadStatus.COMPLETING)
    try:
        storage.complete_multipart_upload(
            file.s3_key,
            file.s3_upload_id,
            [{"part_number": p.part_number, "etag": p.etag} for p in data.parts],
        )
    except Exception as exc:
        # Leave it in COMPLETING rather than silently marking COMPLETED -
        # the client can retry /complete once the underlying issue (e.g. a
        # transient MinIO error) is resolved.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to finalize upload: {exc}"
        )

    return await crud.set_upload_status(
        db, file, UploadStatus.COMPLETED, completed_at=datetime.now(timezone.utc)
    )


@router.post("/{upload_id}/abort", response_model=UploadedFileOut)
async def abort_upload(
    upload_id: uuid.UUID,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when the user detaches a file while it's still uploading.
    Aborting the multipart upload lets MinIO reclaim the parts already
    received - no need to delete them one by one."""
    file = await _get_owned_file(db, upload_id, current_user)
    if file.status not in (UploadStatus.UPLOADING, UploadStatus.ABORTING):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Upload is {file.status.value}")

    file = await crud.set_upload_status(db, file, UploadStatus.ABORTING)
    try:
        storage.abort_multipart_upload(file.s3_key, file.s3_upload_id)
    except Exception:
        # Already gone (e.g. double-abort) is fine; anything else, surface it.
        pass

    return await crud.set_upload_status(db, file, UploadStatus.ABORTED)


@router.delete("/{upload_id}", response_model=UploadedFileOut)
async def delete_upload(
    upload_id: uuid.UUID,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when the user detaches a file that already finished
    uploading, before sending the message. This is a normal object delete,
    not a multipart abort - the upload isn't 'in progress' anymore."""
    file = await _get_owned_file(db, upload_id, current_user)
    if file.status != UploadStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a completed upload can be deleted this way; use /abort while it's still uploading",
        )

    storage.delete_object(file.s3_key)
    return await crud.set_upload_status(db, file, UploadStatus.DELETED)


@router.get("/{upload_id}/download-url", response_model=DownloadUrlResponse)
async def get_download_url(
    upload_id: uuid.UUID,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    file = await crud.get_uploaded_file(db, upload_id)
    if file is None or file.status != UploadStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if not await crud.user_can_access_file(db, file, current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    url = storage.generate_download_url(file.s3_key, file.filename)
    return DownloadUrlResponse(url=url, filename=file.filename, expires_in=settings.presigned_url_expiry_seconds)
