from __future__ import annotations

import asyncio
import uuid

from fastmcp import FastMCP

from app import crud, storage
from app.assistant.cache import message_context_cache
from app.assistant.document_extract import extract_text
from app.assistant.scope import require_scope
from app.assistant.vectorstore import vector_store
from app.config import get_settings
from app.database import AsyncSessionLocal

mcp = FastMCP("chat_scope")

# Cap on text returned in one tool result (~15k tokens); 500k chars would
# blow the model context window and the bill.
MAX_TOOL_DOCUMENT_CHARS = 60_000


def _clamp(limit: int, maximum: int) -> int:
    return max(1, min(int(limit), maximum))


@mcp.tool
async def get_recent_chat_context() -> dict:
    """Return recent decrypted messages supplied by the client for this run."""
    scope = require_scope()
    messages = await message_context_cache.get(uuid.UUID(scope["run_id"]))
    return {
        "available": bool(messages),
        "messages": [m.__dict__ for m in messages[-get_settings().assistant_recent_messages_limit :]],
    }


@mcp.tool
async def search_chat_messages(query: str, limit: int = 8) -> dict:
    """Search decrypted messages supplied by the client for this run.

    PostgreSQL stores ciphertext only; this tool never attempts to decrypt it.
    """
    settings = get_settings()
    scope = require_scope()
    limit = _clamp(limit, settings.assistant_max_search_results)
    messages = await message_context_cache.get(uuid.UUID(scope["run_id"]))
    if not messages:
        return {
            "available": False,
            "reason": "plaintext chat history was not supplied by the client for this run",
            "messages": [],
        }

    terms = {t for t in query.lower().split() if len(t) > 1}
    scored = []
    for message in messages:
        haystack = message.text.lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, message))
    scored.sort(key=lambda item: item[0], reverse=True)
    return {
        "available": True,
        "messages": [m.__dict__ for _, m in scored[:limit]],
    }


@mcp.tool
async def search_chat_documents(query: str, limit: int = 6) -> dict:
    """Search only documents shared in the current 1:1 conversation."""
    scope = require_scope()
    conversation_id = scope["conversation_id"]
    results = await asyncio.to_thread(
        vector_store.search,
        conversation_id=conversation_id,
        query=query,
        limit=_clamp(limit, get_settings().assistant_max_search_results),
    )
    return {"results": results}


@mcp.tool
async def get_document_content(file_id: str) -> dict:
    """Read a document only if it is attached to this exact conversation."""
    scope = require_scope()
    try:
        db_file_id = uuid.UUID(file_id)
    except ValueError:
        raise ValueError("file_id must be a valid document id returned by search_chat_documents")
    current_user = uuid.UUID(scope["user_id"])
    peer = uuid.UUID(scope["peer_id"])

    async with AsyncSessionLocal() as db:
        if not await crud.file_belongs_to_conversation(db, db_file_id, current_user, peer):
            raise ValueError("document is not part of the current conversation")
        file = await crud.get_uploaded_file(db, db_file_id)

    if file is None:
        raise ValueError("document not found")

    data = await asyncio.to_thread(storage.download_object_bytes_by_key, file.s3_key)
    text = await asyncio.to_thread(extract_text, file.filename, file.content_type, data)

    return {
        "file_id": str(file.id),
        "filename": file.filename,
        "content_type": file.content_type,
        "content": text[:MAX_TOOL_DOCUMENT_CHARS],
        "truncated": len(text) > MAX_TOOL_DOCUMENT_CHARS,
    }