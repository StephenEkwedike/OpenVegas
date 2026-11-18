"""Confirmed, fixed-payer Claude launch. Native Claude owns all context work."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import click

from . import native
from .billing_choices import model_choices
from .claude_http_policy import _endpoint
from .codex_billing_route import _settle
from .credentials import IntegrationError
from .wallet_offer_digest import offer_digest

VERSION = '2.1.322'
CONTEXT_OWNER = 'native-v1'


def _check(condition, message):
    if not condition:
        raise IntegrationError(message)


def preflight(info, backend, source_env=None):
    _check(isinstance(info, dict) and info.get('version') == VERSION,
           'Claude wallet launch requires exactly 2.1.322. No grant opened.')
    _endpoint(backend + '/integrations/native/v1/messages')
    env = native.child_environment('claude', backend, '', 0, '', source_env=source_env)
    if os.name == 'nt':
        from .windows_customer_claude import check_settings
        check_settings({k: v for k, v in env.items()
                        if k not in {'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_GATEWAY'}}, os.getcwd())
    else:
        from .inference_router_claude import verify_settings
        verify_settings(env, os.getcwd())
    profile = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude')))
    sources = {profile / 'settings.json', profile / 'settings.local.json'}
    for parent in (Path.cwd(), *Path.cwd().parents):
        sources.update(parent / '.claude' / name for name in ('settings.json', 'settings.local.json'))
    # Do not disable settings, permissions, hooks or networking to isolate billing.
    # An authentication override needs an explicit policy review instead.
    try:
        for path in sources:
            if not path.exists():
                continue
            if path.stat().st_size > 1024 * 1024:
                raise ValueError
            settings = json.loads(path.read_text())
            if not isinstance(settings, dict) or any(key in settings for key in (
                    'apiKeyHelper', 'awsAuthRefresh', 'awsCredentialExport', 'gcpAuthRefresh',
                    'forceLoginMethod', 'forceLoginOrgUUID', 'forceLoginGatewayUrl', 'modelOverrides')):
                raise ValueError
            for key in settings.get('env', {}):
                if (key in {'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_CUSTOM_HEADERS',
                            'ANTHROPIC_PROFILE', 'AGENT_PROXY_AUTH_TOKEN', 'CLAUDE_CODE_MAX_RETRIES',
                            'CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST', 'CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH',
                            'CLAUDE_CODE_HOST_CREDS_FILE', 'CLAUDE_CODE_HOST_GATEWAY_LINEAGE',
                            'CLAUDE_BG_AUTH_SNAPSHOT_PATH', 'CLAUDE_SESSION_INGRESS_TOKEN_FILE',
                            'CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR',
                            'CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR', 'CCR_AGENT_PROXY_TOKEN_FILE_DESCRIPTOR',
                            'CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY',
                            'CLAUDE_CODE_SESSIONS_OAUTH_TOKEN', '_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL'}
                        or key.startswith(('CLAUDE_CODE_OAUTH_', 'CLAUDE_CODE_API_KEY_',
                                           'CLAUDE_CODE_HOST_AUTH_', 'CLAUDE_CODE_USER_DELEGATED_SESSION_'))):
                    raise ValueError
    except (OSError, ValueError, TypeError, AttributeError):
        raise IntegrationError('Claude settings override wallet authentication or routing. '
                               'No grant opened; native permissions are unchanged.') from None


async def session(broker, info, runner, emit, confirm, *, managed_args=None, source_env=None):
    from .fresh_wallet import _revoke, _selection

    preflight(info, broker.backend, source_env)
    device = broker.credentials.load()
    _check(isinstance(device, str) and device.startswith('ov_device_'),
           'Existing OpenVegas device sign-in required. No grant opened.')
    opts = await broker.request('GET', '/native/options?host=claude&context_owner=' + CONTEXT_OWNER,
                                token=device)
    _check(isinstance(opts, dict) and opts.get('host') == 'claude'
           and opts.get('provider') == 'anthropic' and opts.get('wallet_billing') is True
           and opts.get('wallet_limit_policy') == 'live_balance'
           and opts.get('context_owner') == CONTEXT_OWNER,
           'Backend did not confirm native-v1 Claude wallet context. No grant opened; no legacy fallback.')
    _check(native._decimal(opts.get('balance_v'), allow_zero=True) >= native._decimal('0.000001'),
           'No spendable wallet balance. No grant opened.')
    rows = opts.get('models')
    choices = model_choices('claude', rows).options
    _check(choices, 'No reviewed Claude wallet models. No grant opened.')
    for row in rows:
        _selection(row)
        _check(not {'ordinary_scope_revision', 'private_review_revision', 'ws_compaction'} & row['pricing'].keys(),
               'Legacy context policy is not a native-v1 wallet offer.')
    requested = (source_env if source_env is not None else native.host_environment() or os.environ).get('ANTHROPIC_MODEL')
    if managed_args is not None:
        # The grant anchor is not the native model choice. All reviewed models
        # are authorized; Claude still resolves its own settings and argv.
        requested = None
    _check(not requested or requested in choices,
           'The configured Claude model has no exact wallet offer. No model substituted.')
    row = next((r for r in rows if r['model'] == requested), rows[0])
    selection = _selection(row)
    quote = await broker.request('POST', '/native/quote', token=device, data=selection)
    _check(quote == row, 'Claude wallet price or scope changed. No grant opened.')
    price = row['pricing']
    if managed_args is None:
        emit('Fresh local Claude chat | native context | OpenVegas $V billing.')
        for offered in rows:
            p = offered['pricing']
            emit(f"{offered['model']}: supplier USD/million input {p['supplier_input_usd_per_million']}, "
                 f"output {p['supplier_output_usd_per_million']}; {p['v_per_supplier_usd']} V/supplier USD.")
        emit(f"Initial model: {row['model']}. Native summaries and supported small-model calls use the same wallet. "
             'No subscription fallback, model remapping or output override. No purchases or automatic top-ups.')
        try:
            if not (confirm or click.confirm)('Start Claude using your available $V?', default=False):
                emit('Cancelled. No wallet grant opened.')
                return 0
        except click.Abort:
            return 130
    token = None
    try:
        issue = asyncio.create_task(broker.request('POST', '/native/sessions', token=device, data={
            **selection, 'context_owner': CONTEXT_OWNER, 'confirmed': True, 'billing_mode': 'wallet',
            'pricing_fingerprint': price['fingerprint'],
            'consent_catalog_digest': offer_digest(rows, selection['max_tokens'])}))
        grant, cancelled = await _settle(issue)
        candidate = grant.get('token') if isinstance(grant, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r'ov_native_[A-Za-z0-9_-]{16,128}', candidate):
            token = candidate
        if cancelled:
            raise asyncio.CancelledError
        _check(token and grant.get('context_owner') == CONTEXT_OWNER
               and isinstance(grant.get('session_id'), str) and native._SAFE_ID.fullmatch(grant['session_id'])
               and type(grant.get('expires_in')) is int and 0 < grant['expires_in'] <= 86400
               and grant.get('billing_mode') == 'wallet' and grant.get('allowed_models') == list(choices)
               and grant.get('pricing') == price
               and all(grant.get(k, v) == v for k, v in selection.items()),
               'Claude grant did not confirm the reviewed native-v1 wallet scope. No child started.')
        status = await broker.request('GET', '/native/session', token=token)
        expected = {'session_id': grant['session_id'], 'host': 'claude', 'model': row['model'],
                    'billing_mode': 'wallet', 'allowed_models': list(choices), 'context_owner': CONTEXT_OWNER}
        _check(isinstance(status, dict) and all(status.get(k) == v for k, v in expected.items())
               and status.get('active', True) is True and status.get('status', 'active') == 'active'
               and native._decimal(status.get('used_or_reserved_v'), allow_zero=True) == 0
               and type(status.get('requests')) is int and status['requests'] == 0,
               'Claude wallet session is not fresh or its native context authorization changed.')
        preflight(info, broker.backend, source_env)
        env = native.child_environment('claude', broker.backend, row['model'], row['max_tokens'], token,
                                       source_env=source_env)
        # No settings-source override, model aliases, tool restrictions or permission flags.
        args = ['--model', row['model']] if managed_args is None else list(managed_args)
        code = await runner([info['executable'], *args], env)
    finally:
        if token:
            closed, cancelled = await _settle(asyncio.create_task(_revoke(broker, token, emit)))
            if cancelled:
                raise asyncio.CancelledError
    return code if closed else code or 1
