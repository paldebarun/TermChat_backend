from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class ClientMessage:
    message_id: str
    sender: str
    text: str
    timestamp: str


@dataclass
class _Entry:
    expires_at: float
    messages: list[ClientMessage]


class MessageContextCache:
    """Bounded, process-local cache for client-decrypted E2E messages.

    PostgreSQL intentionally cannot be used for plaintext message retrieval.
    The frontend may opt in to sending decrypted history for one assistant run;
    this cache keeps it in memory only, for the duration of that run. Note the
    plaintext still reaches the LLM provider (OpenRouter), and what Hermes
    answers is stored in assistant_runs.response; tool *results* are stored
    only as size + SHA-256 (see crud.finish_assistant_run).
    """

    def __init__(self) -> None:
        self._items: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def put(self, run_id: uuid.UUID, messages: list[ClientMessage]) -> None:
        settings = get_settings()
        if len(messages) > settings.assistant_max_context_messages:
            messages = messages[-settings.assistant_max_context_messages :]
        total = sum(len(m.text.encode("utf-8")) for m in messages)
        if total > settings.assistant_max_context_bytes:
            kept: list[ClientMessage] = []
            used = 0
            for msg in reversed(messages):
                size = len(msg.text.encode("utf-8"))
                if used + size > settings.assistant_max_context_bytes:
                    break
                kept.append(msg)
                used += size
            messages = list(reversed(kept))
        async with self._lock:
            self._items[str(run_id)] = _Entry(
                expires_at=time.monotonic() + settings.assistant_context_ttl_seconds,
                messages=messages,
            )

    async def get(self, run_id: uuid.UUID) -> list[ClientMessage]:
        async with self._lock:
            item = self._items.get(str(run_id))
            if item is None:
                return []
            if item.expires_at <= time.monotonic():
                self._items.pop(str(run_id), None)
                return []
            return list(item.messages)

    async def delete(self, run_id: uuid.UUID) -> None:
        async with self._lock:
            self._items.pop(str(run_id), None)


message_context_cache = MessageContextCache()
