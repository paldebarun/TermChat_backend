import base64
import hashlib
import hmac
import json
import time
import uuid
from contextvars import ContextVar

from fastapi import HTTPException, status

from app.config import get_settings

_scope_var: ContextVar[dict | None] = ContextVar("assistant_scope", default=None)


def conversation_id_for_users(user_a: uuid.UUID, user_b: uuid.UUID) -> uuid.UUID:
    """Stable conversation id for the existing 1:1 chat model.

    The current repo has no Conversation table; the pair of participant ids is
    the existing conversation boundary. UUID5 gives us a durable identifier
    without rewriting the message schema or migrating existing messages.
    """
    first, second = sorted((str(user_a), str(user_b)))
    return uuid.uuid5(uuid.NAMESPACE_URL, f"e2e-chat:conversation:{first}:{second}")


MIN_SECRET_LENGTH = 32


def require_strong_secret() -> str:
    """An empty/short secret makes every token forgeable, so refuse to run."""
    secret = get_settings().assistant_scope_token_secret
    if len(secret) < MIN_SECRET_LENGTH or secret.startswith("change-this"):
        raise RuntimeError(
            f"ASSISTANT_SCOPE_TOKEN_SECRET must be set to a random value of "
            f"at least {MIN_SECRET_LENGTH} characters"
        )
    return secret


def create_scope_token(*, run_id: uuid.UUID, user_id: uuid.UUID, peer_id: uuid.UUID) -> str:
    settings = get_settings()
    require_strong_secret()
    payload = {
        "run_id": str(run_id),
        "user_id": str(user_id),
        "peer_id": str(peer_id),
        "conversation_id": str(conversation_id_for_users(user_id, peer_id)),
        "exp": int(time.time()) + settings.assistant_scope_token_ttl_seconds,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = hmac.new(
        settings.assistant_scope_token_secret.encode(),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{sig}"


def validate_scope_token(token: str) -> dict:
    settings = get_settings()
    try:
        require_strong_secret()
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid assistant scope",
        )
    try:
        encoded, supplied_sig = token.split(".", 1)
        expected_sig = hmac.new(
            settings.assistant_scope_token_secret.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_sig, expected_sig):
            raise ValueError("invalid signature")
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError("expired")
        uuid.UUID(payload["run_id"])
        uuid.UUID(payload["user_id"])
        uuid.UUID(payload["peer_id"])
        uuid.UUID(payload["conversation_id"])
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid assistant scope",
        )


def set_scope(scope: dict):
    return _scope_var.set(scope)


def reset_scope(token) -> None:
    _scope_var.reset(token)


def require_scope() -> dict:
    scope = _scope_var.get()
    if scope is None:
        raise RuntimeError("assistant scope is not established")
    return scope
