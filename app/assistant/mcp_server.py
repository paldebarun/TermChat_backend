from __future__ import annotations

import asyncio
import uuid

from fastmcp import FastMCP

from app import crud
from app.assistant.cache import message_context_cache
from app.assistant.document_cache import MediaInfo, classify, get_or_parse
from app.assistant.document_extract import UnsupportedDocument
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
    """Search only documents shared in the current conversation (1:1 chat or group)."""
    scope = require_scope()
    conversation_id = scope["conversation_id"]
    file_ids = None
    if scope["kind"] == "group":
        async with AsyncSessionLocal() as db:
            file_ids = await crud.group_file_ids_for_user(
                db, uuid.UUID(scope["group_id"]), uuid.UUID(scope["user_id"])
            )
    results = await asyncio.to_thread(
        vector_store.search,
        conversation_id=conversation_id,
        query=query,
        limit=_clamp(limit, get_settings().assistant_max_search_results),
        file_ids=file_ids,
    )
    return {"results": results}


def file_entry(file) -> dict:
    """One row of list_chat_documents. `kind` tells the agent what
    get_document_content will return for it."""
    kind = classify(file.filename, file.content_type)
    return {
        "file_id": str(file.id),
        "filename": file.filename,
        "content_type": file.content_type,
        "kind": "document" if kind in ("docling", "text") else kind,
        "size_bytes": file.size,
        "shared_at": file.created_at.isoformat() if file.created_at else None,
    }


async def _scope_files(db, scope: dict) -> list:
    """Every file the caller may see in the scoped conversation."""
    current_user = uuid.UUID(scope["user_id"])
    if scope["kind"] == "group":
        return await crud.group_files_for_user(db, uuid.UUID(scope["group_id"]), current_user)
    return await crud.conversation_files(db, current_user, uuid.UUID(scope["peer_id"]))


def resolve_file(files: list, reference: str):
    """Pick the file a get_document_content call means, out of the files
    already in scope: by id, or - models often only remember the name from an
    earlier answer - by exact filename. Returns (file, None) or (None, error)."""
    reference = str(reference).strip()
    try:
        wanted = uuid.UUID(reference)
    except ValueError:
        matches = [f for f in files if f.filename.lower() == reference.lower()]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            return None, "several files have that name; pass the file_id of the one you mean"
        return None, "no file with that id or filename in this conversation; use a file_id from available_files"
    for file in files:
        if file.id == wanted:
            return file, None
    return None, "that file is not part of the current conversation; use a file_id from available_files"


@mcp.tool
async def list_chat_documents() -> dict:
    """List every file shared in the current conversation (1:1 chat or group).

    Use this to find out which files exist - search_chat_documents only
    matches text inside files, so it cannot answer "is there an audio file?"
    and misses files whose content doesn't match the query. Each entry has a
    file_id and a kind: "document" (read as page-level markdown), "audio"
    (get_document_content returns its transcript), "video" (metadata only) or
    "unsupported".
    """
    async with AsyncSessionLocal() as db:
        files = await _scope_files(db, require_scope())
    return {"files": [file_entry(f) for f in files]}


def _page_dict(page) -> dict:
    # Tables are already inline in content as markdown; the structured copy
    # stays in Postgres so it doesn't double the tokens sent to the model.
    out = {"page_number": page.page_number, "content": page.content, "table_count": len(page.tables or [])}
    if page.start_seconds is not None:
        out["start_seconds"] = page.start_seconds
        out["end_seconds"] = page.end_seconds
    return out


def select_pages(pages: list, start_page: int, end_page: int | None, max_chars: int) -> dict:
    """Whole pages from start_page..end_page until max_chars is used up.
    A single page larger than the budget is cut rather than skipped."""
    in_range = [
        p for p in pages if p.page_number >= start_page and (end_page is None or p.page_number <= end_page)
    ]
    selected: list[dict] = []
    used = 0
    for i, page in enumerate(in_range):
        item = _page_dict(page)
        if used + len(item["content"]) > max_chars:
            if not selected:
                item["content"] = item["content"][:max_chars]
                item["content_truncated"] = True
                selected.append(item)
                i += 1
            next_page = in_range[i].page_number if i < len(in_range) else None
            return {"pages": selected, "truncated": True, "next_page": next_page}
        selected.append(item)
        used += len(item["content"])
    return {"pages": selected, "truncated": False, "next_page": None}


@mcp.tool
async def get_document_content(file_id: str, start_page: int = 1, end_page: int | None = None) -> dict:
    """Read a document only if it is attached to this exact conversation.

    Documents are parsed page by page, with tables rendered inline as
    markdown tables; audio files are transcribed into
    time-window "pages" with start/end seconds. Pass start_page/end_page to
    read part of a long document - e.g. the page number from a
    search_chat_documents hit. If `truncated` is true, call again with
    start_page=next_page to continue. Video files return metadata only.

    file_id is the id from list_chat_documents or search_chat_documents; the
    exact filename is accepted too. Never invent an id. A result with an
    `error` key means nothing was read - `available_files` then lists the
    files you can ask for.
    """
    # Only files already in scope can match, so resolving is also the
    # authorization check. Mistakes come back as results, not exceptions:
    # the model can correct itself from available_files in one step.
    async with AsyncSessionLocal() as db:
        files = await _scope_files(db, require_scope())
    file, error = resolve_file(files, file_id)
    if file is None:
        return {"error": error, "available_files": [file_entry(f) for f in files]}
    if end_page is not None and end_page < start_page:
        return {"error": "end_page must be >= start_page"}

    meta = {"file_id": str(file.id), "filename": file.filename, "content_type": file.content_type}
    try:
        parsed = await get_or_parse(file)
    except UnsupportedDocument:
        return {**meta, "error": "this file type cannot be read"}
    if isinstance(parsed, MediaInfo):
        return {
            **meta,
            "media": True,
            "kind": parsed.kind,
            "size_bytes": parsed.size,
            "note": f"{parsed.kind} content is not transcribed; only metadata is available",
        }

    result = {
        **meta,
        "parser": parsed.parser,
        "page_count": parsed.page_count,
        **select_pages(parsed.pages, max(int(start_page), 1), end_page, MAX_TOOL_DOCUMENT_CHARS),
    }
    if parsed.parser == "whisper" and not parsed.page_count:
        result["note"] = "the audio was transcribed but no speech was detected (e.g. music or silence)"
    return result