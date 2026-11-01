"""In-memory IDE bridge session registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Tuple
import asyncio

from openvegas.ide.bridge_types import IDEBridge


BridgeSessionKey = Tuple[str, str]  # (run_id, runtime_session_id)


@dataclass
class BridgeSession:
    run_id: str
    runtime_session_id: str
    actor_id: str
    ide_type: str
    workspace_root: str
    workspace_fingerprint: str
    bridge: IDEBridge
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BridgeRegistry:
    def __init__(self):
        self._sessions: Dict[BridgeSessionKey, BridgeSession] = {}
        self._event_queues: Dict[BridgeSessionKey, asyncio.Queue[dict[str, Any]]] = {}

    def register(self, session: BridgeSession) -> bool:
        key: BridgeSessionKey = (session.run_id, session.runtime_session_id)
        replaced = key in self._sessions
        self._sessions[key] = session
        self._event_queues.setdefault(key, asyncio.Queue())
        return replaced

    def get(self, *, run_id: str, runtime_session_id: str) -> BridgeSession | None:
        return self._sessions.get((run_id, runtime_session_id))

    def get_for_actor(self, *, run_id: str, runtime_session_id: str, actor_id: str) -> BridgeSession | None:
        session = self.get(run_id=run_id, runtime_session_id=runtime_session_id)
        if not session:
            return None
        if str(session.actor_id) != str(actor_id):
            return None
        return session

    def unregister(self, *, run_id: str, runtime_session_id: str) -> None:
        key = (run_id, runtime_session_id)
        self._sessions.pop(key, None)
        self._event_queues.pop(key, None)

    async def publish_event(self, *, run_id: str, runtime_session_id: str, event: dict[str, Any]) -> None:
        key = (run_id, runtime_session_id)
        queue = self._event_queues.get(key)
        if not queue:
            return
        await queue.put(dict(event))

    async def stream_events(
        self,
        *,
        run_id: str,
        runtime_session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        key = (run_id, runtime_session_id)
        queue = self._event_queues.get(key)
        if queue is None:
            return
        while True:
            event = await queue.get()
            yield event


_REGISTRY = BridgeRegistry()


def get_bridge_registry() -> BridgeRegistry:
    return _REGISTRY
