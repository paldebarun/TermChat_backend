import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.groups import pick_successor_admin
from app.models import (
    AssistantRun,
    AssistantToolCall,
    Group,
    GroupMember,
    GroupMessage,
    GroupMessageRecipient,
    Message,
    ParsedDocument,
    ParsedDocumentPage,
    ParseStatus,
    UploadedFile,
    UploadStatus,
    User,
)
from app.schemas import EncryptedMessageIn, GroupMessageIn, UserSignup


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
    if result.scalar():
        return True

    # Group messages: anyone the message was delivered to (or queued for).
    result = await db.execute(
        select(
            exists().where(
                GroupMessage.attachment_file_id == file.id,
                GroupMessage.id == GroupMessageRecipient.message_id,
                GroupMessageRecipient.recipient_id == user_id,
            )
        )
    )
    return bool(result.scalar())


async def get_orphaned_completed_files(db: AsyncSession, older_than_minutes: int) -> list[UploadedFile]:
    """Completed uploads that were never attached to any message and are
    older than the given threshold - candidates for the cleanup sweep."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)

    attached_file_ids = select(Message.attachment_file_id).where(Message.attachment_file_id.is_not(None))
    group_attached_file_ids = select(GroupMessage.attachment_file_id).where(
        GroupMessage.attachment_file_id.is_not(None)
    )

    result = await db.execute(
        select(UploadedFile).where(
            UploadedFile.status == UploadStatus.COMPLETED,
            UploadedFile.created_at < cutoff,
            UploadedFile.id.not_in(attached_file_ids),
            UploadedFile.id.not_in(group_attached_file_ids),
        )
    )
    return list(result.scalars().all())


# --- Groups -----------------------------------------------------------------

def _group_query():
    return select(Group).options(selectinload(Group.members).selectinload(GroupMember.user))


async def get_group(db: AsyncSession, group_id: uuid.UUID) -> Group | None:
    """Group with members (and their users) eagerly loaded. Always re-reads
    so callers see membership changes made earlier in the same session."""
    result = await db.execute(
        _group_query().where(Group.id == group_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_member_role(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    result = await db.execute(
        select(GroupMember.role).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_group(db: AsyncSession, name: str, creator: User, members: list[User]) -> Group:
    group = Group(name=name, created_by=creator.id)
    group.members.append(GroupMember(user_id=creator.id, role="admin"))
    for user in members:
        group.members.append(GroupMember(user_id=user.id, role="member"))
    db.add(group)
    await db.commit()
    return await get_group(db, group.id)


async def list_groups_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[Group]:
    result = await db.execute(
        _group_query()
        .where(Group.id.in_(select(GroupMember.group_id).where(GroupMember.user_id == user_id)))
        .order_by(Group.created_at.desc())
    )
    return list(result.scalars().all())


async def rename_group(db: AsyncSession, group: Group, name: str) -> Group:
    group.name = name
    await db.commit()
    return await get_group(db, group.id)


async def add_group_members(db: AsyncSession, group: Group, users: list[User]) -> Group:
    for user in users:
        db.add(GroupMember(group_id=group.id, user_id=user.id, role="member"))
    await db.commit()
    return await get_group(db, group.id)


async def remove_group_member(db: AsyncSession, group: Group, user_id: uuid.UUID) -> Group | None:
    """Removes a member. If no admin remains, the oldest remaining member is
    promoted. Deletes the group (returns None) when it becomes empty."""
    leaving = next((m for m in group.members if m.user_id == user_id), None)
    if leaving is None:
        return group
    remaining = [m for m in group.members if m.user_id != user_id]
    group_id = group.id

    if not remaining:
        await db.delete(group)
        await db.commit()
        return None

    successor = pick_successor_admin((m.user.username, m.role, m.joined_at) for m in remaining)
    if successor is not None:
        next(m for m in remaining if m.user.username == successor).role = "admin"
    await db.delete(leaving)
    await db.commit()
    return await get_group(db, group_id)


async def group_message_exists(db: AsyncSession, message_id: uuid.UUID) -> bool:
    result = await db.execute(select(exists().where(GroupMessage.id == message_id)))
    return bool(result.scalar())


async def save_group_message(
    db: AsyncSession,
    *,
    sender_id: uuid.UUID,
    payload: GroupMessageIn,
    keys_by_user_id: dict[uuid.UUID, str],
    online_user_ids: set[uuid.UUID],
) -> GroupMessage:
    message = GroupMessage(
        id=payload.message_id,
        group_id=payload.group_id,
        sender_id=sender_id,
        encrypted_content=payload.encrypted_content,
        nonce=payload.nonce,
        tag=payload.tag,
        attachment_file_id=payload.attachment.file_id if payload.attachment else None,
        attachment_filename=payload.attachment.filename if payload.attachment else None,
    )
    for user_id, encrypted_key in keys_by_user_id.items():
        message.recipients.append(
            GroupMessageRecipient(
                recipient_id=user_id,
                encrypted_key=encrypted_key,
                # The sender already has their own plaintext; never queue for them.
                delivered=(user_id == sender_id) or (user_id in online_user_ids),
            )
        )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def get_undelivered_group_messages(
    db: AsyncSession, recipient_id: uuid.UUID
) -> list[tuple[GroupMessage, GroupMessageRecipient]]:
    result = await db.execute(
        select(GroupMessage, GroupMessageRecipient)
        .join(GroupMessageRecipient, GroupMessageRecipient.message_id == GroupMessage.id)
        .where(GroupMessageRecipient.recipient_id == recipient_id, GroupMessageRecipient.delivered.is_(False))
        .order_by(GroupMessage.created_at.asc())
    )
    return [(m, r) for m, r in result.all()]


async def mark_group_delivered(db: AsyncSession, recipient: GroupMessageRecipient) -> None:
    recipient.delivered = True
    await db.commit()

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


async def conversation_files(db: AsyncSession, user_a: uuid.UUID, user_b: uuid.UUID) -> list[UploadedFile]:
    """Every completed file attached to a message between exactly these two
    users, oldest first - what the assistant's list_chat_documents shows."""
    result = await db.execute(
        select(UploadedFile)
        .join(Message, Message.attachment_file_id == UploadedFile.id)
        .where(
            UploadedFile.status == UploadStatus.COMPLETED,
            ((Message.sender_id == user_a) & (Message.recipient_id == user_b))
            | ((Message.sender_id == user_b) & (Message.recipient_id == user_a)),
        )
        .distinct()
        .order_by(UploadedFile.created_at)
    )
    return list(result.scalars().all())


async def group_files_for_user(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> list[UploadedFile]:
    """The files behind group_file_ids_for_user, oldest first."""
    file_ids = await group_file_ids_for_user(db, group_id, user_id)
    if not file_ids:
        return []
    result = await db.execute(
        select(UploadedFile)
        .where(UploadedFile.id.in_([uuid.UUID(file_id) for file_id in file_ids]))
        .order_by(UploadedFile.created_at)
    )
    return list(result.scalars().all())


async def group_file_ids_for_user(db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID) -> list[str]:
    """Completed files attached to messages of this group that were sent by
    the user or addressed to them (so a later joiner never gets files that
    were shared before they joined)."""
    result = await db.execute(
        select(GroupMessage.attachment_file_id)
        .join(UploadedFile, UploadedFile.id == GroupMessage.attachment_file_id)
        .outerjoin(
            GroupMessageRecipient,
            (GroupMessageRecipient.message_id == GroupMessage.id)
            & (GroupMessageRecipient.recipient_id == user_id),
        )
        .where(
            GroupMessage.group_id == group_id,
            UploadedFile.status == UploadStatus.COMPLETED,
            (GroupMessage.sender_id == user_id) | (GroupMessageRecipient.recipient_id == user_id),
        )
        .distinct()
    )
    return [str(file_id) for file_id in result.scalars().all()]


async def group_file_accessible(
    db: AsyncSession, file_id: uuid.UUID, group_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Authorization for the assistant's get_document_content in a group."""
    return str(file_id) in await group_file_ids_for_user(db, group_id, user_id)


# --- Parsed document cache --------------------------------------------------


async def get_parsed_document_by_url(db: AsyncSession, document_url: str) -> ParsedDocument | None:
    result = await db.execute(
        select(ParsedDocument)
        .where(ParsedDocument.document_url == document_url)
        .options(selectinload(ParsedDocument.pages))
    )
    return result.scalar_one_or_none()


async def create_parsed_document(
    db: AsyncSession,
    *,
    document_url: str,
    file_id: uuid.UUID,
    parser: str,
    status: ParseStatus,
    pages: list[dict],
    error: str | None = None,
) -> ParsedDocument:
    """Store a parse once. ON CONFLICT DO NOTHING: if another writer (a
    second worker process) stored the same URL first, keep theirs."""
    document_id = uuid.uuid4()
    result = await db.execute(
        pg_insert(ParsedDocument)
        .values(
            id=document_id,
            document_url=document_url,
            file_id=file_id,
            parser=parser,
            status=status,
            page_count=len(pages),
            error=error,
        )
        .on_conflict_do_nothing(index_elements=[ParsedDocument.document_url])
        .returning(ParsedDocument.id)
    )
    if result.scalar_one_or_none() is not None and pages:
        await db.execute(
            pg_insert(ParsedDocumentPage),
            [
                {
                    "id": uuid.uuid4(),
                    "document_id": document_id,
                    "page_number": page["page_number"],
                    "content": page.get("content") or "",
                    "tables": page.get("tables") or [],
                    "start_seconds": page.get("start_seconds"),
                    "end_seconds": page.get("end_seconds"),
                }
                for page in pages
            ],
        )
    await db.commit()
    document = await get_parsed_document_by_url(db, document_url)
    assert document is not None
    return document


async def delete_parsed_documents_for_file(db: AsyncSession, file_id: uuid.UUID) -> None:
    """Deleted files keep their uploaded_files row (status DELETED), so the
    FK cascade never fires; parsed plaintext must be removed explicitly."""
    await db.execute(delete(ParsedDocument).where(ParsedDocument.file_id == file_id))
    await db.commit()
