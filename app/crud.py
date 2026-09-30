import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, UploadedFile, UploadStatus, User
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
