"""In-memory realtime relay session manager."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RealtimeRelaySession:
    id: str
    user_id: str
    provider: str
    model: str
    voice: str
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    status: str = "active"
    cancel_requested: bool = False
    cancel_reason: str | None = None
    connected: bool = False
    event_count: int = 0
    audio_chunks: int = 0
    input_audio_bytes: int = 0
    token_payload: dict[str, Any] = field(default_factory=dict)


class RealtimeRelayService:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._sessions: dict[str, RealtimeRelaySession] = {}

    async def create_session(
        self,
        *,
        user_id: str,
        provider: str,
        model: str,
        voice: str,
        token_payload: dict[str, Any] | None = None,
    ) -> RealtimeRelaySession:
        async with self._lock:
            sid = str(uuid.uuid4())
            row = RealtimeRelaySession(
                id=sid,
                user_id=str(user_id or ""),
                provider=str(provider or ""),
                model=str(model or ""),
                voice=str(voice or ""),
                token_payload=dict(token_payload or {}),
            )
            self._sessions[sid] = row
            return row

    async def get_session(self, *, relay_id: str, user_id: str | None = None) -> RealtimeRelaySession | None:
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return None
            if user_id is not None and row.user_id != str(user_id or ""):
                return None
            return row

    async def mark_connected(self, *, relay_id: str, connected: bool) -> None:
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return
            row.connected = bool(connected)
            row.updated_at = _utc_now()

    async def record_event(self, *, relay_id: str, event_type: str, input_audio_bytes: int = 0) -> None:
        del event_type
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return
            row.event_count += 1
            if input_audio_bytes > 0:
                row.audio_chunks += 1
                row.input_audio_bytes += int(input_audio_bytes)
            row.updated_at = _utc_now()

    async def request_cancel(self, *, relay_id: str, user_id: str | None = None, reason: str = "user_cancel") -> bool:
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return False
            if user_id is not None and row.user_id != str(user_id or ""):
                return False
            row.cancel_requested = True
            row.cancel_reason = str(reason or "user_cancel")
            row.status = "cancelled"
            row.updated_at = _utc_now()
            return True

    async def close(self, *, relay_id: str, status: str = "closed") -> None:
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return
            row.status = str(status or "closed")
            row.connected = False
            row.updated_at = _utc_now()

