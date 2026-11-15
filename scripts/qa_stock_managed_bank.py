"""Existing strict synthetic Bank; HTTP exposure is in qa_stock_managed_fixture."""
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

AFTER_WALLET = '73.24'

SESSION = 'windows-qa-synthetic'

class Bank:
    """Strict in-memory HTTP oracle. Events contain only predeclared operation names."""
    def __init__(self, host, catalog=None):
        self.host, self.model = host, MODELS[host]
        self.catalog = catalog
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
            op, value = 'balance', {'balance': AFTER_WALLET if self.state == 'closed' else BALANCE}
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

def bundled_catalog(exe, *, env, cwd):
    from openvegas.integrations.codex_billing_route import _catalog_digest, _filter_catalog
    from openvegas.integrations.windows_native_image import _IMAGES
    result = subprocess.run([exe, 'debug', 'models', '--bundled'], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env, cwd=cwd, check=True, timeout=40)
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
