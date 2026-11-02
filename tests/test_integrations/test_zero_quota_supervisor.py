"""Local control dispatch without a provider, wallet mutation or real host."""
import asyncio
import json
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openvegas.integrations import billing_host, local_controls, use
from openvegas.integrations.credentials import IntegrationError

SID = '01a17668-079e-7e90-b434-58f63208e39f'
OTHER = '01a17668-079e-7e90-b434-58f63208e39e'


def supervisor(tmp_path, host):
    value = billing_host.Supervisor(host, '/fixture', [], {
        'HOME': str(tmp_path), 'OPENVEGAS_TERMINAL_SOCKET': '/tmp/fixture',
        'OPENVEGAS_TERMINAL_TOKEN': 'fixture',
    })
    value.child = SimpleNamespace(returncode=None)
    value.quota.values = [{'remaining': 0, 'reset': time.time() + 3600}]
    return value


async def send(value, **changes):
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({
        'token': value.token, 'host': value.host, 'action': 'play',
        'session_id': SID, 'model': 'original-model', **changes,
    }).encode() + b'\n')
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
    await value.handle(reader, writer)
    return json.loads(writer.write.call_args.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['codex', 'claude'])
async def test_first_play_after_trust_connects_identity_without_billing(tmp_path, monkeypatch, host):
    value = supervisor(tmp_path, host)
    value.open_controls = Mock()
    value.preflight_v = AsyncMock(side_effect=AssertionError('No billing check for play'))
    reply = await send(value, game='roulette')
    assert reply['ok'] is True
    assert value.identifier == SID and value.original_model == 'original-model'
    assert value.mode == 'host' and value.pending is None
    value.open_controls.assert_called_once_with(game='roulette', delay=.75)
    value.preflight_v.assert_not_called()
    assert value.quota.remaining() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'token': 'incorrect'}, {'host': 'other'}, {'game': 'other'},
])
async def test_invalid_play_never_binds_session_or_opens_game(tmp_path, changes):
    value = supervisor(tmp_path, 'codex')
    value.open_controls = Mock()
    assert (await send(value, **changes))['ok'] is False
    assert value.identifier is None
    value.open_controls.assert_not_called()


@pytest.mark.asyncio
async def test_other_conversation_cannot_replace_active_identity(tmp_path):
    value = supervisor(tmp_path, 'codex')
    value.identifier, value.original_model = OTHER, 'keep-model'
    value.open_controls = Mock()
    assert (await send(value))['ok'] is False
    assert (value.identifier, value.original_model) == (OTHER, 'keep-model')
    value.open_controls.assert_not_called()


@pytest.mark.asyncio
async def test_failed_game_open_does_not_bind_session(tmp_path):
    value = supervisor(tmp_path, 'codex')
    value.open_controls = Mock(side_effect=IntegrationError('Unavailable relay'))
    assert (await send(value))['ok'] is False
    assert value.identifier is None


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['register', 'play'])
async def test_bad_optional_transcript_never_disagrees_with_dispatch(tmp_path, action):
    value = supervisor(tmp_path, 'codex')
    value.run_controls = AsyncMock(return_value=None)
    path = str(tmp_path / '.codex' / 'sessions') + '/bad\0-' + SID + '.jsonl'
    reply = await send(value, action=action, transcript_path=path)
    assert reply['ok'] is True and value.identifier == SID
    assert value.quota.path is None and value.pending is None
    if action == 'play':
        await value.local_task
        value.run_controls.assert_awaited_once()
    else:
        value.run_controls.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize('mode', ['host', 'v'])
@pytest.mark.parametrize('setting', [None, '0', '1'])
async def test_title_default_is_child_scoped_and_preserves_explicit_setting(tmp_path, monkeypatch, host, mode, setting):
    value = supervisor(tmp_path, host)
    value.mode = mode
    value.env['OPENVEGAS_LOCAL_CONTROLS'] = '0'
    env = dict(value.env)
    key = 'CLAUDE_CODE_DISABLE_TERMINAL_TITLE'
    if setting is not None:
        env[key] = setting
    original = dict(env)
    child = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(billing_host.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(billing_host.os, 'isatty', lambda _: False)
    assert await value.run_child(['/synthetic/native-host'], env) == 0
    passed = spawn.call_args.kwargs['env']
    assert env == original and key not in value.env
    assert passed.get(key) == (setting if setting is not None else '1' if host == 'claude' else None)
    assert {k:v for k,v in passed.items() if k != key} == {k:v for k,v in original.items() if k != key}


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['codex', 'claude'])
async def test_local_switch_validates_connection_before_network(tmp_path, host):
    value = supervisor(tmp_path, host)
    value.preflight_v = AsyncMock()
    with pytest.raises(IntegrationError, match='Games remain available'):
        await value.preflight_local_v()
    value.preflight_v.assert_not_called()
    value.identifier = SID
    await value.preflight_local_v()
    value.preflight_v.assert_awaited_once()
    assert value.pending is None and value.mode == 'host'


@pytest.mark.asyncio
async def test_local_switch_rejects_unresumable_prompt_before_network(tmp_path):
    value = supervisor(tmp_path, 'codex')
    value.identifier, value.args = SID, ['a private initial task']
    value.preflight_v = AsyncMock()
    with pytest.raises(IntegrationError, match='task prompt'):
        await value.preflight_local_v()
    value.preflight_v.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('host,prefix', [('codex', '$'), ('claude', '/')])
async def test_zero_quota_hook_to_owned_supervisor_without_provider(tmp_path, monkeypatch, host, prefix):
    value = supervisor(tmp_path, host)
    calls = AsyncMock(return_value='quit')
    monkeypatch.setattr(local_controls, 'controls', calls)
    monkeypatch.setattr(billing_host, 'Broker', Mock(side_effect=AssertionError('No billing broker')))
    with tempfile.TemporaryDirectory(prefix='ov-zero-', dir='/tmp') as directory:
        socket = directory + '/switch.sock'
        server = await asyncio.start_unix_server(value.handle, socket)
        monkeypatch.setenv(use.SOCKET, socket)
        monkeypatch.setenv(use.TOKEN, value.token)
        try:
            result = await use.hook(host, {'hook_event_name': 'UserPromptSubmit',
                'prompt': prefix + 'openvegas play', 'session_id': SID, 'model': 'original-model'})
            assert result['continue'] is False and result['suppressOutput'] is True
            await asyncio.wait_for(value.local_task, 2)
            calls.assert_awaited_once()
            assert value.identifier == SID and value.mode == 'host' and value.pending is None
            assert value.quota.remaining() == 0
        finally:
            server.close()
            await server.wait_closed()
