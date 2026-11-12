"""Explicit standalone wallet TUI. Never looks up a conversation or a supervisor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import tempfile
from pathlib import Path

import click

from . import native
from .billing_choices import model_choices, wallet_ceiling
from .broker import Broker
from .codex_billing_route import _bundled_catalog, _filter_catalog, _settle
from .credentials import IntegrationError
from .host_adapter import require_version
from .native_owned_process import OwnedLeader, spawn_leader


def _check(condition, message):
    if not condition:
        raise IntegrationError(message)


def _selection(row):
    host = row['host']
    provider = {'codex': 'openai', 'claude': 'anthropic'}[host]
    price = row.get('pricing')
    _check(isinstance(price, dict) and price.get('model') == row['model']
           and price.get('provider') == row.get('provider') == provider
           and price.get('native_transport') == {'codex': 'responses', 'claude': 'messages'}[host] + '_direct_v1'
           and price.get('policy') == 'reviewed_direct_token_cost_plus_markup_v1',
           'The backend did not return a reviewed direct price.')
    fingerprint, rate, multiplier = native._pricing(price)
    expected = hashlib.sha256(json.dumps({k: v for k, v in price.items() if k != 'fingerprint'},
                                         sort_keys=True).encode()).hexdigest()
    _check(fingerprint == expected and native._decimal(price.get('v_per_supplier_usd')) == rate * multiplier,
           'The backend returned inconsistent direct pricing.')
    for field in ('supplier_input_usd_per_million', 'supplier_output_usd_per_million'):
        native._decimal(price.get(field), allow_zero=True)
    return {k: row[k] for k in ('host', 'provider', 'model', 'max_tokens')}


def _child_env(host, backend, model, max_tokens, token):
    env = native.child_environment(host, backend, model, max_tokens, token)
    for key in tuple(env):
        if (key.startswith(('OPENVEGAS_SWITCH_', 'OPENVEGAS_TERMINAL_', 'CODEX_REMOTE_'))
                or key in {'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_APP_SERVER_URL'}):
            del env[key]
    return env


async def _revoke(broker, token, emit):
    try:
        async with asyncio.timeout(5):
            result = await broker.request('DELETE', '/native/session', token=token)
            if not isinstance(result, dict) or result.get('closed') is not True:
                raise ValueError
        emit('Wallet grant revoked. Back in the invoking shell.')
    except Exception:  # noqa: BLE001 - transport diagnostics can contain the bearer
        emit('Warning: wallet grant revocation was not confirmed. Its server expiry still applies.')
        return False
    return True


_CHILD_GRACE_SECONDS = 5
_CHILD_KILL_SECONDS = 5


async def _stop_local(process):
    if not isinstance(process, OwnedLeader):
        raise IntegrationError('Refusing to stop an unowned interactive child.')
    async with process.stop_lock:
        if process.retired:
            return
        if process.returncode is None:
            process.signal_leader(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), _CHILD_GRACE_SECONDS)
            except TimeoutError:
                process.signal_leader(signal.SIGKILL)
                await asyncio.wait_for(process.wait(), _CHILD_KILL_SECONDS)
        process.reap_exited_leader()


async def _run_local(argv, env):
    process, cancelled = await _settle(asyncio.create_task(spawn_leader(*argv, env=env)))
    try:
        if cancelled:
            raise asyncio.CancelledError
        code = await process.wait()
        return code if code >= 0 else 128 - code
    finally:
        _, cancelled = await _settle(asyncio.create_task(_stop_local(process)))
        if cancelled:
            raise asyncio.CancelledError


async def _session(host, broker, info, directory, runner, emit):
    device = broker.credentials.load()
    _check(isinstance(device, str) and device.startswith('ov_device_'),
           'Existing OpenVegas device sign-in required. No session opened.')
    opts = await broker.request('GET', '/native/options?host=' + host, token=device)
    _check(isinstance(opts, dict) and opts.get('wallet_billing') is True
           and opts.get('host') == host and opts.get('provider') == 'openai',
           'No reviewed direct wallet route for this host.')
    balance = native._decimal(opts.get('balance_v'), allow_zero=True)
    _check(balance >= native._decimal('0.000001'), 'No spendable wallet balance. No grant opened.')
    policy = opts.get('wallet_limit_policy')
    _check(policy in (None, 'live_balance'), 'Unknown wallet billing policy; no grant opened.')
    # Older wallet-mode servers require a numeric wire field, but use their
    # own balance. Never retry a grant request with a different payload.
    compatibility = {'limit_v': format(wallet_ceiling(balance), 'f')} if policy is None else {}
    rows = opts.get('models')
    choices = model_choices(host, rows).options
    _check(choices and all(row.get('host') == host for row in rows), 'No exact reviewed host models.')
    for row in rows:
        _selection(row)
    bundled = await _bundled_catalog(info['executable'], directory, os.environ)
    catalog = _filter_catalog(bundled, choices)
    model_file = Path(directory) / 'models.json'
    with model_file.open('x') as output:
        os.chmod(model_file, 0o600)
        json.dump(catalog, output)

    row = rows[0]
    selection = _selection(row)
    quote = await broker.request('POST', '/native/quote', token=device, data=selection)
    _check(quote == row, 'The direct model, token cap or price changed. Start again.')
    price = quote['pricing']
    conversion = native._decimal(price['v_per_supplier_usd'])
    rates = [native._decimal(price[k], allow_zero=True) * conversion for k in
             ('supplier_input_usd_per_million', 'supplier_output_usd_per_million')]
    emit(f"Fresh local {host} chat | OpenVegas wallet | {row['model']}. "
         f"Available now: {balance:f} $V; input/output per million: {rates[0]:f}/{rates[1]:f} $V. "
         "Each request needs available funds; no purchases or top-ups.")
    emit('No chat was resumed. No managed play/switch controls here. Quit to the shell for the lobby '
         'or return to your managed session. Hosted search and subagents are disabled.')
    token = None
    try:
        # If the terminal closes during issuance, finish receiving the handle
        # before honoring cancellation so an acknowledged grant is not orphaned.
        issue = asyncio.create_task(broker.request('POST', '/native/sessions', token=device, data={
            **selection, **compatibility, 'confirmed': True, 'billing_mode': 'wallet',
            'pricing_fingerprint': price['fingerprint']}))
        grant, cancelled = await _settle(issue)
        candidate = grant.get('token') if isinstance(grant, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r'ov_native_[A-Za-z0-9_-]{16,128}', candidate):
            token = candidate
        if cancelled:
            raise asyncio.CancelledError
        _check(token and isinstance(grant.get('session_id'), str) and native._SAFE_ID.fullmatch(grant['session_id'])
               and type(grant.get('expires_in')) is int and grant['expires_in'] > 0
               and all(grant.get(k, v) == v for k, v in selection.items())
               and ('catalog_hash' not in grant or isinstance(grant['catalog_hash'], str)
                    and re.fullmatch(r'[0-9a-f]{64}', grant['catalog_hash']))
               and grant.get('pricing') == price and grant.get('billing_mode') == 'wallet'
               and grant.get('allowed_models') == list(choices), 'Grant differs from reviewed wallet options.')
        status = await broker.request('GET', '/native/session', token=token)
        expected = {'session_id': grant['session_id'], 'host': host, 'model': row['model'],
                    'billing_mode': 'wallet', 'allowed_models': list(choices)}
        _check(isinstance(status, dict) and all(status.get(k) == v for k, v in expected.items())
               and status.get('active', True) is True and status.get('status', 'active') == 'active'
               and all(status.get(k, v) == v for k, v in selection.items())
               and ('catalog_hash' not in status or status['catalog_hash'] == grant.get('catalog_hash'))
               and (policy == 'live_balance' or native._decimal(status.get('budget_v')) ==
                    native._decimal(grant.get('budget_v')))
               and native._decimal(status.get('used_or_reserved_v'), allow_zero=True) == 0
               and type(status.get('requests')) is int and status['requests'] == 0,
               'The wallet session is not fresh or its authorization changed.')
        env = _child_env(host, broker.backend, row['model'], row['max_tokens'], token)
        # A unique provider table cannot merge previously configured auth hooks.
        argv = native.host_argv(info['executable'], host, broker.backend, row['model'], [],
                                local_tools_only=True, provider_identity='openvegas_wallet_' + secrets.token_hex(8))
        argv[1:1] = ['--no-daemon', '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
                     '-c', 'model_catalog_json=' + json.dumps(str(model_file))]
        code = await runner(argv, env)
    finally:
        if token:
            closed, cancelled = await _settle(asyncio.create_task(_revoke(broker, token, emit)))
            if cancelled:
                raise asyncio.CancelledError
    return code if closed else code or 1


async def launch(host, *, broker=None, runner=None, emit=click.echo):
    _check(all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)),
           '--wallet requires an interactive local terminal. No grant opened.')
    _check(host == 'codex', 'Fresh Claude wallet launch is unavailable: subscription isolation with '
           'managed policy intact is not verified. No grant opened; host billing is unchanged.')
    info = native.detect(host)
    require_version(host, (info or {}).get('version'))
    owned = broker is None
    broker = broker or Broker()
    try:
        with tempfile.TemporaryDirectory(prefix='ov-wallet-') as directory:
            return await _session(host, broker, info, directory, runner or _run_local, emit)
    except IntegrationError:
        raise
    except Exception:  # noqa: BLE001 - never echo a credential-bearing subprocess or transport error
        raise IntegrationError('Fresh wallet launch failed. No automatic retry or subscription fallback.') from None
    finally:
        if owned:
            await broker.close()


async def command(host):
    _check(os.name == 'posix', 'Fresh wallet lifecycle controls require a supported POSIX terminal.')
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous = {}

    def stop():
        if not task.cancelling():
            task.cancel()

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.getsignal(sig)
            loop.add_signal_handler(sig, stop)
        return await launch(host)
    except asyncio.CancelledError:
        return 130
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
