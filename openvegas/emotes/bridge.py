"""Silent lifecycle bridge for an authoritative chat/process coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from .events import IDENTITY, Event, Phase
from .spool import publish_event

HEARTBEAT_SECONDS = 5.0


@dataclass(frozen=True)
class TurnToken:
    turn_id: str
    generation: int


class ChatEmoteBridge:
    """One bridge per source/session; call from the owning lifecycle thread.

    Methods return whether metadata was delivered. State advances even when the
    publisher fails: cosmetic failures never retry or resurrect terminal turns.
    begin returns a token rather than delivery status. Supply that token to
    asynchronous callbacks so an old completion cannot finish a newer turn.
    """

    def __init__(
        self,
        session_id: str,
        publish: Callable[[Event], object] = publish_event,
        *,
        source: str = "openvegas",
        initial_generation: int = 0,
    ):
        if not all(isinstance(v, str) and IDENTITY.fullmatch(v) for v in (source, session_id)):
            raise ValueError("Invalid source/session identity")
        if type(initial_generation) is not int or not 0 <= initial_generation < 2**53 - 1:
            raise ValueError("Invalid initial generation")
        self.source, self.session_id = source, session_id
        self._publish = publish
        self._generation = initial_generation
        self._sequence = -1
        self._turn: TurnToken | None = None
        self._seen: set[str] = set()
        self._terminal = True
        self._paused = False
        self._closed = False
        self._supervised: set[TurnToken] = set()

    @property
    def current_turn(self) -> TurnToken | None:
        return self._turn

    def _emit(self, phase: Phase) -> bool:
        if self._turn is None:
            return False
        self._sequence += 1
        try:
            event = Event(
                self.source,
                self.session_id,
                self._turn.turn_id,
                uuid4().hex,
                phase,
                self._turn.generation,
                self._sequence,
                "success" if phase == Phase.COMPLETE else None,
            )
            return self._publish(event) is True
        except Exception:  # noqa: BLE001 - cosmetics must not interrupt the host
            return False

    def _eligible(self, turn: TurnToken | None) -> bool:
        return (
            not self._closed
            and not self._terminal
            and self._turn is not None
            and (turn is None or turn == self._turn)
        )

    def begin(self, turn_id: str | None = None) -> TurnToken | None:
        if self._closed:
            return None
        turn_id = uuid4().hex if turn_id is None else turn_id
        if not isinstance(turn_id, str) or not IDENTITY.fullmatch(turn_id):
            raise ValueError("Invalid logical turn identity")
        if turn_id in self._seen:
            return self._turn if self._turn and self._turn.turn_id == turn_id else None
        if len(self._seen) >= 10000 or self._generation >= 2**53 - 2:
            self.close()
            return None
        self.cancel()
        self._generation += 1
        self._sequence = -1
        self._turn = TurnToken(turn_id, self._generation)
        self._seen.add(turn_id)
        self._terminal = False
        self._paused = False
        self._emit(Phase.START)
        return self._turn

    def active(self, *, turn: TurnToken | None = None) -> bool:
        if not self._eligible(turn) or self._paused:
            return False
        return self._emit(Phase.BUSY)

    async def _heartbeat(self, turn: TurnToken) -> None:
        try:
            while self._eligible(turn):
                await asyncio.sleep(HEARTBEAT_SECONDS)
                self.active(turn=turn)  # pause/terminal/stale tokens emit nothing
        except Exception:  # noqa: BLE001 - cosmetic tasks never interrupt chat
            return

    @asynccontextmanager
    async def supervise(self, *, turn: TurnToken | None):
        """Heartbeat an accepted turn; caller still owns finish/cancel decisions.

        No task for None/stale/terminal tokens or nested supervision. Exiting stops
        and joins the task, but never invents completion or changes chat outcomes.
        """
        task = None
        owner = turn is not None and self._eligible(turn) and turn not in self._supervised
        if owner:
            self._supervised.add(turn)
            heartbeat = self._heartbeat(turn)
            try:
                task = asyncio.create_task(heartbeat, name="emote-heartbeat")
            except Exception:  # noqa: BLE001 - task setup is cosmetic too
                heartbeat.close()
                self._supervised.discard(turn)
                owner = False
        try:
            yield
        finally:
            try:
                if task is not None:
                    task.cancel()
                    caller = asyncio.current_task()
                    cancelling = caller.cancelling() if caller else 0
                    try:
                        await task
                    except asyncio.CancelledError:
                        # Suppress the child's cancellation, never a new host cancellation.
                        if caller and caller.cancelling() > cancelling:
                            raise
                    except Exception:  # noqa: BLE001, S110 - cosmetic failures stay silent
                        pass
            finally:
                if owner:
                    self._supervised.discard(turn)

    def pause(self, *, turn: TurnToken | None = None) -> bool:
        if not self._eligible(turn) or self._paused:
            return False
        self._paused = True
        return self._emit(Phase.PAUSE)

    def resume(self, *, turn: TurnToken | None = None) -> bool:
        if not self._eligible(turn) or not self._paused:
            return False
        self._paused = False
        return self._emit(Phase.RESUME)

    def finish(self, success: bool, *, turn: TurnToken | None = None) -> bool:
        if type(success) is not bool or not self._eligible(turn):
            return False
        self._terminal = True
        return self._emit(Phase.COMPLETE if success else Phase.ERROR)

    def cancel(self, *, turn: TurnToken | None = None) -> bool:
        if not self._eligible(turn):
            return False
        self._terminal = True
        return self._emit(Phase.CANCEL)

    def close(self) -> bool:
        """End the session, canceling unfinished work; not a per-turn finalizer.

        EXIT is session metadata, not a second turn outcome. Do not call close
        immediately after successful finish: EXIT intentionally cancels rendering.
        """
        if self._closed:
            return False
        self.cancel()
        self._closed = True
        return self._emit(Phase.EXIT)
