"""In-memory tool event stream fan-out (best-effort)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, AsyncGenerator, DefaultDict


StreamKey = tuple[str, str]  # (run_id, tool_call_id)
_SUBSCRIBERS: DefaultDict[StreamKey, set[asyncio.Queue]] = defaultdict(set)


def publish_tool_event(*, run_id: str, tool_call_id: str, event: dict[str, Any]) -> None:
    key: StreamKey = (run_id, tool_call_id)
    for q in list(_SUBSCRIBERS.get(key, set())):
        try:
            q.put_nowait(dict(event))
        except Exception:
            continue


async def stream_tool_events(*, run_id: str, tool_call_id: str) -> AsyncGenerator[dict[str, Any], None]:
    key: StreamKey = (run_id, tool_call_id)
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _SUBSCRIBERS[key].add(q)
    try:
        while True:
            event = await q.get()
            yield event
    finally:
        _SUBSCRIBERS[key].discard(q)
        if not _SUBSCRIBERS[key]:
            _SUBSCRIBERS.pop(key, None)

