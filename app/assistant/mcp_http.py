from __future__ import annotations

import uuid

from fastapi import HTTPException
from starlette.responses import JSONResponse

from app.assistant.scope import reset_scope, set_scope, validate_scope_token
from app.database import AsyncSessionLocal
from app.models import AssistantRun


def _bearer_token(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token:
                return token.strip()
    return None


async def _run_is_active(signed_scope: dict) -> bool:
    """A valid signature isn't enough: the token only works while its run is
    still RUNNING and belongs to the user it was minted for, so a token
    lifted from a log after the run ends (or from another run) is useless."""
    async with AsyncSessionLocal() as db:
        run = await db.get(AssistantRun, uuid.UUID(signed_scope["run_id"]))
    return (
        run is not None
        and run.status == "RUNNING"
        and str(run.user_id) == signed_scope["user_id"]
        and str(run.conversation_id) == signed_scope["conversation_id"]
    )


class ScopedMCPApp:
    """ASGI boundary that turns the signed bearer token into a ContextVar.

    The token travels in the Authorization header (never the URL, so it does
    not end up in access logs) and is never part of an LLM-visible MCP tool
    schema.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)

        try:
            token = _bearer_token(scope)
            if not token:
                raise HTTPException(status_code=401, detail="Missing assistant scope")
            signed_scope = validate_scope_token(token)
            if not await _run_is_active(signed_scope):
                raise HTTPException(status_code=401, detail="Invalid assistant scope")
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return await response(scope, receive, send)

        ctx_token = set_scope(signed_scope)
        try:
            return await self.app(scope, receive, send)
        finally:
            reset_scope(ctx_token)
