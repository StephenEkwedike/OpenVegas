"""Owned POSIX cleanup with disposable processes, no native host or provider calls."""

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from openvegas.integrations import codex_billing_route as routing
from openvegas.integrations.credentials import IntegrationError

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.name != 'posix', reason='Unix route')]
SPAWN = asyncio.create_subprocess_exec
BASE = 'http://127.0.0.1:1'
TREE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
info, mode, endpoint, lifetime = sys.argv[1:]
lifetime = int(lifetime)
if mode == 'stubborn':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child_code = ''' + repr("""
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
time.sleep(120)
""") + r'''
child = subprocess.Popen([sys.executable, '-B', '-u', '-c', child_code],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    pass_fds=(lifetime,))
os.close(lifetime)
child_pid = int(child.stdout.readline())
child.stdout.close()
Path(info + '.tmp').write_text(json.dumps({'parent': os.getpid(), 'child': child_pid}))
Path(info + '.tmp').replace(info)
if endpoint:
    Path(endpoint).touch()  # RPC is mocked; only process lifetime is under test.
if mode == 'exit-first':
    sys.exit(0)
time.sleep(120)
'''


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def running(child):
    # Only the disposable child holds the writer: EOF proves it stopped even
    # where orphan zombies await PID 1 reaping. No process-table access needed.
    try:
        return os.read(child.fd, 1) != b''
    except BlockingIOError:
        return True


@pytest.fixture
def fast_cleanup(monkeypatch):
    monkeypatch.setattr(routing, '_STOP_GRACE_SECONDS', .15)
    monkeypatch.setattr(routing, '_STOP_KILL_SECONDS', 2)


@pytest.fixture
def trees(tmp_path):
    owned = []

    @asynccontextmanager
    async def create(mode='stubborn', endpoint='', spawn_kwargs=None):
        info = tmp_path / f'tree-{len(owned)}.json'
        kwargs = {'env': {'PATH': '/usr/bin:/bin'}, 'stdin': asyncio.subprocess.DEVNULL,
                  'stdout': asyncio.subprocess.DEVNULL, 'stderr': asyncio.subprocess.DEVNULL,
                  'start_new_session': True}
        if spawn_kwargs:
            kwargs.update(spawn_kwargs)
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        try:
            process = await SPAWN(sys.executable, '-B', '-u', '-c', TREE, str(info), mode,
                                  endpoint, str(write_fd), pass_fds=(write_fd,), **kwargs)
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        owned.append(process)
        try:
            await eventually(info.exists)
            data = json.loads(info.read_text())
            assert data['parent'] == process.pid
            assert os.getpgid(data['child']) == process.pid
            yield process, SimpleNamespace(pid=data['child'], fd=read_fd)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            finally:
                os.close(read_fd)

    return create


@pytest.mark.parametrize('mode', ['stubborn', 'term-parent', 'exit-first'])
async def test_stop_owned_group_even_when_parent_exits_first(trees, fast_cleanup, mode):
    async with trees(mode) as (process, child), trees() as (unrelated, unrelated_child):
        if mode == 'exit-first':
            await asyncio.wait_for(process.wait(), 3)
            assert running(child)
        await routing.stop(process, process_group=process.pid)
        await eventually(lambda: not running(child))
        assert process.returncode is not None
        assert unrelated.returncode is None and running(unrelated_child)
        # An already-cleaned leader/group is harmless to stop again.
        await routing.stop(process, process_group=process.pid)
        assert unrelated.returncode is None and running(unrelated_child)


async def test_second_cancellation_waits_for_group_cleanup(trees, fast_cleanup, monkeypatch):
    monkeypatch.setattr(routing, '_STOP_GRACE_SECONDS', .3)
    async with trees() as (process, child):
        task = asyncio.create_task(routing.stop(process, process_group=process.pid))
        await asyncio.sleep(.03)
        task.cancel()
        await asyncio.sleep(.03)
        task.cancel()
        assert not task.done()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert process.returncode is not None
        await eventually(lambda: not running(child))


async def test_refuse_callers_or_other_process_group(trees, fast_cleanup):
    async with trees() as (process, child), trees() as (unrelated, unrelated_child):
        for group in (os.getpgrp(), unrelated.pid, 0, -1):
            with pytest.raises(IntegrationError, match='unowned'):
                await routing.stop(process, process_group=group)
        assert process.returncode is None and running(child)
        assert unrelated.returncode is None and running(unrelated_child)


@pytest.fixture
def fake_connection(monkeypatch):
    class Connection:
        async def send(self, raw):
            self.request = json.loads(raw)

        async def recv(self):
            method = self.request['method']
            result = {}
            if method == 'config/read':
                result = {'config': {'cli_auth_credentials_store': 'ephemeral',
                                    'model_provider': 'openai',
                                    'openai_base_url': BASE + '/integrations/native/v1'}}
            elif method == 'account/read':
                result = {'account': {'type': 'apiKey'}}
            return json.dumps({'id': self.request['id'], 'result': result})

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Connection()

    monkeypatch.setattr(routing, 'unix_connect', connect)


@pytest.mark.parametrize('cancel_during_spawn', [False, True])
async def test_route_cleanup_after_repeated_cancel(trees, fast_cleanup, fake_connection,
                                                 monkeypatch, cancel_during_spawn):
    spawned, entered, release_spawn = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = {}
    contexts = []

    async def spawn(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        endpoint = args[-1].removeprefix('unix://')
        context = trees(endpoint=endpoint, spawn_kwargs=kwargs)
        contexts.append(context)
        process, child = await context.__aenter__()
        state.update(process=process, child=child, endpoint=Path(endpoint))
        spawned.set()
        if cancel_during_spawn:
            await release_spawn.wait()
        return process

    monkeypatch.setattr(routing.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(routing, '_STOP_GRACE_SECONDS', .3)

    async def session():
        async with routing.route('/synthetic/codex', BASE, 'fixture-model',
                                 {'OPENVEGAS_NATIVE_TOKEN': 'ov_native_synthetic'}):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(session())
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        if not cancel_during_spawn:
            await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(.03)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done()
        release_spawn.set()
        if cancel_during_spawn:
            await asyncio.sleep(.03)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert state['process'].returncode is not None
        await eventually(lambda: not running(state['child']))
        assert not state['endpoint'].exists()
        assert not state['endpoint'].parent.exists()
        assert entered.is_set() is not cancel_during_spawn
    finally:
        release_spawn.set()
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(task, 3)
        except asyncio.CancelledError:
            pass
        for context in reversed(contexts):
            await context.__aexit__(None, None, None)
