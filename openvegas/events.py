"""Normalized UI/stream event envelope helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

EventType = Literal[
    "attachment_added",
    "attachment_removed",
    "upload_started",
    "upload_succeeded",
    "upload_failed",
    "tool_start",
    "tool_progress",
    "tool_result",
    "stream_start",
    "stream_delta",
    "stream_end",
    "capability_unavailable",
    "warning",
    "error",
    "response.started",
    "response.delta",
    "response.completed",
    "response.error",
    "tool.call",
    "tool.result",
]


@dataclass(frozen=True)
class UIEventEnvelope:
    schema_version: str
    run_id: str
    turn_id: str
    sequence_no: int
    ts: str
    type: EventType
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mk_event(
    *,
    run_id: str,
    turn_id: str,
    sequence_no: int,
    event_type: EventType,
    payload: dict[str, Any] | None = None,
) -> UIEventEnvelope:
    return UIEventEnvelope(
        schema_version="ui_event_v1",
        run_id=str(run_id or ""),
        turn_id=str(turn_id or ""),
        sequence_no=max(1, int(sequence_no)),
        ts=datetime.now(timezone.utc).isoformat(),
        type=event_type,
        payload=dict(payload or {}),
    )
