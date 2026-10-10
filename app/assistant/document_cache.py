"""Parse-once cache for shared documents.

The first request for a file (upload-time indexing or the agent's
get_document_content, whichever comes first) parses it page by page and
stores the result in Postgres under its canonical object URL; every later
request is served from there without touching S3 or the parser.

  documents (PDF, Office, HTML, Markdown, CSV, images) -> Docling
  audio                                                -> Whisper transcript
  code / plain text / JSON                             -> extract_text, one page
  video                                                -> metadata only, never cached
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path

from app import crud, storage
from app.assistant.document_extract import UnsupportedDocument, extract_text
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import ParsedDocument, ParseStatus, UploadedFile

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).resolve().parents[2]
_DOCKER_PARSER_PYTHON = Path("/opt/parser-venv/bin/python")
_EXIT_UNSUPPORTED = 3

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wma"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv"}
DOCLING_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".markdown", ".csv",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
}
TEXT_SUFFIXES = {".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".json", ".jsonl"}
_DOCLING_CTYPES = ("pdf", "wordprocessingml", "presentationml", "spreadsheetml", "text/html", "text/markdown", "text/csv")


@dataclass
class MediaInfo:
    """Returned instead of a parse for media that isn't transcribed."""

    kind: str
    filename: str
    content_type: str
    size: int


def classify(filename: str, content_type: str) -> str:
    """"audio" | "video" | "docling" | "text" | "unsupported". The suffix
    wins over the client-supplied content type."""
    suffix = Path(filename).suffix.lower()
    ctype = (content_type or "").lower()
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in DOCLING_SUFFIXES:
        return "docling"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if ctype.startswith("audio/"):
        return "audio"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("image/") or any(t in ctype for t in _DOCLING_CTYPES):
        return "docling"
    if ctype.startswith("text/") or "json" in ctype:
        return "text"
    return "unsupported"


def document_url(file: UploadedFile) -> str:
    return f"s3://{get_settings().s3_bucket_name}/{file.s3_key}"


def _parser_python() -> str:
    configured = get_settings().assistant_parser_python
    if configured:
        return configured
    return str(_DOCKER_PARSER_PYTHON) if _DOCKER_PARSER_PYTHON.exists() else sys.executable


_PARSE_SEMAPHORE: asyncio.Semaphore | None = None
# One lock per URL so the upload indexer and the agent never parse the same
# file twice concurrently; weak values let unused locks disappear.
_URL_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _semaphore() -> asyncio.Semaphore:
    global _PARSE_SEMAPHORE
    if _PARSE_SEMAPHORE is None:
        _PARSE_SEMAPHORE = asyncio.Semaphore(max(get_settings().assistant_max_concurrent_parses, 1))
    return _PARSE_SEMAPHORE


async def run_parse_worker(mode: str, filename: str, data: bytes) -> list[dict] | None:
    """Run parse_worker in the parser venv. Returns the pages, or None if the
    worker reported the format as unsupported."""
    settings = get_settings()
    # Allowlisted env only, as for the Hermes worker: the parser handles
    # untrusted files and must not inherit DB/JWT/S3 secrets.
    env = {
        key: os.environ[key]
        for key in (
            "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "TMPDIR",
            "HF_HOME", "HF_HUB_OFFLINE", "DOCLING_ARTIFACTS_PATH", "OMP_NUM_THREADS",
        )
        if key in os.environ
    }
    proc = await asyncio.create_subprocess_exec(
        _parser_python(),
        "-m",
        "app.assistant.parse_worker",
        "--mode", mode,
        "--filename", Path(filename).name or "document",
        "--max-pages", str(settings.assistant_max_parse_pages),
        "--whisper-model", settings.assistant_whisper_model,
        "--audio-page-seconds", str(settings.assistant_audio_page_seconds),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(_APP_ROOT),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(data), timeout=settings.assistant_parse_timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError("document parsing timed out")

    if proc.returncode == _EXIT_UNSUPPORTED:
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"parse worker failed: {stderr.decode('utf-8', errors='replace')[-2000:]}")
    return json.loads(stdout.decode("utf-8"))["pages"]


async def _parse(kind: str, file: UploadedFile) -> tuple[str, list[dict] | None]:
    settings = get_settings()
    limit = settings.assistant_max_audio_bytes if kind == "audio" else settings.assistant_max_document_bytes
    data = await asyncio.to_thread(storage.download_object_bytes_by_key, file.s3_key, limit)
    if kind == "text":
        text = await asyncio.to_thread(extract_text, file.filename, file.content_type, data)
        return "text", [{"page_number": 1, "content": text, "tables": []}]
    async with _semaphore():
        pages = await run_parse_worker(kind, file.filename, data)
    return ("whisper" if kind == "audio" else "docling"), pages


async def get_or_parse(file: UploadedFile) -> ParsedDocument | MediaInfo:
    """Cached page-level parse of file. Raises UnsupportedDocument for files
    that can't be parsed; transient failures (timeouts, crashes) raise and
    are not cached, so a later request retries."""
    kind = classify(file.filename, file.content_type)
    if kind == "video":
        return MediaInfo(kind="video", filename=file.filename, content_type=file.content_type, size=file.size)
    if kind == "unsupported":
        raise UnsupportedDocument(f"unsupported document type: {file.content_type or file.filename}")

    url = document_url(file)
    async with AsyncSessionLocal() as db:
        cached = await crud.get_parsed_document_by_url(db, url)
    if cached is None:
        lock = _URL_LOCKS.setdefault(url, asyncio.Lock())
        async with lock:
            async with AsyncSessionLocal() as db:
                cached = await crud.get_parsed_document_by_url(db, url)
            if cached is None:
                parser, pages = await _parse(kind, file)
                async with AsyncSessionLocal() as db:
                    cached = await crud.create_parsed_document(
                        db,
                        document_url=url,
                        file_id=file.id,
                        parser=parser,
                        status=ParseStatus.READY if pages is not None else ParseStatus.UNSUPPORTED,
                        pages=pages or [],
                        error=None if pages is not None else "parser could not read this format",
                    )
                logger.info("Parsed file_id=%s parser=%s pages=%d", file.id, parser, cached.page_count)

    if cached.status == ParseStatus.UNSUPPORTED:
        raise UnsupportedDocument(cached.error or "unsupported document")
    return cached
