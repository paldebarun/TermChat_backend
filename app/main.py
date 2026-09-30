import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from app import auth, crud, storage
from app.config import get_settings
from app.database import AsyncSessionLocal, init_models
from app.models import UploadStatus
from app.routers import auth as auth_router
from app.routers import uploads as uploads_router
from app.routers import users as users_router
from app.schemas import AttachmentRef, EncryptedMessageIn, EncryptedMessageOut
from app.websocket_manager import ConnectionManager

settings = get_settings()
manager = ConnectionManager()
logger = logging.getLogger(__name__)


async def _orphan_cleanup_loop() -> None:
    """Periodically deletes completed uploads that were never attached to
    a message (browser closed, attachment removed post-upload, etc)."""
    while True:
        await asyncio.sleep(settings.orphan_cleanup_interval_minutes * 60)
        try:
            async with AsyncSessionLocal() as db:
                orphans = await crud.get_orphaned_completed_files(
                    db, older_than_minutes=settings.orphan_cleanup_threshold_minutes
                )
                for file in orphans:
                    try:
                        storage.delete_object(file.s3_key)
                    except Exception:
                        logger.exception("Failed to delete orphaned object %s", file.s3_key)
                        continue
                    await crud.set_upload_status(db, file, UploadStatus.DELETED)
                if orphans:
                    logger.info("Orphan cleanup removed %d unattached file(s)", len(orphans))
        except Exception:
            logger.exception("Orphan cleanup pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    storage.ensure_bucket()
    cleanup_task = asyncio.create_task(_orphan_cleanup_loop())
    yield
    cleanup_task.cancel()


app = FastAPI(title="E2E Encrypted Chat", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(users_router.router)
app.include_router(uploads_router.router)


@app.get("/")
async def health_check():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """Authenticated relay for end-to-end encrypted messages.

    The client connects with `?token=<access_token>` (obtained from
    POST /auth/login). Every field in the message payload other than
    message_id/recipient/timestamp is ciphertext produced client-side
    (see client/crypto_utils.py) - this endpoint never decrypts anything,
    it only authenticates the connection, persists ciphertext, and
    forwards it to the recipient if they're online (or queues it for
    delivery on their next connect).
    """
    async with AsyncSessionLocal() as db:
        user = await auth.get_user_from_ws_token(token, db)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        username = user.username
        user_id = user.id
        connection_id = await manager.connect(user_id=username, websocket=websocket)

        # Flush anything that arrived while this user was offline.
        for pending in await crud.get_undelivered_messages(db, user_id):
            sender = await crud.get_user_by_id(db, pending.sender_id)
            out = EncryptedMessageOut(
                message_id=pending.id,
                sender=sender.username if sender else "unknown",
                encrypted_content=pending.encrypted_content,
                encrypted_key=pending.encrypted_key,
                nonce=pending.nonce,
                tag=pending.tag,
                timestamp=pending.created_at,
                attachment=(
                    AttachmentRef(file_id=pending.attachment_file_id, filename=pending.attachment_filename)
                    if pending.attachment_file_id
                    else None
                ),
            )
            await websocket.send_text(out.model_dump_json())
            await crud.mark_delivered(db, pending)

    try:
        while True:
            raw_message = await websocket.receive_text()

            async with AsyncSessionLocal() as db:
                try:
                    payload = EncryptedMessageIn.model_validate_json(raw_message)
                except Exception:
                    await websocket.send_text('{"type": "error", "detail": "invalid message payload"}')
                    continue

                recipient = await crud.get_user_by_username(db, payload.recipient)
                if recipient is None:
                    await websocket.send_text('{"type": "error", "detail": "unknown recipient"}')
                    continue

                if payload.attachment is not None:
                    attached_file = await crud.get_uploaded_file(db, payload.attachment.file_id)
                    if (
                        attached_file is None
                        or attached_file.owner_id != user_id
                        or attached_file.status != UploadStatus.COMPLETED
                    ):
                        await websocket.send_text('{"type": "error", "detail": "invalid attachment"}')
                        continue

                recipient_online = manager.is_connected(recipient.username)

                await crud.save_message(
                    db,
                    sender_id=user_id,
                    recipient_id=recipient.id,
                    payload=payload,
                    delivered=recipient_online,
                )

                if recipient_online:
                    out = EncryptedMessageOut(
                        message_id=payload.message_id,
                        sender=username,
                        encrypted_content=payload.encrypted_content,
                        encrypted_key=payload.encrypted_key,
                        nonce=payload.nonce,
                        tag=payload.tag,
                        timestamp=payload.timestamp,
                        attachment=payload.attachment,
                    )
                    await manager.send_to_user(recipient.username, out.model_dump_json())

    except WebSocketDisconnect:
        await manager.disconnect(user_id=username, connection_id=connection_id)


def main():
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
