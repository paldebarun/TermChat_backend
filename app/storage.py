"""
Object storage layer for chat attachments (SeaweedFS, S3-compatible).

FastAPI is the control plane: it creates/tracks/completes/aborts multipart
uploads and hands out presigned URLs. It never proxies file bytes - those
go directly between the browser and SeaweedFS (the data plane).

Two boto3 clients are used deliberately:

- `_internal_client` talks to SeaweedFS over the docker network
  (S3_INTERNAL_ENDPOINT, e.g. http://seaweedfs:8333) for calls the backend
  makes directly: create/list/complete/abort multipart upload, delete,
  bucket setup. Nothing here produces a URL the browser has to resolve.

- `_presign_client` is configured with S3_PUBLIC_ENDPOINT (e.g.
  http://localhost:8333) and is used ONLY for generate_presigned_url().
  SigV4 presigned URLs sign the Host header as part of the request, so
  the endpoint used to *generate* the URL must match the host the browser
  will actually connect to - otherwise SeaweedFS rejects the signature.
"""
import logging

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_boto_config = BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})


def _make_client(endpoint_url: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=_boto_config,
    )


_internal_client = _make_client(settings.s3_internal_endpoint)
_presign_client = _make_client(settings.s3_public_endpoint)


def ensure_bucket() -> None:
    """Idempotently make sure the attachments bucket exists. Called at app
    startup as a safety net alongside the docker-compose `createbuckets`
    init container - either one alone is sufficient."""
    try:
        _internal_client.head_bucket(Bucket=settings.s3_bucket_name)
    except ClientError:
        _internal_client.create_bucket(Bucket=settings.s3_bucket_name)
        logger.info("Created SeaweedFS bucket %s", settings.s3_bucket_name)


def create_multipart_upload(key: str, content_type: str) -> str:
    """Starts a multipart upload session in SeaweedFS and returns its upload ID
    (distinct from our own DB-level file_id/upload_id)."""
    resp = _internal_client.create_multipart_upload(
        Bucket=settings.s3_bucket_name,
        Key=key,
        ContentType=content_type or "application/octet-stream",
    )
    return resp["UploadId"]


def generate_part_urls(key: str, s3_upload_id: str, part_numbers: list[int]) -> dict[int, str]:
    """Batch-generate presigned PUT URLs for the given part numbers, so the
    client isn't making one backend round-trip per chunk."""
    return {
        part_number: _presign_client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings.s3_bucket_name,
                "Key": key,
                "UploadId": s3_upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=settings.presigned_url_expiry_seconds,
        )
        for part_number in part_numbers
    }


def list_parts(key: str, s3_upload_id: str) -> list[dict]:
    """Authoritative source of truth for which parts have actually landed
    in storage - used to drive both the resume flow and validation at
    complete time. Paginates since S3-compatible APIs cap a single
    ListParts response at 1000 parts."""
    parts: list[dict] = []
    part_number_marker = 0
    while True:
        resp = _internal_client.list_parts(
            Bucket=settings.s3_bucket_name,
            Key=key,
            UploadId=s3_upload_id,
            PartNumberMarker=part_number_marker,
        )
        parts.extend(
            {"part_number": p["PartNumber"], "etag": p["ETag"], "size": p["Size"]}
            for p in resp.get("Parts", [])
        )
        if not resp.get("IsTruncated"):
            break
        part_number_marker = resp["NextPartNumberMarker"]
    return parts


def complete_multipart_upload(key: str, s3_upload_id: str, parts: list[dict]) -> None:
    ordered = sorted(parts, key=lambda p: p["part_number"])
    _internal_client.complete_multipart_upload(
        Bucket=settings.s3_bucket_name,
        Key=key,
        UploadId=s3_upload_id,
        MultipartUpload={
            "Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in ordered]
        },
    )


def abort_multipart_upload(key: str, s3_upload_id: str) -> None:
    """Aborts the multipart upload so SeaweedFS reclaims the already-uploaded
    parts itself - no need to enumerate and delete each part manually."""
    _internal_client.abort_multipart_upload(
        Bucket=settings.s3_bucket_name,
        Key=key,
        UploadId=s3_upload_id,
    )


def delete_object(key: str) -> None:
    """Deletes a completed object - used both for explicit removal (user
    detaches an already-completed upload before sending) and for the
    background orphan-cleanup sweep."""
    _internal_client.delete_object(Bucket=settings.s3_bucket_name, Key=key)


def generate_download_url(key: str, filename: str) -> str:
    return _presign_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=settings.presigned_url_expiry_seconds,
    )


def download_object_bytes_by_key(key: str, max_bytes: int | None = None) -> bytes:
    """Direct (non-presigned) download for server-side processing - used
    only by the document-indexing/search pipeline (app/assistant/).

    Raises ValueError if the object is larger than the limit instead of
    silently truncating (a truncated PDF/DOCX just fails to parse with a
    misleading error, and a truncated read made the size check unreachable).
    """
    limit = max_bytes if max_bytes is not None else settings.assistant_max_document_bytes
    obj = _internal_client.get_object(Bucket=settings.s3_bucket_name, Key=key)
    body = obj["Body"]
    try:
        if int(obj.get("ContentLength", 0)) > limit:
            raise ValueError("document exceeds configured indexing size limit")
        data = body.read(limit + 1)
        if len(data) > limit:
            raise ValueError("document exceeds configured indexing size limit")
        return data
    finally:
        body.close()
