import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssistantRun, AssistantToolCall, Message, UploadedFile, UploadStatus, User
from app.schemas import EncryptedMessageIn, UserSignup


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, data: UserSignup, hashed_password: str) -> User:
    user = User(username=data.username, email=data.email, hashed_password=hashed_password)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def set_public_key(db: AsyncSession, user: User, public_key: str) -> User:
    user.public_key = public_key
    await db.commit()
    await db.refresh(user)
    return user


async def save_message(
    db: AsyncSession,
    sender_id: uuid.UUID,
    recipient_id: uuid.UUID,
    payload: EncryptedMessageIn,
    delivered: bool,
) -> Message:
    message = Message(
        id=payload.message_id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        encrypted_content=payload.encrypted_content,
        encrypted_key=payload.encrypted_key,
        nonce=payload.nonce,
        tag=payload.tag,
        delivered=delivered,
        attachment_file_id=payload.attachment.file_id if payload.attachment else None,
        attachment_filename=payload.attachment.filename if payload.attachment else None,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def get_undelivered_messages(db: AsyncSession, recipient_id: uuid.UUID) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.recipient_id == recipient_id, Message.delivered.is_(False))
        .order_by(Message.created_at.asc())
    )
    return list(result.scalars().all())


async def mark_delivered(db: AsyncSession, message: Message) -> None:
    message.delivered = True
    await db.commit()


# --- Uploaded files / attachments -------------------------------------------

async def create_uploaded_file(
    db: AsyncSession,
    *,
    file_id: uuid.UUID,
    owner_id: uuid.UUID,
    filename: str,
    content_type: str,
    size: int,
    chunk_size: int,
    total_parts: int,
    s3_key: str,
    s3_upload_id: str,
) -> UploadedFile:
    file = UploadedFile(
        id=file_id,
        owner_id=owner_id,
        filename=filename,
        content_type=content_type,
        size=size,
        chunk_size=chunk_size,
        total_parts=total_parts,
        s3_key=s3_key,
        s3_upload_id=s3_upload_id,
        status=UploadStatus.UPLOADING,
    )
    db.add(file)
    await db.commit()
    await db.refresh(file)
    return file


async def get_uploaded_file(db: AsyncSession, file_id: uuid.UUID) -> UploadedFile | None:
    return await db.get(UploadedFile, file_id)


async def set_upload_status(
    db: AsyncSession,
    file: UploadedFile,
    status: UploadStatus,
    *,
    completed_at: datetime | None = None,
) -> UploadedFile:
    file.status = status
    if completed_at is not None:
        file.completed_at = completed_at
    await db.commit()
    await db.refresh(file)
    return file


async def user_can_access_file(db: AsyncSession, file: UploadedFile, user_id: uuid.UUID) -> bool:
    """The uploader can always access their own file. Anyone the file was
    actually sent to (sender or recipient of a message referencing it) can
    also fetch a download URL for it."""
    if file.owner_id == user_id:
        return True

    result = await db.execute(
        select(
            exists().where(
                Message.attachment_file_id == file.id,
                (Message.sender_id == user_id) | (Message.recipient_id == user_id),
            )
        )
    )
    return bool(result.scalar())


async def get_orphaned_completed_files(db: AsyncSession, older_than_minutes: int) -> list[UploadedFile]:
    """Completed uploads that were never attached to any message and are
    older than the given threshold - candidates for the cleanup sweep."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)

    attached_file_ids = select(Message.attachment_file_id).where(Message.attachment_file_id.is_not(None))

    result = await db.execute(
        select(UploadedFile).where(
            UploadedFile.status == UploadStatus.COMPLETED,
            UploadedFile.created_at < cutoff,
            UploadedFile.id.not_in(attached_file_ids),
        )
    )
    return list(result.scalars().all())

# --- AI assistant runs / tool calls -----------------------------------------

async def create_assistant_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    provider: str,
    model: str,
) -> AssistantRun:
    run = AssistantRun(
        id=run_id,
        conversation_id=conversation_id,
        user_id=user_id,
        question=question,
        status="RUNNING",
        provider=provider,
        model=model,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _tool_result_failed(text: str | None) -> bool:
    """Hermes wraps MCP results as <untrusted_tool_result ...>\n...\n{json}\n</...>;
    the JSON line carries `error` / `isError` when the tool call failed."""
    if not text:
        return False
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if lines and lines[-1].startswith("</untrusted_tool_result"):
        lines = lines[:-1]
    candidate = lines[-1] if lines else text
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return candidate.lstrip().lower().startswith("error")
    return isinstance(data, dict) and bool(data.get("error") or data.get("isError"))


async def finish_assistant_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    response: str | None,
    status: str,
    latency_ms: int,
    tool_messages: list | None = None,
    error: str | None = None,
) -> AssistantRun:
    run = await db.get(AssistantRun, run_id)
    if run is None:
        raise ValueError(f"assistant run {run_id} not found")

    run.response = response
    run.status = status
    run.latency_ms = latency_ms
    run.error = error
    run.completed_at = datetime.now(timezone.utc)

    # Best-effort audit trail. `messages` is OpenAI-style: an assistant
    # message carries tool_calls=[{id, function:{name, arguments}}], and each
    # call is answered by a role="tool" message with tool_call_id + content.
    # Tolerates missing fields rather than failing a run over an audit row.
    calls: dict[str, dict] = {}
    for item in tool_messages or []:
        if not isinstance(item, dict):
            continue
        for call in item.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            calls[str(call.get("id"))] = {
                "name": fn.get("name") or call.get("name") or "unknown",
                "arguments": fn.get("arguments") or call.get("arguments"),
            }
    now = datetime.now(timezone.utc)
    for item in tool_messages or []:
        if not isinstance(item, dict) or item.get("role") != "tool":
            continue
        call = calls.get(str(item.get("tool_call_id")), {})
        content = item.get("content")
        text = str(content) if content is not None else None
        failed = _tool_result_failed(text)
        # Metadata only. The result is decrypted chat/document text, which the
        # server must not retain (the chat is end-to-end encrypted); size and
        # a hash are enough to audit that a call happened and what it returned.
        result_meta = (
            json.dumps(
                {
                    "bytes": len(text.encode("utf-8")),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
            if text is not None
            else None
        )
        db.add(
            AssistantToolCall(
                assistant_run_id=run_id,
                tool_name=str(call.get("name") or item.get("name") or "unknown")[:255],
                arguments=str(call["arguments"])[:4000] if call.get("arguments") else None,
                result=result_meta,
                status="error" if failed else "ok",
                completed_at=now,
            )
        )

    await db.commit()
    await db.refresh(run)
    return run


async def file_belongs_to_conversation(
    db: AsyncSession, file_id: uuid.UUID, user_a: uuid.UUID, user_b: uuid.UUID
) -> bool:
    """True only if file_id was actually attached to a message exchanged
    between exactly these two users - the authorization check
    get_document_content relies on before returning any file content."""
    result = await db.execute(
        select(
            exists().where(
                UploadedFile.id == file_id,
                UploadedFile.status == UploadStatus.COMPLETED,
                Message.attachment_file_id == UploadedFile.id,
                (
                    ((Message.sender_id == user_a) & (Message.recipient_id == user_b))
                    | ((Message.sender_id == user_b) & (Message.recipient_id == user_a))
                ),
            )
        )
    )
    return bool(result.scalar())