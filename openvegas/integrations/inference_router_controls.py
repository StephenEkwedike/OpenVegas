"""Session-local billing controls, independent of native UI and HTTP transport.

Callbacks are trust boundaries, not provider guarantees. In particular, a task
boundary must account for source-owned work; an empty HTTP request set or wallet
revocation is not that proof. No callback here dispatches or replays inference.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .broker import GrantNotCreated
from .terminal_wallet import monitor


@dataclass(frozen=True)
class RouterControlPort:
    """Adapt the synchronous router state without exposing grants in status.

    prepare(target) atomically holds admission and returns an owner ticket.
    commit(ticket, grant) atomically installs prepared routing and releases the
    hold; it must not mutate on failure. abort(ticket) releases only that hold.
    status() supplies the committed payer as ``billing`` ('host' or 'v').
    The parent can adapt its RouterState signatures with four small callables.
    """

    prepare: Callable[[str], Any]
    commit: Callable[[Any, Any], None]
    abort: Callable[[Any], None]
    status: Callable[[], Mapping[str, Any]]

    @classmethod
    def from_state(cls, state):
        """Bind RouterState; only the coordinator supplies the boundary assertion."""
        return cls(state.prepare,
                   lambda ticket, _grant: state.commit(ticket, task_boundary_verified=True),
                   state.abort, lambda: {'billing': state.status()['mode']})


class WalletPreparationRejected(RuntimeError):
    """Adapter proved no grant was issued (for example a failed local precheck).

    Never use this for an unacknowledged POST, generic HTTP error or timeout.
    The exact server pre-creation rejection remains broker.GrantNotCreated.
    """


@dataclass(frozen=True)
class SwitchResult:
    billing: str
    changed: bool
    outcome: str


class WalletHUD:
    """Restart the existing monitor's authenticated lease, never relabel it.

    confirm_lease must independently verify the current relay owner/generation
    and payer. monitor's ready event also fires on failure and is not proof.
    No replacement starts until the previous monitor has fully stopped.
    """

    def __init__(self, env, *, control, status, status_changed, confirm_lease,
                 credentials=None, monitor_factory=monitor, timeout=3.5,
                 monitor_options=None):
        self.env = dict(env)
        self.control = control
        self.base_status = status
        self.status_changed = status_changed
        self.confirm_lease = confirm_lease
        self.credentials = credentials
        self.monitor_factory = monitor_factory
        self.timeout = _seconds(timeout)
        self.monitor_options = dict(monitor_options or {})
        reserved = {'ready', 'credentials', 'control', 'status', 'status_changed'}
        if reserved.intersection(self.monitor_options):
            raise ValueError('Monitor options cannot replace lease/control callbacks')
        self._task = None
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _payload(base, transition):
        result = {**base, **transition}
        # The relay derives this from its authenticated connection handshake.
        result.pop('billing', None)
        return result

    async def _stop(self):
        if self._task is None:
            return True
        if not self._task.done():
            self._task.cancel()
        done, _ = await asyncio.wait({self._task}, timeout=self.timeout)
        if not done:
            return False
        _consume(self._task)
        self._task = None
        return True

    async def restart(self, payer, *, status, status_changed):
        if payer not in ('host', 'v'):
            raise ValueError('Invalid billing mode')
        async with self._lock:
            if self._closed or not await self._stop():
                return False
            ready = asyncio.Event()
            self._task = asyncio.create_task(self.monitor_factory(
                self.env, payer, ready=ready, credentials=self.credentials,
                control=self.control,
                status=lambda: self._payload(self.base_status(), status()),
                status_changed=status_changed, **self.monitor_options))
            try:
                async with asyncio.timeout(self.timeout):
                    await ready.wait()
                    if self._task.done():
                        return False
                    # This verifier must not issue an inference/model request.
                    confirmed = await self.confirm_lease(payer)
                    return confirmed is True and not self._task.done()
            except Exception:  # noqa: BLE001 - Optional HUD cannot change routing.
                return False

    async def close(self):
        self._closed = True
        async with self._lock:
            return await self._stop()


def _seconds(value):
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError('Timeout must be finite and positive')
    return float(value)


def _consume(task):
    if not task.cancelled():
        task.exception()


class RouterBillingCoordinator:
    """Prepare, prove and commit between tasks; refuse uncertainty without replay.

    task_boundary(ticket) must return exactly True for a fresh source-task
    boundary while admission is held, including detached work and retry owners.
    prepare_wallet(ticket) returns a prepared opaque grant or raises the exact
    GrantNotCreated/WalletPreparationRejected rejection. Other failures are
    ambiguous; an adapter cannot turn an unknown POST into a local rejection.
    revoke_wallet(grant) must return exactly True for confirmed retirement of
    the grant, not retirement of tasks. Callbacks must be cancellation-aware.

    A timed-out/cancelled mutating callback is retained until it resolves; late
    results never commit. A late prepared grant is best-effort revoked while
    admission stays held. An operator/parent must reconcile unresolved outcomes;
    this coordinator never guesses that it is safe to release them.
    """

    def __init__(self, state: RouterControlPort, *, task_boundary,
                 prepare_wallet, revoke_wallet, hud=None, wallet_grant=None,
                 timeout=3.5, status_changed=None):
        self.state = state
        self.task_boundary = task_boundary
        self.prepare_wallet = prepare_wallet
        self.revoke_wallet = revoke_wallet
        self.hud = hud
        self.wallet_grant = wallet_grant
        self.timeout = _seconds(timeout)
        self.status_changed = (status_changed if status_changed is not None else
                               getattr(hud, 'status_changed', None) or asyncio.Event())
        self._lock = asyncio.Lock()
        self._pending = set()
        self._hud_task = None
        self._hud_generation = 0
        self._phase = None
        self._outcome = 'idle'
        self._held = None
        self._reserved = None
        self._closed = False

    def status(self):
        payer = self.state.status()['billing']
        if payer not in ('host', 'v'):
            raise ValueError('Invalid committed payer')
        return {'billing': payer, 'switch_status': self._phase,
                'switch_outcome': self._outcome,
                'router_admission_held': self._held is not None}

    def _set(self, phase, outcome):
        self._phase, self._outcome = phase, outcome
        self.status_changed.set()

    def _retain(self, task):
        self._pending.add(task)
        def done(completed):
            self._pending.discard(completed)
            _consume(completed)
        task.add_done_callback(done)

    async def _await(self, operation: Awaitable, deadline, *, late_grant=False):
        task = asyncio.ensure_future(operation)
        self._retain(task)
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                raise TimeoutError
            return task.result()
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            if late_grant:
                task.add_done_callback(self._late_grant)
            raise

    def _late_grant(self, task):
        if task.cancelled() or task.exception() is not None:
            return
        grant = task.result()
        if grant is not None:
            self._retain(asyncio.create_task(self._cleanup_grant(grant)))

    async def _cleanup_grant(self, grant):
        try:
            deadline = asyncio.get_running_loop().time() + self.timeout
            return await self._await(self.revoke_wallet(grant), deadline) is True
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - Keep the hold on uncertainty.
            return False

    def _abort(self, ticket, outcome):
        try:
            self.state.abort(ticket)
        except Exception:  # noqa: BLE001 - State refusal never authorizes fallback.
            self._set('blocked', 'abort_unconfirmed')
            return False
        self._held = None
        self._set(None, outcome)
        return True

    def refresh_hud(self):
        """Start/retry the committed payer's display, separate from billing work."""
        if self.hud is None or self._closed:
            return
        if self._phase is None:
            self._set('refreshing', 'hud_refreshing')
        self._hud_generation += 1
        if self._hud_task is None or self._hud_task.done():
            self._hud_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        while not self._closed:
            generation = self._hud_generation
            payer = self.status()['billing']
            try:
                confirmed = await self.hud.restart(
                    payer, status=self.status, status_changed=self.status_changed)
            except Exception:  # noqa: BLE001 - Display failure cannot undo payer commit.
                confirmed = False
            if generation != self._hud_generation:
                continue
            if self._phase == 'refreshing':
                self._set(None if confirmed is True else 'refreshing',
                          'committed' if confirmed is True else 'hud_unconfirmed')
            return

    async def wait_hud(self):
        if self._hud_task is not None:
            await asyncio.shield(self._hud_task)

    def reserve(self, target):
        """Hold request admission before acknowledging an exact local command."""
        if target not in ('host', 'v'):
            raise ValueError('Invalid billing mode')
        if self._closed or self._lock.locked() or self._held is not None:
            raise ValueError('Billing control is already held')
        if target == self.status()['billing']:
            return None
        ticket = self.state.prepare(target)
        self._held, self._reserved = ticket, (target, ticket)
        self._set('pending', 'command_reserved')
        return ticket

    def cancel_reservation(self):
        """Abort only an unconsumed command; never undo a wallet mutation."""
        if self._reserved is None or self._lock.locked():
            return False
        (_, ticket), self._reserved = self._reserved, None
        return self._abort(ticket, 'cancelled')

    async def switch(self, target):
        if target not in ('host', 'v'):
            raise ValueError('Invalid billing mode')
        # Concurrent local commands must not enqueue a surprise later switch.
        if self._lock.locked():
            return SwitchResult(self.status()['billing'], False, 'busy')
        async with self._lock:
            source = self.status()['billing']
            if self._closed or self._held is not None and self._reserved is None:
                return SwitchResult(source, False, 'held')
            if source == target:
                return SwitchResult(source, False, 'unchanged')
            deadline = asyncio.get_running_loop().time() + self.timeout
            try:
                if self._reserved is not None:
                    if self._reserved[0] != target:
                        return SwitchResult(source, False, 'held')
                    (_, ticket), self._reserved = self._reserved, None
                else:
                    ticket = self.state.prepare(target)
            except Exception:  # noqa: BLE001 - State preparation must be atomic on failure.
                return SwitchResult(source, False, 'prepare_refused')
            self._held = ticket
            self._set('pending', 'checking_boundary')
            mutation = False
            grant = None
            try:
                if await self._await(self.task_boundary(ticket), deadline) is not True:
                    self._abort(ticket, 'task_boundary_refused')
                    return SwitchResult(source, False, self._outcome)
                mutation = True
                if target == 'v':
                    grant = await self._await(self.prepare_wallet(ticket), deadline, late_grant=True)
                    if grant is None:
                        raise ValueError('Wallet preparation returned no confirmed grant')
                else:
                    if self.wallet_grant is None:
                        raise ValueError('Source grant is unknown')
                    if await self._await(self.revoke_wallet(self.wallet_grant), deadline) is not True:
                        raise ValueError('Revocation is unconfirmed')
                if await self._await(self.task_boundary(ticket), deadline) is not True:
                    raise ValueError('Fresh task boundary is unconfirmed')
                # Clock check and commit are synchronous: no late async commit.
                if self._closed or asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                self.state.commit(ticket, grant)
            except (GrantNotCreated, WalletPreparationRejected):
                if target == 'v' and grant is None:
                    self._abort(ticket, 'wallet_not_created')
                else:
                    self._set('blocked', 'wallet_outcome_unconfirmed')
                    if grant is not None:
                        self._retain(asyncio.create_task(self._cleanup_grant(grant)))
                return SwitchResult(source, False, self._outcome)
            except (Exception, asyncio.CancelledError) as exc:
                if mutation:
                    self._set('blocked', 'wallet_outcome_unconfirmed')
                    if grant is not None:
                        # Even confirmed cleanup does not establish task retirement.
                        self._retain(asyncio.create_task(self._cleanup_grant(grant)))
                else:
                    self._abort(ticket, 'cancelled' if isinstance(exc, asyncio.CancelledError)
                                else 'boundary_unconfirmed')
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return SwitchResult(source, False, self._outcome)
            self.wallet_grant = grant
            self._held = None
            self._set('refreshing' if self.hud is not None else None, 'committed')
            self.refresh_hud()
            return SwitchResult(target, True, 'committed')

    async def close(self):
        """Stop display and disallow later commits, not certify remote cleanup.

        False means a switch/callback/hold or display owner is unresolved. The
        parent must retain those attempt records even after closing its HTTP
        listener. Active-wallet revocation remains the parent's responsibility.
        """
        self._closed = True
        if self._hud_task is not None:
            self._hud_task.cancel()
            await asyncio.gather(self._hud_task, return_exceptions=True)
        display_closed = True
        if self.hud is not None:
            display_closed = await self.hud.close()
        return (display_closed is not False and not self._pending
                and not self._lock.locked() and self._held is None)

    async def settle_callbacks(self):
        """Join retained mutation owners before their broker is closed.

        No new commit or retry is authorized. Late grants can schedule their
        one existing cleanup callback, which must also finish before returning.
        A held/uncertain boundary remains held even after these tasks finish.
        """
        self._closed = True
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
            await asyncio.sleep(0)
