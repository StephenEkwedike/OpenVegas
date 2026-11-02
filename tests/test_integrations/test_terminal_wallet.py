import asyncio
import json
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.integrations import terminal_wallet as wallet
from openvegas.integrations.credentials import IntegrationError


@pytest.mark.asyncio
async def test_stuck_keychain_is_single_flight_and_never_leaks_a_different_account():
    release = threading.Event()
    calls = []
    def load():
        calls.append(True)
        release.wait(5)
        return 'first-account'
    reader = wallet.CredentialReader()
    original = SimpleNamespace(identity='one', load=load)
    try:
        for _ in range(3):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.load(original), .05)
        assert calls == [True]
        with pytest.raises(IntegrationError):
            await reader.load(SimpleNamespace(identity='two', load=lambda: 'other'))
        release.set()
        assert await reader.load(original) == 'first-account'
        assert await reader.load(SimpleNamespace(identity='two', load=lambda: 'other')) == 'other'
    finally:
        release.set()


def test_stuck_keychain_does_not_hold_asyncio_or_process_exit():
    # Subprocess timeout catches executor-join hangs that an async-only test misses.
    result = subprocess.run([sys.executable, '-c', '''
import asyncio, threading
from types import SimpleNamespace
from openvegas.integrations.terminal_wallet import CredentialReader
async def check():
    reader = CredentialReader()
    try:
        await asyncio.wait_for(reader.load(SimpleNamespace(load=lambda: threading.Event().wait())), .05)
    except TimeoutError:
        pass
asyncio.run(check())
print('EXITED_WITH_KEYCHAIN_PENDING')
'''], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'EXITED_WITH_KEYCHAIN_PENDING'


@pytest.mark.parametrize('raw,expected', [('0.000000', '0.00'), ('89.249260', '89.25'),
                                         ('500', '500.00'), ('12000.120000', '12,000.12')])
def test_balance_formatting_preserves_value(raw, expected):
    assert wallet.wallet_value(raw) == expected


@pytest.mark.parametrize('bad', [None, 0, -1, 'NaN', 'Infinity', '-5', '1e12', '1.0000001',
                                '1\x1b[2J', '1\n', '9' * 10000, {}, True])
def test_untrusted_balances_cannot_draw_controls_or_allocate_huge_text(bad):
    with pytest.raises(ValueError):
        wallet.wallet_value(bad)


@pytest.mark.parametrize('columns', [1, 12, 22, 40, 80, 120])
@pytest.mark.parametrize('state', ['ready', 'loading', 'login', 'unavailable'])
def test_narrow_strip_is_bounded_and_never_substitutes_zero(columns, state):
    text = wallet.wallet_label({'state': state, 'balance': '0.000000', 'billing': 'v'}, columns)
    assert len(text) < columns
    if columns >= 80:
        assert '$V billing' in text
        assert ('$V 0.00' in text) == (state == 'ready')


@pytest.mark.parametrize('shortcut', [None, 'f8', 'ctrl-x-o', wallet.LOCAL_CONTROL_SHORTCUT])
@pytest.mark.parametrize('low,error', [(False, False), (True, False), (True, True)])
def test_hud_only_advertises_negotiated_menu_shortcut(shortcut, low, error):
    text = wallet.wallet_label({'state': 'ready', 'balance': '50', 'controls': True,
        'controls_shortcut': shortcut, 'quota_remaining': 5 if low else None,
        'local_error': error}, 220)
    assert 'F8' not in text
    assert 'Ctrl+X' not in text
    assert ('Ctrl+] then O' in text) == (shortcut == wallet.LOCAL_CONTROL_SHORTCUT)
    assert '$V 50.00' in text


def test_native_viewport_keeps_separate_footer_and_does_not_replace_native_labels():
    screen = wallet.WalletScreen()
    screen.rows, screen.columns = 40, 120
    assert b'\x1b[1;39r' in screen.activate()
    assert screen.child_rows == 39
    assert screen.feed(b'\x1b[999;1HGPT-model / shortcuts') == b'\x1b[39;1HGPT-model / shortcuts'
    screen.payload = {'state': 'ready', 'balance': '500.000000', 'billing': 'host'}
    strip = screen.paint()
    assert b'\x1b[40;1H' in strip and b'OPENVEGAS / $V 500.00 / host billing' in strip
    assert b'\x1b[r' in screen.deactivate()
    assert not screen.paint() and screen.child_rows == 40


@pytest.mark.parametrize('data', [
    b'\x1bPq' + b'?' * 10000 + b'\x1b\\',
    b'\x1bPqimage\x07still-image\x1b\\',
    b'\x1b_Ga=T;image-payload\x1b\\',
    b'\x1b]0;Native terminal title\x07',
    b'\x1b[?2026htext\x1b[?2026l',
    b'\x1b7\x1b[12;3HPET\x1b8',
    b'\x1b[s\x1b[12;3HPET\x1b[u',
    b'\x1b(B',
    'Hello \u2665'.encode(),
])
def test_fragmented_sequences_are_lossless_and_never_interleaved(data):
    screen = wallet.WalletScreen()
    screen.active = True
    result = bytearray()
    for byte in data:
        result += screen.feed(bytes([byte]))
        if not screen.safe:
            assert screen.paint() == b''
    assert bytes(result) == data
    assert screen.safe and screen.paint()


def test_custom_scrolling_region_survives_wallet_and_resizes():
    screen = wallet.WalletScreen()
    screen.active = True
    assert screen.feed(b'\x1b[2;10r') == b'\x1b[2;10r'
    assert screen.margins() == b'\x1b[2;10r'
    assert b'\x1b[r' not in screen.paint()
    assert screen.feed(b'\x1b[r') == b'\x1b[1;23r'
    screen.rows = 12
    assert screen.margins() == b'\x1b[1;11r'
    screen.rows = 50
    assert screen.margins() == b'\x1b[1;49r'


@pytest.mark.asyncio
@pytest.mark.parametrize('result,state', [
    ({'balance': '500.000000'}, 'ready'),
    ({'balance': '0.000000'}, 'ready'),
    ({'balance': '\x1b[2J'}, 'unavailable'),
    (IntegrationError('secret must not print', 401), 'login'),
    (IntegrationError('secret must not print', 500), 'unavailable'),
    (TimeoutError(), 'unavailable'),
])
async def test_monitor_read_only_refresh_and_cleanup(monkeypatch, result, state):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"wallet":1}\n{"refresh":true}\n')
    reader.feed_eof()
    written = []
    writer = SimpleNamespace(write=written.append, drain=AsyncMock(),
                             close=lambda: None, wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: 'fixture-only'), close=AsyncMock(),
        request=AsyncMock(**({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})))
    ready = asyncio.Event()
    await wallet.monitor({wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'terminal-fixture'}, 'host',
                         broker_factory=lambda: broker, interval=.01, ready=ready,
                         account_factory=lambda b: b.credentials)
    payloads = [json.loads(x) for x in written]
    assert ready.is_set() and len(payloads) == 3
    assert payloads[1]['state'] == state and payloads[2]['state'] == state
    assert b'secret' not in b''.join(written) and b'fixture-only' not in b''.join(written)
    assert all(call.args == ('GET', '/account') for call in broker.request.await_args_list)
    broker.close.assert_awaited_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_older_relay_and_missing_connection_do_not_contact_wallet(monkeypatch):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_eof()
    writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(asyncio, 'open_unix_connection', connect)
    def no_broker():
        pytest.fail('Older/missing relay must not start account access')
    ready = asyncio.Event()
    await wallet.monitor({}, 'host', broker_factory=no_broker, ready=ready)
    assert ready.is_set()
    connect.assert_not_awaited()
    await wallet.monitor({wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'}, 'host', broker_factory=no_broker)


@pytest.mark.asyncio
async def test_cancel_closes_connection_while_account_fetch_pending(monkeypatch):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"wallet":1}\n')
    closed = []
    writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: closed.append(True), wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    started = asyncio.Event()
    async def fetch(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: 'fixture'), close=AsyncMock(), request=fetch)
    task = asyncio.create_task(wallet.monitor({wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'},
                                             'v', broker_factory=lambda: broker,
                                             account_factory=lambda b: b.credentials))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert closed == [True]
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_rejects_non_socket_before_credentials(monkeypatch, tmp_path):
    not_socket = tmp_path / 'wrong'
    not_socket.write_text('not a socket')
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, 'open_unix_connection', connect)
    def no_broker():
        pytest.fail('No account access for an unsafe relay path')
    await wallet.monitor({wallet.SOCKET_ENV: str(not_socket), wallet.TOKEN_ENV: 'fixture'},
                         'host', broker_factory=no_broker)
    connect.assert_not_awaited()

@pytest.mark.asyncio
async def test_local_control_is_not_blocked_by_wallet_network(monkeypatch):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"wallet":1}\n')
    writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    started, received = asyncio.Event(), asyncio.Event()
    async def fetch(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: 'fixture'), close=AsyncMock(), request=fetch)
    task = asyncio.create_task(wallet.monitor({wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'},
        'host', broker_factory=lambda: broker, account_factory=lambda b: b.credentials,
        control=lambda event: received.set() if event.get('control') == 'menu' else None))
    try:
        await asyncio.wait_for(started.wait(), 1)
        reader.feed_data(b'{"control":"menu"}\n')
        await asyncio.wait_for(received.wait(), .1)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('shortcut', [None, 'f8', wallet.LOCAL_CONTROL_SHORTCUT])
async def test_monitor_negotiates_shortcut_before_advertising_it(monkeypatch, shortcut):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({'wallet': 1, 'controls_shortcut': shortcut}).encode() + b'\n')
    written = []
    writer = SimpleNamespace(write=written.append, drain=AsyncMock(), close=lambda: None,
                             wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    started = asyncio.Event()
    async def fetch(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: 'fixture'),
                             close=AsyncMock(), request=fetch)
    task = asyncio.create_task(wallet.monitor({wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'},
        'host', broker_factory=lambda: broker, account_factory=lambda b: b.credentials,
        control=lambda _: None, status=lambda: {'controls': True, 'quota_remaining': 5, 'local_error': True}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        payload = json.loads(written[1])
        supported = shortcut == wallet.LOCAL_CONTROL_SHORTCUT
        assert payload['controls'] == supported
        assert payload['relay_restart_required'] == (not supported)
        assert payload['controls_shortcut'] == (wallet.LOCAL_CONTROL_SHORTCUT if supported else None)
        assert ('quota_remaining' in payload) == supported
        assert ('local_error' in payload) == supported
        label = wallet.wallet_label(payload, 220)
        assert ('restart terminal' in label) == (not supported)
        assert 'F8' not in label
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
