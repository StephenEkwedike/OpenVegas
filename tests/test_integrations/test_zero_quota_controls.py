"""No real sockets, credentials, account writes, wagers or provider requests."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openvegas.integrations import billing_host, local_controls
from openvegas.integrations import terminal_wallet as wallet
from openvegas.integrations.credentials import IntegrationError


class Relay:
    def __init__(self, monkeypatch, handshake=None):
        self.reader = asyncio.StreamReader(limit=wallet.MAX_MESSAGE)
        self.feed(handshake if handshake is not None else {
            'wallet': 1, 'controls_shortcut': wallet.LOCAL_CONTROL_SHORTCUT})
        self.packets = []
        self.writer = SimpleNamespace(write=lambda raw: self.packets.append(json.loads(raw)),
            drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
        self.connect = AsyncMock(return_value=(self.reader, self.writer))
        monkeypatch.setattr(wallet, 'validate_socket', Mock())
        monkeypatch.setattr(wallet.asyncio, 'open_unix_connection', self.connect)
        self.env = {wallet.SOCKET_ENV: '/synthetic/relay.sock', wallet.TOKEN_ENV: 'synthetic-capability'}

    def feed(self, packet):
        self.reader.feed_data(json.dumps(packet).encode() + b'\n')

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(.001)


def fake_broker():
    return SimpleNamespace(credentials=object(), close=AsyncMock(),
                           request=AsyncMock(return_value={'balance': '0.000000'}))


async def cancel(task):
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('quota', [0, 5])
@pytest.mark.parametrize('failure,state', [
    ('broker_factory', 'unavailable'), ('account_factory', 'unavailable'),
    ('signed_out', 'login'), ('credentials', 'unavailable'),
    ('unauthorized', 'login'), ('forbidden', 'login'),
    ('unavailable', 'unavailable'), ('quota_429', 'unavailable'),
    ('timeout', 'unavailable'), ('malformed', 'unavailable'), ('zero', 'ready'),
])
async def test_private_controls_survive_wallet_failures(monkeypatch, failure, state, quota):
    relay = Relay(monkeypatch)
    broker = fake_broker()
    broker_factory = Mock(return_value=broker)
    account_factory = Mock(return_value=broker.credentials)
    credentials = SimpleNamespace(load=AsyncMock(return_value='synthetic-wallet-token'))
    if failure == 'broker_factory':
        broker_factory.side_effect = RuntimeError('private initialization detail')
    elif failure == 'account_factory':
        account_factory.side_effect = RuntimeError('private account detail')
    elif failure == 'signed_out':
        credentials.load.return_value = None
    elif failure == 'credentials':
        credentials.load.side_effect = RuntimeError('private credential detail')
    elif failure in {'unauthorized', 'forbidden', 'unavailable', 'quota_429'}:
        status = {'unauthorized': 401, 'forbidden': 403, 'unavailable': 503, 'quota_429': 429}[failure]
        broker.request.side_effect = IntegrationError('private backend detail', status)
    elif failure == 'timeout':
        broker.request.side_effect = TimeoutError('private timeout detail')
    elif failure == 'malformed':
        broker.request.return_value = {'balance': '\x1b[2J'}
    events, ready = [], asyncio.Event()
    task = asyncio.create_task(wallet.monitor(relay.env, 'host', ready=ready,
        broker_factory=broker_factory, account_factory=account_factory, credentials=credentials,
        interval=.005, control=events.append, status=lambda: {'controls': True, 'quota_remaining': quota}))
    try:
        await relay.until(lambda: len(relay.packets) >= 4)
        assert ready.is_set() and not task.done()
        assert relay.packets[1]['state'] == 'loading'
        for packet in relay.packets[1:]:
            assert packet['controls'] is True
            assert packet['controls_shortcut'] == wallet.LOCAL_CONTROL_SHORTCUT
            assert packet['quota_remaining'] == quota
            assert not packet['relay_restart_required']
        assert all(packet['state'] == state for packet in relay.packets[2:])
        if state != 'ready':
            assert all('balance' not in packet for packet in relay.packets[1:])
        relay.feed({'control': 'menu'})
        await relay.until(lambda: events)
        assert events == [{'control': 'menu'}]
        assert 'private' not in json.dumps(relay.packets)
        assert 'synthetic-wallet-token' not in json.dumps(relay.packets)
        assert all(call.args == ('GET', '/account') for call in broker.request.await_args_list)
        if failure in {'broker_factory', 'account_factory', 'signed_out', 'credentials'}:
            broker.request.assert_not_awaited()
        relay.connect.assert_awaited_once()
        if failure == 'broker_factory':
            assert broker_factory.call_count >= 2
        else:
            broker_factory.assert_called_once()
    finally:
        await cancel(task)
    relay.writer.close.assert_called_once()
    if failure != 'broker_factory':
        broker.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', ['credentials', 'request'])
async def test_pending_wallet_does_not_delay_handshake_or_controls(monkeypatch, pending):
    relay = Relay(monkeypatch)
    monkeypatch.setattr(wallet, 'REQUEST_SECONDS', .04)
    broker = fake_broker()
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    credentials = SimpleNamespace(load=AsyncMock(return_value='synthetic-wallet-token'))
    if pending == 'credentials':
        credentials.load.side_effect = hang
    else:
        broker.request.side_effect = hang
    events, ready = [], asyncio.Event()
    task = asyncio.create_task(wallet.monitor(relay.env, 'host', ready=ready,
        broker_factory=lambda: broker, account_factory=lambda b: b.credentials,
        credentials=credentials, control=events.append, interval=10))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert ready.is_set() and relay.packets[1]['controls'] is True
        assert relay.packets[1]['state'] == 'loading'
        relay.feed({'control': 'menu'})
        await relay.until(lambda: events)
        assert not task.done()
        await relay.until(lambda: len(relay.packets) >= 3)
        assert relay.packets[2]['state'] == 'unavailable'
        assert relay.packets[2]['controls'] is True
    finally:
        await cancel(task)


@pytest.mark.asyncio
async def test_account_factory_recovery_reuses_broker_and_receiver(monkeypatch):
    relay = Relay(monkeypatch)
    broker, events = fake_broker(), []
    factory = Mock(return_value=broker)
    account_factory = Mock(side_effect=[ValueError('unavailable'), broker.credentials])
    task = asyncio.create_task(wallet.monitor(relay.env, 'host', broker_factory=factory,
        account_factory=account_factory, credentials=SimpleNamespace(load=AsyncMock(return_value='synthetic')),
        control=events.append, interval=.005))
    try:
        await relay.until(lambda: any(p.get('state') == 'ready' for p in relay.packets))
        assert relay.packets[2]['state'] == 'unavailable'
        for _ in range(3):
            relay.feed({'control': 'menu'})
        await relay.until(lambda: len(events) >= 3)
        assert events == [{'control': 'menu'}] * 3
        factory.assert_called_once()
        assert account_factory.call_count == 2
        relay.connect.assert_awaited_once()
        relay.reader.feed_eof()
        await asyncio.wait_for(task, 1)
    finally:
        await cancel(task)
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('handshake', [[], None, {'wallet': 0}, {'wallet': 1}])
async def test_control_advertisement_requires_private_capability(monkeypatch, handshake):
    relay = Relay(monkeypatch, handshake if handshake is not None else {'wallet': 1, 'controls_shortcut': 'f8'})
    factory = Mock(side_effect=RuntimeError('wallet offline'))
    task = asyncio.create_task(wallet.monitor(relay.env, 'host', broker_factory=factory,
        control=lambda _: None, status=lambda: {'controls': True, 'quota_remaining': 0}, interval=10))
    try:
        if handshake in ([], {'wallet': 0}):
            await asyncio.wait_for(task, 1)
            factory.assert_not_called()
            assert len(relay.packets) == 1
        else:
            await relay.until(lambda: len(relay.packets) >= 3)
            for packet in relay.packets[1:]:
                assert packet['controls'] is False and packet['controls_shortcut'] is None
                assert packet['relay_restart_required'] is True
                assert 'quota_remaining' not in packet
    finally:
        await cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'exception'])
async def test_cancel_bounds_and_isolates_each_cleanup(monkeypatch, failure):
    relay = Relay(monkeypatch)
    monkeypatch.setattr(wallet, 'CLEANUP_SECONDS', .01)
    broker = fake_broker()

    async def fail():
        if failure == 'timeout':
            await asyncio.Event().wait()
        raise RuntimeError('private cleanup detail')

    relay.writer.wait_closed.side_effect = fail
    broker.close.side_effect = fail
    task = asyncio.create_task(wallet.monitor(relay.env, 'host', broker_factory=lambda: broker,
        account_factory=lambda b: b.credentials, credentials=SimpleNamespace(load=AsyncMock(return_value=None)),
        control=lambda _: None, interval=10))
    await relay.until(lambda: len(relay.packets) >= 3)
    await cancel(task)
    assert task.cancelled()
    relay.writer.close.assert_called_once()
    relay.writer.wait_closed.assert_awaited_once()
    broker.close.assert_awaited_once()
    relay.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_stalled_relay_write_times_out_without_reconnect(monkeypatch):
    relay = Relay(monkeypatch)
    monkeypatch.setattr(wallet, 'REQUEST_SECONDS', .02)
    factory = Mock(side_effect=AssertionError('No account access before control advertisement'))

    async def drain():
        if len(relay.packets) > 1:
            await asyncio.Event().wait()

    relay.writer.drain.side_effect = drain
    await asyncio.wait_for(wallet.monitor(relay.env, 'host', broker_factory=factory,
                                         control=lambda _: None), 1)
    factory.assert_not_called()
    relay.connect.assert_awaited_once()
    relay.writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_wallet_success_cannot_advertise_controls_without_handler(monkeypatch):
    relay = Relay(monkeypatch)
    relay.reader.feed_eof()
    broker = fake_broker()
    await wallet.monitor(relay.env, 'host', broker_factory=lambda: broker,
        account_factory=lambda b: b.credentials,
        credentials=SimpleNamespace(load=AsyncMock(return_value='synthetic')),
        status=lambda: {'controls': True})
    assert relay.packets[1]['state'] == 'ready'
    assert relay.packets[1]['controls'] is False
    assert relay.packets[1]['controls_shortcut'] is None


def menu_transport(monkeypatch, answers):
    reader, packets = asyncio.StreamReader(), []
    for index, choice in enumerate(answers, 1):
        reader.feed_data(json.dumps({'id': index, 'answer': {'choice': choice}}).encode() + b'\n')
    reader.feed_data(b'{"closed":true}\n')
    writer = SimpleNamespace(write=lambda raw: packets.append(json.loads(raw)), drain=AsyncMock(),
                             close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr(local_controls, 'connect', AsyncMock(return_value=(reader, writer)))
    return packets, writer


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [[], None, {'closed': False}])
async def test_unacknowledged_menu_close_never_starts_play(monkeypatch, ack):
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"id":1,"answer":{"choice":"play"}}\n')
    reader.feed_data(json.dumps(ack).encode() + b'\n')
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr(local_controls, 'connect', AsyncMock(return_value=(reader, writer)))
    factory = Mock(side_effect=AssertionError('No game initialization before terminal restoration'))
    monkeypatch.setattr(local_controls, 'Broker', factory)
    with pytest.raises(IntegrationError, match='No action started'):
        await local_controls.controls('host', menu=True)
    factory.assert_not_called()
    writer.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,choice', [('host', 'quit'), ('v', 'use_host')])
async def test_menu_exit_or_host_selection_needs_no_wallet(monkeypatch, mode, choice):
    packets, writer = menu_transport(monkeypatch, [choice])
    factory = Mock(side_effect=AssertionError('Must not initialize wallet'))
    monkeypatch.setattr(local_controls, 'Broker', factory)
    check = AsyncMock(side_effect=AssertionError('Must not check native balance'))
    assert await local_controls.controls(mode, menu=True, check_v=check) == choice
    assert 'no wallet balance or host credits' in packets[0]['message']
    factory.assert_not_called()
    check.assert_not_awaited()
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_menu_play_bypasses_native_billing_preflight(monkeypatch):
    _, writer = menu_transport(monkeypatch, ['play'])
    broker = fake_broker()
    monkeypatch.setattr(local_controls, 'Broker', Mock(return_value=broker))
    play = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(local_controls, 'play', play)
    check = AsyncMock(side_effect=AssertionError('Play must not check native balance'))
    assert await local_controls.controls('host', menu=True, check_v=check) == 'quit'
    play.assert_awaited_once_with(None, broker, requested_game=None)
    check.assert_not_awaited()
    broker.request.assert_not_awaited()
    broker.close.assert_awaited_once()
    writer.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unavailable', 'timeout', 'empty', 'signed_out', 'missing', 'factory'])
async def test_billing_failure_retains_menu_without_authorizing_spend(monkeypatch, failure):
    packets, _ = menu_transport(monkeypatch, ['use_v', 'quit'])
    monkeypatch.setattr(local_controls, 'BILLING_CHECK_SECONDS', .01)
    factory = Mock(side_effect=AssertionError('No game or spending session'))
    monkeypatch.setattr(local_controls, 'Broker', factory)

    async def check():
        if failure == 'timeout':
            await asyncio.Event().wait()
        if failure == 'unavailable':
            raise OSError('private transport detail')
        if failure == 'factory':
            raise RuntimeError('private initialization detail')
        raise IntegrationError('Wallet empty.' if failure == 'empty' else 'Sign in to OpenVegas.')

    assert await local_controls.controls('host', menu=True,
        check_v=None if failure == 'missing' else check) == 'quit'
    assert [packet['kind'] for packet in packets] == ['form', 'form', 'done']
    assert 'private' not in json.dumps(packets)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_successful_billing_preflight_returns_selection_not_spending(monkeypatch):
    menu_transport(monkeypatch, ['use_v'])
    factory = Mock(side_effect=AssertionError('No spending session on menu selection'))
    monkeypatch.setattr(local_controls, 'Broker', factory)
    check = AsyncMock()
    assert await local_controls.controls('host', menu=True, check_v=check) == 'use_v'
    check.assert_awaited_once()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_play_factory_failure_is_safe_and_not_retried(monkeypatch):
    _, writer = menu_transport(monkeypatch, ['play'])
    factory = Mock(side_effect=RuntimeError('private configuration detail'))
    monkeypatch.setattr(local_controls, 'Broker', factory)
    play = AsyncMock()
    monkeypatch.setattr(local_controls, 'play', play)
    with pytest.raises(IntegrationError, match='No game request or new wager') as error:
        await local_controls.controls('host', menu=True)
    assert 'private' not in str(error.value)
    factory.assert_called_once()
    play.assert_not_awaited()
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_play_cleanup_cannot_hang_or_replace_result(monkeypatch):
    monkeypatch.setattr(local_controls, 'CLEANUP_SECONDS', .01)
    broker = fake_broker()
    broker.close.side_effect = asyncio.Event().wait
    monkeypatch.setattr(local_controls, 'Broker', Mock(return_value=broker))
    monkeypatch.setattr(local_controls, 'play', AsyncMock(return_value={'status': 'cancelled'}))
    assert await asyncio.wait_for(local_controls.controls('host'), 1) == 'quit'
    broker.request.assert_not_awaited()
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['claude', 'codex'])
async def test_zero_quota_controls_are_single_flight_without_session_or_wallet(monkeypatch, tmp_path, host):
    relay = Relay(monkeypatch)
    supervisor = billing_host.Supervisor(host, '/synthetic/host', [], {'HOME': str(tmp_path), **relay.env})
    supervisor.child = SimpleNamespace(returncode=None)
    supervisor.quota.remaining = lambda: 0
    finished = asyncio.Event()

    async def menu(*args, **kwargs):
        await finished.wait()
        return 'quit'

    controls = AsyncMock(side_effect=menu)
    monkeypatch.setattr(local_controls, 'controls', controls)
    factory = Mock(side_effect=RuntimeError('wallet unavailable'))
    task = asyncio.create_task(wallet.monitor(relay.env, 'host', broker_factory=factory,
        control=supervisor.control_event, status=supervisor.local_status, interval=.005))
    try:
        await relay.until(lambda: len(relay.packets) >= 3)
        for _ in range(3):
            relay.feed({'control': 'menu'})
        await relay.until(lambda: controls.await_count == 1)
        assert supervisor.identifier is None and supervisor.pending is None
        assert not supervisor.changed.is_set() and not supervisor.local_error
        finished.set()
        await asyncio.wait_for(supervisor.local_task, 1)
        assert controls.await_count == 1
        assert not supervisor.changed.is_set()
        relay.connect.assert_awaited_once()
    finally:
        finished.set()
        if supervisor.local_task:
            await supervisor.local_task
        await cancel(task)


@pytest.mark.parametrize('size', [1, 2, 3, 1000])
def test_only_lowercase_menu_chord_is_consumed_paste_and_host_keys_unchanged(size):
    keys = local_controls.LocalKeys()
    passthrough = (b'\x18o\x1b[19~\x1b[20~/voice stop\r\x1dO\x1dz'
                   + keys.START + keys.MENU + b'\x18o' + keys.END)
    data = passthrough + keys.MENU
    output, events = b'', 0
    for at in range(0, len(data), size):
        chunk, opened = keys.feed(data[at:at + size], True)
        output += chunk
        events += int(opened)
    assert output + keys.flush() == passthrough
    assert events == 1
