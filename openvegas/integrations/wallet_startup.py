"""Wallet-first managed launch, using the existing quoted grant protocol."""
from __future__ import annotations

import asyncio

from .codex_billing_route import _settle
from .credentials import IntegrationError
from .inference_router_wallet import CodexWalletGrant

# Only standard service is priced. Apply to the child, never the user's profile.
STANDARD_CODEX_ARGS = ('-c', 'service_tier="default"', '-c', 'features.fast_mode=false',
                       '-c', 'features.ultrafast_mode=false')


def standard_catalog(catalog):
    unavailable = {'service_tiers': [], 'additional_speed_tiers': [], 'default_service_tier': None}
    return {**catalog, 'models': [{**row, **{k: v for k, v in unavailable.items() if k in row}}
                                  for row in catalog['models']]}


def initial_billing(env):
    payer = env.get('OPENVEGAS_INITIAL_BILLING', 'v')
    if payer not in {'host', 'v'}:
        raise IntegrationError('Initial billing must be v or host; no session opened.')
    return payer


async def prepare_wallet(broker, model, version, *, emit=None):
    grant = CodexWalletGrant(broker, version=version)
    try:
        return await grant.open(model, bind_catalog=True)
    except BaseException:
        if not grant.released:
            await _settle(asyncio.create_task(grant.close()))
        raise
