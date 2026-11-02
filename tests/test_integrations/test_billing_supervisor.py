"""Owned-process billing lifecycle, synthetic backend, no supplier requests."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import click
import httpx
import pytest

from openvegas.integrations import billing_host, native
from openvegas.integrations.broker import Broker
from openvegas.integrations.credentials import IntegrationError

SID = '01a17668-079e-7e90-b434-58f63208e39f'
TOKEN = 'ov_native_supervisor_fixture_only'
PRICING = {'fingerprint': 'a' * 64, 'v_per_usd': '100', 'multiplier': '1.4'}


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize('scenario', ['approved', 'no_extra_confirmation', 'native_model_selection', 'grant_error',
                                     'v_exit', 'v_exit_zero', 'v_bootstrap_error', 'no_session'])
async def test_same_terminal_owned_process_switch_lifecycle(tmp_path, monkeypatch, host, scenario):
    home = tmp_path / 'home'
    home.mkdir()
    config = home / 'config.toml'
    config.write_text('model_provider="openai"\nsandbox_mode="read-only"\n')
    config_before = config.read_bytes()
    auth = home / 'auth.json'
    auth.write_text('{"fixture":"unchanged personal login"}')
    before = auth.read_bytes()
    events = tmp_path / 'events.jsonl'
    stub = tmp_path / 'native-fixture'
    # Simulate only the native lifecycle/hook, not its model or visual interface.
    stub.write_text(f'#!{sys.executable}\n' + r'''import json, os, socket, sys, time
from pathlib import Path
p = Path(FIXTURE_EVENTS)
rows = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
n = sum(row['kind'] == 'child' for row in rows)
v = 'OPENVEGAS_NATIVE_TOKEN' in os.environ
row = {'kind': 'child', 'v': v, 'argv': sys.argv[1:], 'cwd': os.getcwd(),
       'personal_key': os.environ.get('OPENAI_API_KEY') == 'personal-fixture',
       'connection': os.environ.get('OPENVEGAS_TERMINAL_SOCKET') == '/tmp/game-fixture'}
with p.open('a') as f: f.write(json.dumps(row) + '\n')
def send(action, identifier=FIXTURE_SID):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(os.environ['OPENVEGAS_SWITCH_SOCKET'])
        s.sendall(json.dumps({'token': os.environ['OPENVEGAS_SWITCH_TOKEN'],
            'action': action, 'session_id': identifier, 'model': 'original-model'}).encode() + b'\n')
        return json.loads(s.recv(8192))
if FIXTURE_SCENARIO == 'no_session':
    assert not send('v', None)['ok']
    sys.exit(0)
if v and FIXTURE_SCENARIO == 'v_bootstrap_error':
    sys.exit(7)
assert send('register')['ok']
if n == 0 or (v and FIXTURE_SCENARIO not in ('v_exit', 'v_exit_zero')):
    assert send('v' if n == 0 else 'host')['ok']
    time.sleep(30)
sys.exit(7 if v and FIXTURE_SCENARIO != 'v_exit_zero' else 0)
'''.replace('FIXTURE_EVENTS', repr(str(events))).replace('FIXTURE_SID', repr(SID))
        .replace('FIXTURE_SCENARIO', repr(scenario)))
    stub.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    for key, value in {
        'OPENVEGAS_HOST_HOME': str(home), 'OPENVEGAS_HOST_CODEX_HOME': str(home),
        'OPENVEGAS_HOST_CLAUDE_CONFIG_DIR': str(home),
        'OPENAI_API_KEY': 'personal-fixture',
        'OPENVEGAS_TERMINAL_SOCKET': '/tmp/game-fixture',
        'OPENVEGAS_TERMINAL_TOKEN': 'private-game-fixture',
        'OPENVEGAS_LOCAL_CONTROLS': '0', 'OPENVEGAS_CODEX_WALLET': '0',
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('OPENVEGAS_NATIVE_TOKEN', raising=False)
    calls, grants, prompts, confirmations = [], [], [], []
    provider = 'anthropic' if host == 'claude' else 'openai'

    def endpoint(req):
        data = json.loads(req.content) if req.content else {}
        calls.append((req.method, req.url.path))
        if req.url.path.endswith('/options'):
            models = [{'model': 'exact-model', 'max_tokens': 128}]
            if scenario == 'native_model_selection':
                models.append({'model': 'other-reviewed-model', 'max_tokens': 256})
            return httpx.Response(200, json={'host': host, 'provider': provider,
                'balance_v': '500', 'models': models, 'wallet_billing': True})
        if req.url.path.endswith('/quote'):
            return httpx.Response(200, json={**data, 'pricing': PRICING})
        if req.url.path.endswith('/sessions'):
            grants.append(data)
            assert data['provider'] == provider and data['confirmed'] is True
            assert data['billing_mode'] == 'wallet'
            assert data['limit_v'] == '500.000000'
            assert data['model'] == 'exact-model' and data['max_tokens'] == 128
            assert data['pricing_fingerprint'] == PRICING['fingerprint']
            if scenario == 'grant_error':
                return httpx.Response(503, json={'detail': 'fixture unavailable'})
            return httpx.Response(200, json={'token': TOKEN, 'session_id': 'fixture',
                'expires_in': 60, 'budget_v': data['limit_v'], 'pricing': PRICING,
                'billing_mode': 'wallet', 'allowed_models': ['exact-model', 'other-reviewed-model']})
        if req.method == 'DELETE':
            with events.open('a') as f:
                f.write(json.dumps({'kind': 'revoked'}) + '\n')
        return httpx.Response(200, json={'active': True, 'session_id': 'fixture',
            'billing_mode': 'wallet', 'allowed_models': ['exact-model', 'other-reviewed-model']})

    def make_broker():
        broker = Broker(backend='http://127.0.0.1:12345', transport=httpx.MockTransport(endpoint))
        broker.credentials = SimpleNamespace(load=lambda: 'ov_device_fixture')
        return broker
    monkeypatch.setattr(billing_host, 'Broker', make_broker)
    monkeypatch.setattr(native, 'detect', lambda _: {'executable': str(stub), 'version': 'fixture'})
    if host == 'codex':
        from openvegas.integrations import codex_billing_route
        @asynccontextmanager
        async def fixture_route(*args, **kwargs):
            assert [row['model'] for row in kwargs['allowed_models']] == ['exact-model', 'other-reviewed-model']
            yield 'unix:///fixture/native-server.sock'
        monkeypatch.setattr(codex_billing_route, 'route', fixture_route)
    def prompt(label, **kwargs):
        prompts.append(label)
        pytest.fail('Native controls must own model selection')
    def confirm(label, **kwargs):
        confirmations.append(label)
        pytest.fail('The explicit use v command is already consent')
    monkeypatch.setattr(click, 'prompt', prompt)
    monkeypatch.setattr(click, 'confirm', confirm)
    messages = []
    # Remote Codex uses persisted policy; invocation overrides have separate
    # fail-closed preflight tests and must not be silently dropped here.
    permission = ['--permission-mode', 'default'] if host == 'claude' else []
    env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home)}
    supervisor = billing_host.Supervisor(host, str(stub), permission, env, emit=messages.append)
    code = await asyncio.wait_for(supervisor.run(), 15)
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    children = [row for row in rows if row['kind'] == 'child']
    activated = scenario in ('approved', 'no_extra_confirmation', 'native_model_selection', 'v_exit', 'v_exit_zero', 'v_bootstrap_error')
    assert [c['v'] for c in children] == (
        [False, True, False] if scenario in ('approved', 'no_extra_confirmation', 'native_model_selection', 'v_bootstrap_error') else
        [False, True] if scenario in ('v_exit', 'v_exit_zero') else
        [False] if scenario == 'no_session' else [False, False])
    assert code == (7 if scenario == 'v_exit' else 0)
    assert auth.read_bytes() == before
    assert config.read_bytes() == config_before
    for child in children:
        assert child['cwd'] == str(tmp_path) and child['connection']
        assert child['personal_key'] is (not child['v'])
        assert all(arg in child['argv'] for arg in permission)
        assert TOKEN not in str(child)
    assert all(SID in child['argv'] for child in children[1:])
    if scenario not in ('v_exit', 'v_exit_zero', 'no_session'):
        assert 'original-model' in children[-1]['argv']
    if scenario in ('approved', 'no_extra_confirmation', 'native_model_selection', 'v_bootstrap_error'):
        assert [row['kind'] for row in rows] == ['child', 'child', 'revoked', 'child']
    if host == 'codex':
        assert all('--remote' in child['argv'] for child in children if child['v'])
        assert all('model_provider="openai"' in child['argv'] for child in children[1:] if not child['v'])
    if scenario in ('v_exit', 'v_exit_zero'):
        assert supervisor.v_child_ready and supervisor.pending is None
        assert not any('Returning to host billing' in message for message in messages)
    if scenario == 'v_bootstrap_error':
        assert not supervisor.v_child_ready
        assert any('no prompt will be resubmitted' in message for message in messages)
    assert prompts == [] and confirmations == []
    assert sum(method == 'DELETE' for method, _ in calls) == int(activated)
    if scenario == 'no_session':
        assert not calls and not grants and supervisor.identifier is None
    assert TOKEN not in '\n'.join(messages)
    assert not os.path.exists(supervisor.env['OPENVEGAS_SWITCH_SOCKET'])


@pytest.mark.asyncio
async def test_cancel_stops_only_owned_child_and_removes_socket(tmp_path):
    supervisor = billing_host.Supervisor('claude', sys.executable,
        ['-c', 'import time; time.sleep(60)'],
        {'HOME': str(tmp_path), 'OPENVEGAS_LOCAL_CONTROLS': '0'})
    task = asyncio.create_task(supervisor.run())
    async with asyncio.timeout(5):
        while supervisor.child is None:
            await asyncio.sleep(.01)
    child = supervisor.child
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None
    assert supervisor.pending is None
    assert not os.path.exists(supervisor.env['OPENVEGAS_SWITCH_SOCKET'])


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize(('balance', 'ceiling'), [
    ('500', '500.000000'), ('100000.9', '100000.000000'), ('12.1234569', '12.123456'),
])
@pytest.mark.parametrize('current', ['reviewed-second', 'unavailable-original', None])
async def test_choose_v_integrates_wallet_model_and_original_provider(
    tmp_path, monkeypatch, host, balance, ceiling, current,
):
    (tmp_path / 'config.toml').write_text('model_provider="openai"\n')
    messages, prompts = [], []
    supervisor = billing_host.Supervisor(host, '/unused', [],
        {'HOME': str(tmp_path), 'CODEX_HOME': str(tmp_path),
         'OPENVEGAS_SWITCH_SOCKET': '/tmp/unused-fixture.sock'}, emit=messages.append)
    supervisor.identifier, supervisor.original_model = SID, current
    models = [{'model': 'reviewed-first', 'max_tokens': 128},
              {'model': 'reviewed-second', 'max_tokens': 512}]
    broker = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(billing_host, 'Broker', lambda: broker)
    monkeypatch.setattr(supervisor, 'v_options', AsyncMock(return_value={
        'models': models, 'balance_v': balance, 'wallet_billing': True,
        'provider': 'openai' if host == 'codex' else 'anthropic'}))
    launch = AsyncMock(return_value=0)
    monkeypatch.setattr(native, 'launch', launch)
    def prompt(label, **kwargs):
        prompts.append(label)
        assert label == 'Model'
        assert kwargs['type'].max == 2
        return 2
    monkeypatch.setattr(click, 'prompt', prompt)

    assert await supervisor.choose_v() == 0
    assert launch.await_count == 1
    expected = ('reviewed-second', 512) if current == 'reviewed-second' else ('reviewed-first', 128)
    assert launch.await_args.args == (host, expected[0], Decimal(ceiling), expected[1])
    kwargs = launch.await_args.kwargs
    assert kwargs['resume_provider'] == ('openai' if host == 'codex' else None)
    assert kwargs['broker'] is broker and kwargs['compact'] is True
    assert kwargs['host_args'] == (['resume', SID] if host == 'codex' else ['--resume', SID])
    assert kwargs['local_tools_only'] is (host == 'codex')
    assert kwargs['control_environment'] == {
        'OPENVEGAS_SWITCH_SOCKET': '/tmp/unused-fixture.sock',
        'OPENVEGAS_SWITCH_TOKEN': supervisor.token,
    }
    assert prompts == []
    assert kwargs['yes'] is True and kwargs['wallet_mode'] is True
    if current == 'unavailable-original':
        assert 'unavailable-original is not enabled for $V on this backend.' in messages
    assert not supervisor.v_child_started and supervisor.pending == 'host'
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('balance', ['0', '0.0000009', '-1', 'NaN', 'Infinity'])
async def test_unspendable_wallet_never_prompts_or_launches(tmp_path, monkeypatch, balance):
    supervisor = billing_host.Supervisor('claude', '/unused', [], {'HOME': str(tmp_path)})
    supervisor.identifier = SID
    broker = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(billing_host, 'Broker', lambda: broker)
    monkeypatch.setattr(supervisor, 'v_options', AsyncMock(return_value={
        'models': [{'model': 'reviewed-model', 'max_tokens': 128}],
        'provider': 'anthropic', 'balance_v': balance, 'wallet_billing': True}))
    launch = AsyncMock()
    monkeypatch.setattr(native, 'launch', launch)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid wallet reached an interactive prompt')
    monkeypatch.setattr(click, 'prompt', forbidden)
    monkeypatch.setattr(click, 'confirm', forbidden)
    with pytest.raises(IntegrationError):
        await supervisor.choose_v()
    launch.assert_not_awaited()
    broker.close.assert_awaited_once()
    assert supervisor.mode == 'host' and not supervisor.v_child_started
