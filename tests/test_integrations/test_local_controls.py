import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.integrations import billing_host, local_controls, use
from openvegas.integrations.credentials import IntegrationError


@pytest.mark.asyncio
@pytest.mark.parametrize('host,prefix', [('codex', '$'), ('claude', '/')])
@pytest.mark.parametrize('game', ['', 'roulette', 'poker', 'horse'])
async def test_play_blocks_model_and_only_passes_game(monkeypatch, host, prefix, game):
    request = AsyncMock(return_value='Local menu')
    monkeypatch.setattr(use, 'request', request)
    reply = await use.hook(host, {'hook_event_name': 'UserPromptSubmit',
                                 'prompt': prefix + 'openvegas:openvegas play ' + game})
    assert reply == {'continue': False, 'stopReason': 'Local menu', 'suppressOutput': True}
    assert request.call_args.args == (host, 'play')
    assert request.call_args.kwargs['game'] == (game or None)


@pytest.mark.asyncio
async def test_subagent_never_opens_human_controls(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(use, 'request', request)
    assert await use.hook('codex', {'hook_event_name': 'UserPromptSubmit', 'agent_id': 'agent',
                                   'prompt': '$openvegas play'}) == {}
    request.assert_not_called()


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('size', [1, 2, 1000])
def test_menu_chord_split_input_and_paste_preservation(enabled, size):
    keys = local_controls.LocalKeys()
    data = b'hello\x1b[A' + keys.START + b'pasted' + keys.MENU + keys.END + keys.MENU + b'end'
    output, events = b'', 0
    for at in range(0, len(data), size):
        value, opened = keys.feed(data[at:at + size], enabled)
        output += value
        events += int(opened)
    assert output == (data.replace(keys.END + keys.MENU, keys.END) if enabled else data)
    assert events == int(enabled)
    assert keys.feed(b'\x1b', enabled) == (b'', False)
    assert keys.flush() == b'\x1b'


@pytest.mark.parametrize('enabled', [True, False])
def test_voice_keys_pass_through_and_unused_prefix_is_lossless(enabled):
    keys = local_controls.LocalKeys()
    for data in (b'\x1b[19~', b'\x1b[20~', b'/voice stop\r', b'\x18o', b'\x1dz'):
        output = b''
        for byte in data:
            value, opened = keys.feed(bytes([byte]), enabled)
            output += value
            assert not opened
        assert output + keys.flush() == data
    # Codex's leader must reach the host immediately, not after a timeout.
    assert keys.feed(b'\x18', enabled) == (b'\x18', False)
    assert keys.feed(b'\x1d', enabled) == ((b'', False) if enabled else (b'\x1d', False))
    assert keys.wait_seconds == (1.5 if enabled else .15)
    assert keys.flush() == (b'\x1d' if enabled else b'')


def test_chord_cannot_cross_disabled_controls():
    keys = local_controls.LocalKeys()
    assert keys.feed(b'\x1d', True) == (b'', False)
    assert keys.feed(b'o', False) == (keys.MENU, False)
    assert keys.feed(b'\x1d', False) == (b'\x1d', False)
    assert keys.feed(b'o', True) == (b'o', False)


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['claude', 'codex'])
async def test_local_controls_no_model_or_switch_and_single_flight(monkeypatch, tmp_path, host):
    gate = asyncio.Event()
    async def controls(*args, **kwargs):
        await gate.wait()
        return 'quit'
    call = AsyncMock(side_effect=controls)
    monkeypatch.setattr(local_controls, 'controls', call)
    supervisor = billing_host.Supervisor(host, '/fixture', [], {'HOME': str(tmp_path),
        'OPENVEGAS_TERMINAL_SOCKET': '/tmp/fixture', 'OPENVEGAS_TERMINAL_TOKEN': 'fixture'})
    supervisor.child = SimpleNamespace(returncode=None)
    supervisor.open_controls(game='poker')
    with pytest.raises(IntegrationError, match='already open'):
        supervisor.open_controls()
    await asyncio.sleep(.01)
    assert call.call_count == 1
    assert supervisor.pending is None and supervisor.identifier is None
    gate.set()
    await supervisor.local_task
    assert not supervisor.local_error and not supervisor.changed.is_set()


@pytest.mark.asyncio
async def test_unavailable_relay_never_opens_controls(tmp_path):
    supervisor = billing_host.Supervisor('codex', '/fixture', [], {'HOME': str(tmp_path)})
    supervisor.child = SimpleNamespace(returncode=None)
    with pytest.raises(IntegrationError, match='managed terminal'):
        supervisor.open_controls()
    assert supervisor.local_task is None


@pytest.mark.asyncio
async def test_control_failure_is_visible_not_retried(monkeypatch, tmp_path):
    call = AsyncMock(side_effect=IntegrationError('fixture failure'))
    monkeypatch.setattr(local_controls, 'controls', call)
    supervisor = billing_host.Supervisor('codex', '/fixture', [], {'HOME': str(tmp_path)})
    await supervisor.run_controls(menu=True, game=None, delay=0)
    assert supervisor.local_status()['local_error'] is True
    call.assert_awaited_once()
    assert supervisor.pending is None


@pytest.mark.asyncio
async def test_menu_cancel_closes_without_broker_or_game(monkeypatch):
    from openvegas.integrations import terminal_handoff
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":1,"answer":null}\n{"closed":true}\n')
    writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock())
    monkeypatch.setattr(local_controls, 'connect', AsyncMock(return_value=(reader, writer)))
    broker = AsyncMock(side_effect=AssertionError('No account access on cancel'))
    monkeypatch.setattr(local_controls, 'Broker', broker)
    assert await local_controls.controls('host', menu=True) == 'quit'
    broker.assert_not_called()
