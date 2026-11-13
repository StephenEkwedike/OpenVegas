"""Private composition of the stable transport, local controls and wallet HUD.

No ordinary launcher selects this path yet. Request-scoped sessions hold the
fixed endpoint, drain admitted sends and retain unresolved outcomes. This is
not a proof of native task retirement. Legacy task-scoped sessions still need
their independent verifier. Existing generation implementations are retained.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import click

from .credentials import IntegrationError
from .inference_router import DarwinTCPPeerAuth, InferenceRouter, serve
from .inference_router_adapter import launch_overlay
from .inference_router_controls import RouterBillingCoordinator, RouterControlPort
from .inference_router_shutdown_receipt import save_unresolved_dispatches
from .inference_router_state import RouterState
from .use import parse_command


@dataclass(frozen=True, repr=False)
class WalletAccess:
    token: str
    expires_at: float
    validate_catalog: object = field(default=None, repr=False)
    grant: object = field(default=None, repr=False)


class RouterSession:
    def __init__(self, adapter, *, task_boundary=None, prepare_wallet, revoke_wallet,
                 hud=None, client=None, timeout=3.5, commit_guard=None, status_changed=None,
                 local_auth='path', request_boundary=False):
        self.adapter = adapter
        self.state = RouterState(request_boundary=request_boundary)
        if not request_boundary and task_boundary is None:
            raise IntegrationError('Legacy task-scoped routing needs a task verifier.')
        self._wallet = None
        self._url = None
        self._closed = False
        self._revoke = revoke_wallet
        self._timeout = timeout
        if local_auth == 'peer' and adapter.host != 'codex':
            raise IntegrationError('Native peer authentication is only verified for Codex.')
        if local_auth == 'header' and adapter.host != 'claude':
            raise IntegrationError('Native header authentication is only verified for Claude.')
        self._peer = DarwinTCPPeerAuth() if local_auth == 'peer' else None

        def commit(ticket, grant):
            if ticket.target == 'v' and (not isinstance(grant, WalletAccess)
                    or not re.fullmatch(r'ov_native_[A-Za-z0-9_-]{16,128}', grant.token)
                    or not time.time() < grant.expires_at < float('inf')):
                raise IntegrationError('Prepared wallet access is invalid or expired.')
            if commit_guard is not None:
                commit_guard(ticket)
            # No await between routing commit and credential installation.
            self.state.commit(ticket, task_boundary_verified=not request_boundary,
                              request_boundary_verified=request_boundary)
            self._wallet = grant if ticket.target == 'v' else None

        port = RouterControlPort(self.state.prepare, commit, self.state.abort,
                                 lambda: {'billing': self.state.mode})
        async def boundary(ticket):
            if request_boundary:
                # The fixed URL owns admission even while local tools continue.
                # No process/task-retirement claim is made by this drain.
                while self.state.status()['active_requests']:
                    if self.state.pending is not ticket:
                        return False
                    await asyncio.sleep(.005)
            if (self.state.status()['active_requests']
                    or (not request_boundary and self.state.status()['ambiguous_outcomes'])
                    or (task_boundary is not None and await task_boundary(ticket) is not True)):
                return False
            await self.router.close_idle_connections()
            return True

        self.controls = RouterBillingCoordinator(port, task_boundary=boundary,
            prepare_wallet=prepare_wallet, revoke_wallet=revoke_wallet, hud=hud, timeout=timeout,
            status_changed=status_changed)
        self.router = InferenceRouter(self.state, adapter, wallet_token=self._token, client=client,
                                      catalog_validator=self._catalog, auth_mode=local_auth,
                                      peer_auth=self._peer, admission_wait=timeout if request_boundary else 0)

    def __repr__(self):
        return '<RouterSession private-native-route>'

    def _token(self):
        if self.state.mode == 'host':
            return None
        if self._wallet is None or time.time() >= self._wallet.expires_at:
            raise IntegrationError('Wallet access expired; no automatic switch to host.')
        return self._wallet.token

    def _catalog(self, value):
        self._token()
        if self._wallet is None or self._wallet.validate_catalog is None:
            raise IntegrationError('Reviewed native model catalog is unavailable.')
        return self._wallet.validate_catalog(value)

    async def command(self, text):
        """Called by the existing authenticated local-command owner, not a model."""
        command = parse_command(text)
        if command is None or command[0] not in ('v', 'host'):
            raise IntegrationError('Not a local billing command.')
        return await self.controls.switch(command[0])

    def status(self):
        return {**self.controls.status(), 'generation': self.state.generation}

    def bind_native(self, process):
        if self._peer is None or self._url is None or self._closed:
            raise IntegrationError('No open native peer-authenticated router to bind.')
        self._peer.bind(process)

    @asynccontextmanager
    async def launch(self, *, version, env, args, effective_policy_verified=False):
        """Yield process-only inputs once. Caller owns the unchanged native child."""
        if effective_policy_verified is not True or self._url is not None or self._closed:
            raise IntegrationError('Native launch policy must be verified before using the router.')
        try:
            async with serve(self.router) as url:
                updated, argv = launch_overlay(self.adapter.host, version, url, env, args,
                    local_capability=self.router.capability if self.router.auth_mode == 'header' else None,
                    peer_authenticated=self.router.auth_mode == 'peer')
                self._url = url
                yield updated, argv
        finally:
            self._url = None
            self._closed = True
            settled = await self.controls.close()
            if not settled:
                # An owned switch may already be revoking this exact grant.
                # Listener shutdown must not start a duplicate mutation.
                raise IntegrationError('Router closed with unresolved work; retain the session for reconciliation.')
            if self._wallet is not None:
                try:
                    async with asyncio.timeout(self._timeout):
                        confirmed = await self._revoke(self._wallet)
                    if confirmed is not True:
                        raise IntegrationError('Wallet closure is unconfirmed; no automatic retry.')
                    self._wallet = None
                except (TimeoutError, OSError):
                    raise IntegrationError('Wallet closure is unconfirmed; no automatic retry.') from None
            status = self.state.status()
            if status['active_requests'] or (status['boundary_scope'] == 'requests'
                                             and status['pending'] is not None):
                raise IntegrationError('Router closed with unresolved work; retain the session for reconciliation.')
            if status['ambiguous_outcomes']:
                if status['boundary_scope'] != 'requests':
                    raise IntegrationError('Router closed with unresolved work; retain the session for reconciliation.')
                receipt = save_unresolved_dispatches(self.state, self.adapter.host)
                click.echo('WARN: Router closed with unresolved dispatch outcomes. '
                           'No outcomes were reconciled; server financial holds are unchanged. '
                           f'Receipt: {json.dumps(str(receipt), ensure_ascii=True)}', err=True)


def claude_wallet_callbacks(broker, *, active_model):
    """Reuse existing reviewed quote/grant/revocation, not another billing API.

    active_model must return the exact observed native model at the held task
    boundary. This helper itself makes no provider request or profile change.
    """
    from .claude_live_switch import WalletGrant
    from .inference_router_controls import WalletPreparationRejected

    retained = []

    async def prepare(ticket):
        grant = WalletGrant(broker)
        retained.append(grant)
        try:
            route = await grant.open(active_model())
        except IntegrationError:
            if grant.released:
                raise WalletPreparationRejected('Wallet preparation refused before grant creation.') from None
            raise
        return WalletAccess(route.token, route.expires_at, grant=grant)

    async def revoke(access):
        await access.grant.close()
        return access.grant.released

    # No credentials are serialized. The caller can reconcile exact retained
    # grant objects even when creation/status verification fails or is cancelled.
    prepare.retained_grants = retained
    return prepare, revoke
