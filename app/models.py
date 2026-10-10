import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UploadStatus(str, enum.Enum):
    UPLOADING = "UPLOADING"
    COMPLETING = "COMPLETING"
    COMPLETED = "COMPLETED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    DELETED = "DELETED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    # Client-generated RSA public key (PEM), uploaded after signup. The
    # matching private key never leaves the client - that split is what
    # makes message content end-to-end encrypted rather than merely
    # encrypted-in-transit.
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sent_messages: Mapped[list["Message"]] = relationship(
        "Message", foreign_keys="Message.sender_id", back_populates="sender"
    )
    received_messages: Mapped[list["Message"]] = relationship(
        "Message", foreign_keys="Message.recipient_id", back_populates="recipient"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Opaque, base64-encoded ciphertext + AES-GCM tag, and the message's
    # per-message AES key, itself encrypted with the recipient's RSA public
    # key. Produced entirely client-side - the server stores and forwards
    # these fields verbatim and has no way to recover the plaintext.
    encrypted_content: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    tag: Mapped[str] = mapped_column(String(64), nullable=False)

    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Optional file attachment. NULL for plain text messages. Points at a
    # completed UploadedFile - the actual bytes were never routed through
    # this server, only this reference. attachment_filename is denormalized
    # so message history can render without an extra join.
    attachment_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True
    )
    attachment_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id], back_populates="sent_messages")
    recipient: Mapped["User"] = relationship(
        "User", foreign_keys=[recipient_id], back_populates="received_messages"
    )
    attachment: Mapped["UploadedFile | None"] = relationship("UploadedFile")


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["GroupMember"]] = relationship(
        "GroupMember", back_populates="group", cascade="all, delete-orphan", order_by="GroupMember.joined_at"
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)  # "admin" | "member"
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped["Group"] = relationship("Group", back_populates="members")
    user: Mapped["User"] = relationship("User")


class GroupMessage(Base):
    """One AES-GCM ciphertext shared by the whole group. The per-message AES
    key is wrapped separately for each member in GroupMessageRecipient."""

    __tablename__ = "group_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # client message_id
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    encrypted_content: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    attachment_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True
    )
    attachment_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    recipients: Mapped[list["GroupMessageRecipient"]] = relationship(
        "GroupMessageRecipient", back_populates="message", cascade="all, delete-orphan"
    )


class GroupMessageRecipient(Base):
    """Per-member wrapped key; also the member's offline queue entry and the
    access-control record for the message's attachment."""

    __tablename__ = "group_message_recipients"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_messages.id", ondelete="CASCADE"), primary_key=True
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    message: Mapped["GroupMessage"] = relationship("GroupMessage", back_populates="recipients")


class UploadedFile(Base):
    """One row per attachment upload session. id doubles as both the
    transient "upload_id" a client polls/resumes against while the
    multipart transfer is in progress, and the durable "file_id" a
    completed upload is referenced by from a chat message."""

    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_parts: Mapped[int] = mapped_column(Integer, nullable=False)

    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    s3_upload_id: Mapped[str] = mapped_column(String(512), nullable=False)

    status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus, name="upload_status"), default=UploadStatus.UPLOADING, nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship("User")


class ParseStatus(str, enum.Enum):
    READY = "READY"
    UNSUPPORTED = "UNSUPPORTED"


class ParsedDocument(Base):
    """Page-level parse of one stored object (Docling for documents, Whisper
    for audio), cached so a document is parsed at most once. Keyed by the
    canonical object URL (s3://bucket/key); presigned URLs expire, so they
    can't be the key."""

    __tablename__ = "parsed_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_url: Mapped[str] = mapped_column(String(1100), unique=True, index=True, nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parser: Mapped[str] = mapped_column(String(32), nullable=False)  # "docling" | "whisper" | "text"
    status: Mapped[ParseStatus] = mapped_column(Enum(ParseStatus, name="parse_status"), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    pages: Mapped[list["ParsedDocumentPage"]] = relationship(
        "ParsedDocumentPage",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="ParsedDocumentPage.page_number",
    )


class ParsedDocumentPage(Base):
    """One page of a parsed document (or one time window of an audio
    transcript). content is markdown with tables inlined; tables holds the
    same tables in structured form."""

    __tablename__ = "parsed_document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parsed_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tables: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    document: Mapped["ParsedDocument"] = relationship("ParsedDocument", back_populates="pages")


class AssistantRun(Base):
    __tablename__ = "assistant_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    tool_calls: Mapped[list["AssistantToolCall"]] = relationship(
        "AssistantToolCall", back_populates="run", cascade="all, delete-orphan"
    )


class AssistantToolCall(Base):
    __tablename__ = "assistant_tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assistant_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assistant_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    run: Mapped["AssistantRun"] = relationship("AssistantRun", back_populates="tool_calls")
