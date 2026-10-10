from __future__ import annotations

import asyncio
import logging
import uuid

from app import crud
from app.assistant.document_cache import MediaInfo, get_or_parse
from app.assistant.document_extract import UnsupportedDocument, chunk_text
from app.assistant.vectorstore import vector_store
from app.config import get_settings

logger = logging.getLogger(__name__)


def chunk_pages(pages: list, max_chars: int) -> tuple[list[tuple[int, str]], list[int]]:
    """Chunk each page separately so every chunk knows its page number; stop
    once max_chars of page text has been indexed."""
    chunks: list[tuple[int, str]] = []
    page_numbers: list[int] = []
    budget = max_chars
    for page in pages:
        if budget <= 0:
            break
        text = page.content[:budget]
        budget -= len(text)
        for _, chunk in chunk_text(text):
            chunks.append((len(chunks), chunk))
            page_numbers.append(page.page_number)
    return chunks, page_numbers


async def index_attachment(
    *,
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    filename: str,
    content_type: str,
) -> None:
    settings = get_settings()
    if not settings.assistant_document_indexing_enabled:
        return

    try:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            file = await crud.get_uploaded_file(db, file_id)
        if file is None:
            return
        # Parses (Docling/Whisper) and caches in Postgres on first sight, so
        # the agent's later get_document_content is a cache hit.
        parsed = await get_or_parse(file)
        if isinstance(parsed, MediaInfo):
            logger.info("Skipping %s attachment file_id=%s", parsed.kind, file_id)
            return
        chunks, page_numbers = chunk_pages(parsed.pages, settings.assistant_max_document_chars)
        await asyncio.to_thread(
            vector_store.upsert_document,
            conversation_id=str(conversation_id),
            file_id=str(file_id),
            filename=filename,
            chunks=chunks,
            page_numbers=page_numbers,
        )
        logger.info("Indexed attachment file_id=%s pages=%d chunks=%d", file_id, parsed.page_count, len(chunks))
    except UnsupportedDocument:
        logger.info("Skipping unsupported attachment file_id=%s", file_id)
    except Exception:
        logger.exception("Attachment indexing failed for file_id=%s", file_id)
