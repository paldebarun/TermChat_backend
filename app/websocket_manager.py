import asyncio
from uuid import uuid4

from fastapi import WebSocket


class ConnectionManager:
    """Tracks live websocket connections per username. A single user may
    have several simultaneous connections (multiple devices/tabs); all of
    them receive a message sent to that user."""

    def __init__(self) -> None:
        self.active_connections: dict[str, dict[str, WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, websocket: WebSocket) -> str:
        await websocket.accept()
        connection_id = str(uuid4())

        async with self._lock:
            self.active_connections.setdefault(user_id, {})[connection_id] = websocket

        return connection_id

    async def disconnect(self, user_id: str, connection_id: str) -> None:
        async with self._lock:
            user_connections = self.active_connections.get(user_id)
            if user_connections is None:
                return
            user_connections.pop(connection_id, None)
            if not user_connections:
                self.active_connections.pop(user_id, None)

    async def send_to_user(self, user_id: str, message: str) -> bool:
        async with self._lock:
            sockets = list(self.active_connections.get(user_id, {}).items())

        if not sockets:
            return False

        for connection_id, websocket in sockets:
            try:
                await websocket.send_text(message)
            except Exception:
                # Connection went bad without a clean disconnect event;
                # drop it so we don't keep trying to write to it.
                await self.disconnect(user_id, connection_id)

        return True

    def is_connected(self, user_id: str) -> bool:
        return bool(self.active_connections.get(user_id))
