"""Private managed Codex integration for the fixed inference endpoint.

The native app-server and foreground TUI stay alive. Existing generation-based
launches remain the default until the candidate's native release gates pass.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

from . import codex_handoff_policy as policy
from .broker import Broker
from .codex_billing_route import _OWNED_SERVERS, _handoff_server_argv, _private_server, host_server
from .codex_binary import resolve_native_codex
from .codex_live_switch import RuntimeOwner, require
from .codex_live_transport import TransportUncertain
from .credentials import IntegrationError
from .inference_router_adapter import ProviderAdapter
from .inference_router_request_boundary import (
    CodexRequestBoundary,
    RequestStableTransport,
    observe_selection,
    route_config,
)
from .inference_router_session import RouterSession
from .inference_router_wallet import codex_wallet_callbacks


async def original_upstream(executable, env, cwd):
    """Read auth *kind*, never its secret, from an owned unmodified native server."""
    async with host_server(executable, env, cwd=cwd) as endpoint:
        await route_config(endpoint, cwd)
        async with policy._reader(endpoint) as call:
            result = await call('account/read', {'refreshToken': False})
            account = result.get('account')
            kind = account.get('type') if isinstance(account, dict) else None
            require(kind in {'chatgpt', 'apiKey'},
                    'Native authentication type is unavailable; ordinary host launch is unchanged.')
    return ('https://chatgpt.com/backend-api/codex' if kind == 'chatgpt'
            else 'https://api.openai.com/v1')


class ManagedCodexRouter(RuntimeOwner):
    """Reuse authenticated commands/leases and native input fencing, not replay."""

    def __init__(self, supervisor, env):
        super().__init__(supervisor, env)
        self.session = None
        self.boundary = None
        self._model = None
        self._router_url = None
        self._display_task = None
        self._display_revision = 0
        self._prepared = None
        self.switch = SimpleNamespace(uncertain=False, phase='idle')

    def finish_switch_window(self, *, committed=False):
        super().finish_switch_window(committed=committed and not self.switch.uncertain)
        if self.switch.uncertain:
            self.switch_status = 'blocked'
            self.s.wallet_status_changed.set()

    async def _abort_input(self):
        self.switch.uncertain = True
        try:
            await self.transport.abort()
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - Keep the failed fence without raw diagnostics.
            # Failure to close is not permission to reopen or clear the hold.
            return False
        return True

    def _assert_commit(self, ticket):
        require(self.boundary is not None, 'Native task boundary is unavailable.')
        self.boundary.assert_current(ticket)
        self.require_switch_window()

    async def _boundary(self, ticket):
        return self.boundary is not None and await self.boundary(ticket)

    async def observe_source(self, *, preview=False):
        selected = await observe_selection(self.source_endpoint, self.s.identifier,
                                           self.cwd, self._router_url)
        return selected.model

    async def run(self, argv):
        s = self.s
        self.executable = await asyncio.to_thread(
            resolve_native_codex, s.executable, s.launch_spec.version)
        broker = Broker()
        try:
            upstream = await original_upstream(self.executable, self.env, self.cwd)
            prepare, revoke = codex_wallet_callbacks(broker, active_model=lambda: self._model,
                                                     version=s.launch_spec.version)
            self._prepared = prepare
            self.session = RouterSession(ProviderAdapter('codex', upstream,
                broker.backend + '/integrations/native'), task_boundary=self._boundary,
                commit_guard=self._assert_commit, prepare_wallet=prepare, revoke_wallet=revoke,
                status_changed=s.wallet_status_changed, local_auth='peer', request_boundary=True)
            async with self.session.launch(version=s.launch_spec.version, env=self.env, args=[],
                                           effective_policy_verified=True) as (server_env, overlay):
                self._router_url = self.session._url
                async with AsyncExitStack() as stack:
                    self.source_endpoint = await stack.enter_async_context(_private_server(
                        lambda endpoint: _handoff_server_argv([self.executable, *overlay,
                            '--no-daemon', 'app-server', '--listen', endpoint]), server_env, self.cwd))
                    self.session.bind_native(_OWNED_SERVERS[self.source_endpoint])
                    await route_config(self.source_endpoint, self.cwd, self._router_url)
                    async with RequestStableTransport(self.source_endpoint, on_notice=s.emit) as transport:
                        self.transport = transport
                        transport.require_cancel_ack = True
                        transport.pending_changed = s.wallet_status_changed.set
                        s.codex_endpoint, s.live_owner = self.source_endpoint, self
                        try:
                            return await s._terminal_child(
                                [self.executable, '--remote', transport.endpoint, *argv], self.env)
                        finally:
                            self.ending = True
                            if self.task is not None:
                                await asyncio.shield(self.task)
                            elif self.intent is not None:
                                self.intent.release()
                            if self._display_task is not None:
                                self._display_task.cancel()
                                await asyncio.gather(self._display_task, return_exceptions=True)
                            if hasattr(self, 'replace_wallet'):
                                await self.replace_wallet(None)
                            # No native server retirement based on HTTP idle.
                            await policy.wait_until_idle(self.source_endpoint, s.identifier)
        finally:
            s.live_owner = s.codex_endpoint = None
            self.source_endpoint = None
            await self._close_wallet(broker)

    async def _close_wallet(self, broker):
        from .codex_billing_route import _settle

        cancelled = False
        try:
            if self.session is not None:
                _, cancelled = await _settle(asyncio.create_task(self.session.controls.settle_callbacks()))
            if self._prepared is not None:
                for grant in self._prepared.retained_grants:
                    if grant.boundary is not None and grant.boundary.state == 'issued':
                        # A known grant can fail validation before session._wallet
                        # is installed. Its mutation owner still needs closure.
                        _, interrupted = await _settle(asyncio.create_task(grant.close()))
                        cancelled |= interrupted
        finally:
            _, interrupted = await _settle(asyncio.create_task(broker.close()))
            cancelled |= interrupted
        if cancelled:
            raise asyncio.CancelledError

    async def _perform(self, ticket):
        generation = self.transport.generation
        committed = False
        refusal = ('Billing is unchanged. Finish or cancel native work, then retry the switch. '
                   'Waiting questions were not sent.')
        try:
            self.switch.phase = 'native_input_fence'
            self.check_owner(ticket, generation)
            await self.transport.freeze(generation, intent=ticket.guard.token)
            self.boundary = CodexRequestBoundary(self.transport, self.source_endpoint,
                ticket.identifier, expected_url=self._router_url, expected_model=ticket.host_model)
            self._model = ticket.host_model
            self.require_switch_window()
            from time import monotonic
            self.session.controls.timeout = max(.001, self.switch_deadline - monotonic())
            self.switch.phase = 'router_payer_boundary'
            result = await self.session.command('openvegas use ' + ticket.target)
            committed = result.changed
            self.s.mode = result.billing
            if getattr(result, 'outcome', None) == 'wallet_not_created':
                refusal = ('Wallet access or pricing is unavailable; billing is unchanged. '
                           'No wallet grant was created and waiting questions were not sent.')
            elif (getattr(result, 'outcome', None) == 'task_boundary_refused'
                  and getattr(getattr(self.transport, 'work', None), 'reason', None)):
                refusal = ('This session\'s tool or background-work ownership cannot be verified. '
                           'Billing is unchanged; continue with the current payer. '
                           'Waiting questions were not sent.')
            if self.session.state.pending is not None:
                self.switch.uncertain = True
                self.switch_status = 'blocked'
                await self._abort_input()
                raise IntegrationError('Billing outcome needs reconciliation; input remains held.')
            if committed:
                self.s.intent_revision += 1
                if result.billing == 'v':
                    self.s.restore_model = ticket.host_model
                self.s.transition_state = 'MODE_COMMITTED'
                # The commit watchdog must never cancel a confirmed payer while
                # the native transport finishes its separate input-release send.
                self.finish_switch_window(committed=True)
                self.switch.phase = 'releasing_native_input'
                await self.transport.resume_same_backend(generation)
                self.switch.phase = 'committed'
                self.finish_switch_window(committed=True)
                self.s.emit('Native payer now ' + ('$V' if result.billing == 'v' else 'host')
                            + '; same chat, no prompt submitted.')
                if not self.ending and self.s.child.returncode is None:
                    # Existing monitor opens a fresh payer-authenticated relay lease.
                    # Its readiness is display state, not the billing commit.
                    self.refresh_display()
            else:
                await self.transport.thaw(generation)
                raise IntegrationError('Switch refused at the native task boundary.')
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - Normalize native transport failures.
            self.s.local_error = True
            if committed:
                await self._abort_input()
                self.s.emit('Billing changed, but native input could not be reopened. '
                            'No question was replayed; exit and resume explicitly.')
                return
            if self.session.state.pending is not None or isinstance(exc, TransportUncertain) or self.transport.state in {'failed', 'closed'}:
                await self._abort_input()
            elif self.transport.state == 'frozen':
                try:
                    await self.transport.thaw(generation)
                except (Exception, asyncio.CancelledError):  # noqa: BLE001 - Never clear an unconfirmed fence.
                    await self._abort_input()
            self.s.emit('Billing requires reconciliation; input remains held.' if self.switch.uncertain
                        else refusal)
        finally:
            self.boundary = None
            self.finish_switch_window(committed=committed)

    def refresh_display(self):
        self._display_revision += 1
        if self._display_task is None or self._display_task.done():
            self._display_task = asyncio.create_task(self._refresh_display())

    async def _refresh_display(self):
        while not self.ending:
            revision, payer = self._display_revision, self.s.mode
            if self.switch_status is None:
                self.switch_status = 'refreshing'
                self.s.wallet_status_changed.set()
            try:
                await self.replace_wallet(payer)
            except Exception:  # noqa: BLE001 - Optional display never undoes a payer commit.
                self.s.local_error = True
                return
            if revision != self._display_revision:
                continue
            if self.switch_status == 'refreshing':
                self.switch_status = None
                self.s.wallet_status_changed.set()
            return
