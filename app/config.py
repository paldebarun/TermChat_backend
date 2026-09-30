from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://chat_user:chat_password@db:5432/chat_db"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    cors_origins: list[str] = ["*"]

    # --- MinIO / S3-compatible object storage -------------------------------
    # The root user doubles as the access/secret key pair, matching MinIO's
    # default behavior (a real deployment should mint a scoped service
    # account instead of using the root credentials - see README).
    minio_root_user: str = "minioadmin"
    minio_root_password: str = "minioadmin123"

    # Used by the backend for direct API calls over the docker network
    # (create/list/complete/abort multipart upload, delete object).
    minio_internal_endpoint: str = "http://minio:9000"

    # Used ONLY to generate presigned URLs. This MUST be a host the
    # browser can actually reach - SigV4 presigned URLs sign the Host
    # header, so a URL signed for "minio:9000" (the docker-network alias)
    # will fail validation if the browser is sent to "localhost:9000"
    # instead. In production this would be your public MinIO/S3 domain.
    minio_public_endpoint: str = "http://localhost:9000"

    minio_bucket_name: str = "chat-attachments"
    minio_region: str = "us-east-1"

    presigned_url_expiry_seconds: int = 3600
    upload_default_chunk_size_mb: int = 10
    upload_init_batch_size: int = 20  # presigned URLs returned immediately on /uploads/init

    orphan_cleanup_interval_minutes: int = 30
    orphan_cleanup_threshold_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
