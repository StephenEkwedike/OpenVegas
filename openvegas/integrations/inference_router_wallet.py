"""Existing Codex wallet protocol without starting a replacement native process."""
from __future__ import annotations

import re
import time
from functools import partial

from . import native
from .codex_billing_route import _grant_catalog, _validate_grant_catalog
from .codex_live_switch import GrantBoundary, Selection, require
from .credentials import IntegrationError
from .host_adapter import require_version
from .inference_router_controls import WalletPreparationRejected
from .inference_router_session import WalletAccess


class CodexWalletGrant:
    """Retain one mutation owner even if preparing its catalog is interrupted."""

    def __init__(self, broker, *, version, wall_clock=time.time):
        require_version('codex', version)
        self.broker, self.version, self.wall_clock = broker, version, wall_clock
        self.boundary = None
        self.access = None
        self.started = False

    def __repr__(self):
        return '<CodexWalletGrant private>'

    @property
    def released(self):
        return self.boundary is None or self.boundary.state in {
            'new', 'quoting', 'quoted', 'rejected', 'closed'}

    async def open(self, model):
        require(not self.started, 'Wallet preparation cannot be retried.')
        self.started = True
        device = self.broker.credentials.load()
        require(isinstance(device, str) and device.startswith('ov_device_'),
                'Sign in to OpenVegas before changing billing.')
        options = await self.broker.request('GET', '/native/options?host=codex', token=device)
        require(isinstance(options, dict) and options.get('wallet_limit_policy') == 'live_balance',
                'The backend must support live wallet balance billing.')
        selection = Selection.reviewed(options, model)
        require(selection.limit_v is None, 'No new customer spending ceiling may be installed.')
        boundary = self.boundary = GrantBoundary(self.broker, selection)
        fields = {'host': 'codex', 'provider': 'openai', 'model': model,
                  'max_tokens': selection.max_tokens}
        quote = await boundary.request('POST', '/native/quote', token=device, data=fields)
        require(isinstance(quote, dict) and all(quote.get(k) == v for k, v in fields.items()),
                'Wallet quote changed the native selection.')
        pricing = quote.get('pricing')
        fingerprint, rate, multiplier = native._pricing(pricing)
        require(re.fullmatch(r'[a-f0-9]{64}', fingerprint) is not None
                and pricing.get('provider') == 'openai' and pricing.get('model') == model,
                'Wallet pricing is not bound to the selected native model.')
        for field in ('supplier_input_usd_per_million', 'supplier_output_usd_per_million'):
            native._decimal(pricing.get(field), allow_zero=True)
        if 'v_per_supplier_usd' in pricing:
            require(native._decimal(pricing['v_per_supplier_usd']) == rate * multiplier,
                    'Wallet conversion prices disagree.')
        issued = self.wall_clock()
        grant = await boundary.request('POST', '/native/sessions', token=device, data={
            **fields, 'billing_mode': 'wallet', 'confirmed': True,
            'pricing_fingerprint': fingerprint})
        require(isinstance(grant, dict) and boundary.state == 'issued'
                and grant.get('pricing') == pricing and grant.get('billing_mode') == 'wallet'
                and isinstance(grant.get('session_id'), str)
                and native._SAFE_ID.fullmatch(grant['session_id']) is not None
                and type(grant.get('expires_in')) is int and 0 < grant['expires_in'] <= 86400,
                'Wallet grant differs from the authorized quote.')
        identifiers = grant.get('allowed_models')
        require(isinstance(identifiers, list) and 1 <= len(identifiers) <= 128
                and all(isinstance(m, str) and native._MODEL.fullmatch(m) for m in identifiers)
                and len(set(identifiers)) == len(identifiers) and model in identifiers
                and set(identifiers) <= {row['model'] for row in options['models']},
                'Wallet models differ from the reviewed native catalog.')
        token = grant['token']
        status = await boundary.request('GET', '/native/session', token=token)
        # GET /native/session resolves the active authorization before HTTP 200;
        # the current server does not include an `active` field in that receipt.
        require(isinstance(status, dict) and status.get('active') is not False
                and status.get('status') not in {'closed', 'expired', 'revoked', 'exhausted'}
                and all(status.get(k) == v for k, v in {
                    'session_id': grant['session_id'], 'host': 'codex', 'model': model,
                    'billing_mode': 'wallet', 'allowed_models': identifiers}.items()),
                'Wallet authorization is not active for this native selection.')
        binding = {'version': self.version, 'session_id': grant['session_id'],
                   'catalog_hash': grant.get('catalog_hash'), 'identifiers': tuple(identifiers)}
        catalog = await _grant_catalog(boundary, token, **binding, grant_status=status)
        require(catalog is not None, 'Reviewed native model metadata is unavailable; no model substituted.')
        self.access = WalletAccess(token, issued + grant['expires_in'],
            validate_catalog=partial(_validate_grant_catalog, **binding), grant=self)
        return self.access

    async def close(self):
        if self.boundary is not None:
            await self.boundary.revoke_known()
            self.boundary.require_released()


def codex_wallet_callbacks(broker, *, active_model, version):
    """Bind the existing one-shot protocol to a freshly fenced native model."""
    retained = []

    async def prepare(ticket):
        grant = CodexWalletGrant(broker, version=version)
        retained.append(grant)
        try:
            return await grant.open(active_model())
        except IntegrationError:
            if grant.released:
                raise WalletPreparationRejected('Wallet refused before grant creation.') from None
            raise

    async def revoke(access):
        require(isinstance(access, WalletAccess) and access.grant in retained,
                'Wallet closure belongs to another router session.')
        await access.grant.close()
        return access.grant.released

    prepare.retained_grants = retained
    return prepare, revoke
