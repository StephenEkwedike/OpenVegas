"""Synthetic terminal boundary checks, not native host visual certification."""
import asyncio
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.integrations import terminal_relay as relay
from openvegas.integrations import terminal_wallet as wallet
from openvegas.integrations.credentials import IntegrationError


@pytest.mark.parametrize('prefix', [b'\x1b[12;', b'\x1b]', b'\x1bPqimage', b'\x1b_Gimage', b'\xe2'])
@pytest.mark.parametrize('cancel', [b'\x18', b'\x1a'])
def test_cancelled_native_packet_cannot_swallow_following_prompt(prefix, cancel):
    screen, modes = wallet.WalletScreen(), relay.Modes()
    screen.feed(prefix)
    modes.feed(prefix)
    following = cancel + b'\x1b[?25h\x1b[HOPENVEGAS / USE $V'
    assert screen.feed(following) == following
    modes.feed(following)
    assert screen.safe and modes.values[25]


def test_escape_restarts_incomplete_csi_and_dcs_bell_does_not_end_image():
    screen, modes = wallet.WalletScreen(), relay.Modes()
    screen.feed(b'\x1b[12;')
    modes.feed(b'\x1b[12;')
    assert screen.feed(b'\x1b[?25l') == b'\x1b[?25l'
    modes.feed(b'\x1b[?25l')
    assert not modes.values[25]
    modes.feed(b'\x1bPqimage\x07\x1b[?1049h\x1b\\')
    assert not modes.alternate


def test_screen_reset_clears_visible_cells_without_erasing_scrollback():
    data = wallet.terminal_reset(clear=True)
    for expected in (b'\x18\x1b\\', b'\x1b[?1049l', b'\x1b[?1047l', b'\x1b[?47l',
                     b'\x1b[?2026l', b'\x1b[?6l', b'\x1b[r', b'\x1b[?25h',
                     b'\x1b[?2004l', b'\x1b[?1006l', b'\x1b[2J\x1b[H'):
        assert expected in data
    assert b'\x1b[3J' not in data and b'\x1bc' not in data
    assert b'\x1b[2J' not in wallet.terminal_reset()


class KeyboardTerminal:
    """Narrow protocol model, not a native emulator or keyboard UX certificate.

    Contracts: kitty keyboard-protocol/#progressive-enhancement and xterm
    ctlseqs.html XTMODKEYS. Codex rust-v0.166.0 tui/keyboard_modes.rs enables
    flags 5/7; installed Textual LinuxDriver uses flags 25 and pops before exit.
    """
    def __init__(self):
        self.alternate = False
        self.stacks = {False: [0], True: [0]}
        self.modify_other_keys = 0

    def feed(self, data):
        for match in re.finditer(rb'\x1b\[([?=><0-9;]*)([A-Za-z])', data):
            body, final = match.groups()
            if body in (b'?1049', b'?1047', b'?47') and final in (b'h', b'l'):
                self.alternate = final == b'h'
            elif final == b'u' and body[:1] in (b'>', b'<', b'='):
                stack = self.stacks[self.alternate]
                value = int(body[1:] or (b'1' if body[:1] == b'<' else b'0'))
                if body[:1] == b'>':
                    stack.append(value)
                elif body[:1] == b'<':
                    del stack[max(1, len(stack) - value):]
                else:
                    stack[-1] = value
            elif final == b'm' and body.startswith(b'>4;'):
                self.modify_other_keys = int(body[3:])

    def key(self, char):
        if self.stacks[self.alternate][-1] & 8:
            return f'\x1b[{ord(char)}u'.encode()
        if self.modify_other_keys == 3:
            return f'\x1b[27;1;{ord(char)}~'.encode()
        return char.encode()


@pytest.mark.parametrize('flags', [5, 7, 25, 31])
@pytest.mark.parametrize('alternate', [False, True])
@pytest.mark.parametrize('modify_keys', [0, 2, 3])
def test_keyboard_reset_disables_current_flags_without_popping_saved_stacks(flags, alternate, modify_keys):
    terminal = KeyboardTerminal()
    terminal.feed(b'\x1b[>5u' + (b'\x1b[?1049h' if alternate else b'')
                  + f'\x1b[>{flags}u\x1b[>4;{modify_keys}m'.encode())
    depths = {screen: len(stack) for screen, stack in terminal.stacks.items()}
    data = wallet.terminal_reset(clear=True)
    terminal.feed(data)
    assert not terminal.alternate
    assert terminal.stacks[False][-1] == 0 and terminal.modify_other_keys == 0
    if alternate:
        assert terminal.stacks[True][-1] == 0
        assert data.index(b'\x1b[=0u') < data.index(b'\x1b[?1049l') < data.rindex(b'\x1b[=0u')
    assert terminal.key('y') == b'y'
    assert {screen: len(stack) for screen, stack in terminal.stacks.items()} == depths
    terminal.feed(data)
    assert terminal.key('y') == b'y'  # Idempotent, including already-clean hosts.
    assert b'\x1b[?u' not in data  # No terminal reply can leak into the next prompt.


def test_keyboard_packets_do_not_end_a_native_saved_cursor_pair():
    screen = wallet.WalletScreen()
    screen.feed(b'\x1b7')
    for packet in (b'\x1b[>7u', b'\x1b[<u', b'\x1b[=0u'):
        assert screen.feed(packet) == packet
        assert screen.saved and not screen.safe
    screen.feed(b'\x1b8')
    assert screen.safe


def test_drain_does_not_wait_for_descendant_to_close_slave():
    master, slave = pty.openpty()
    try:
        os.write(slave, b'final-renderer-bytes')
        assert select.select([master], [], [], 1)[0]
        chunks = []
        started = time.monotonic()
        assert relay.drain_ready(master, chunks.append)
        assert b''.join(chunks) == b'final-renderer-bytes'
        assert time.monotonic() - started < .5
        assert relay.drain_ready(master, chunks.append)
    finally:
        os.close(master)
        os.close(slave)


def test_owned_child_ignoring_term_is_killed_and_reaped(tmp_path):
    ready = tmp_path / 'ready'
    proc = subprocess.Popen([sys.executable, '-c', '''
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).touch()
while True: time.sleep(1)
''', str(ready)], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        relay.stop_child(proc.pid, timeout=.05)
        with pytest.raises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_owned_group_is_cleaned_when_direct_child_already_exited():
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen([sys.executable, '-c', '''
import os, signal, sys, time
fd = int(sys.argv[1])
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(fd, b'ready')
    while True: time.sleep(1)
os._exit(0)
''', str(write_fd)], start_new_session=True, pass_fds=(write_fd,))
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 2)[0]
        assert os.read(read_fd, 5) == b'ready'
        assert proc.wait(timeout=2) == 0
        relay.stop_child(proc.pid, timeout=.05)
        assert select.select([read_fd], [], [], 2)[0]
        assert os.read(read_fd, 1) == b''
    finally:
        relay.stop_child(proc.pid, timeout=.05)
        os.close(read_fd)


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [b'', b'{"ready":true}\n', b'{"reset":false}\n', b'not json\n'])
async def test_handoff_fails_closed_when_old_or_busy_relay_cannot_ack(monkeypatch, reply):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(reply)
    reader.feed_eof()
    chunks, closed = [], []
    writer = SimpleNamespace(write=chunks.append, drain=AsyncMock(),
                             close=lambda: closed.append(True), wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    with pytest.raises(IntegrationError, match='Restart the managed terminal'):
        await wallet.handoff_screen({wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'})
    assert json.loads(chunks[0]) == {'kind': 'handoff', 'token': 'fixture'}
    assert closed == [True]


@pytest.mark.asyncio
async def test_unmanaged_redirected_output_gets_no_terminal_controls(monkeypatch):
    monkeypatch.setattr(os, 'isatty', lambda _: False)
    monkeypatch.setattr(os, 'write', lambda *_: pytest.fail('Redirected output was reset'))
    await wallet.handoff_screen({})


@pytest.mark.asyncio
async def test_repeated_monitor_cancellation_releases_lease_and_finishes_cleanup(monkeypatch):
    monkeypatch.setattr(wallet, 'validate_socket', lambda _: None)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"wallet":1,"controls_shortcut":"ctrl-bracket-o"}\n')
    fetching, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed, events = [], []
    async def wait_closed():
        closing.set()
        await release.wait()
    writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock(),
                             close=lambda: closed.append(True), wait_closed=wait_closed)
    monkeypatch.setattr(asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    async def fetch(*args, **kwargs):
        fetching.set()
        await asyncio.Event().wait()
    broker = SimpleNamespace(credentials=SimpleNamespace(load=lambda: 'fixture'),
                             request=fetch, close=AsyncMock())
    task = asyncio.create_task(wallet.monitor(
        {wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'}, 'host',
        broker_factory=lambda: broker, account_factory=lambda b: b.credentials,
        control=events.append))
    try:
        await asyncio.wait_for(fetching.wait(), 1)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        assert closed == [True]
        task.cancel()
        reader.feed_data(b'{"control":"menu"}\n')
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not events
        broker.close.assert_awaited_once()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,pending,tty,reset', [
    ('host', 'v', True, True), ('v', 'host', True, True), ('v', None, True, True),
    ('host', None, True, False), ('host', 'v', False, False),
])
async def test_supervisor_handoff_follows_reap_tasks_and_termios(
        monkeypatch, tmp_path, mode, pending, tty, reset):
    from openvegas.integrations import billing_host

    events = []
    started, exited = asyncio.Event(), asyncio.Event()
    attributes = [0, 0, 0, 0, 0, 0, []]
    env = {'HOME': str(tmp_path), wallet.SOCKET_ENV: '/fixture', wallet.TOKEN_ENV: 'fixture'}
    supervisor = billing_host.Supervisor('codex', '/fixture-host', [], env)
    supervisor.mode = mode
    monkeypatch.setattr(os, 'isatty', lambda fd: tty and fd == 0)
    monkeypatch.setattr(termios, 'tcgetattr', lambda _: attributes)

    def restore(fd, when, value):
        assert fd == 0 and value is attributes
        assert child.returncode == 0
        assert 'local-stopped' in events and 'wallet-stopped' in events
        events.append('termios-restored')
    monkeypatch.setattr(termios, 'tcsetattr', restore)

    async def monitor(*args, ready, **kwargs):
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append('wallet-stopped')
    monkeypatch.setattr(wallet, 'monitor', monitor)

    async def local_controls():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append('local-stopped')
    supervisor.local_task = asyncio.create_task(local_controls())
    child = SimpleNamespace(returncode=None)
    async def wait():
        await exited.wait()
        child.returncode = 0
        events.append('reaped')
        return 0
    def terminate():
        events.append('terminate')
        exited.set()
    child.wait, child.terminate = wait, terminate
    async def spawn(*args, **kwargs):
        started.set()
        return child
    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn_mock)

    async def handoff(given_env):
        assert given_env == env
        assert child.returncode == 0
        assert supervisor.local_task.done()
        assert events[-1] == 'termios-restored'
        events.append('reset')
    reset_mock = AsyncMock(side_effect=handoff)
    monkeypatch.setattr(wallet, 'handoff_screen', reset_mock)
    task = asyncio.create_task(supervisor.run_child(['/fixture-host'], env))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if pending:
            supervisor.pending = pending
            supervisor.changed.set()
        else:
            exited.set()
        assert await asyncio.wait_for(task, 1) == 0
        assert events.index('reaped') < events.index('local-stopped') < events.index('wallet-stopped')
        assert reset_mock.await_count == int(reset)
        assert ('termios-restored' in events) == tty
        spawn_mock.assert_awaited_once()
    finally:
        exited.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        supervisor.local_task.cancel()
        await asyncio.gather(supervisor.local_task, return_exceptions=True)


def run_pty(tmp_path, probe, *, after_start=None):
    """Only local fixture processes; no native host, accounts or provider traffic."""
    root = Path(__file__).resolve().parents[2]
    script = tmp_path / 'probe.py'
    script.write_text(textwrap.dedent(probe))
    wrapper = '''
import fcntl, os, subprocess, sys, termios
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
before = termios.tcgetattr(0)
result = subprocess.run(sys.argv[1:])
after = termios.tcgetattr(0)
for attrs in (before, after): attrs[3] &= ~getattr(termios, 'PENDIN', 0)
print('TERMIOS_RESTORED=' + str(before == after), flush=True)
sys.exit(result.returncode)
'''
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    proc = subprocess.Popen([sys.executable, '-c', wrapper, sys.executable, '-m',
                             'openvegas.integrations.terminal_relay', '--', sys.executable, str(script)],
                            cwd=root, env={**os.environ, 'PYTHONPATH': str(root), 'TERM': 'xterm-256color'},
                            stdin=slave, stdout=slave, stderr=slave)
    output, prompted = bytearray(), False
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                output.extend(data)
                if after_start and not prompted and b'AWAITING_INPUT' in output:
                    after_start(master, bytes(output))
                    prompted = True
                if b'TERMIOS_RESTORED=' in output:
                    break
        assert proc.wait(timeout=2) == 0, output[-2000:]
        assert b'TERMIOS_RESTORED=True' in output, output[-2000:]
        assert b'Traceback' not in output, output[-2000:]
        return bytes(output)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize('unfinished', ['saved', 'sync', 'csi', 'osc', 'alternate', 'keyboard'])
def test_relay_barrier_removes_stale_native_screen_before_blocking_prompt(tmp_path, unfinished):
    probe = r'''
        import asyncio, json, os, sys
        from tests.collection_safety import install
        install()
        from openvegas.integrations.terminal_wallet import handoff_screen, SOCKET_ENV, TOKEN_ENV
        async def main():
            reader, writer = await asyncio.open_unix_connection(os.environ[SOCKET_ENV])
            writer.write(json.dumps({'kind': 'wallet', 'token': os.environ[TOKEN_ENV],
                                     'billing': 'host'}).encode() + b'\n')
            await writer.drain()
            assert json.loads(await reader.readline())['wallet'] == 1
            writer.write(b'{"state":"ready","balance":"50"}\n')
            await writer.drain()
            await asyncio.sleep(.05)
            suffixes = {'saved': b'\x1b7', 'sync': b'\x1b[?2026h', 'csi': b'\x1b[12;',
                        'osc': b'\x1b]0;unfinished', 'alternate': b'\x1b[?1049h',
                        'keyboard': b'\x1b[>31u\x1b[>4;3m'}
            os.write(1, b'\x1b[?25l\x1b[?2004h\x1b[?1006h\x1b[23;1HAsk anything'
                     + suffixes[UNFINISHED])
            # Leaving the lease open exercises explicit relay revocation even
            # when safe drawing is impossible. Real supervisors cancel it first.
            await handoff_screen(os.environ)
            assert await reader.read() == b''
            assert os.get_terminal_size().lines == 24
            print('OPENVEGAS / USE $V\nAWAITING_INPUT', flush=True)
            assert input('Allow spending? [y/N]: ') == 'n'
            print('DECLINED_NO_SPEND', flush=True)
            writer.close()
            await writer.wait_closed()
            # A fresh host must be able to acquire a clean, resized wallet row.
            reader, writer = await asyncio.open_unix_connection(os.environ[SOCKET_ENV])
            writer.write(json.dumps({'kind': 'wallet', 'token': os.environ[TOKEN_ENV],
                                     'billing': 'v'}).encode() + b'\n')
            await writer.drain()
            assert json.loads(await reader.readline())['wallet'] == 1
            assert os.get_terminal_size().lines == 23
            writer.close()
            await writer.wait_closed()
            print('FRESH_LEASE', flush=True)
        asyncio.run(main())
    '''.replace('UNFINISHED', repr(unfinished))
    def decline(fd, observed):
        keyboard = KeyboardTerminal()
        keyboard.feed(observed)
        assert keyboard.key('n') == b'n'
        time.sleep(.15)  # Leave the blocking prompt visible across relay ticks.
        os.write(fd, keyboard.key('n') + b'\r')
    output = run_pty(tmp_path, probe, after_start=decline)
    before, prompt = output.split(b'OPENVEGAS / USE $V', 1)
    assert b'Ask anything' in before
    assert wallet.terminal_reset(clear=True) in before
    waiting = prompt.split(b'DECLINED_NO_SPEND', 1)[0]
    assert b'anything' not in waiting and b'wallet' not in waiting and b'billing' not in waiting
    assert b'FRESH_LEASE' in output


def test_relay_exit_restores_modes_after_uncooperative_native_exit(tmp_path):
    output = run_pty(tmp_path, r'''
        import os
        os.write(1, b'\x1b[?1049h\x1b[?25l\x1b[?2026h\x1b[?2004h\x1b[?1006hCRASHED')
    ''')
    after = output.split(b'CRASHED', 1)[1]
    assert wallet.terminal_reset(clear=True) in after
