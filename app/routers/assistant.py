from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, crud
from app.assistant.cache import ClientMessage
from app.assistant.scope import conversation_id_for_users
from app.assistant.service import AssistantBusy, run_assistant
from app.database import get_db
from app.models import User
from app.schemas import AssistantQueryRequest, AssistantQueryResponse

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/query", response_model=AssistantQueryResponse)
async def query_assistant(
    data: AssistantQueryRequest,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    peer = await crud.get_user_by_username(db, data.peer_username)
    if peer is None or peer.id == current_user.id or not peer.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    conversation_id = conversation_id_for_users(current_user.id, peer.id)

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
            peer_id=peer.id,
            question=data.question.strip(),
            client_messages=client_messages,
        )
    except AssistantBusy:
        raise HTTPException(status_code=429, detail="Assistant is busy, try again shortly")
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Assistant timed out")
    except Exception:
        raise HTTPException(status_code=502, detail="Assistant execution failed")

    return AssistantQueryResponse(
        run_id=run_id,
        conversation_id=conversation_id,
        response=response,
    )