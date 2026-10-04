import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import UploadStatus


class UserSignup(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def username_alnum(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError("username may only contain letters, numbers and underscores")
        return v


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: EmailStr
    public_key: str | None
    created_at: datetime


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class PublicKeyUpdate(BaseModel):
    public_key: str


# --- Websocket payloads -----------------------------------------------------
# Every field below except message_id/recipient/sender/timestamp/attachment
# is opaque ciphertext produced client-side (see client/crypto_utils.py).
# The server never decrypts these - it only stores and relays them.

class AttachmentRef(BaseModel):
    """A reference to an already-completed upload, not the file itself.
    The actual bytes went straight from the browser to object storage -
    see app/routers/uploads.py."""

    file_id: uuid.UUID
    filename: str


class EncryptedMessageIn(BaseModel):
    type: str = "message"
    message_id: uuid.UUID
    recipient: str
    encrypted_content: str
    encrypted_key: str
    nonce: str
    tag: str
    timestamp: datetime
    attachment: AttachmentRef | None = None


class EncryptedMessageOut(BaseModel):
    type: str = "message"
    message_id: uuid.UUID
    sender: str
    encrypted_content: str
    encrypted_key: str
    nonce: str
    tag: str
    timestamp: datetime
    attachment: AttachmentRef | None = None


class WsError(BaseModel):
    type: str = "error"
    detail: str


# --- Groups -----------------------------------------------------------------

class GroupKeyEntry(BaseModel):
    recipient: str  # member username
    encrypted_key: str  # the message's AES key, RSA-OAEP wrapped for that member


class GroupMessageIn(BaseModel):
    type: Literal["group_message"] = "group_message"
    message_id: uuid.UUID
    group_id: uuid.UUID
    encrypted_content: str
    nonce: str = Field(max_length=64)
    tag: str = Field(max_length=64)
    timestamp: datetime
    attachment: AttachmentRef | None = None
    keys: list[GroupKeyEntry] = Field(min_length=1)


class GroupMessageOut(BaseModel):
    type: Literal["group_message"] = "group_message"
    message_id: uuid.UUID
    group_id: uuid.UUID
    sender: str
    encrypted_content: str
    encrypted_key: str  # wrapped for the receiving member only
    nonce: str
    tag: str
    timestamp: datetime
    attachment: AttachmentRef | None = None


class GroupEvent(BaseModel):
    type: Literal["group_event"] = "group_event"
    group_id: uuid.UUID
    event: Literal["created", "renamed", "member_added", "member_removed", "member_left"]
    actor: str
    username: str | None = None  # the member affected, where applicable


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    members: list[str] = Field(default_factory=list)  # usernames, creator is added automatically

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Group name cannot be empty")
        return v


class GroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Group name cannot be empty")
        return v


class GroupMembersAdd(BaseModel):
    usernames: list[str] = Field(min_length=1)


class GroupMemberOut(BaseModel):
    username: str
    role: Literal["admin", "member"]
    public_key: str | None
    joined_at: datetime


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    created_by: str  # username
    created_at: datetime
    members: list[GroupMemberOut]


class GroupSummary(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    member_count: int
    my_role: Literal["admin", "member"]


# --- Multipart upload control plane -----------------------------------------

class UploadInitRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    content_type: str = "application/octet-stream"
    chunk_size: int | None = Field(default=None, gt=0, description="Bytes per part; server default if omitted")


class UploadInitResponse(BaseModel):
    upload_id: uuid.UUID  # == file_id, used for every subsequent call
    chunk_size: int
    total_parts: int
    status: UploadStatus
    part_urls: dict[int, str]  # first batch; fetch the rest via /uploads/{id}/parts


class PartUrlsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1)


class PartUrlsResponse(BaseModel):
    part_urls: dict[int, str]


class UploadedPartInfo(BaseModel):
    part_number: int
    etag: str
    size: int


class UploadStatusResponse(BaseModel):
    upload_id: uuid.UUID
    status: UploadStatus
    total_parts: int
    chunk_size: int
    uploaded_parts: list[UploadedPartInfo]
    missing_part_numbers: list[int]


class CompletePartInfo(BaseModel):
    part_number: int
    etag: str


class CompleteUploadRequest(BaseModel):
    parts: list[CompletePartInfo] = Field(min_length=1)


class UploadedFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size: int
    status: UploadStatus
    created_at: datetime
    completed_at: datetime | None


class DownloadUrlResponse(BaseModel):
    url: str
    filename: str
    expires_in: int


class ClientMessageContext(BaseModel):
    message_id: uuid.UUID
    sender: str = Field(min_length=1, max_length=50)
    text: str = Field(min_length=1, max_length=50_000)
    timestamp: datetime


class AssistantHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=20_000)


class AssistantQueryRequest(BaseModel):
    peer_username: str = Field(min_length=3, max_length=50)
    question: str = Field(min_length=1, max_length=20_000)
    # Only decrypted by the frontend. The backend keeps it in memory for this run.
    message_context: list[ClientMessageContext] = Field(default_factory=list, max_length=100)
    # Earlier turns of the user's own assistant console, oldest first.
    assistant_history: list[AssistantHistoryItem] = Field(default_factory=list, max_length=20)


class AssistantGroupQueryRequest(BaseModel):
    group_id: uuid.UUID
    question: str = Field(min_length=1, max_length=20_000)
    # Decrypted by the frontend; for a group this is the whole group's messages
    # (all senders), so the user's consent must cover other members' text.
    message_context: list[ClientMessageContext] = Field(default_factory=list, max_length=100)
    assistant_history: list[AssistantHistoryItem] = Field(default_factory=list, max_length=20)


class AssistantQueryResponse(BaseModel):
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    response: str
