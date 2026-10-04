from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, crud
from app.assistant.cache import ClientMessage
from app.assistant.scope import conversation_id_for_group, conversation_id_for_users
from app.assistant.service import AssistantBusy, run_assistant
from app.database import get_db
from app.models import User
from app.schemas import AssistantGroupQueryRequest, AssistantQueryRequest, AssistantQueryResponse

router = APIRouter(prefix="/assistant", tags=["assistant"])


async def _answer(
    db: AsyncSession,
    *,
    current_user: User,
    conversation_id: uuid.UUID,
    data: AssistantQueryRequest | AssistantGroupQueryRequest,
    **scope: object,
) -> AssistantQueryResponse:
    """Shared by the 1:1 and group routes; `scope` is the peer_id or the
    group_id/group_name/participants that bound this run."""
    if not data.question.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Question cannot be empty")

    client_messages = [
        ClientMessage(
            message_id=str(item.message_id),
            sender=item.sender,
            text=item.text,
            timestamp=item.timestamp.isoformat(),
        )
        for item in data.message_context
    ]

    try:
        run_id, response, _ = await run_assistant(
            db,
            user_id=current_user.id,
            question=data.question.strip(),
            client_messages=client_messages,
            assistant_history=[item.model_dump() for item in data.assistant_history],
            **scope,
        )
    except AssistantBusy:
        raise HTTPException(status_code=429, detail="Assistant is busy, try again shortly")
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Assistant timed out")
    except Exception:
        raise HTTPException(status_code=502, detail="Assistant execution failed")

    return AssistantQueryResponse(run_id=run_id, conversation_id=conversation_id, response=response)


@router.post("/query", response_model=AssistantQueryResponse)
async def query_assistant(
    data: AssistantQueryRequest,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    peer = await crud.get_user_by_username(db, data.peer_username)
    if peer is None or peer.id == current_user.id or not peer.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return await _answer(
        db,
        current_user=current_user,
        conversation_id=conversation_id_for_users(current_user.id, peer.id),
        data=data,
        peer_id=peer.id,
    )


@router.post("/group-query", response_model=AssistantQueryResponse)
async def query_assistant_in_group(
    data: AssistantGroupQueryRequest,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ask the assistant about a group chat you belong to. Behaves like the
    1:1 endpoint, but its context is the whole group."""
    group = await crud.get_group(db, data.group_id)
    # Same 404 for "no such group" and "not a member".
    if group is None or not any(m.user_id == current_user.id for m in group.members):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return await _answer(
        db,
        current_user=current_user,
        conversation_id=conversation_id_for_group(group.id),
        data=data,
        group_id=group.id,
        group_name=group.name,
        participants=[m.user.username for m in group.members],
    )
