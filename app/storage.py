"""
Object storage layer for chat attachments (MinIO, S3-compatible).

FastAPI is the control plane: it creates/tracks/completes/aborts multipart
uploads and hands out presigned URLs. It never proxies file bytes - those
go directly between the browser and MinIO (the data plane).

Two boto3 clients are used deliberately:

- `_internal_client` talks to MinIO over the docker network
  (MINIO_INTERNAL_ENDPOINT, e.g. http://minio:9000) for calls the backend
  makes directly: create/list/complete/abort multipart upload, delete,
  bucket setup. Nothing here produces a URL the browser has to resolve.

- `_presign_client` is configured with MINIO_PUBLIC_ENDPOINT (e.g.
  http://localhost:9000) and is used ONLY for generate_presigned_url().
  SigV4 presigned URLs sign the Host header as part of the request, so
  the endpoint used to *generate* the URL must match the host the browser
  will actually connect to - otherwise MinIO rejects the signature.
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
        aws_access_key_id=settings.minio_root_user,
        aws_secret_access_key=settings.minio_root_password,
        region_name=settings.minio_region,
        config=_boto_config,
    )


_internal_client = _make_client(settings.minio_internal_endpoint)
_presign_client = _make_client(settings.minio_public_endpoint)


def ensure_bucket() -> None:
    """Idempotently make sure the attachments bucket exists. Called at app
    startup as a safety net alongside the docker-compose `createbuckets`
    init container - either one alone is sufficient."""
    try:
        _internal_client.head_bucket(Bucket=settings.minio_bucket_name)
    except ClientError:
        _internal_client.create_bucket(Bucket=settings.minio_bucket_name)
        logger.info("Created MinIO bucket %s", settings.minio_bucket_name)


def create_multipart_upload(key: str, content_type: str) -> str:
    """Starts a multipart upload session in MinIO and returns its upload ID
    (distinct from our own DB-level file_id/upload_id)."""
    resp = _internal_client.create_multipart_upload(
        Bucket=settings.minio_bucket_name,
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
                "Bucket": settings.minio_bucket_name,
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
            Bucket=settings.minio_bucket_name,
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
        Bucket=settings.minio_bucket_name,
        Key=key,
        UploadId=s3_upload_id,
        MultipartUpload={
            "Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in ordered]
        },
    )


def abort_multipart_upload(key: str, s3_upload_id: str) -> None:
    """Aborts the multipart upload so MinIO reclaims the already-uploaded
    parts itself - no need to enumerate and delete each part manually."""
    _internal_client.abort_multipart_upload(
        Bucket=settings.minio_bucket_name,
        Key=key,
        UploadId=s3_upload_id,
    )


def delete_object(key: str) -> None:
    """Deletes a completed object - used both for explicit removal (user
    detaches an already-completed upload before sending) and for the
    background orphan-cleanup sweep."""
    _internal_client.delete_object(Bucket=settings.minio_bucket_name, Key=key)


def generate_download_url(key: str, filename: str) -> str:
    return _presign_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.minio_bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=settings.presigned_url_expiry_seconds,
    )
