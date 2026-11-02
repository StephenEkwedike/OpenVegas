"""Owned-process billing lifecycle, synthetic backend, no supplier requests."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import click
import httpx
import pytest

from openvegas.integrations import billing_host, native
from openvegas.integrations.broker import Broker

SID = '01a17668-079e-7e90-b434-58f63208e39f'
TOKEN = 'ov_native_supervisor_fixture_only'
PRICING = {'fingerprint': 'a' * 64, 'v_per_usd': '100', 'multiplier': '1.4'}


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize('scenario', ['approved', 'declined', 'model_cancel', 'grant_error', 'v_exit'])
async def test_same_terminal_owned_process_switch_lifecycle(tmp_path, monkeypatch, host, scenario):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text('model_provider="original"\n')
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
def send(action):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(os.environ['OPENVEGAS_SWITCH_SOCKET'])
        s.sendall(json.dumps({'token': os.environ['OPENVEGAS_SWITCH_TOKEN'],
            'action': action, 'session_id': FIXTURE_SID, 'model': 'original-model'}).encode() + b'\n')
        assert json.loads(s.recv(8192))['ok']
send('register')
if n == 0 or (v and FIXTURE_SCENARIO != 'v_exit'):
    send('v' if n == 0 else 'host')
    time.sleep(30)
sys.exit(7 if v else 0)
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
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('OPENVEGAS_NATIVE_TOKEN', raising=False)
    calls = []
    provider = 'anthropic' if host == 'claude' else 'openai'

    def endpoint(req):
        data = json.loads(req.content) if req.content else {}
        calls.append((req.method, req.url.path))
        if req.url.path.endswith('/options'):
            return httpx.Response(200, json={'host': host, 'provider': provider,
                'balance_v': '500', 'models': [{'model': 'exact-model', 'max_tokens': 128}]})
        if req.url.path.endswith('/quote'):
            return httpx.Response(200, json={**data, 'pricing': PRICING})
        if req.url.path.endswith('/sessions'):
            assert data['provider'] == provider and data['confirmed'] is True
            if scenario == 'grant_error':
                return httpx.Response(503, json={'detail': 'fixture unavailable'})
            return httpx.Response(200, json={'token': TOKEN, 'session_id': 'fixture',
                'expires_in': 60, 'budget_v': data['limit_v'], 'pricing': PRICING})
        if req.method == 'DELETE':
            with events.open('a') as f:
                f.write(json.dumps({'kind': 'revoked'}) + '\n')
        return httpx.Response(200, json={'active': True, 'session_id': 'fixture'})

    def make_broker():
        broker = Broker(backend='http://127.0.0.1:12345', transport=httpx.MockTransport(endpoint))
        broker.credentials = SimpleNamespace(load=lambda: 'ov_device_fixture')
        return broker
    monkeypatch.setattr(billing_host, 'Broker', make_broker)
    monkeypatch.setattr(native, 'detect', lambda _: {'executable': str(stub), 'version': 'fixture'})
    def prompt(label, **kwargs):
        if scenario == 'model_cancel':
            raise click.Abort()
        return kwargs['type'].convert('1', None, None)
    monkeypatch.setattr(click, 'prompt', prompt)
    monkeypatch.setattr(click, 'confirm', lambda *a, **kw: scenario != 'declined')
    messages = []
    permission = ['--permission-mode', 'default'] if host == 'claude' else ['--sandbox', 'read-only']
    env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home)}
    supervisor = billing_host.Supervisor(host, str(stub), permission, env, emit=messages.append)
    code = await asyncio.wait_for(supervisor.run(), 15)
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    children = [row for row in rows if row['kind'] == 'child']
    activated = scenario in ('approved', 'v_exit')
    assert [c['v'] for c in children] == ([False, True, False] if scenario == 'approved' else
        [False, True] if scenario == 'v_exit' else [False, False])
    assert code == (7 if scenario == 'v_exit' else 0)
    assert auth.read_bytes() == before
    for child in children:
        assert child['cwd'] == str(tmp_path) and child['connection']
        assert child['personal_key'] is (not child['v'])
        assert all(arg in child['argv'] for arg in permission)
        assert TOKEN not in str(child)
    assert all(SID in child['argv'] for child in children[1:])
    if scenario != 'v_exit':
        assert 'original-model' in children[-1]['argv']
    if scenario == 'approved':
        assert [row['kind'] for row in rows] == ['child', 'child', 'revoked', 'child']
    assert sum(method == 'DELETE' for method, _ in calls) == int(activated)
    if scenario in ('declined', 'model_cancel'):
        assert not any(path.endswith('/sessions') for _, path in calls)
    assert TOKEN not in '\n'.join(messages)
    assert not os.path.exists(supervisor.env['OPENVEGAS_SWITCH_SOCKET'])


@pytest.mark.asyncio
async def test_cancel_stops_only_owned_child_and_removes_socket(tmp_path):
    supervisor = billing_host.Supervisor('claude', sys.executable,
        ['-c', 'import time; time.sleep(60)'], {'HOME': str(tmp_path)})
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
