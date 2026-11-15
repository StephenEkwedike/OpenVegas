"""Isolated ConPTY child. Replace only the remote billing service and device credential."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx


MODELS = {'codex': 'gpt-6-astra', 'claude': 'claude-sonnet-4-6'}
DEVICE = 'ov_device_windows_qa_not_a_credential'
GRANT = 'ov_native_fixture_windows_conpty_only'
BALANCE = '73.25'
SESSION = 'windows-qa-synthetic'


class Bank:
    """Strict in-memory HTTP oracle. Events contain only predeclared operation names."""
    def __init__(self, host, catalog=None, diagnostic=None):
        self.host, self.model = host, MODELS[host]
        self.catalog = catalog
        self.diagnostic = diagnostic
        self.events = []
        self.state = 'initial'
        self.selection = {'host': host, 'provider': {'codex': 'openai', 'claude': 'anthropic'}[host],
                          'model': self.model, 'max_tokens': 512}
        self.price = {'provider': self.selection['provider'], 'model': self.model, 'fingerprint': 'a' * 64,
                      'v_per_usd': '100', 'multiplier': '1', 'supplier_input_usd_per_million': '1',
                      'supplier_output_usd_per_million': '2'}

    def __call__(self, request):
        from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL
        path = request.url.raw_path.decode('ascii')
        status = {'session_id': SESSION, 'host': self.host, 'model': self.model,
                  'billing_mode': 'wallet', 'allowed_models': [self.model]}
        account = path == '/integrations/account'
        device = account or path in ('/integrations/native/options?host=' + self.host,
                                     '/integrations/native/quote', '/integrations/native/sessions')
        key = DEVICE if device else GRANT
        body = json.loads(request.content) if request.content else None
        if (str(request.url) != FALLBACK_DEFAULT_BACKEND_URL + path
                or request.headers.get_list('authorization') != ['Bearer ' + key]):
            return self.reject()
        method = request.method
        if account and method == 'GET':
            op, value = 'balance', {'balance': BALANCE}
        elif path == '/integrations/native/options?host=' + self.host and method == 'GET':
            op, value = 'options', {**self.selection, 'wallet_billing': True,
                'balance_v': BALANCE, 'wallet_limit_policy': 'live_balance',
                'models': [{'model': self.model, 'max_tokens': 512}]}
        elif (path == '/integrations/native/quote' and method == 'POST'
              and self.state == 'initial' and body == self.selection):
            self.state, op, value = 'quoted', 'quote', {**self.selection, 'pricing': self.price}
        elif (path == '/integrations/native/sessions' and method == 'POST' and self.state == 'quoted'
              and body == {**self.selection, 'billing_mode': 'wallet', 'confirmed': True,
                           'pricing_fingerprint': self.price['fingerprint']}):
            self.state, op, value = 'active', 'issue', {**status, 'pricing': self.price,
                'token': GRANT, 'expires_in': 600, 'catalog_hash': 'b' * 64}
        elif path == '/integrations/native/session' and self.state == 'active' and method == 'GET':
            op, value = 'verify', status
            if self.host == 'codex':
                value['codex_catalog'] = {'mode': 'reviewed-v1', 'client_version': '0.166.0',
                                          'session_id': SESSION, 'catalog_hash': 'b' * 64}
        elif (path == '/integrations/native/codex/catalog?client_version=0.166.0'
              and method == 'GET' and self.state == 'active' and self.catalog is not None):
            op, value = 'catalog', self.catalog
        elif path == '/integrations/native/session' and self.state == 'active' and method == 'DELETE':
            self.state, op, value = 'closed', 'revoke', {'closed': True}
        else:
            return self.reject()
        self.events.append(op)
        if self.diagnostic is not None:
            with self.diagnostic.open('a', encoding='ascii') as out:
                out.write('bank_' + op + ':1\n')
        return httpx.Response(200, json=value)

    def reject(self):
        self.events.append('unexpected')
        return httpx.Response(403, json={'detail': 'Unexpected QA request'})

    def broker(self):
        from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL
        from openvegas.integrations.broker import Broker
        client = Broker(backend=FALLBACK_DEFAULT_BACKEND_URL, transport=httpx.MockTransport(self))
        client.credentials = SimpleNamespace(load=lambda: DEVICE)
        return client


def bundled_catalog(exe):
    from openvegas.integrations.codex_billing_route import _catalog_digest, _filter_catalog
    from openvegas.integrations.windows_native_image import _IMAGES
    result = subprocess.run([exe, 'debug', 'models', '--bundled'], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, check=True, timeout=40)
    source = json.loads(result.stdout)
    rows = _filter_catalog(source, [MODELS['codex']])
    now = int(time.time())
    payload = {'schema_version': 1, 'client_version': '0.166.0', 'session_id': SESSION,
               'catalog_hash': 'b' * 64, 'catalog_fence_hash': 'c' * 64,
               'review_revisions': {MODELS['codex']: 'd' * 64}, 'artifact_sha256': 'e' * 64,
               'source': {'kind': 'codex_bundled_export', 'client_version': '0.166.0',
                          'binary_sha256': _IMAGES['codex', '0.166.0'][1],
                          'catalog_sha256': _catalog_digest(source), 'captured_at': now},
               'expires_at': now + 600, 'models_response': rows, 'catalog_sha256': _catalog_digest(rows)}
    return {**payload, 'delivery_sha256': _catalog_digest(payload)}


class LocalSupplier:
    def __init__(self, bank, port):
        if type(port) is not int or not 0 < port <= 65535:
            raise ValueError('Expected a local fixture port.')
        self.bank, self.port = bank, port

    def destination(self, plan, mode):
        from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL
        from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY
        parsed = urlsplit(plan.endpoint)
        origins = {'codex': 'api.openai.com', 'claude': 'api.anthropic.com'}
        expected = origins[self.bank.host] if mode == 'host' else urlsplit(FALLBACK_DEFAULT_BACKEND_URL).netloc
        auth = [(k.lower(), v) for k, v in plan.headers if k.lower() in {
            'authorization', 'x-api-key', 'cookie', 'proxy-authorization', 'chatgpt-account-id',
            'openai-organization', 'openai-project', 'x-openvegas-local-auth'}]
        permitted = ([('authorization', 'Bearer ' + SYNTHETIC_KEY)], [('x-api-key', SYNTHETIC_KEY)])
        if (parsed.netloc != expected or parsed.scheme not in ('https', 'wss') or parsed.fragment
                or mode not in ('host', 'v') or (mode == 'host' and auth not in permitted)
                or (mode == 'v' and (auth != [('authorization', 'Bearer ' + GRANT)]
                                    or self.bank.state != 'active'))):
            self.bank.events.append('unexpected')
            raise ValueError('Refusing non-synthetic supplier traffic.')
        protocol = 'ws' if plan.transport == 'websocket' else 'http'
        return (f'{protocol}://127.0.0.1:{self.port}' + ('/_host' if mode == 'host' else '')
                + parsed.path + ('?' + parsed.query if parsed.query else ''))


def diagnostics(path):
    from openvegas.integrations.credentials import IntegrationError
    modules = {'windows_customer', 'windows_customer_codex', 'windows_customer_claude',
               'codex_handoff_policy', 'windows_session_lease', 'session_ownership',
               '_windows_state', 'codex_billing_route', 'codex_live_switch', 'inference_router_wallet',
               'inference_router_controls', 'claude_live_switch', 'windows_billing_host'}
    seen = set()

    def trace(frame, event, arg):
        if event == 'call':
            if frame.f_globals.get('__name__', '').rsplit('.', 1)[-1] not in modules:
                return None
            frame.f_trace_lines = False
        if event == 'exception' and isinstance(arg[1], (IntegrationError, OSError, ValueError, KeyError)):
            label = f"{frame.f_globals['__name__'].rsplit('.', 1)[-1]}:{frame.f_lineno}"
            if len(seen) < 100 and label not in seen:
                seen.add(label)
                with path.open('a', encoding='ascii') as out:
                    out.write(label + '\n')
                    if frame.f_code.co_name in ('selection', 'verify') and 'loaded' in frame.f_locals:
                        loaded = frame.f_locals.get('loaded')
                        rows = loaded.get('data') if isinstance(loaded, dict) else None
                        if isinstance(rows, list):
                            sid = frame.f_locals.get('sid')
                            out.write(f'loaded_count:{min(len(rows), 99)}\n')
                            out.write(f'loaded_contains:{int(sid in rows)}\n')
                            out.write(f'loaded_paged:{int(loaded.get("nextCursor") is not None)}\n')
                            witness = frame.f_locals.get('self')
                            if witness is not None and type(witness).__name__ == 'NativeThreads' and all(
                                    isinstance(i, str) for i in rows):
                                others = set(rows) - {sid}
                                out.write(f'owned_root:{int(sid in witness.roots)}\n')
                                out.write(f'pending_count:{min(len(witness.pending), 99)}\n')
                                out.write(f'other_unknown:{min(len(others - witness.cold), 99)}\n')
                                out.write(f'other_used:{min(len(others & witness.used), 99)}\n')
        return trace
    return trace


def main(host, exe, port, diagnostic):
    import openvegas
    from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY
    from openvegas.integrations import commands, inference_router_fixture, windows_customer

    root = Path(os.environ['USERPROFILE'])
    if (sys.platform != 'win32' or not sys.flags.isolated or not all(os.isatty(i) for i in range(3))
            or not root.name.startswith('ov-rehearsal-') or Path.cwd() != root / 'work'
            or not Path(openvegas.__file__).resolve().is_relative_to(Path(sysconfig.get_path('purelib')))
            or os.environ.get('OPENAI_API_KEY' if host == 'codex' else 'ANTHROPIC_API_KEY') != SYNTHETIC_KEY):
        return 21
    print('OV-QA-READY', flush=True)
    if input() != 'OV-QA-START':
        return 22
    bank = Bank(host, bundled_catalog(exe) if host == 'codex' else None, Path(diagnostic))
    windows_customer.Broker = bank.broker
    upstream = LocalSupplier(bank, int(port))
    inference_router_fixture.configured = lambda adapter=None: upstream
    sys.settrace(diagnostics(Path(diagnostic)))
    # Use the installed Click command, with real stdin/out, not a CliRunner.
    code = commands.terminal.main(args=['--attach', '--host', host, '--managed-billing'],
                                  standalone_mode=False)
    required = ['options', 'quote', 'issue', 'verify', *(['catalog'] if host == 'codex' else []), 'revoke']
    if (code not in (None, 0) or bank.state != 'closed' or bank.events.count('balance') < 2
            or [op for op in bank.events if op != 'balance'] != required):
        return 23
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(*sys.argv[1:]))
    except Exception as exc:
        import traceback
        frames = []
        while exc is not None and len(frames) < 12:
            frames.extend(f'{Path(f.filename).stem}:{f.lineno}' for f in traceback.extract_tb(exc.__traceback__)
                          if Path(f.filename).stem.startswith(('windows_', 'verify_windows_', 'codex_', 'inference_router')))
            exc = exc.__context__
        print('OV-QA-FAIL ' + ','.join(frames[-12:]) + ' END', flush=True)
        sys.exit(26)
