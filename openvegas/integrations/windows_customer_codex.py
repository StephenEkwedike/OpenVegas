"""A single authenticated native TUI over a retained Windows stdio app-server."""
from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
from contextlib import asynccontextmanager

import click
from websockets.asyncio.server import serve
from websockets.protocol import State

from .billing_host import exact_model
from .codex_handoff_policy import _identity
from .credentials import IntegrationError
from .inference_router_adapter import VERSIONS
from .inference_router_windows import WindowsTCPPeerAuth
from .use import parse_command
from .windows_customer import Selection, require

MAX = 4 * 1024 * 1024
PREFIX = 'ov-win-rpc-'
ORIGINS = {'apiKey': 'https://api.openai.com/v1', 'chatgpt': 'https://chatgpt.com/backend-api/codex'}


@asynccontextmanager
async def server_process(exe, env, cwd, overlay=()):
    p = await asyncio.create_subprocess_exec(exe, *overlay, '--no-daemon', 'app-server', '--listen',
        'stdio://', env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, limit=MAX)
    try:
        yield p
    finally:
        if p.returncode is None:
            p.stdin.close()
            try:
                await asyncio.wait_for(p.wait(), 3)
            except TimeoutError:
                p.kill()
                await p.wait()


class Pipe:
    """Supervisor RPC results never reach the TUI. Native RPCs pass unchanged."""
    def __init__(self, process):
        self.p = process
        self.waiters = {}
        self.client = None
        self.write_lock = asyncio.Lock()

    async def send(self, message):
        raw = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
        require(len(raw) <= MAX)
        async with self.write_lock:
            self.p.stdin.write(raw + b'\n')
            await self.p.stdin.drain()

    async def call(self, method, params):
        key = PREFIX + secrets.token_hex(16)
        f = self.waiters[key] = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(6):
                await self.send({'id': key, 'method': method, 'params': params})
                return await f
        finally:
            self.waiters.pop(key, None)

    async def read(self):
        try:
            while raw := await self.p.stdout.readline():
                require(len(raw) <= MAX and raw.endswith(b'\n'))
                packet = json.loads(raw)
                key = packet.get('id')
                if isinstance(key, str) and key.startswith(PREFIX):
                    require(not packet.get('method'))
                    future = self.waiters.get(key)
                    if future is not None and not future.done():
                        if 'result' in packet and 'error' not in packet:
                            future.set_result(packet['result'])
                        else:
                            future.set_exception(IntegrationError('Native metadata read refused.'))
                elif self.client:
                    await self.client.send(raw.decode('utf-8'))
                else:
                    require(key is None)
        finally:
            for future in self.waiters.values():
                if not future.done():
                    future.set_exception(IntegrationError('Native metadata connection ended.'))


async def route(pipe, cwd, expected=None):
    cfg = (await pipe.call('config/read', {'cwd': cwd, 'includeLayers': True}))['config']
    require(cfg.get('model_provider') in (None, 'openai') and not (cfg.get('model_providers') or {}).get('openai')
            and cfg.get('openai_base_url') in ((None, '', ORIGINS['apiKey']) if expected is None else (expected,)))
    kind = (await pipe.call('account/read', {'refreshToken': False}))['account']['type']
    require(kind in ORIGINS)
    return ORIGINS[kind]


async def original_upstream(exe, env, cwd):
    require(not any(k in env for k in ('CODEX_CONFIG', 'CODEX_PROVIDER', 'OPENAI_BASE_URL',
                                       'CHATGPT_BASE_URL', 'CODEX_APP_SERVER_ADDRESS')))
    async with server_process(exe, env, cwd) as p:
        pipe = Pipe(p)
        reader = asyncio.create_task(pipe.read())
        try:
            await pipe.call('initialize', {'clientInfo': {'name': 'openvegas', 'version': '1'},
                                           'capabilities': {'experimentalApi': True}})
            await pipe.send({'method': 'initialized', 'params': {}})
            return await route(pipe, cwd)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def action_for(method, params):
    if method not in ('turn/start', 'turn/steer'):
        return None
    items = params.get('input')
    if not isinstance(items, list) or len(items) != 1 or items[0].get('type') != 'text':
        return None
    text = items[0].get('text')
    if not isinstance(text, str) or items[0].get('text_elements') or items[0].get('textElements'):
        return None
    if text.strip(' \t').lower() == 'openvegas status':
        return 'status'
    parsed = parse_command(text)
    return parsed[0] if parsed and parsed[0] in ('v', 'host') else None


async def selection(pipe, owner, params):
    sid = params.get('threadId')
    require(await route(pipe, owner.cwd, owner.session._url) == owner.session.adapter.host_base)
    loaded = await pipe.call('thread/loaded/list', {'limit': 2})
    require(loaded.get('data') == [sid] and loaded.get('nextCursor') is None)
    thread = (await pipe.call('thread/read', {'threadId': sid, 'includeTurns': False}))['thread']
    native = _identity(thread, sid)
    # The TUI may have picked a new model for this turn without changing
    # thread/read. Use only explicit, agreeing native turn selections.
    names = [v for v in (params.get('model'), ((params.get('collaborationMode') or {})
             .get('settings') or {}).get('model')) if v is not None]
    require(not names or (all(exact_model(v) for v in names) and len(set(names)) == 1))
    return Selection(sid, names[0] if names else native.model, native.cwd, owner.session._url)


async def bridge(ws, pipe, owner, tui):
    def guard():
        require(ws.state is State.OPEN and tui.poll() is None and pipe.p.returncode is None)

    async def reply(key, message):
        # An intercepted turn did not start. Do not invent a completed native turn.
        await ws.send(json.dumps({'id': key, 'error': {'code': -32000, 'message': message}}))

    async def control(key, action, params):
        try:
            message = await owner.command(action, observe=lambda: selection(pipe, owner, params), guard=guard)
        except Exception:  # Never send backend errors, raw frames or secrets to the client.
            message = 'Local control unconfirmed. Check status; no automatic retry.'
        await reply(key, message)

    reader = asyncio.create_task(pipe.read())
    pending = None
    async def front():
        nonlocal pending
        async for raw in ws:
            msg = json.loads(raw)
            key, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
            require(not (isinstance(key, str) and key.startswith(PREFIX)))
            if pending is not None and pending.done():
                await pending
                pending = None
            action = action_for(method, params)
            if action:
                require(key is not None)
                if pending is not None or method == 'turn/steer':
                    await reply(key, 'Local control busy. Finish or cancel the turn, then retry.')
                else:
                    pending = asyncio.create_task(control(key, action, params))
            elif method and pending is not None and key is not None:
                await reply(key, 'Local control pending; request not sent. Retry explicitly.')
            elif method and (method.startswith(('account/login', 'account/logout', 'config/write',
                    'config/value/write', 'config/batchWrite')) or (owner.session.state.mode == 'v'
                    and method in ('thread/start', 'thread/resume', 'thread/fork'))):
                await reply(key, 'Return to host billing and exit managed mode for this operation.')
            else:
                if method in ('thread/start', 'thread/resume', 'thread/fork'):
                    require(await route(pipe, owner.cwd, owner.session._url) == owner.session.adapter.host_base)
                await pipe.send(msg)

    writer = asyncio.create_task(front())
    try:
        done, _ = await asyncio.wait((reader, writer), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            await t
    finally:
        for t in (writer, reader, pending):
            if t is not None:
                t.cancel()
        await asyncio.gather(*(t for t in (writer, reader, pending) if t is not None), return_exceptions=True)


async def run(owner, exe, env):
    session = owner.session
    async with session.launch(version=VERSIONS['codex'], env=env, args=[],
            effective_policy_verified=True) as (child_env, overlay), server_process(
                exe, child_env, owner.cwd, overlay) as server:
        # asyncio owns the process; the Windows verifier duplicates the actual
        # CreateProcess handle. Never re-open by a PID from a frame or process list.
        session.bind_native(server._transport.get_extra_info('subprocess'))
        peer = WindowsTCPPeerAuth()
        secret = secrets.token_urlsafe(32)
        tui = None
        claimed = False
        failed = asyncio.Event()
        async def accept(ws):
            nonlocal claimed
            if (ws.request.path != '/' or ws.request.headers.get_all('Authorization') != ['Bearer ' + secret]
                    or not await peer({'client': ws.remote_address, 'server': ws.local_address}) or claimed):
                await ws.close(1008)
                return
            claimed = True
            pipe = Pipe(server)
            pipe.client = ws
            try:
                await bridge(ws, pipe, owner, tui)
            except Exception:
                failed.set()
        try:
            async with serve(accept, '127.0.0.1', 0, max_size=MAX, compression=None) as listener:
                url = 'ws://127.0.0.1:' + str(listener.sockets[0].getsockname()[1])
                click.echo('Windows managed Codex. Type openvegas use v, openvegas use host, or openvegas status.', err=True)
                tui = subprocess.Popen([exe, '--remote', url, '--remote-auth-token-env', 'OV_CODEX_UI_TOKEN'],
                        env={**env, 'OV_CODEX_UI_TOKEN': secret}, cwd=owner.cwd, shell=False)
                try:
                    peer.bind(tui)
                    while tui.poll() is None:
                        require(server.returncode is None and not failed.is_set())
                        await asyncio.sleep(.05)
                    return tui.returncode
                finally:
                    if tui.poll() is None:
                        tui.kill()
                        await asyncio.to_thread(tui.wait)
        finally:
            peer.close()
