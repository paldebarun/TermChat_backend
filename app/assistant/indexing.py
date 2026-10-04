from __future__ import annotations

import asyncio
import logging
import uuid

from app import crud, storage
from app.assistant.document_extract import UnsupportedDocument, chunk_text, extract_text
from app.assistant.scope import conversation_id_for_users
from app.assistant.vectorstore import vector_store
from app.config import get_settings

logger = logging.getLogger(__name__)


async def index_attachment(
    *,
    file_id: uuid.UUID,
    sender_id: uuid.UUID,
    recipient_id: uuid.UUID,
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
            data = await asyncio.to_thread(storage.download_object_bytes_by_key, file.s3_key)
        text = await asyncio.to_thread(extract_text, filename, content_type, data)
        chunks = chunk_text(text)
        await asyncio.to_thread(
            vector_store.upsert_document,
            conversation_id=str(conversation_id_for_users(sender_id, recipient_id)),
            file_id=str(file_id),
            filename=filename,
            chunks=chunks,
        )
        logger.info("Indexed attachment file_id=%s chunks=%d", file_id, len(chunks))
    except UnsupportedDocument:
        logger.info("Skipping unsupported attachment file_id=%s", file_id)
    except Exception:
        logger.exception("Attachment indexing failed for file_id=%s", file_id)
