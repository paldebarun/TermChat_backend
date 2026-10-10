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

    # --- S3-compatible object storage (SeaweedFS) ----------------------------
    # Must match the identity in seaweedfs/s3.json. A real deployment should
    # use scoped credentials instead of an admin identity - see README.
    s3_access_key: str = "seaweedadmin"
    s3_secret_key: str = "seaweedadmin123"

    # Used by the backend for direct API calls over the docker network
    # (create/list/complete/abort multipart upload, delete object).
    s3_internal_endpoint: str = "http://seaweedfs:8333"

    # Used ONLY to generate presigned URLs. This MUST be a host the
    # browser can actually reach - SigV4 presigned URLs sign the Host
    # header, so a URL signed for "seaweedfs:8333" (the docker-network alias)
    # will fail validation if the browser is sent to "localhost:8333"
    # instead. In production this would be your public S3 domain.
    s3_public_endpoint: str = "http://localhost:8333"

    s3_bucket_name: str = "chat-attachments"
    s3_region: str = "us-east-1"

    presigned_url_expiry_seconds: int = 3600
    upload_default_chunk_size_mb: int = 10
    upload_init_batch_size: int = 20  # presigned URLs returned immediately on /uploads/init

    orphan_cleanup_interval_minutes: int = 30
    orphan_cleanup_threshold_minutes: int = 60

    group_max_members: int = 256

    assistant_hermes_model: str = "nousresearch/hermes-4-405b"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    assistant_scope_token_secret: str = ""
    assistant_scope_token_ttl_seconds: int = 300
    assistant_context_ttl_seconds: int = 120
    assistant_allow_message_context: bool = False
    assistant_max_context_messages: int = 100
    assistant_max_context_bytes: int = 1_000_000
    assistant_max_history_bytes: int = 40_000
    assistant_recent_messages_limit: int = 20
    assistant_max_search_results: int = 8
    assistant_run_timeout_seconds: int = 180
    assistant_max_concurrent_runs: int = 4
    assistant_max_iterations: int = 12
    assistant_max_output_tokens: int = 1200
    # After the tool-calling agent finishes, a second tool-less model call
    # merges its tool results into one answer. Model empty = the agent's model.
    assistant_synthesizer_enabled: bool = True
    assistant_synthesizer_model: str = ""
    assistant_synthesizer_max_input_chars: int = 60_000
    # Interpreter of the isolated Hermes venv. Empty = use /opt/hermes-venv if
    # it exists (Docker image), else this process's interpreter (local dev).
    assistant_hermes_python: str = ""
    # How long a request may wait for a free run slot before getting a 429.
    assistant_queue_timeout_seconds: int = 30
    assistant_mcp_internal_url: str = "http://127.0.0.1:8000/mcp/"
    assistant_document_indexing_enabled: bool = True
    assistant_max_document_bytes: int = 25 * 1024 * 1024
    assistant_max_document_chars: int = 500_000
    assistant_chunk_size_chars: int = 1_200
    assistant_chunk_overlap_chars: int = 200
    # Page-level parsing (Docling for documents, Whisper for audio) runs in an
    # isolated venv: torch/transformers pins conflict with chromadb's.
    # Empty = /opt/parser-venv if it exists (Docker image), else this interpreter.
    assistant_parser_python: str = ""
    assistant_parse_timeout_seconds: int = 600
    assistant_max_concurrent_parses: int = 1
    assistant_max_parse_pages: int = 500
    assistant_whisper_model: str = "base"
    assistant_max_audio_bytes: int = 200 * 1024 * 1024
    assistant_audio_page_seconds: int = 300

    chroma_host: str = "chroma"
    chroma_port: int = 8000
    chroma_collection_name: str = "chat_documents"


@lru_cache
def get_settings() -> Settings:
    return Settings()
