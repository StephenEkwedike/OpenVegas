"""Session-local payer admission; no transport, credentials, timers or native guesses.

All operations are synchronous and must run on one owning event-loop thread.
The controller must cover managed billable traffic and retain uncertain
dispatches. Legacy mode needs a task-retirement proof; explicit request mode
needs held route admission instead. HTTP idle does not prove task retirement.

Only adapters with verified logical-operation identity may supply operation_id.
Local dispatch IDs are not native identities, body hashes or session/prompt IDs.
Old-generation retries are refused, never rebound to the current payer. With no
identity, a known retry is refused after the first payer change: its originating
generation cannot be established. Callers must not disguise a known retry as new.

The bounded ledger retains even settled attempts so delayed retries cannot lose
their payer binding. Capacity exhaustion refuses new dispatches, not finish or
abort. Ambiguity has no automatic expiry or resolution in this module; a later
successful attempt cannot reconcile an earlier uncertain supplier outcome.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

Mode = Literal["host", "v"]
Outcome = Literal["completed", "rejected", "ambiguous"]


class RouterStateError(RuntimeError):
    """A fail-closed refusal with a stable code and no request/credential details."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Lease:
    """One physical dispatch, bound to a payer before the controller sends it."""

    mode: Mode
    generation: int
    operation_id: str
    attempt_id: str


@dataclass(frozen=True, slots=True)
class SwitchTicket:
    """One-use, in-process prepare handle; copies are not commit authority."""

    source_mode: Mode
    target: Mode
    generation: int
    ticket_id: str
    noop: bool


@dataclass(frozen=True, slots=True)
class _Operation:
    mode: Mode
    generation: int
    operation_id: str
    method: str
    path: str
    verified: bool


@dataclass(slots=True)
class _Attempt:
    lease: Lease
    operation: _Operation
    outcome: Outcome | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    ticket: SwitchTicket
    source_mode: Mode
    target: Mode
    generation: int
    ticket_id: str
    noop: bool


def _refuse(code: str, message: str):
    raise RouterStateError(code, message)


def _mode(value: str) -> Mode:
    if type(value) is not str or value not in ("host", "v"):
        _refuse("invalid_mode", "Payer must be host or v.")
    return value


class RouterState:
    """Fail-closed request ledger and compare-and-commit payer state.

    prepare(target) holds new admission while admitted attempts drain. commit
    requires task_boundary_verified=True from the trusted controller *for this
    ticket*. Failed commits retain the hold; abort releases it without clearing
    unresolved evidence. Same-payer tickets do not hold admission or increment
    generation and can commit without a task-boundary assertion.

    Outcomes describe each physical transport attempt, not financial settlement.
    An ambiguous finished attempt retains its original payer and evidence. In
    request-scoped mode it does not prevent routing a new request to another
    payer; known retries cannot cross that generation boundary. Legacy task
    mode still requires all outcomes to be conclusive before switching.
    """

    def __init__(self, *, mode: Mode = "host", max_entries: int = 4096,
                 request_boundary: bool = False):
        self._mode = _mode(mode)
        if type(request_boundary) is not bool:
            _refuse("invalid_boundary", "Boundary mode must be explicit.")
        self.request_boundary = request_boundary
        if type(max_entries) is not int or max_entries < 1:
            _refuse("invalid_capacity", "Ledger capacity must be a positive integer.")
        self._max_entries = max_entries
        self._generation = 0
        self._prepared: _Prepared | None = None
        self._operations: dict[str, _Operation] = {}
        self._attempts: dict[str, _Attempt] = {}

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def pending(self) -> SwitchTicket | None:
        return self._prepared.ticket if self._prepared else None

    def begin(
        self, method: str, path: str, operation_id: str | None = None, retry: bool = False
    ) -> Lease:
        """Reserve before dispatch; supplied IDs require adapter-verified identity.

        Explicit retries need a known, inactive operation at the same endpoint
        in the current generation. Each accepted retry gets a fresh attempt ID,
        even if its predecessor was rejected or ambiguous. No outcome is erased.
        """
        if self._prepared and not self._prepared.noop:
            _refuse("admission_held", "Payer switch is pending; dispatch is held.")
        if (type(method) is not str or not re.fullmatch(r"[A-Z]{1,32}", method)
                or type(path) is not str or not path.startswith("/")
                or len(path) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or type(retry) is not bool):
            _refuse("invalid_request", "A bounded method/path and explicit retry flag are required.")
        if operation_id is not None and (
            type(operation_id) is not str or not 1 <= len(operation_id) <= 256
            or operation_id.startswith("local:")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in operation_id)
        ):
            _refuse("invalid_operation", "Only a verified, bounded native operation ID is accepted.")

        operation = self._operations.get(operation_id) if operation_id is not None else None
        if retry and operation_id is not None:
            if operation is None or not operation.verified:
                _refuse("unknown_retry", "Retry has no retained verified operation binding.")
            if (operation.generation, operation.mode) != (self._generation, self._mode):
                _refuse("retired_retry", "Retry belongs to a retired payer generation.")
            if (operation.method, operation.path) != (method, path):
                _refuse("retry_endpoint_mismatch", "Retry does not match its original endpoint.")
            if any(a.operation is operation and a.outcome is None
                   for a in self._attempts.values()):
                _refuse("operation_active", "The logical operation already has an active attempt.")
        elif retry and self._generation != 0:
            _refuse("unbound_retry", "An anonymous retry cannot establish its original payer.")
        elif operation is not None:
            _refuse("duplicate_operation", "Known operation requires an explicit retry.")

        if len(self._attempts) >= self._max_entries:
            _refuse("ledger_full", "Request evidence capacity reached; no dispatch admitted.")
        if operation is None:
            local_id = "local:" + uuid4().hex if operation_id is None else operation_id
            operation = _Operation(self._mode, self._generation, local_id, method, path,
                                   operation_id is not None)
        attempt_id = uuid4().hex
        # Even an unexpected identifier collision must not overwrite evidence.
        if attempt_id in self._attempts or (
            operation_id is None and operation.operation_id in self._operations
        ):
            _refuse("identity_collision", "Dispatch identity collision; no request admitted.")
        lease = Lease(operation.mode, operation.generation, operation.operation_id, attempt_id)
        self._operations[operation.operation_id] = operation
        self._attempts[attempt_id] = _Attempt(lease, operation)
        return lease

    def finish(self, lease: Lease, outcome: Outcome) -> None:
        """Settle the original issued lease exactly once; never reconcile by guess."""
        if type(lease) is not Lease or type(lease.attempt_id) is not str:
            _refuse("invalid_lease", "An original issued lease is required.")
        attempt = self._attempts.get(lease.attempt_id)
        if (attempt is None or attempt.lease is not lease
                or (lease.mode, lease.generation, lease.operation_id) != (
                    attempt.operation.mode, attempt.operation.generation,
                    attempt.operation.operation_id)):
            _refuse("invalid_lease", "An original unmodified lease is required.")
        if attempt.outcome is not None:
            _refuse("already_finished", "Attempt already has a retained terminal outcome.")
        if type(outcome) is not str or outcome not in ("completed", "rejected", "ambiguous"):
            _refuse("invalid_outcome", "Unknown attempt outcome; lease remains active.")
        attempt.outcome = outcome

    def prepare(self, target: Mode) -> SwitchTicket:
        """Claim the sole switch slot; active work may drain but new work is held."""
        target = _mode(target)
        if self._prepared is not None:
            _refuse("switch_pending", "Another prepare ticket already owns the switch slot.")
        ticket = SwitchTicket(self._mode, target, self._generation, uuid4().hex,
                              target == self._mode)
        self._prepared = _Prepared(ticket, ticket.source_mode, ticket.target, ticket.generation,
                                   ticket.ticket_id, ticket.noop)
        return ticket

    def _check_ticket(self, ticket: SwitchTicket) -> _Prepared:
        prepared = self._prepared
        if (type(ticket) is not SwitchTicket or prepared is None
                or prepared.ticket is not ticket
                or (ticket.source_mode, ticket.target, ticket.generation, ticket.ticket_id,
                    ticket.noop) != (prepared.source_mode, prepared.target, prepared.generation,
                                     prepared.ticket_id, prepared.noop)
                or (self._mode, self._generation) != (prepared.source_mode, prepared.generation)):
            _refuse("invalid_ticket", "An original, current prepare ticket is required.")
        return prepared

    def commit(self, ticket: SwitchTicket, *, task_boundary_verified: bool = False,
               request_boundary_verified: bool = False) -> Mode:
        """Commit only at a controller-attested boundary, never at inferred idle."""
        prepared = self._check_ticket(ticket)
        if not prepared.noop:
            if any(a.outcome is None for a in self._attempts.values()):
                _refuse("requests_active", "Admitted requests have not conclusively settled.")
            if not self.request_boundary and any(
                    a.outcome == "ambiguous" for a in self._attempts.values()):
                _refuse("outcome_ambiguous", "Unreconciled supplier outcomes block payer changes.")
            if self.request_boundary and request_boundary_verified is not True:
                _refuse("request_boundary_required", "Controller must verify held request admission.")
            if not self.request_boundary and task_boundary_verified is not True:
                _refuse("task_boundary_required", "Controller must verify this task boundary.")
            self._mode = prepared.target
            self._generation += 1
        self._prepared = None
        return self._mode

    def abort(self, ticket: SwitchTicket) -> None:
        """Release this ticket's hold; keep all active and ambiguous evidence."""
        self._check_ticket(ticket)
        self._prepared = None

    def status(self) -> dict:
        """Detached, credential-free snapshot; counts never attest task completion."""
        prepared = self._prepared
        return {
            "mode": self._mode,
            "generation": self._generation,
            "boundary_scope": "requests" if self.request_boundary else "tasks",
            "pending": prepared.target if prepared else None,
            "pending_noop": prepared.noop if prepared else False,
            "active_requests": sum(a.outcome is None for a in self._attempts.values()),
            "ambiguous_outcomes": sum(a.outcome == "ambiguous" for a in self._attempts.values()),
            "attempts": len(self._attempts),
            "operations": len(self._operations),
            "max_entries": self._max_entries,
            "admission_open": not (prepared and not prepared.noop)
            and len(self._attempts) < self._max_entries,
        }
