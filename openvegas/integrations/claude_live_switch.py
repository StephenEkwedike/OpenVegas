"""Private Claude HTTP-hook coordinator; deliberately not a public launcher.

No host credentials, environment changes, transcript replay or replacement agent
loop. A caller must establish native hook coverage before constructing this.
Hook unload/managed-guard coverage is a separate, currently unverified gate.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from urllib.parse import urlsplit

from . import native
from .claude_http_policy import VERSION, RequestRoutes, WalletRoute, _endpoint, _require
from .credentials import IntegrationError


class WalletGrant:
    """One-shot quote/issue/close. Unknown mutation outcomes never auto-retry."""

    def __init__(self, broker, *, wall_clock=time.time):
        self.broker, self.wall_clock = broker, wall_clock
        self.state, self.token = 'new', None
        self.route = None

    async def open(self, model):
        _require(self.state == 'new' and isinstance(model, str) and native._MODEL.fullmatch(model))
        endpoint = _endpoint(self.broker.backend + '/integrations/native/v1/messages')
        device = self.broker.credentials.load()
        _require(isinstance(device, str) and device.startswith('ov_device_'))
        self.state = 'checking'
        options = await self.broker.request('GET', '/native/options?host=claude', token=device)
        _require(isinstance(options, dict) and options.get('host') == 'claude'
                 and options.get('provider') == 'anthropic' and options.get('wallet_billing') is True
                 and options.get('wallet_limit_policy') == 'live_balance'
                 and native._decimal(options.get('balance_v'), allow_zero=True) > 0)
        rows = options.get('models')
        _require(isinstance(rows, list) and 1 <= len(rows) <= 128
                 and all(isinstance(row, dict) and isinstance(row.get('model'), str)
                         and native._MODEL.fullmatch(row['model'])
                         and type(row.get('max_tokens')) is int and 1 <= row['max_tokens'] <= 128000
                         for row in rows))
        ids = [row['model'] for row in rows]
        _require(len(set(ids)) == len(ids) and model in ids)
        chosen = rows[ids.index(model)]
        selection = {'host': 'claude', 'provider': 'anthropic', 'model': model, 'max_tokens': chosen['max_tokens']}
        quote = await self.broker.request('POST', '/native/quote', token=device, data=selection)
        _require(isinstance(quote, dict) and all(quote.get(k) == v for k, v in selection.items()))
        pricing = quote.get('pricing')
        fingerprint, rate, multiplier = native._pricing(pricing)
        _require(re.fullmatch(r'[a-f0-9]{64}', fingerprint)
                 and pricing.get('provider') == 'anthropic' and pricing.get('model') == model)
        for name in ('supplier_input_usd_per_million', 'supplier_output_usd_per_million'):
            native._decimal(pricing.get(name), allow_zero=True)
        if 'v_per_supplier_usd' in pricing:
            _require(native._decimal(pricing['v_per_supplier_usd']) == rate * multiplier)
        self.state = 'issue_unknown'
        # An expiry is conservatively measured BEFORE the request, not after a
        # potentially delayed response. This is admission, not a spending cap.
        issued = self.wall_clock()
        grant = await self.broker.request('POST', '/native/sessions', token=device, data={
            **selection, 'billing_mode': 'wallet', 'confirmed': True, 'pricing_fingerprint': fingerprint})
        if isinstance(grant, dict) and isinstance(grant.get('token'), str) and re.fullmatch(
                r'ov_native_[A-Za-z0-9_-]{16,128}', grant['token']):
            self.token, self.state = grant['token'], 'issued'
        _require(self.token is not None and grant.get('pricing') == pricing
                 and isinstance(grant.get('session_id'), str) and native._SAFE_ID.fullmatch(grant['session_id'])
                 and type(grant.get('expires_in')) is int and 0 < grant['expires_in'] <= 86400
                 and grant.get('billing_mode') == 'wallet'
                 and isinstance(grant.get('allowed_models'), list)
                 and model in grant['allowed_models'])
        route = WalletRoute(endpoint, self.token, tuple(grant['allowed_models']), issued + grant['expires_in'])
        route.validate(self.wall_clock())
        _require(set(route.models) <= set(ids))
        status = await self.broker.request('GET', '/native/session', token=self.token)
        _require(isinstance(status, dict) and status.get('active') is not False
                 and status.get('status') not in {'closed', 'expired', 'revoked', 'exhausted'}
                 and all(status.get(k) == v for k, v in {
                     'session_id': grant['session_id'], 'host': 'claude', 'model': model,
                     'billing_mode': 'wallet'}.items())
                 and status.get('allowed_models') == grant['allowed_models'])
        self.route = route
        return route

    async def close(self):
        if self.state == 'closed':
            return
        _require(self.state == 'issued' and self.token is not None)
        self.state = 'close_unknown'
        token, self.token = self.token, None
        result = await self.broker.request('DELETE', '/native/session', token=token)
        _require(isinstance(result, dict) and result.get('closed') is True)
        self.state, self.route = 'closed', None

    @property
    def released(self):
        return self.state in {'new', 'checking', 'closed'}


class Controller:
    """One serial authority, with immutable native-origin and session binding.

    Sends arriving during a switch wait behind the transition, never against
    the previous payer. A failed close freezes admission. Already accepted
    streams are not magically undone by a grant-close acknowledgement.
    """

    def __init__(self, broker, *, version, host_origin='https://api.anthropic.com', grant_factory=WalletGrant):
        _require(version == VERSION)
        origin = urlsplit(host_origin)
        _require(origin.scheme == 'https' and origin.hostname and not origin.username
                 and not origin.password and origin.path == '' and not origin.query and not origin.fragment
                 and host_origin == f'https://{origin.netloc}' and host_origin.isascii()
                 and not any(ord(c) <= 32 or c == '\\' for c in host_origin))
        self.host_origin = host_origin
        self.broker, self.grant_factory = broker, grant_factory
        self.routes = RequestRoutes(secrets.token_hex(16))
        self.grant = None
        self.session = None
        self._lock = asyncio.Lock()

    def bind(self, identifier):
        _require(isinstance(identifier, str) and re.fullmatch(
            r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', identifier))
        _require(self.session is None or self.session == identifier)
        self.session = identifier

    def _check_session(self, identifier):
        _require(self.session is not None and identifier == self.session)

    async def switch(self, identifier, target, model):
        async with self._lock:
            self._check_session(identifier)
            _require(target in {'host', 'v'})
            if target == self.routes.mode:
                # Still enforce uncertainty; an idempotent click is not a reset.
                _require(not self.routes._uncertain and self.routes._pending is None)
                return self.routes.mode
            ticket = self.routes.prepare(target)
            try:
                if target == 'v':
                    self.grant = self.grant_factory(self.broker)
                    route = await self.grant.open(model)
                    self.routes.commit(ticket, wallet=route)
                else:
                    await self.grant.close()
                    self.routes.commit(ticket, released=True)
            except BaseException as exc:
                # Even task cancellation may follow an accepted POST. Preserve
                # uncertainty; never guess that no grant was issued.
                if target == 'v' and self.grant is not None and self.grant.state == 'issued':
                    try:
                        from .codex_billing_route import _settle
                        await _settle(asyncio.create_task(self.grant.close()))
                    except BaseException:  # noqa: BLE001, S110 - preserve unknown-close state; never log a bearer
                        pass
                self.routes.abort(ticket, uncertain=self.grant is not None and not self.grant.released)
                if isinstance(exc, Exception):
                    raise IntegrationError(
                        'Claude billing switch could not be verified. No payer fallback was made.'
                    ) from None
                raise
            return self.routes.mode

    async def route(self, identifier, request):
        async with self._lock:
            self._check_session(identifier)
            _require(isinstance(request, dict) and isinstance(request.get('url'), str))
            url = request['url']
            _require(url.startswith(self.host_origin + '/'))
            init = request.get('init')
            _require(isinstance(init, dict) and isinstance(init.get('body'), str)
                     and init.get('socketPath') is None and init.get('auth') is None)
            headers = init.get('headers', {})
            _require(isinstance(headers, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in headers.items()))
            counts = [v for k, v in headers.items() if k.lower() == 'x-stainless-retry-count']
            _require(len(counts) == 1 and re.fullmatch(r'0|[1-9][0-9]{0,3}', counts[0]))
            dispatch = self.routes.admit(path=url[len(self.host_origin):], method=init.get('method'),
                                         body=init['body'].encode(), retry_count=int(counts[0]), headers=headers)
            if dispatch.mode == 'host':
                return {'mode': 'host', 'generation': dispatch.generation}
            return {'mode': 'v', 'generation': dispatch.generation, 'url': dispatch.endpoint,
                    'headers': dict(dispatch.headers)}
