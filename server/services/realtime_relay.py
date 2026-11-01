"""In-memory realtime relay session manager."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mono_now() -> float:
    return float(time.monotonic())


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
    created_mono: float = field(default_factory=_mono_now)
    updated_mono: float = field(default_factory=_mono_now)


class RealtimeRelayService:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._sessions: dict[str, RealtimeRelaySession] = {}

    @staticmethod
    def _session_ttl_sec() -> float:
        return max(60.0, float(os.getenv("OPENVEGAS_REALTIME_SESSION_TTL_SEC", "3600")))

    @staticmethod
    def _closed_ttl_sec() -> float:
        return max(5.0, float(os.getenv("OPENVEGAS_REALTIME_CLOSED_SESSION_TTL_SEC", "120")))

    @staticmethod
    def _max_sessions_per_user() -> int:
        return max(1, min(64, int(os.getenv("OPENVEGAS_REALTIME_MAX_SESSIONS_PER_USER", "8"))))

    def _is_expired(self, row: RealtimeRelaySession, now_mono: float) -> bool:
        age = max(0.0, now_mono - float(row.updated_mono or row.created_mono or now_mono))
        if row.status in {"closed", "cancelled"}:
            return age >= self._closed_ttl_sec()
        return age >= self._session_ttl_sec()

    def _prune_locked(self, now_mono: float) -> int:
        stale_ids = [sid for sid, row in self._sessions.items() if self._is_expired(row, now_mono)]
        for sid in stale_ids:
            self._sessions.pop(sid, None)
        return len(stale_ids)

    def _enforce_user_cap_locked(self, user_id: str) -> None:
        cap = self._max_sessions_per_user()
        owned = [row for row in self._sessions.values() if row.user_id == user_id]
        if len(owned) <= cap:
            return
        # Prune oldest first, preferring already-closed sessions.
        ordered = sorted(
            owned,
            key=lambda r: (
                0 if r.status in {"closed", "cancelled"} else 1,
                float(r.updated_mono or r.created_mono),
            ),
        )
        for row in ordered[: max(0, len(owned) - cap)]:
            self._sessions.pop(row.id, None)

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
            now_mono = _mono_now()
            self._prune_locked(now_mono)
            sid = str(uuid.uuid4())
            row = RealtimeRelaySession(
                id=sid,
                user_id=str(user_id or ""),
                provider=str(provider or ""),
                model=str(model or ""),
                voice=str(voice or ""),
                token_payload=dict(token_payload or {}),
                created_mono=now_mono,
                updated_mono=now_mono,
            )
            self._sessions[sid] = row
            self._enforce_user_cap_locked(row.user_id)
            return row

    async def get_session(self, *, relay_id: str, user_id: str | None = None) -> RealtimeRelaySession | None:
        async with self._lock:
            now_mono = _mono_now()
            self._prune_locked(now_mono)
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return None
            if user_id is not None and row.user_id != str(user_id or ""):
                return None
            if self._is_expired(row, now_mono):
                self._sessions.pop(str(relay_id or ""), None)
                return None
            return row

    async def mark_connected(self, *, relay_id: str, connected: bool) -> None:
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return
            row.connected = bool(connected)
            row.updated_at = _utc_now()
            row.updated_mono = _mono_now()

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
            row.updated_mono = _mono_now()

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
            row.updated_mono = _mono_now()
            return True

    async def close(self, *, relay_id: str, status: str = "closed") -> None:
        async with self._lock:
            row = self._sessions.get(str(relay_id or ""))
            if not row:
                return
            row.status = str(status or "closed")
            row.connected = False
            row.updated_at = _utc_now()
            row.updated_mono = _mono_now()

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            now_mono = _mono_now()
            pruned = self._prune_locked(now_mono)
            active = sum(1 for row in self._sessions.values() if row.status == "active")
            connected = sum(1 for row in self._sessions.values() if row.connected)
            return {
                "total": len(self._sessions),
                "active": active,
                "connected": connected,
                "pruned": pruned,
            }

