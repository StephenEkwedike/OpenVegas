"""No-spend availability checks; neither a browser nor a native model is driven."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openvegas.integrations import billing_host, local_controls
from openvegas.integrations.broker import Broker
from openvegas.integrations.credentials import IntegrationError

SID = '01a17668-079e-7e90-b434-58f63208e39f'


class Writer:
    def write(self, value):
        self.reply = json.loads(value)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


async def command(supervisor, action='v'):
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(json.dumps({'host': supervisor.host, 'token': supervisor.token,
        'session_id': SID, 'action': action}).encode() + b'\n')
    await supervisor.handle(reader, writer)
    return writer.reply


def supervisor_at(tmp_path, host):
    supervisor = billing_host.Supervisor(host, '/never-execute', [], {'HOME': str(tmp_path)})
    supervisor.identifier = SID
    supervisor.child = SimpleNamespace(returncode=None, terminate=AsyncMock(), kill=AsyncMock())
    return supervisor


def options(host):
    return {'host': host, 'provider': 'openai' if host == 'codex' else 'anthropic',
            'wallet_billing': True, 'balance_v': '500', 'models': [{'model': 'reviewed-model', 'max_tokens': 128}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['codex', 'claude'])
@pytest.mark.parametrize('failure', ['disabled', 'offline', 'signed_out', 'expired',
    'empty_wallet', 'missing_models', 'wrong_route', 'bad_model', 'bad_budget', 'bad_tokens', 'legacy_backend'])
async def test_rejected_switch_keeps_owned_host_and_billing(tmp_path, monkeypatch, host, failure):
    supervisor = supervisor_at(tmp_path, host)
    calls, brokers = [], []
    def endpoint(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.path == '/integrations/native/options'
        assert request.url.params['host'] == host
        if failure == 'disabled':
            return httpx.Response(503, json={'detail': '$V-funded native inference is not enabled on this backend.'})
        if failure == 'offline':
            raise httpx.ConnectError('fixture offline')
        if failure == 'expired':
            return httpx.Response(401)
        data = options(host)
        if failure == 'empty_wallet': data['balance_v'] = '0'
        if failure == 'bad_budget': data['balance_v'] = 'NaN'
        if failure == 'missing_models': data['models'] = []
        if failure == 'wrong_route': data['host'] = 'other'
        if failure == 'bad_model': data['models'][0]['model'] = '\x1b[secret'
        if failure == 'bad_tokens': data['models'][0]['max_tokens'] = True
        if failure == 'legacy_backend': data.pop('wallet_billing')
        return httpx.Response(200, json=data)
    def make_broker():
        broker = Broker(backend='https://example.invalid', transport=httpx.MockTransport(endpoint))
        broker.credentials = SimpleNamespace(load=lambda: None if failure == 'signed_out' else 'ov_device_fixture')
        brokers.append(broker)
        return broker
    monkeypatch.setattr(billing_host, 'Broker', make_broker)
    result = await command(supervisor)
    assert result['ok'] is False
    assert supervisor.mode == 'host' and supervisor.pending is None
    assert supervisor.identifier == SID and not supervisor.changed.is_set() and not supervisor.checking_v
    supervisor.child.terminate.assert_not_called()
    supervisor.child.kill.assert_not_called()
    assert all(b.http.is_closed for b in brokers)
    assert len(calls) == (0 if failure == 'signed_out' else 1)
    assert 'secret' not in result['message']
    if failure == 'disabled':
        assert '$V inference is disabled' in result['message']
        assert 'Native integrations' not in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['codex', 'claude'])
async def test_timeout_leaves_host_alive_and_releases_preflight(tmp_path, monkeypatch, host):
    supervisor = supervisor_at(tmp_path, host)
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: 'ov_device_fixture'),
        request=AsyncMock(side_effect=lambda *a, **k: None), close=AsyncMock())
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    broker.request.side_effect = slow
    monkeypatch.setattr(billing_host, 'Broker', lambda: broker)
    result = await asyncio.wait_for(command(supervisor), 2.9)
    assert not result['ok'] and 'timed out' in result['message']
    assert supervisor.pending is None and not supervisor.changed.is_set() and not supervisor.checking_v
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,args', [('custom', []),
    ('openai', ['--sandbox', 'read-only']), ('openai', ['--ask-for-approval', 'on-request']),
    ('openai', ['--add-dir', '/fixture/extra'])])
async def test_unsupported_remote_policy_keeps_host_alive_before_billing_lookup(tmp_path, monkeypatch, provider, args):
    supervisor = supervisor_at(tmp_path, 'codex')
    supervisor.original_provider, supervisor.args = provider, args
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: pytest.fail('No credential read expected')),
                             request=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(billing_host, 'Broker', lambda: broker)
    result = await command(supervisor)
    assert result['ok'] is False
    assert supervisor.mode == 'host' and supervisor.pending is None
    assert not supervisor.changed.is_set()
    supervisor.child.terminate.assert_not_called()
    supervisor.child.kill.assert_not_called()
    broker.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_check_closes_connection_without_switch(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path, 'codex')
    waiting = asyncio.Event()
    async def read_options(*args):
        waiting.set()
        await asyncio.sleep(10)
    broker = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(billing_host, 'Broker', lambda: broker)
    monkeypatch.setattr(supervisor, 'v_options', read_options)
    task = asyncio.create_task(command(supervisor))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not supervisor.checking_v and supervisor.pending is None
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_is_bounded_and_cannot_authorize_an_exited_host(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path, 'codex')
    async def close():
        supervisor.child.returncode = 0
        await asyncio.sleep(10)
    broker = SimpleNamespace(close=close)
    monkeypatch.setattr(billing_host, 'Broker', lambda: broker)
    monkeypatch.setattr(supervisor, 'v_options', AsyncMock(return_value=options('codex')))
    result = await asyncio.wait_for(command(supervisor), 1)
    assert not result['ok'] and 'ended' in result['message']
    assert not supervisor.checking_v and supervisor.pending is None


@pytest.mark.asyncio
async def test_concurrent_checks_and_child_exit_never_queue_stale_switch(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path, 'codex')
    waiting, release = asyncio.Event(), asyncio.Event()
    async def read_options(*args):
        waiting.set()
        await release.wait()
        return options('codex')
    monkeypatch.setattr(supervisor, 'v_options', read_options)
    monkeypatch.setattr(billing_host, 'Broker', lambda: SimpleNamespace(close=AsyncMock()))
    first = asyncio.create_task(command(supervisor))
    await waiting.wait()
    second = await command(supervisor)
    assert not second['ok'] and supervisor.pending is None
    supervisor.child.returncode = 0
    release.set()
    assert not (await first)['ok']
    assert not supervisor.changed.is_set() and not supervisor.checking_v


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['codex', 'claude'])
async def test_host_restoration_does_not_require_backend(tmp_path, monkeypatch, host):
    supervisor = supervisor_at(tmp_path, host)
    supervisor.mode = 'v'
    check = AsyncMock(side_effect=AssertionError('Host billing must not require backend'))
    monkeypatch.setattr(supervisor, 'preflight_v', check)
    result = await command(supervisor, 'host')
    assert result['ok'] and supervisor.pending == 'host'
    check.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/native/options?host=codex', '/native/quote', '/native/sessions', '/native/session'])
@pytest.mark.parametrize('detail', [None, {'private': 'NEVER_ECHO'}, 'NEVER_ECHO'])
async def test_503_never_echoes_backend_payload_or_mislabels_integrations(path, detail):
    broker = Broker(backend='https://example.invalid', transport=httpx.MockTransport(
        lambda _: httpx.Response(503, json={'detail': detail})))
    try:
        with pytest.raises(IntegrationError) as error:
            await broker.request('GET', path)
        assert error.value.status == 503
        assert '$V billing' in str(error.value) and 'NEVER_ECHO' not in str(error.value)
        assert 'Native integrations' not in str(error.value)
    finally:
        await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', [True, False])
async def test_local_menu_explains_failure_before_closing(monkeypatch, unavailable):
    messages, events = [], []
    answers = iter(['use_v', 'quit'])
    class View:
        def __init__(self, *args): pass
        async def ask(self, message, schema):
            messages.append(message)
            return SimpleNamespace(choice=next(answers))
        async def send(self, **packet): events.append(packet['kind'])
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"closed":true}\n')
    monkeypatch.setattr(local_controls, 'connect', AsyncMock(return_value=(reader, Writer())))
    monkeypatch.setattr(local_controls, 'TerminalView', View)
    async def check():
        assert events == []
        if unavailable: raise IntegrationError('$V inference is disabled on this server.')
    result = await local_controls.controls('host', menu=True, check_v=check)
    assert result == ('quit' if unavailable else 'use_v')
    assert len(messages) == (2 if unavailable else 1) and events == ['done']
    if unavailable: assert '$V inference is disabled' in messages[-1]
