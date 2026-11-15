"""Offline Windows ownership contract tests and a real Win32 loopback check."""
import asyncio
import ctypes
import os
import socket
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openvegas.integrations import inference_router as router
from openvegas.integrations import inference_router_windows as win
from openvegas.integrations.credentials import IntegrationError

PORTS = (48513, 8912)
SCOPE = dict(zip(('client', 'server'), (('127.0.0.1', p) for p in PORTS)))
PID = os.getpid() + 1
BIRTH = 133000000000000000


def row(*, reverse=False, pid=None, state=5, address=b'\x7f\0\0\1', created=BIRTH + 100):
    local, remote = PORTS[::-1] if reverse else PORTS
    if pid is None:
        pid = os.getpid() if reverse else PID
    # SDK layout, independent of the production ctypes definition. The port
    # DWORD's unused high bytes need not be zero.
    return (struct.pack('<I', state) + address + struct.pack('!H', local) + b'\x01\xff'
            + b'\x7f\0\0\1' + struct.pack('!H', remote) + b'\xff\xab'
            + struct.pack('<Iq128x', pid, created))


def table(*rows):
    return struct.pack('<I4x', len(rows)) + b''.join(rows)


def inspect(raw):
    buf = ctypes.create_string_buffer(raw, len(raw))
    return win._connection(buf, len(raw), PORTS, (PID, BIRTH))


def test_binary_layout_exact_tuple_and_network_order():
    assert inspect(table(row(address=b'\x7f\0\0\2'), row(reverse=True, created=13), row())) == (BIRTH + 100, 13)


@pytest.mark.parametrize('raw', [
    b'', table(), table(row()), table(row(reverse=True)),
    table(row(), row(reverse=True))[:-1], struct.pack('<I4x', 2**32 - 1),
    table(row(), row(), row(reverse=True)), table(row(), row(reverse=True), row(reverse=True, state=8)),
    table(row(pid=PID + 1), row(reverse=True)), table(row(), row(reverse=True, pid=PID)),
    table(row(state=8), row(reverse=True)), table(row(), row(reverse=True, state=2)),
    table(row(address=b'\x7f\0\0\2'), row(reverse=True)), table(row(created=BIRTH - 1), row(reverse=True)),
    table(row(created=BIRTH), row(reverse=True)), table(row(), row(reverse=True, created=0)),
])
def test_incomplete_ambiguous_or_stale_table(raw):
    with pytest.raises(ValueError):
        inspect(raw)


def test_native_inventory_retries_size_race_with_bounds():
    api = object.__new__(win._Windows)
    raw = table(row(), row(reverse=True))
    sizes = iter((len(raw) - 160, len(raw), len(raw)))

    def query(buf, size, sort, family, kind, reserved):
        assert (sort, family, kind, reserved) == (False, socket.AF_INET, 7, 0)
        needed = next(sizes)
        ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] = needed
        if buf is None or ctypes.sizeof(buf) < needed:
            return 122
        ctypes.memmove(buf, raw, len(raw))
        return 0

    api.table = query
    assert api.connection(PORTS, (PID, BIRTH)) == (BIRTH + 100,) * 2


@pytest.mark.parametrize('status,needed', [(5, 400), (122, win._MAX_TABLE + 1), (122, 3), (0, 400)])
def test_inventory_error_or_bad_size_does_not_retry(status, needed):
    api = object.__new__(win._Windows)

    def query(buf, size, *args):
        assert buf is None
        ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32))[0] = needed
        return status

    api.table = query
    with pytest.raises(OSError):
        api.connection(PORTS, (PID, BIRTH))


@pytest.fixture
def backend(monkeypatch):
    handle = Mock()
    api = SimpleNamespace(now=Mock(return_value=BIRTH + 150), pin=Mock(return_value=handle),
                          identity=Mock(return_value=(PID, BIRTH)),
                          connection=Mock(return_value=(BIRTH + 100, BIRTH + 90)))
    monkeypatch.setattr(win, '_Windows', lambda: api)
    child = object.__new__(subprocess.Popen)
    child.pid, child.returncode = PID, None
    return win.WindowsTCPPeerAuth(), api, child


def test_factory_selects_windows(monkeypatch, backend):
    monkeypatch.setattr(router.sys, 'platform', 'win32')
    assert isinstance(router.native_tcp_peer_auth(), win.WindowsTCPPeerAuth)


@pytest.mark.skipif(sys.platform == 'win32', reason='non-Windows guard')
def test_refuse_wrong_os():
    with pytest.raises(OSError):
        win.WindowsTCPPeerAuth()


@pytest.mark.asyncio
async def test_binding_waits_for_retained_child_and_does_not_rebind(backend):
    auth, api, child = backend
    request = asyncio.create_task(auth(SCOPE))
    await asyncio.sleep(0)
    wrapper = SimpleNamespace(pid=PID, returncode=None)
    with pytest.raises(IntegrationError):
        auth.bind(wrapper)
    api.pin.assert_not_called()
    wrapper._child = child
    auth.bind(wrapper)
    assert await request
    api.pin.assert_called_once_with(child)
    assert api.connection.call_count == 2
    with pytest.raises(IntegrationError):
        auth.bind(child)


def test_pid_mismatch_releases_duplicate_without_binding(backend):
    auth, api, child = backend
    api.identity.return_value = (PID + 1, BIRTH)
    with pytest.raises(IntegrationError):
        auth.bind(child)
    api.pin.return_value.Close.assert_called_once_with()
    assert not auth._bound.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['birth', 'handle-lost', 'exit', 'new-socket', 'table-error'])
async def test_races_and_inspection_failure_deny(backend, failure):
    auth, api, child = backend
    auth.bind(child)

    def second_snapshot(*args):
        if failure == 'birth':
            api.identity.return_value = (PID, BIRTH + 1)
        elif failure == 'handle-lost':
            api.identity.side_effect = OSError()
        elif failure == 'exit':
            child.returncode = 0
        elif failure == 'new-socket':
            return (BIRTH + 101, BIRTH + 90)
        else:
            raise OSError()
        return (BIRTH + 100, BIRTH + 90)

    def snapshot(*args):
        if api.connection.call_count == 1:
            return (BIRTH + 100, BIRTH + 90)
        return second_snapshot(*args)

    api.connection.side_effect = snapshot
    assert not await auth(SCOPE)


@pytest.mark.asyncio
async def test_connection_created_while_request_waited_cannot_authenticate_it(backend):
    auth, api, child = backend
    request = asyncio.create_task(auth(SCOPE))
    await asyncio.sleep(0)
    api.now.assert_called_once_with()
    api.connection.return_value = (api.now.return_value + 1, api.now.return_value)
    api.now.return_value += 10
    auth.bind(child)
    assert not await request


@pytest.mark.asyncio
@pytest.mark.parametrize('client', [None, ('localhost', PORTS[0]), ('::1', PORTS[0]),
                                    ('127.0.0.1', True), ('127.0.0.1', PORTS[1]), ('127.0.0.1', 65536)])
async def test_invalid_endpoint_denied_before_query(backend, client):
    auth, api, child = backend
    auth.bind(child)
    assert not await auth(dict(SCOPE, client=client))
    api.connection.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_keeps_inspector_slot_until_worker_really_stops(monkeypatch, backend):
    auth, api, child = backend
    auth.bind(child)
    auth._limit = asyncio.Semaphore(1)
    entered, finish = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        assert finish.wait(5)
        raise OSError()

    api.connection.side_effect = slow
    monkeypatch.setattr(win, '_TIMEOUT', .1)
    try:
        assert not await auth(SCOPE)
        assert entered.is_set()
        assert not await auth(SCOPE)
        assert api.connection.call_count == 1
    finally:
        finish.set()
    async with asyncio.timeout(3):
        await auth._limit.acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['http', 'websocket'])
async def test_router_refuses_before_request_admission(backend, protocol):
    auth, api, child = backend
    auth.bind(child)
    api.connection.side_effect = PermissionError()
    app = router.InferenceRouter(None, None, auth_mode='peer', peer_auth=auth)
    app.authority = '127.0.0.1:8912'
    sent = []

    async def send(event):
        sent.append(event)

    await app(dict(SCOPE, type=protocol, headers=[(b'host', app.authority.encode())],
                   raw_path=b'/responses'), None, send)
    if protocol == 'http':
        assert sent[0]['status'] == 403
    else:
        assert sent == [{'type': 'websocket.close', 'code': 1008}]


@pytest.mark.skipif(sys.platform != 'win32', reason='requires native Windows IP Helper and process handles')
@pytest.mark.asyncio
async def test_native_windows_child_socket_only():
    import _winapi

    auth = router.native_tcp_peer_auth()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        addr = listener.getsockname()
        script = ("import socket,sys; s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),5);"
                  "s.settimeout(20); s.recv(1); s.close()")
        # A Windows venv's python.exe may be a redirector that spawns a grandchild.
        with subprocess.Popen([sys._base_executable, '-I', '-S', '-c', script, str(addr[1])],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            try:
                auth.bind(child)
                accepted, peer = listener.accept()
                with accepted, socket.create_connection(addr, timeout=5) as unrelated:
                    other, other_peer = listener.accept()
                    with other:
                        assert unrelated.getsockname() == other_peer
                        assert await auth({'client': peer, 'server': addr})
                        assert not await auth({'client': other_peer, 'server': addr})
                        assert not await auth({'client': addr, 'server': peer})
                        accepted.sendall(b'.')
                        assert _winapi.WaitForSingleObject(child._handle, 5000) == _winapi.WAIT_OBJECT_0
                        assert child.returncode is None  # No Popen.poll()/wait() to reveal the exit.
                        assert not await auth({'client': peer, 'server': addr})
                        with pytest.raises(IntegrationError):
                            router.native_tcp_peer_auth().bind(child)
                        assert child.wait(timeout=5) == 0
            finally:
                if child.poll() is None:
                    child.kill()
