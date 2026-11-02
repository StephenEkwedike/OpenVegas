"""Offline contracts only: no installed Codex, provider, auth store or network."""
import asyncio
import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from openvegas.integrations import codex_billing_route as routing
from openvegas.integrations import native
from openvegas.integrations.broker import Broker
from openvegas.integrations.credentials import IntegrationError

SID = '01a17668-079e-7e90-b434-58f63208e39f'
TOKEN = 'ov_native_route_contract_fixture_only'
BACKEND = 'http://127.0.0.1:12345'
MODEL = 'reviewed-exact-model'
PRICING = {'fingerprint': 'a' * 64, 'v_per_usd': '100', 'multiplier': '1.4'}


@pytest.mark.parametrize('provider', ['openai', 'ollama', 'lmstudio'])
def test_builtin_provider_identity_cannot_be_replaced_with_custom_table(provider):
    with pytest.raises(IntegrationError, match='Built-in providers'):
        native.host_argv('/unused', 'codex', BACKEND, MODEL, ['resume', SID],
                         provider_identity=provider)


def test_custom_original_provider_is_retained_without_replaying_prompt():
    argv = native.host_argv('/unused', 'codex', BACKEND, MODEL,
                            ['--sandbox', 'read-only', 'resume', SID],
                            provider_identity='original', local_tools_only=True)
    assert 'model_provider="original"' in argv
    assert any(arg.startswith('model_providers.original=') for arg in argv)
    assert not any('model_providers.openai=' in arg or 'openvegas_v' in arg for arg in argv)
    assert argv.count(SID) == 1 and argv.count('resume') == 1
    assert argv[argv.index('--model') + 1] == MODEL
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert TOKEN not in str(argv)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['exit', 'decline', 'bootstrap_error'])
async def test_native_launch_uses_private_route_for_original_openai_and_revokes(
    tmp_path, monkeypatch, outcome,
):
    calls, events, messages = [], [], []
    for key in ('OPENVEGAS_HOST_HOME', 'OPENVEGAS_HOST_CODEX_HOME'):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setattr(native, 'detect', lambda _: {'executable': '/fixture/codex'})
    def endpoint(request):
        data = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, data))
        events.append(request.url.path.rsplit('/', 1)[-1] + ':' + request.method)
        if request.url.path.endswith('/quote'):
            return httpx.Response(200, json={**data, 'pricing': PRICING})
        if request.url.path.endswith('/sessions'):
            assert data == {'host': 'codex', 'provider': 'openai', 'model': MODEL,
                            'max_tokens': 128, 'limit_v': '500', 'confirmed': True,
                            'pricing_fingerprint': PRICING['fingerprint']}
            return httpx.Response(200, json={'token': TOKEN, 'session_id': 'fixture',
                'expires_in': 60, 'budget_v': data['limit_v'], 'pricing': PRICING})
        return httpx.Response(200, json={'active': True, 'session_id': 'fixture'})
    broker = Broker(backend=BACKEND, transport=httpx.MockTransport(endpoint))
    broker.credentials = SimpleNamespace(load=lambda: 'ov_device_contract_fixture')
    control = {'OPENVEGAS_SWITCH_SOCKET': '/tmp/fixture-controls.sock',
               'OPENVEGAS_SWITCH_TOKEN': 'fixture-switch-only'}
    @asynccontextmanager
    async def route(executable, backend, model, env, **kwargs):
        assert (executable, backend, model) == ('/fixture/codex', BACKEND, MODEL)
        assert env['OPENVEGAS_NATIVE_TOKEN'] == TOKEN
        assert env['CODEX_HOME'] == str(tmp_path)
        assert all(env[key] == value for key, value in control.items())
        assert kwargs == {'local_tools_only': True}
        events.append('route-enter')
        if outcome == 'bootstrap_error':
            raise IntegrationError('Synthetic route bootstrap failed')
        try:
            yield 'unix:///tmp/fixture-private.sock'
        finally:
            events.append('route-exit')
    monkeypatch.setattr(routing, 'route', route)
    def forbidden(*args, **kwargs):
        pytest.fail('Built-in openai must not reach custom provider-table generation')
    monkeypatch.setattr(native, 'host_argv', forbidden)
    monkeypatch.setattr(native.click, 'prompt', forbidden)
    confirmations = []
    def confirm(label, **kwargs):
        confirmations.append(label)
        assert label == f'Use $V with {MODEL}?' and kwargs['default'] is False
        return outcome != 'decline'
    monkeypatch.setattr(native.click, 'confirm', confirm)
    async def runner(argv, env):
        events.append('runner')
        assert argv == ['/fixture/codex', '--sandbox', 'read-only', 'resume', SID,
                        '--remote', 'unix:///tmp/fixture-private.sock', '--model', MODEL]
        assert TOKEN not in str(argv)
        assert all(env[key] == value for key, value in control.items())
        return 7
    run = AsyncMock(side_effect=runner)
    try:
        invocation = native.launch('codex', MODEL, '500', 128, provider='openai',
            broker=broker, host_args=['--sandbox', 'read-only', 'resume', SID],
            resume_provider='openai', compact=True, local_tools_only=True,
            control_environment=control, runner=run, emit=messages.append)
        if outcome == 'bootstrap_error':
            with pytest.raises(IntegrationError, match='Synthetic route bootstrap'):
                await invocation
        else:
            assert await invocation == (0 if outcome == 'decline' else 7)
    finally:
        await broker.close()
    assert len(confirmations) == 1
    assert run.await_count == int(outcome == 'exit')
    assert TOKEN not in '\n'.join(messages)
    assert any(MODEL in message and 'no purchases or automatic top-ups' in message
               for message in messages)
    assert not any('Session limit' in message for message in messages)
    if outcome == 'decline':
        assert events == ['quote:POST']
    else:
        expected = ['quote:POST', 'sessions:POST', 'session:GET', 'route-enter']
        if outcome == 'exit':
            expected += ['runner', 'route-exit']
        assert events == [*expected, 'session:DELETE']


@pytest.fixture
def route_fixture(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    auth = home / 'auth.json'
    auth.write_text('{"synthetic":"unchanged"}')
    before = auth.read_bytes()
    env = {'HOME': str(home), 'CODEX_HOME': str(home), 'OPENVEGAS_NATIVE_TOKEN': TOKEN,
           'OPENVEGAS_SWITCH_SOCKET': '/tmp/fixture-controls.sock',
           'OPENVEGAS_SWITCH_TOKEN': 'fixture-switch-only'}
    config = {'cli_auth_credentials_store': 'ephemeral', 'model_provider': 'openai',
              'openai_base_url': BACKEND + '/integrations/native/v1'}
    state = SimpleNamespace(config=config, account={'type': 'apiKey'}, error_method=None,
                            early_exit=False, argv=None, socket=None)
    sent, replies = [], []
    async def send(raw):
        value = json.loads(raw)
        sent.append(value)
        if 'id' not in value:
            return
        method = value['method']
        replies.append(json.dumps({'method': 'fixture/notification'}))
        if method == state.error_method:
            replies.append(json.dumps({'id': value['id'], 'error': {'message': TOKEN}}))
        else:
            result = ({'config': state.config} if method == 'config/read' else
                      {'account': state.account} if method == 'account/read' else {})
            replies.append(json.dumps({'id': value['id'], 'result': result}))
    async def receive():
        assert replies, 'Unexpected native RPC beyond the authentication contract'
        return replies.pop(0)
    connection = SimpleNamespace(send=AsyncMock(side_effect=send),
                                 recv=AsyncMock(side_effect=receive))
    @asynccontextmanager
    async def connect(path, **kwargs):
        assert path == str(state.socket)
        assert kwargs == {'open_timeout': 3, 'max_size': 1024 * 1024}
        yield connection
    monkeypatch.setattr(routing, 'unix_connect', connect)
    process = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0), kill=Mock())
    def terminate():
        process.returncode = 0
    process.terminate = Mock(side_effect=terminate)
    async def spawn(*argv, **kwargs):
        state.argv = argv
        assert kwargs['env'] == env
        assert all(kwargs[key] == asyncio.subprocess.DEVNULL
                   for key in ('stdin', 'stdout', 'stderr'))
        state.socket = Path(argv[-1].removeprefix('unix://'))
        if state.early_exit:
            process.returncode = 9
        else:
            state.socket.touch()  # A readiness marker, not a socket or native app-server.
        return process
    create = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(routing.asyncio, 'create_subprocess_exec', create)
    yield SimpleNamespace(env=env, state=state, process=process, sent=sent, create=create,
                          auth=auth, before=before)
    assert auth.read_bytes() == before
    assert list(home.iterdir()) == [auth]


@pytest.mark.asyncio
async def test_owned_route_checks_isolation_before_memory_login_and_cleans_up(route_fixture):
    rig = route_fixture
    original_env = dict(rig.env)
    async with routing.route('/fixture/codex', BACKEND, MODEL, rig.env) as endpoint:
        assert endpoint == 'unix://' + str(rig.state.socket)
        assert stat.S_IMODE(rig.state.socket.stat().st_mode) == 0o600
        assert stat.S_IMODE(rig.state.socket.parent.stat().st_mode) == 0o700
        assert rig.state.argv[-3:] == ('app-server', '--listen', endpoint)
        for control in ('model_provider="openai"', 'cli_auth_credentials_store="ephemeral"',
                        f'model="{MODEL}"', f'review_model="{MODEL}"', 'web_search="disabled"'):
            assert control in rig.state.argv
        assert 'openai_base_url=' + json.dumps(BACKEND + '/integrations/native/v1') in rig.state.argv
        assert not any(arg.startswith('model_providers.openai=') for arg in rig.state.argv)
        assert TOKEN not in str(rig.state.argv)
        assert [message['method'] for message in rig.sent] == [
            'initialize', 'initialized', 'config/read', 'account/login/start', 'account/read']
        assert rig.sent[3]['params'] == {'type': 'apiKey', 'apiKey': TOKEN}
        assert rig.sent[4]['params'] == {'refreshToken': False}
        rig.process.terminate.assert_not_called()
    assert rig.env == original_env
    rig.process.terminate.assert_called_once()
    rig.process.wait.assert_awaited_once()
    rig.process.kill.assert_not_called()
    assert not rig.state.socket.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(('key', 'value'), [
    ('cli_auth_credentials_store', 'file'), ('model_provider', 'other'),
    ('openai_base_url', 'https://untrusted.invalid/v1'),
])
async def test_route_refuses_effective_config_mismatch_before_sending_grant(route_fixture, key, value):
    rig = route_fixture
    rig.state.config[key] = value
    with pytest.raises(IntegrationError, match='cannot isolate') as error:
        async with routing.route('/fixture/codex', BACKEND, MODEL, rig.env):
            pytest.fail('Mismatched effective configuration reached the TUI')
    assert TOKEN not in str(error.value)
    assert 'account/login/start' not in [message['method'] for message in rig.sent]
    assert TOKEN not in json.dumps(rig.sent)
    rig.process.terminate.assert_called_once()
    assert not rig.state.socket.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['null_account', 'wrong_account', 'rpc_error', 'early_exit'])
async def test_route_bootstrap_failure_is_sanitized_and_stops_owned_server(route_fixture, failure):
    rig = route_fixture
    if failure == 'null_account':
        rig.state.account = None
    elif failure == 'wrong_account':
        rig.state.account = {'type': 'chatgpt'}
    elif failure == 'rpc_error':
        rig.state.error_method = 'account/login/start'
    else:
        rig.state.early_exit = True
    with pytest.raises(IntegrationError) as error:
        async with routing.route('/fixture/codex', BACKEND, MODEL, rig.env):
            pytest.fail('Failed bootstrap reached the native child')
    assert TOKEN not in str(error.value)
    assert not rig.state.socket.parent.exists()
    assert rig.process.terminate.call_count == int(failure != 'early_exit')
    assert not any(message['method'].startswith(('thread/', 'turn/')) for message in rig.sent)


@pytest.mark.asyncio
async def test_route_cancellation_stops_owned_server(route_fixture):
    rig = route_fixture
    with pytest.raises(asyncio.CancelledError):
        async with routing.route('/fixture/codex', BACKEND, MODEL, rig.env):
            raise asyncio.CancelledError
    rig.process.terminate.assert_called_once()
    rig.process.wait.assert_awaited_once()
    assert not rig.state.socket.parent.exists()


@pytest.mark.asyncio
async def test_missing_grant_never_starts_process_or_opens_connection(route_fixture):
    rig = route_fixture
    rig.env.pop('OPENVEGAS_NATIVE_TOKEN')
    with pytest.raises(IntegrationError, match='Missing OpenVegas spending authorization'):
        async with routing.route('/fixture/codex', BACKEND, MODEL, rig.env):
            pytest.fail('Route started without a grant')
    rig.create.assert_not_awaited()
    assert not rig.sent


@pytest.mark.asyncio
async def test_owned_server_cleanup_escalates_only_after_timeout():
    process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(),
                              wait=AsyncMock(side_effect=[TimeoutError(), 0]))
    await routing.stop(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.await_count == 2
