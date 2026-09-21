"""Opt-in observer for a host's already-owned, ordered Codex metadata feed.

No process, socket, raw-JSON reader, provider request, config, or background task.
Call on the owning lifecycle thread, not competing callbacks. Ownership assertions
are a host contract, NOT authentication or permission to attach to a session.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass

from .adapters import adapt_codex_app_server, supports_codex_app_server
from .bridge import ChatEmoteBridge, TurnToken
from .events import IDENTITY, Event, Phase
from .runner import reserve_generation
from .spool import EventSpool

MAX_TURNS = 512
MAX_ORDINAL = 2**53 - 1
HEARTBEAT_SECONDS = 5.0
TURN_METHODS = ("turn/started", "turn/completed")
PAUSE_METHODS = (
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/tool/requestUserInput", "item/permissions/requestApproval",
)
METHODS = (*TURN_METHODS, *PAUSE_METHODS, "error")
STATUSES = ("inProgress", "completed", "failed", "interrupted", "unknown")


def _identity(value: object) -> bool:
    return type(value) is str and IDENTITY.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class CodexLifecycleMetadata:
    """Only allowlisted scalars; never retain a native message or error body."""

    thread_id: str
    turn_id: str
    method: str
    status: str | None = None
    has_error: bool = False
    will_retry: bool | None = None

    def __post_init__(self):
        if (
            not _identity(self.thread_id) or not _identity(self.turn_id)
            or type(self.method) is not str or self.method not in METHODS
            or type(self.has_error) is not bool
            or (self.will_retry is not None and type(self.will_retry) is not bool)
            or (self.method in TURN_METHODS
                and (type(self.status) is not str or self.status not in STATUSES))
            or (self.method not in TURN_METHODS and self.status is not None)
        ):
            raise ValueError("Invalid lifecycle metadata")

    def event(self, version: tuple[int, int, int]) -> Event | None:
        params = {"threadId": self.thread_id}
        if self.method in TURN_METHODS:
            params["turn"] = {
                "id": self.turn_id, "status": self.status,
                "error": {} if self.has_error else None,
            }
        else:
            params.update(turnId=self.turn_id, willRetry=self.will_retry)
        return adapt_codex_app_server(
            {"method": self.method, "params": params}, host_version=version,
            session_id=self.thread_id, turn_id=self.turn_id,
            generation=0, sequence=0, event_id="projection",
        )


def project_codex_lifecycle(
    payload: dict, *, host_version: tuple[int, int, int],
) -> CodexLifecycleMetadata | None:
    """Project an ALREADY decoded owner message before any emote tee/queue.

    Never walk/copy items, text, tools, paths, errors, or other content. None is
    not evidence of success. If a required lifecycle message cannot be projected,
    the owner must close the observer, rather than silently lose that barrier.
    """
    if not supports_codex_app_server(host_version) or type(payload) is not dict:
        return None
    method, params = payload.get("method"), payload.get("params")
    if type(method) is not str or method not in METHODS or type(params) is not dict:
        return None
    status, has_error, will_retry = None, False, None
    if method in TURN_METHODS:
        turn = params.get("turn")
        if type(turn) is not dict:
            return None
        turn_id = turn.get("id")
        status = turn.get("status")
        if type(status) is not str or status not in STATUSES:
            status = "unknown"
        has_error = turn.get("error") is not None
    else:
        turn_id = params.get("turnId")
        if method == "error":
            will_retry = params.get("willRetry")
            if type(will_retry) is not bool:
                return None
    try:
        return CodexLifecycleMetadata(
            params.get("threadId"), turn_id, method, status, has_error, will_retry,
        )
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class OwnedStreamAuthorization:
    """Out-of-band, user-authorized host assertions, never parsed from the feed.

    Use a fresh stream_id per connection. The host must own the provider transport
    and opt-in decision. These tokens are correlation metadata, not credentials.
    """

    owner_id: str
    stream_id: str
    thread_id: str
    session_id: str
    host_version: tuple[int, int, int]
    owns_stream: bool = False
    ordered: bool = False
    metadata_only: bool = False


@dataclass(frozen=True, slots=True)
class OwnedStreamRecord:
    owner_id: str
    stream_id: str
    ordinal: int
    metadata: CodexLifecycleMetadata


class OwnedCodexStream:
    """One opt-in observer per stream/thread; deliver before downstream fan-out.

    Ordinals start at zero and are assigned by the owner to the selected metadata
    feed BEFORE buffering, not by arrival here. Gaps disable, old/duplicate records
    drop. No replay/reconnect recovery or implicit approval resume. The owner may
    explicitly heartbeat a validated active turn on this same lifecycle thread;
    this object never creates a timer or a second writer.
    The spool bounds output to 256 events; any failed write disables this observer.
    Filesystem latency is OS-dependent, not a hard-real-time guarantee.
    """

    def __init__(
        self, authorization: OwnedStreamAuthorization, *, enabled: bool = False,
        spool: EventSpool | None = None, max_turns: int = MAX_TURNS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._authorization = authorization
        self._spool = spool
        self._bridge: ChatEmoteBridge | None = None
        self._seen: set[str] = set()
        self._ordinal = -1
        self._terminal = True
        self._paused = False
        self._clock = clock
        self._last_heartbeat_at: float | None = None
        self._consuming = False
        self._max_turns = max_turns
        self.reason = ""
        if enabled is not True:
            self.reason = "not_enabled"
        elif type(authorization) is not OwnedStreamAuthorization or not all(
            value is True for value in (
                authorization.owns_stream, authorization.ordered, authorization.metadata_only,
            )
        ):
            self.reason = "not_authorized"
        elif not all(_identity(value) for value in (
            authorization.owner_id, authorization.stream_id,
            authorization.thread_id, authorization.session_id,
        )):
            self.reason = "invalid_authorization"
        elif not supports_codex_app_server(authorization.host_version):
            self.reason = "unsupported_version"
        elif type(max_turns) is not int or not 1 <= max_turns <= MAX_TURNS:
            self.reason = "invalid_bound"
        elif not callable(clock):
            self.reason = "invalid_clock"

    @property
    def enabled(self) -> bool:
        return not self.reason

    @property
    def active_turn(self) -> TurnToken | None:
        """Snapshot for the owner's heartbeat; stale tokens never renew a turn."""
        if not self.enabled or self._terminal or self._paused or self._bridge is None:
            return None
        return self._bridge.current_turn

    def _heartbeat_time(self) -> float | None:
        try:
            now = self._clock()
            if type(now) not in (int, float) or not math.isfinite(now):
                raise ValueError("Invalid monotonic clock")
            if self._last_heartbeat_at is not None and now < self._last_heartbeat_at:
                raise ValueError("Clock moved backwards")
        except Exception:  # noqa: BLE001 - retire cosmetics, never fail the host
            self.close(reason="invalid_clock")
            return None
        return float(now)

    def heartbeat(self, *, turn: TurnToken) -> bool:
        """Renew only a validated active turn, at most once per five seconds.

        Call serially from the transport owner, only while BOTH protocol ends
        remain connected. This is task liveness, never provider progress or
        success. No START/RESUME, generation, native ordinal, timer, queue, or
        writer is created here. PAUSE, cancellation, failure, close, and lost
        publication remain latched; neither old timers nor heartbeats revive them.
        The controller still rejects late renewals after its own lease expires.
        """
        current = self.active_turn
        if type(turn) is not TurnToken or current is None or turn != current:
            return False
        now = self._heartbeat_time()
        if now is None or now - self._last_heartbeat_at < HEARTBEAT_SECONDS:
            return False
        self._last_heartbeat_at = now
        return self._bridge.active(turn=turn)

    def _publish(self, event: Event) -> bool:
        if not self.enabled:
            return False
        try:
            delivered = self._spool.publish(event) is True
        except Exception:  # noqa: BLE001 - no cosmetic exception into the provider
            delivered = False
        if not delivered:
            # Do not retry, even a terminal: a missing pause/cancel must never be
            # followed by an apparent success. Existing controller leases go idle.
            self.reason = "publication_failed"
        return delivered

    def _remember(self, turn_id: str) -> bool:
        if turn_id in self._seen:
            return True
        if len(self._seen) >= self._max_turns:
            self.close(reason="turn_limit")
            return False
        self._seen.add(turn_id)
        return True

    def deliver(self, record: OwnedStreamRecord) -> bool:
        """Return True only for delivered lifecycle edges, not duplicates/no-ops."""
        if not self.enabled:
            return False
        if type(record) is not OwnedStreamRecord or type(record.metadata) is not CodexLifecycleMetadata:
            self.close(reason="invalid_record")
            return False
        auth, metadata = self._authorization, record.metadata
        if (record.owner_id != auth.owner_id or record.stream_id != auth.stream_id
                or metadata.thread_id != auth.thread_id):
            return False
        ordinal = record.ordinal
        if type(ordinal) is not int or not 0 <= ordinal <= MAX_ORDINAL:
            self.close(reason="invalid_order")
            return False
        if ordinal <= self._ordinal:
            return False
        if ordinal != self._ordinal + 1:
            self.close(reason="stream_gap")
            return False
        self._ordinal = ordinal
        turn_id = metadata.turn_id
        if turn_id in self._seen and (
            self._bridge is None or self._terminal
            or self._bridge.current_turn.turn_id != turn_id
        ):
            return False  # Even malformed late status cannot retire a newer turn.
        event = metadata.event(auth.host_version)
        if event is None:
            if metadata.method == "error" and metadata.will_retry is True:
                return False
            self.close(reason="unknown_lifecycle")
            return False
        if event.phase == Phase.START:
            if turn_id in self._seen or not self._remember(turn_id):
                return False
            if self._bridge is not None and not self._terminal:
                self._bridge.cancel()
            if not self.enabled:
                return False
            if self._spool is None:
                self._spool = EventSpool()
            generation = reserve_generation(self._spool, source="codex", session_id=auth.session_id)
            if generation is None:
                self.close(reason="generation_unavailable")
                return False
            now = self._heartbeat_time()
            if now is None:
                return False
            self._bridge = ChatEmoteBridge(
                auth.session_id, self._publish, source="codex", initial_generation=generation - 1,
            )
            self._terminal = False
            self._paused = False
            self._last_heartbeat_at = now
            self._bridge.begin(turn_id)
            return self.enabled
        if self._bridge is None or self._bridge.current_turn.turn_id != turn_id:
            # A terminal/approval before START tombstones the turn, without
            # manufacturing a start or letting a delayed start revive it.
            self._remember(turn_id)
            return False
        if self._terminal:
            return False
        if event.phase == Phase.PAUSE:
            self._paused = True
            return self._bridge.pause()
        self._terminal = True
        if event.phase == Phase.CANCEL:
            return self._bridge.cancel()
        return self._bridge.finish(event.phase == Phase.COMPLETE)

    def cancel(self, turn_id: str) -> bool:
        """Owner calls BEFORE dispatching its own interrupt; never sends an RPC.

        Cancellation intent latches even if the provider later reports success.
        This is conservative visual retirement, not proof of provider interruption.
        """
        if not self.enabled or not _identity(turn_id) or not self._remember(turn_id):
            return False
        if (self._bridge is None or self._terminal
                or self._bridge.current_turn.turn_id != turn_id):
            return False
        self._terminal = True
        return self._bridge.cancel()

    def close(self, *, reason: str = "closed") -> None:
        """Detach cosmetics only. Never close/interrupt the owner's transport.

        EOF is not success. Unfinished visuals get CANCEL/EXIT; a completed turn
        is left to its authored duration, rather than erased by an immediate EXIT.
        """
        if type(reason) is not str or reason not in (
            "closed", "turn_limit", "invalid_record", "invalid_order", "stream_gap",
            "unknown_lifecycle", "generation_unavailable", "stream_error", "overflow",
            "invalid_clock",
        ):
            reason = "closed"
        if self.enabled:
            if self._bridge is not None and not self._terminal:
                self._bridge.close()
            if self.enabled:
                self.reason = reason
        self._terminal = True
        self._paused = False
        self._last_heartbeat_at = None
        self._bridge = None
        self._seen.clear()

    async def consume(self, records: AsyncIterable[OwnedStreamRecord]) -> str:
        """Pull a dedicated metadata iterable with no queue/tasks of our own.

        The host owns tee capacity and must close this observer on overflow even
        if no next record arrives. Disabled observers never read the iterable.
        We do not aclose the host's iterable; caller cancellation still propagates.
        """
        if not self.enabled or self._consuming:
            return self.reason or "already_consuming"
        self._consuming = True
        try:
            async for record in records:
                self.deliver(record)
                if not self.enabled:
                    break
        except Exception:  # noqa: BLE001 - silently detach on feed failure
            self.close(reason="stream_error")
        finally:
            self.close()
            self._consuming = False
        return self.reason
