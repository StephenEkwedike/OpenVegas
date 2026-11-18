"""Wallet-first managed launch, using the existing quoted grant protocol."""
from __future__ import annotations

import asyncio
import json
import re

from .codex_billing_route import _settle
from .credentials import IntegrationError
from .inference_router_wallet import CodexWalletGrant

# Only standard service is priced. Apply to the child, never the user's profile.
STANDARD_CODEX_ARGS = ('-c', 'service_tier="default"', '-c', 'features.fast_mode=false',
                       '-c', 'features.ultrafast_mode=false')


def codex_wallet_overlay(url, env, args, identity, token):
    """Dedicated process-only route; native retains tools, policy and compaction."""
    if (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identity)
            or identity in {'openai', 'ollama', 'lmstudio'}):
        raise IntegrationError('A dedicated wallet provider identity is required.')
    provider = {'name': 'OpenVegas $V', 'base_url': url, 'env_key': 'OPENVEGAS_NATIVE_TOKEN',
                'wire_api': 'responses', 'requires_openai_auth': False,
                'supports_websockets': False,
                'request_max_retries': 0, 'stream_max_retries': 0}
    table = ('{' + ','.join(key + '=' + json.dumps(value) for key, value in provider.items())
             + ',capabilities={remote_compaction="unsupported"}}')
    updated = {k: v for k, v in env.items() if k not in {
        'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_ORGANIZATION', 'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID'}}
    updated['OPENVEGAS_NATIVE_TOKEN'] = token
    return updated, ['-c', 'model_provider=' + json.dumps(identity), '-c',
                     'model_providers.' + identity + '=' + table, *args]


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
