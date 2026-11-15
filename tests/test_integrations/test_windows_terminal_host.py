"""Run on Windows with --confcutdir=tests/test_integrations. No provider access."""
import asyncio
import json
import os
import secrets
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from openvegas.integrations import windows_billing_host as host
from openvegas.integrations import windows_terminal as terminal
from openvegas.integrations.credentials import IntegrationError


class MemoryDirectory(dict):
    atomic_write = dict.__setitem__
    unlink = dict.__delitem__

    def read(self, name, limit):
        if name not in self:
            raise FileNotFoundError
        assert len(self[name]) <= limit
        return self[name]


@pytest.mark.asyncio
async def test_session_control_mutual_auth_replay_and_retirement():
    store = MemoryDirectory()
    command = AsyncMock(return_value='host')
    async with terminal.controls(command, directory=store) as sid:
        assert await terminal.request(sid, 'status', directory=store) == 'host'
        port, key = store[sid].decode().split()
        wrong = MemoryDirectory({sid: f'{port} {secrets.token_hex(32)}'.encode()})
        with pytest.raises(IntegrationError):
            await terminal.request(sid, 'v', directory=wrong)
        client = secrets.token_hex(16)
        async def begin():
            reader, writer = await asyncio.open_connection('127.0.0.1', int(port))
            await terminal._send(writer, client)
            nonce, _ = (await terminal._line(reader)).split()
            return reader, writer, nonce
        r1, w1, nonce = await begin()
        r2, w2, other = await begin()
        assert nonce != other
        proof = terminal._mac(bytes.fromhex(key), 'cmd', sid, client, nonce, 'v')
        await terminal._send(w2, 'v ' + proof)
        assert await r2.read() == b''
        await terminal._send(w1, 'host ' + proof)
        assert await r1.read() == b''
        w1.close()
        w2.close()
        command.assert_awaited_once_with('status')
    assert not store
    with pytest.raises(IntegrationError):
        await terminal.request(sid, 'status', directory=store)


@pytest.mark.parametrize('value', ['0', 0, -1, True, 65536, 'https://example.invalid'])
def test_fixture_port_is_not_an_upstream_override(value):
    with pytest.raises(IntegrationError):
        host.RehearsalAdapter('claude', value)


def test_rehearsal_policy_rejects_ambient_settings_and_nonempty_profile(tmp_path):
    root = tmp_path / ('ov-rehearsal-' + 'a' * 32)
    for name in host._DIRS:
        (root / name).mkdir(parents=True)
    env = host._fixture_env('claude', root, tmp_path)
    assert host._fixture_policy('claude', env, root / 'work')
    for extra in ({'ANTHROPIC_BASE_URL': 'https://example.invalid'}, {'SOME_UNEXPECTED_KEY': 'override'}):
        with pytest.raises(IntegrationError):
            host._fixture_policy('claude', env | extra, root / 'work')
    (root / 'claude' / 'settings.json').write_text('{}')
    with pytest.raises(IntegrationError):
        host._fixture_policy('claude', env, root / 'work')


def test_windows_terminal_cli_separates_launch_and_controls(monkeypatch):
    from openvegas.integrations import commands
    monkeypatch.setattr(commands, 'os', SimpleNamespace(name='nt'))
    calls = []
    monkeypatch.setattr(host, 'launch', lambda name, *, fixture_port: calls.append((name, fixture_port)) or 12)
    run = CliRunner()
    assert run.invoke(commands.terminal, ['--attach', '--host', 'claude']).exit_code == 12
    assert run.invoke(commands.terminal, ['--attach', '--host', 'codex', '--windows-fixture-port', '7777']).exit_code == 12
    assert run.invoke(commands.terminal, ['--windows-session', 'id', '--payer', 'v', '--attach']).exit_code == 2
    assert calls == [('claude', None), ('codex', 7777)]


@pytest.mark.parametrize('mode', ['host', 'v'])
def test_rehearsal_refuses_nonfixture_auth_before_substitution(mode):
    adapter = host.RehearsalAdapter('codex', 4567)
    for headers in ({}, {'authorization': 'Bearer not-fixture'},
                    {'authorization': 'Bearer ' + host.SYNTHETIC_KEY, 'cookie': 'value'}):
        with pytest.raises(IntegrationError):
            adapter.request_plan('POST', '/responses', headers, mode, 'ov_native_fixture_' + 'a'*32)
    headers = {'authorization': 'Bearer ' + host.SYNTHETIC_KEY, 'upgrade': 'websocket'}
    plan = adapter.request_plan('GET', '/responses', headers, mode, 'ov_native_fixture_'+'a'*32 if mode == 'v' else None)
    assert plan.endpoint.startswith('ws://127.0.0.1:4567/')


CHILD = """
import json, os, pathlib, sys, time, urllib.request
work = pathlib.Path.cwd()
host = sys.argv[1]
if host == 'codex':
    base = json.loads(sys.argv[3].split('=', 1)[1])
    headers = {'authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']}
    suffix = '/responses'
else:
    base = os.environ['ANTHROPIC_BASE_URL']
    key, value = os.environ['ANTHROPIC_CUSTOM_HEADERS'].split(': ')
    headers = {key: value, 'x-api-key': os.environ['ANTHROPIC_API_KEY']}
    suffix = '/v1/messages'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for n in range(3):
    deadline = time.monotonic() + 12
    while not (work / ('go' + str(n))).exists():
        if time.monotonic() > deadline: sys.exit(3)
        time.sleep(.01)
    req = urllib.request.Request(base + suffix, data=b'{}', headers=headers, method='POST')
    with opener.open(req, timeout=4) as reply:
        marker = json.loads(reply.read())['marker']
    (work / ('out' + str(n))).write_text(marker + ':' + str(os.getpid()))
"""


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['claude', 'codex'])
async def test_owned_child_routes_and_switches_without_restart(tmp_path, monkeypatch, name):
    if name == 'codex' and sys.platform != 'win32':
        pytest.skip('exercise the actual Windows retained-handle TCP peer authenticator')
    seen = []
    async def upstream(reader, writer):
        lines = (await reader.readuntil(b'\r\n\r\n')).decode().split('\r\n')
        path = lines[0].split()[1]
        headers = dict(line.lower().split(': ', 1) for line in lines[1:] if line)
        await reader.readexactly(int(headers['content-length']))
        mode = 'host' if path.startswith('/_host/') else 'v'
        seen.append((mode, headers))
        data = json.dumps({'object': 'response', 'status': 'completed', 'type': 'message',
                           'stop_reason': 'end_turn', 'marker': mode}).encode()
        writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n'
                     + b'Content-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data)
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    public = asyncio.get_running_loop().create_future()
    store = MemoryDirectory()
    children = []
    def spawn(argv, env, cwd):
        children.append(subprocess.Popen([sys.executable, '-I', '-c', CHILD, name, *argv[1:]],
                                         env=env, cwd=cwd))
        return children[-1]
    monkeypatch.setattr(host, '_spawn', spawn)
    root = tmp_path / ('ov-rehearsal-' + secrets.token_hex(16))
    for item in host._DIRS:
        (root / item).mkdir(parents=True)
    cwd = root / 'work'
    env = host._fixture_env(name, root, Path(os.environ.get('SystemRoot', '/test-system')))
    async with server:
        task = asyncio.create_task(host.run_routed(name, '/test/host', server.sockets[0].getsockname()[1],
                                                   env, str(cwd), store, public.set_result))
        try:
            async with asyncio.timeout(20):
                await asyncio.wait((task, public), return_when=asyncio.FIRST_COMPLETED)
                if task.done():
                    await task
                    pytest.fail('Native child retired before controls opened')
                sid = public.result()
                for index, action in enumerate(('status', 'v', 'host')):
                    mode = 'host' if action == 'status' else action
                    assert await terminal.request(sid, action, directory=store) == mode
                    (cwd / f'go{index}').touch()
                    out = cwd / f'out{index}'
                    while not out.exists() or not out.stat().st_size:
                        if task.done():
                            pytest.fail(f'child exited early: {task.result()}')
                        await asyncio.sleep(.01)
                    assert out.read_text() == f'{mode}:{children[0].pid}'
                assert await task == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(children) == 1 and not store
    assert [m for m, _ in seen] == ['host', 'v', 'host']
    assert all('x-openvegas-local-auth' not in headers for _, headers in seen)
    assert seen[1][1]['authorization'].startswith('bearer ov_native_fixture_')
    assert 'x-api-key' not in seen[1][1]


@pytest.mark.skipif(sys.platform != 'win32', reason='real NT handle-relative DACL, not POSIX chmod')
@pytest.mark.asyncio
async def test_windows_locator_and_profile_are_private(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setenv('OPENAI_API_KEY', 'forbidden-parent-value')
    with terminal._directory() as directory, host.fresh_environment('claude') as (env, cwd):
        assert 'OPENAI_API_KEY' not in env and Path(cwd).parent != Path.home()
        assert env['ANTHROPIC_API_KEY'] == host.SYNTHETIC_KEY
        async with terminal.controls(AsyncMock(return_value='host'), directory=directory) as sid:
            assert directory.stat(sid).security.protected
            assert await terminal.request(sid, 'status') == 'host'
        with pytest.raises(FileNotFoundError):
            directory.stat(sid)


@pytest.mark.skipif(sys.platform != 'win32', reason='native Win32 console modes and code pages')
def test_native_windows_console_restore():
    # CI itself may redirect all three streams. Allocate a console for this
    # test process only; product launches never open a window or new terminal.
    code = """
import ctypes, msvcrt, os, sys
from ctypes import wintypes as W
from openvegas.integrations.windows_terminal import console_modes
# Match default Windows console handles: GetConsoleMode and SetConsoleMode
# both require GENERIC_READ, including on the output screen buffer.
for fd, name in [(0,'CONIN$'),(1,'CONOUT$'),(2,'CONOUT$')]:
    source = os.open(name, os.O_RDWR)
    if source != fd:
        os.dup2(source, fd)
        os.close(source)
api = ctypes.WinDLL('kernel32.dll', winmode=0x800)
h = W.HANDLE(msvcrt.get_osfhandle(0))
def current():
    result = W.DWORD()
    assert api.GetConsoleMode(h, ctypes.byref(result))
    return result.value
old = current()
cp = api.GetConsoleOutputCP()
try:
    with console_modes():
        assert api.SetConsoleMode(h, old ^ 1)
        alternate_cp = 437 if cp == 65001 else 65001
        assert api.SetConsoleOutputCP(alternate_cp)
        assert current() == old ^ 1 and api.GetConsoleOutputCP() == alternate_cp
        raise ValueError('test cleanup')
except ValueError:
    pass
assert current() == old and api.GetConsoleOutputCP() == cp
"""
    subprocess.run([sys.executable, '-c', code], creationflags=subprocess.CREATE_NEW_CONSOLE,
                   timeout=30, check=True)


@pytest.mark.asyncio
async def test_failed_peer_bind_reaps_exact_owned_child(tmp_path):
    store, children = MemoryDirectory(), []
    owner = host.WindowsConsoleHost()
    session = SimpleNamespace(state=SimpleNamespace(request_boundary=True),
                              adapter=SimpleNamespace(host='codex'), router=SimpleNamespace(auth_mode='peer'))
    def refuse(child):
        assert isinstance(child, subprocess.Popen) and child is owner.process
        children.append(child)
        raise IntegrationError('unbound peer')
    session.bind_native = refuse
    with pytest.raises(IntegrationError, match='unbound'):
        await owner.run(session, [sys.executable, '-I', '-c', 'import time; time.sleep(20)'],
                        {k: os.environ[k] for k in ('SystemRoot', 'WINDIR') if k in os.environ},
                        str(tmp_path), store, lambda _: None)
    assert len(children) == 1 and children[0].poll() is not None and not store
    with pytest.raises(IntegrationError):
        owner.alive()


@pytest.mark.asyncio
async def test_unverified_router_not_attached():
    owner = host.WindowsConsoleHost()
    session = SimpleNamespace(state=SimpleNamespace(request_boundary=True),
                              adapter=SimpleNamespace(host='codex'), router=SimpleNamespace(auth_mode='path'))
    with pytest.raises(IntegrationError, match='authenticated'):
        await owner.run(session, [], {}, '', MemoryDirectory(), lambda _: pytest.fail('no controls'))


def test_unmanaged_launch_does_not_pin_version_or_rewrite_argv(monkeypatch):
    from openvegas.integrations import native, windows_native_image
    monkeypatch.setattr(host.sys, 'platform', 'win32')
    monkeypatch.setattr(host, 'find_entry', lambda *_: 'native.exe')
    monkeypatch.setattr(host, 'ordinary_image', lambda *_: 'native.exe')
    monkeypatch.setattr(host.terminal, 'console_modes', nullcontext)
    monkeypatch.setattr(native, 'host_environment', lambda: {'EXAMPLE': 'preserved'})
    run = AsyncMock(return_value=43)
    monkeypatch.setattr(native, 'run_host', run)
    monkeypatch.setattr(windows_native_image, 'pinned_windows_image', lambda *_: pytest.fail('pinned ordinary host'))
    assert host.launch('codex', ['--version']) == 43
    run.assert_awaited_once_with(['native.exe', '--version'], {'EXAMPLE': 'preserved'})
