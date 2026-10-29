"""Deterministic lifecycle; no terminal writes, threads, sleeps, or global state."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from enum import StrEnum

from PIL import Image

from .events import IDENTITY, Event, Phase
from .manifest import LoadedPack


class State(StrEnum):
    OFF = "off"
    IDLE = "idle"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


class EmoteController:
    """One source/session per owner. Keep this instance for the session lifetime.

    Call on the compositor's event loop. Only START may advance generation;
    sequence increases within that generation. Never derive either from arrival
    time or an untrusted event ID. Cosmetic callback failures are isolated.
    """

    def __init__(
        self,
        pack: LoadedPack,
        *,
        source: str,
        session_id: str,
        clock: Callable[[], float] = time.monotonic,
        request_invalidation: Callable[[], None] = lambda: None,
        reduced_motion: bool = False,
        enabled: bool = True,
        max_turns: int = 10000,
        active_lease_seconds: float = 20.0,
        completion_pack: LoadedPack | None = None,
    ):
        if not all(
            isinstance(v, str) and IDENTITY.fullmatch(v) for v in (source, session_id)
        ):
            raise ValueError("Invalid source or session identity")
        if type(max_turns) is not int or max_turns < 1:
            raise ValueError("max_turns must be positive")
        if (
            type(active_lease_seconds) not in {int, float}
            or not math.isfinite(active_lease_seconds)
            or not 0 < active_lease_seconds <= 300
        ):
            raise ValueError("Active lease must be finite and within 0-300 seconds")
        self.active_lease_seconds = float(active_lease_seconds)
        self._last_active: float | None = None
        self.pack = pack
        self.completion_pack = completion_pack
        self.source, self.session_id = source, session_id
        self._clock, self._invalidate = clock, request_invalidation
        self.reduced_motion = reduced_motion
        self.max_turns = max_turns
        self._state = State.IDLE if enabled else State.OFF
        self._last_time = float("-inf")
        self._since = self._now()
        self._active_elapsed = 0.0
        self._generation, self._sequence = -1, -1
        self._turn: str | None = None
        self._seen_turns: set[str] = set()
        self._retired: set[str] = set()
        self._transport: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self.reason = ""
        self._signature = self._visual_signature(self._since)

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("Clock must be finite")
        self._last_time = max(self._last_time, now)
        return self._last_time

    def _notify(self, now: float, *, force: bool = False) -> bool:
        signature = self._visual_signature(now)
        changed = force or signature != self._signature
        self._signature = signature
        if changed:
            try:
                self._invalidate()
            except Exception:  # noqa: BLE001 - isolate the host's cosmetic callback
                self.reason = "invalidation_failed"
        return changed

    def _set(self, state: State, now: float) -> None:
        self._state, self._since = state, now

    def _expire_active(self, now: float) -> None:
        if (
            self._state == State.ACTIVE
            and self._last_active is not None
            and now - self._last_active >= self.active_lease_seconds
        ):
            if self._turn:
                self._retired.add(self._turn)
            self.reason = "active_lease_expired"
            self._set(State.IDLE, now)

    def handle(self, event: Event) -> bool:
        """Accept metadata only. Return false for stale/duplicate/foreign events."""
        if not isinstance(event, Event):
            raise TypeError("Expected a validated Event")
        if self._closed or (event.source, event.session_id) != (
            self.source,
            self.session_id,
        ):
            return False
        if event.event_id in self._transport:
            return False
        if event.generation < self._generation:
            return False
        if event.generation == self._generation and event.sequence <= self._sequence:
            return False
        now = self._now()
        self._expire_active(now)
        if event.phase == Phase.START:
            if event.turn_id == self._turn and event.generation == self._generation:
                pass  # Repeated busy/start notifications must not reset phase.
            elif (
                event.generation <= self._generation
                or event.turn_id in self._seen_turns
            ):
                return False
            else:
                if len(self._seen_turns) >= self.max_turns:
                    self.reason = "session_turn_limit"
                    self.close()
                    return False
                if self._turn:
                    self._retired.add(self._turn)
                self._turn, self._generation = event.turn_id, event.generation
                self._seen_turns.add(event.turn_id)
                self._active_elapsed = 0.0
                if self._state == State.OFF:
                    self._retired.add(event.turn_id)
                else:
                    self._set(State.ACTIVE, now)
        elif event.generation != self._generation or event.turn_id != self._turn:
            return False
        self._sequence = event.sequence
        self._transport[event.event_id] = None
        if len(self._transport) > 4096:
            self._transport.popitem(last=False)
        if event.phase == Phase.EXIT:
            self.close()
            return True
        if self._state == State.OFF:
            self._retired.add(event.turn_id)
            return True
        if event.phase in {Phase.CANCEL, Phase.ERROR}:
            self._retired.add(event.turn_id)
            self._set(
                State.CANCELLED if event.phase == Phase.CANCEL else State.ERROR, now
            )
        elif event.turn_id not in self._retired:
            if event.phase == Phase.PAUSE and self._state == State.ACTIVE:
                self._active_elapsed += now - self._since
                self._set(State.PAUSED, now)
            elif event.phase == Phase.RESUME and self._state == State.PAUSED:
                self._set(State.ACTIVE, now)
            elif event.phase == Phase.COMPLETE and self._state in {
                State.ACTIVE,
                State.PAUSED,
            }:
                self._retired.add(event.turn_id)
                self._set(State.COMPLETE, now)
        if (
            event.turn_id not in self._retired
            and self._state == State.ACTIVE
            and event.phase in {Phase.START, Phase.BUSY, Phase.RESUME}
        ):
            self._last_active = now
        self._notify(now)
        return True

    def tick(self) -> bool:
        """Host timer (at most 30 Hz) calls this; no second render owner exists."""
        now = self._now()
        self._expire_active(now)
        if (
            self._state == State.COMPLETE
            and now - self._since
            >= self._frame_pack().manifest.animations["complete"].duration
        ):
            self._set(State.IDLE, now)
        return self._notify(now)

    @property
    def current_state(self) -> State:
        self.tick()
        return self._state

    @property
    def currentstate(self) -> State:
        return self.current_state

    def _index(self, now: float) -> int | None:
        if self._state == State.OFF:
            return None
        manifest = self._frame_pack().manifest
        if self.reduced_motion:
            return manifest.reduced_motion_frame
        if self._state == State.ACTIVE:
            return manifest.animations["waiting"].frame_at(
                self._active_elapsed + now - self._since
            )
        if self._state == State.COMPLETE:
            return manifest.animations["complete"].frame_at(now - self._since)
        if self._state == State.ERROR and "error" in manifest.animations:
            return manifest.animations["error"].frames[0]
        # Quiet states do not keep repainting an idle dance or flash an error.
        return manifest.animations["idle"].frames[0]

    def _visual_signature(self, now: float) -> tuple[State, int | None]:
        return self._state, self._index(now)

    @property
    def frame_index(self) -> int | None:
        self.tick()
        return self._index(self._last_time)

    def current_frame(self) -> Image.Image | None:
        index = self.frame_index
        return None if index is None else self._frame_pack().frame(index)

    def _frame_pack(self) -> LoadedPack:
        return (
            self.completion_pack
            if self._state == State.COMPLETE and self.completion_pack is not None
            else self.pack
        )

    def replace_completion_pack(self, pack: LoadedPack | None) -> None:
        """Access must be checked by the owner; never replay a retired success."""
        self.completion_pack = pack
        self.set_enabled(self._state != State.OFF)
        self._notify(self._now(), force=True)

    def set_enabled(self, enabled: bool) -> None:
        if self._closed:
            return
        if self._turn:
            self._retired.add(self._turn)
        self._set(State.IDLE if enabled else State.OFF, self._now())
        self._notify(self._last_time)

    def replace_pack(self, pack: LoadedPack) -> None:
        """Call only after access validation; pack changes retire active effects."""
        self.pack = pack
        self.set_enabled(self._state != State.OFF)
        self._notify(self._now(), force=True)

    def close(self) -> None:
        if self._turn:
            self._retired.add(self._turn)
        self._closed = True
        self._set(State.OFF, self._now())
        self._notify(self._last_time)
