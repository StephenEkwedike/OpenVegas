"""Metadata-only v1 events with an authoritative per-session ordering extension."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum

from .manifest import PackError, parse_json

MAX_EVENT_BYTES = 4096
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class Phase(StrEnum):
    START = "turn_started"
    BUSY = "turn_active"
    PAUSE = "awaiting_user"
    RESUME = "execution_resumed"
    COMPLETE = "turn_completed"
    CANCEL = "turn_cancelled"
    ERROR = "turn_failed"
    EXIT = "session_exited"


@dataclass(frozen=True)
class Event:
    source: str
    session_id: str
    turn_id: str
    event_id: str
    phase: Phase
    generation: int
    sequence: int
    outcome: str | None = None
    schema_version: int = 1

    def __post_init__(self):
        for value in (self.source, self.session_id, self.turn_id, self.event_id):
            if not isinstance(value, str) or not IDENTITY.fullmatch(value):
                raise ValueError(
                    "Event identifiers must be bounded opaque ASCII tokens"
                )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported event schema")
        for number in (self.generation, self.sequence):
            if type(number) is not int or not 0 <= number < 2**53:
                raise ValueError("Invalid event ordering token")
        object.__setattr__(self, "phase", Phase(self.phase))
        if self.phase == Phase.COMPLETE:
            if self.outcome != "success":
                raise ValueError("Completion requires authoritative successful outcome")
        elif self.outcome is not None:
            raise ValueError("Outcome is only supported on successful turn completion")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source, self.session_id, self.turn_id

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode("ascii")

    @classmethod
    def from_bytes(cls, data: bytes) -> Event:
        try:
            raw = parse_json(data, MAX_EVENT_BYTES)
            required = {
                "schema_version",
                "source",
                "session_id",
                "turn_id",
                "event_id",
                "phase",
                "generation",
                "sequence",
            }
            if not required <= raw.keys() or raw.keys() - required - {"outcome"}:
                raise ValueError("Invalid event fields")
            return cls(**raw)
        except (PackError, TypeError) as exc:
            raise ValueError("Invalid event payload") from exc
