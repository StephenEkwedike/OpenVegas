"""Session-local inference transport. Not enabled by ordinary native launches.

The native client keeps one URL. Credentials only leave for the selected,
validated upstream; the local capability never does. Uncertain dispatches hold
payer changes. This transport does not infer task completion from idle sockets.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import socket
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, field

import httpx
from websockets.asyncio.client import connect

from .credentials import IntegrationError
from .inference_router_adapter import LOCAL_AUTH_HEADER
from .inference_router_state import RouterStateError

MAX_BODY = 16 * 1024 * 1024
MAX_EVENT = 1024 * 1024
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length'}


def anonymous_retry(headers):
    """A retry counter can reject a cross-payer retry, never identify an intent."""
    values = [v for k, v in headers if k.lower() == 'x-stainless-retry-count']
    if not values:
        return False
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit() or len(values[0]) > 6:
        raise IntegrationError('Ambiguous native retry metadata; nothing was sent.')
    return int(values[0]) > 0


class DirectConnect(connect):
    """Never forward authentication to a WebSocket redirect destination."""

    def process_redirect(self, exc):
        return exc


class DarwinTCPPeerAuth:
    """Authenticate the retained native process, not any user on loopback.

    Birth identity and established socket ownership are OS observations. This
    is not proof of task completion or ownership of detached background work.
    Never inspect native argv, environment, profile files or provider secrets.
    """

    def __init__(self):
        from .native_process import _library
        self.library = _library()
        self.process = self.identity = None
        self.bound = asyncio.Event()
        self.limit = asyncio.Semaphore(4)

    def bind(self, process):
        from .native_process import _identity
        if self.process is not None or process.returncode is not None:
            raise IntegrationError('Local router native process binding refused.')
        identity = _identity(self.library, process.pid)
        if identity[1] != os.getpid() or identity[3] != os.geteuid() or identity[4] == 5:
            raise IntegrationError('Local router requires its retained native child.')
        self.process, self.identity = process, identity
        self.bound.set()

    def _same_process(self):
        from .native_process import _identity
        if self.process is None or self.process.returncode is not None:
            return False
        current = _identity(self.library, self.process.pid)
        return (current[:4] == self.identity[:4] and current[5:] == self.identity[5:]
                and current[4] in (1, 2, 3, 4))

    async def _connections(self):
        child = await asyncio.create_subprocess_exec(
            '/usr/sbin/lsof', '-nP', '-a', '-p', str(self.process.pid),
            '-iTCP', '-sTCP:ESTABLISHED', '-Fn', stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=65536,
            env={'PATH': '/usr/bin:/bin:/usr/sbin', 'LANG': 'C'})
        try:
            data = await child.stdout.read(65537)
            if len(data) > 65536:
                return None
            await child.wait()
            return data if child.returncode == 0 else None
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    async def __call__(self, scope):
        client, server = scope.get('client'), scope.get('server')
        if not all(isinstance(endpoint, (list, tuple)) and len(endpoint) == 2
                   and endpoint[0] == '127.0.0.1' and type(endpoint[1]) is int
                   and 1 <= endpoint[1] <= 65535 for endpoint in (client, server)):
            return False
        expected = f'n127.0.0.1:{client[1]}->127.0.0.1:{server[1]}'.encode()
        try:
            async with asyncio.timeout(1):
                await self.bound.wait()
                async with self.limit:
                    if not self._same_process():
                        return False
                    data = await self._connections()
                    return (data is not None and expected in data.splitlines()
                            and self._same_process())
        except (OSError, ValueError, TimeoutError, IntegrationError):
            return False


@dataclass(eq=False)
class NativeSocket:
    mode: str
    generation: int
    active: object = None
    connecting: bool = True
    close: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Completion:
    """Recognize protocol completion, not payment settlement or task retirement."""

    def __init__(self, *, streaming, response_kind='passthrough', sensitive_values=()):
        self.streaming, self.completed = streaming, False
        self.response_kind = response_kind
        self._sensitive_values = tuple(value for value in sensitive_values if isinstance(value, str) and value)
        self.buffer = bytearray()
        self.invalid = streaming and response_kind == 'codex_alpha_search'

    def feed(self, data):
        if self.invalid:
            return
        if self.response_kind == 'codex_alpha_search' and len(self.buffer) + len(data) > MAX_EVENT:
            self.invalid = True
            self.buffer.clear()
            return
        self.buffer.extend(data)
        if self.streaming:
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                if line.startswith(b'data:'):
                    self._value(line[5:].strip())
                if len(line) > MAX_EVENT:
                    self.invalid = True
                    break
        if len(self.buffer) > MAX_EVENT:
            self.invalid = True
            self.buffer.clear()

    def _value(self, raw):
        if self.response_kind == 'codex_alpha_search':
            self._alpha_value(raw)
            return
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            return
        if not isinstance(value, dict):
            return
        if not self.streaming and self.response_kind == 'claude_token_count':
            count = value.get('input_tokens')
            self.completed = type(count) is int and 0 <= count <= 100_000_000
            return
        if not self.streaming and self.response_kind == 'codex_compaction':
            self.completed = (value.get('object') == 'response.compaction'
                              and isinstance(value.get('output'), list)
                              and isinstance(value.get('usage'), dict))
            return
        kind = value.get('type')
        if kind == 'message_stop':
            self.completed = True
        elif kind == 'response.completed':
            response = value.get('response')
            self.completed |= isinstance(response, dict) and response.get('status') == 'completed'
        elif ((kind == 'message' and value.get('stop_reason'))
              or (value.get('object') == 'response' and value.get('status') == 'completed')):
            self.completed = True

    def reflects_sensitive_value(self, value):
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and any(secret in item for secret in self._sensitive_values):
                return True
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
        return False

    def _alpha_value(self, raw):
        def pairs(items):
            result = dict(items)
            if len(result) != len(items):
                raise ValueError('Ambiguous Alpha JSON')
            return result

        def constant(_):
            raise ValueError('Nonfinite Alpha JSON')

        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        except (ValueError, RecursionError):
            self.invalid = True
            return
        if self.reflects_sensitive_value(value):
            self.invalid = True
            return
        # The reviewed Alpha envelope is not a Responses/Message completion.
        # Unknown fields (including error/status) cannot turn a refusal into success.
        self.completed = (not self.streaming and isinstance(value, dict)
            and not set(value) - {'id', 'output', 'encrypted_output', 'results'}
            and isinstance(value.get('output'), str) and bool(value['output'].strip())
            and ('id' not in value or isinstance(value['id'], str)
                 and 0 < len(value['id'].strip()) <= 256)
            and (value.get('encrypted_output') is None or isinstance(value['encrypted_output'], str))
            and (value.get('results') is None or isinstance(value['results'], list)))

    def finish(self):
        if not self.streaming and not self.invalid:
            self._value(bytes(self.buffer))
        return self.completed and not self.invalid

    def request_rejected(self, status):
        """Complete admission/validation errors are not interrupted inference.

        Timeouts, conflicts and server errors stay uncertain. A later rejection
        never reconciles or removes a previous attempt's uncertain outcome.
        """
        if self.response_kind == 'codex_alpha_search':
            self.finish()
        if status not in {400, 401, 402, 403, 404, 405, 413, 415, 422, 429} or self.streaming or self.invalid:
            return False
        try:
            value = json.loads(self.buffer)
            error = value.get('error') if isinstance(value, dict) else None
            return (isinstance(error, dict) and isinstance(error.get('type'), str)
                    and isinstance(error.get('message'), str))
        except (ValueError, RecursionError):
            return False


class InferenceRouter:
    def __init__(self, state, adapter, *, wallet_token=lambda: None,
                 client=None, capability=None, catalog_validator=None, auth_mode='path',
                 peer_auth=None, admission_wait=0):
        self.state, self.adapter = state, adapter
        from .inference_router_fixture import configured
        self.fixture = configured(adapter)
        self.wallet_token = wallet_token
        self.capability = capability or secrets.token_urlsafe(32)
        if (not isinstance(self.capability, str) or not self.capability.isascii()
                or not 32 <= len(self.capability) <= 128
                or not all(c.isalnum() or c in '-_' for c in self.capability)):
            raise ValueError('Invalid local router capability')
        if auth_mode not in ('path', 'header', 'peer') or ((auth_mode == 'peer') != (peer_auth is not None)):
            raise ValueError('Invalid local router authentication mode')
        self.auth_mode = auth_mode
        self.peer_auth = peer_auth
        self.authority = None
        self.client = client
        self.catalog_validator = catalog_validator
        self.sockets = set()
        self.admission_wait = admission_wait

    def __repr__(self):
        return '<InferenceRouter session-local>'

    def _path(self, scope):
        headers = [(k.decode('latin1').lower(), v.decode('latin1')) for k, v in scope['headers']]
        hosts = [v for k, v in headers if k == 'host']
        if (self.authority is None or hosts != [self.authority]
                or any(k == 'origin' for k, _ in headers)):
            raise IntegrationError('Local router access refused.')
        raw = scope.get('raw_path', b'').decode('ascii')
        local_headers = [v for k, v in headers if k == LOCAL_AUTH_HEADER]
        if self.auth_mode in ('header', 'peer'):
            authenticated = (not local_headers if self.auth_mode == 'peer' else
                             len(local_headers) == 1 and local_headers[0].isascii()
                             and hmac.compare_digest(local_headers[0], self.capability))
            if (not authenticated or not raw.startswith('/') or raw.startswith('//')
                    or '%' in raw or '\\' in raw or '..' in raw.split('/')):
                raise IntegrationError('Local router access refused.')
            return raw, [(k, v) for k, v in headers if k != LOCAL_AUTH_HEADER]
        if local_headers:
            raise IntegrationError('Local router access refused.')
        prefix, separator, tail = raw.lstrip('/').partition('/')
        if (not separator or not hmac.compare_digest(prefix, self.capability)
                or '%' in raw or '\\' in raw or '..' in tail.split('/')):
            raise IntegrationError('Local router access refused.')
        return '/' + tail, headers

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return
        try:
            path, headers = self._path(scope)
            if self.auth_mode == 'peer' and not await self.peer_auth(scope):
                raise IntegrationError('Local router access refused.')
        except (IntegrationError, UnicodeError, ValueError):
            await self._error(scope, send, 403, 'Local router access refused.')
            return
        if scope['type'] == 'websocket':
            await self._websocket(scope, receive, send, path, headers)
        else:
            await self._http(scope, receive, send, path, headers)

    @staticmethod
    async def _error(scope, send, code, message):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        body = json.dumps({'error': {'message': message, 'type': 'local_router_error'}}).encode()
        await send({'type': 'http.response.start', 'status': code,
                    'headers': [(b'content-type', b'application/json'), (b'x-should-retry', b'false')]})
        await send({'type': 'http.response.body', 'body': body})

    async def _http(self, scope, receive, send, path, headers):
        try:
            await self._wait_admission()
            query = scope.get('query_string', b'').decode('ascii')
            full_path = path + ('?' + query if query else '')
            plan = self.adapter.request_plan(scope['method'], full_path, headers,
                                             self.state.mode, self.wallet_token())
            lease = self.state.begin(scope['method'], path, retry=anonymous_retry(headers))
        except (IntegrationError, RouterStateError, ValueError):
            await self._error(scope, send, 409, 'Billing route is held or unsupported. Nothing was replayed.')
            return
        body = bytearray()
        handing_off = False
        lifetime = {'started': False}
        try:
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
                body.extend(event.get('body', b''))
                if len(body) > MAX_BODY:
                    await self._error(scope, send, 413, 'Native request exceeds the local transport limit.')
                    return
                if not event.get('more_body', False):
                    break
            # Admission precedes body awaits: switching cannot reassign an
            # already-uploading native request to the next payer.
            task = asyncio.create_task(self._forward(scope, send, plan, lease, bytes(body), lifetime))
            handing_off = True
        finally:
            if not handing_off:
                self.state.finish(lease, 'rejected')

        async def disconnected():
            while (await receive())['type'] != 'http.disconnect':
                pass

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            else:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            try:
                await asyncio.gather(task, watcher, return_exceptions=True)
            finally:
                if not lifetime['started']:
                    self.state.finish(lease, 'rejected')

    async def _forward(self, scope, send, plan, lease, body, lifetime):
        lifetime['started'] = True
        outcome, started = 'rejected', False
        try:
            # This is an OpenVegas dispatch identity, not provider idempotency
            # or proof that two native retries are the same logical operation.
            url = self.fixture.destination(plan, lease.mode) if self.fixture else plan.endpoint
            outgoing = [(k, v) for k, v in plan.headers if k.lower() != 'accept-encoding']
            outgoing.append(('accept-encoding', 'identity'))
            if (lease.mode == 'v' and scope['method'] == 'POST'
                    and getattr(plan, 'response_kind', None) != 'claude_token_count'):
                outgoing.append(('idempotency-key', 'router.' + lease.attempt_id))
            outcome = 'ambiguous'
            async with self.client.stream(scope['method'], url, headers=outgoing, content=body,
                                          follow_redirects=False) as response:
                if getattr(plan, 'response_kind', None) == 'codex_catalog_envelope':
                    if self.catalog_validator is None or response.status_code != 200:
                        raise IntegrationError('Native catalog verification unavailable.')
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY:
                            raise IntegrationError('Native catalog exceeds the transport limit.')
                    catalog = self.catalog_validator(json.loads(raw))
                    data = json.dumps(catalog, separators=(',', ':'), allow_nan=False).encode()
                    await send({'type': 'http.response.start', 'status': 200,
                                'headers': [(b'content-type', b'application/json')]})
                    started = True
                    await send({'type': 'http.response.body', 'body': data})
                    outcome = 'completed'
                    return
                response_headers = [(k, v) for k, v in response.headers.raw
                                    if k.decode().lower() not in HOP_HEADERS | {'set-cookie', 'location'}]
                if getattr(plan, 'response_kind', None) == 'codex_alpha_search':
                    sensitive = [self.capability]
                    for key, value in outgoing:
                        if key.lower() in {'authorization', 'x-api-key', 'chatgpt-account-id',
                                           'openai-organization', 'openai-project'}:
                            sensitive.append(value)
                            if key.lower() == 'authorization' and ' ' in value:
                                sensitive.append(value.split(' ', 1)[1])
                    media_type = response.headers.get('content-type', '').partition(';')[0].strip().lower()
                    completed = Completion(streaming=media_type == 'text/event-stream',
                        response_kind='codex_alpha_search', sensitive_values=sensitive)
                    completed.invalid |= (media_type != 'application/json'
                        or completed.reflects_sensitive_value(list(response.headers.multi_items())))
                    # Alpha is nonstreaming: verify a bounded body before exposing
                    # bytes, so malformed envelopes and reflected capabilities stay private.
                    async for chunk in response.aiter_raw():
                        completed.feed(chunk)
                        if completed.invalid:
                            break
                    success = completed.finish()
                    if completed.invalid or (200 <= response.status_code < 300
                                               and (response.status_code != 200 or not success)):
                        await self._error(scope, send, 502,
                            'Search response could not be verified. Original outcome retained; no retry or fallback.')
                        started = True
                        return
                    await send({'type': 'http.response.start', 'status': response.status_code,
                                'headers': response_headers})
                    started = True
                    await send({'type': 'http.response.body', 'body': bytes(completed.buffer)})
                    if response.status_code == 200 and success:
                        outcome = 'completed'
                    elif completed.request_rejected(response.status_code):
                        outcome = 'rejected'
                    return
                await send({'type': 'http.response.start', 'status': response.status_code,
                            'headers': response_headers})
                started = True
                completed = Completion(streaming='text/event-stream' in response.headers.get('content-type', ''),
                                       response_kind=getattr(plan, 'response_kind', 'passthrough'))
                async for chunk in response.aiter_raw():
                    completed.feed(chunk)
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                await send({'type': 'http.response.body', 'body': b''})
                if 200 <= response.status_code < 300 and (scope['method'] == 'GET' or completed.finish()):
                    outcome = 'completed'
                elif completed.request_rejected(response.status_code):
                    outcome = 'rejected'
        except (IntegrationError, RouterStateError, ValueError):
            if not started:
                await self._error(scope, send, 409, 'Billing route is held or unsupported. Nothing was replayed.')
        except (httpx.HTTPError, OSError):
            if not started:
                await self._error(scope, send, 502, 'Upstream outcome is uncertain. No payer fallback was attempted.')
            # Once headers have left, terminate the stream instead of fabricating
            # a successful completion or sending a second response header block.
            else:
                raise
        finally:
            # Model discovery and the reviewed free token-count API cannot
            # leave an inference charge. Their failures still reach the client.
            if outcome == 'ambiguous' and (scope['method'] == 'GET'
                    or getattr(plan, 'response_kind', None) == 'claude_token_count'):
                outcome = 'rejected'
            self.state.finish(lease, outcome)

    async def _websocket(self, scope, receive, send, path, headers):
        from websockets.exceptions import WebSocketException

        slot, tasks, outcome = None, [], 'rejected'
        try:
            await self._wait_admission()
            retry = anonymous_retry(headers)
            query = scope.get('query_string', b'').decode('ascii')
            full_path = path + ('?' + query if query else '')
            plan = self.adapter.request_plan('GET', full_path, headers, self.state.mode, self.wallet_token())
            handshake = self.state.begin('GET', path, retry=retry)
            slot = NativeSocket(self.state.mode, self.state.generation, handshake)
            self.sockets.add(slot)
            if (await receive())['type'] != 'websocket.connect':
                return
            endpoint = self.fixture.destination(plan, slot.mode) if self.fixture else plan.endpoint
            endpoint = endpoint.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
            outgoing = [(k, v) for k, v in plan.headers
                        if not k.lower().startswith('sec-websocket-') and k.lower() not in HOP_HEADERS | {'host'}]
            outcome = 'ambiguous'
            async with DirectConnect(endpoint, additional_headers=outgoing, proxy=None, max_size=MAX_BODY,
                                     open_timeout=10) as upstream:
                self.state.finish(slot.active, 'completed')
                slot.active, slot.connecting = None, False
                response_headers = [(k.lower().encode(), v.encode('latin1'))
                    for k, v in upstream.response.headers.raw_items()
                    if k.lower() not in HOP_HEADERS | {'set-cookie', 'location'}
                    and not k.lower().startswith('sec-websocket-')]
                await send({'type': 'websocket.accept', 'headers': response_headers})

                async def to_upstream():
                    while True:
                        event = await receive()
                        if event['type'] == 'websocket.disconnect':
                            return
                        message = event.get('text') if event.get('text') is not None else event['bytes']
                        value = json.loads(message)
                        if (not isinstance(value, dict) or value.get('type') != 'response.create'
                                or slot.active is not None
                                or (slot.mode, slot.generation) != (self.state.mode, self.state.generation)):
                            raise IntegrationError('Native socket request cannot be assigned to this payer.')
                        # A held socket is retired rather than forwarding a new
                        # response.create with its old credential after commit.
                        slot.active = self.state.begin('POST', path, retry=retry)
                        if self.fixture:
                            self.fixture.destination(plan, slot.mode)
                        await upstream.send(message)

                async def from_upstream():
                    async for message in upstream:
                        await send({'type': 'websocket.send', 'text' if isinstance(message, str) else 'bytes': message})
                        value = json.loads(message)
                        if (isinstance(value, dict) and value.get('type') == 'response.completed'
                                and isinstance(value.get('response'), dict)
                                and value['response'].get('status') == 'completed'):
                            if slot.active is None:
                                raise IntegrationError('Native completion has no admitted request.')
                            self.state.finish(slot.active, 'completed')
                            slot.active = None

                tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(from_upstream()),
                         asyncio.create_task(slot.close.wait())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
                await send({'type': 'websocket.close', 'code': 1000})
        except (IntegrationError, RouterStateError, ValueError, OSError, WebSocketException):
            await self._error(scope, send, 409, 'Native persistent transport is not ready to change payer.')
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if slot is not None:
                    try:
                        if slot.active is not None:
                            self.state.finish(slot.active, outcome)
                    finally:
                        self.sockets.discard(slot)
                        slot.done.set()

    async def _wait_admission(self):
        if not self.admission_wait or self.state.pending is None:
            return
        deadline = asyncio.get_running_loop().time() + self.admission_wait
        while self.state.pending is not None:
            if asyncio.get_running_loop().time() >= deadline:
                raise IntegrationError('Billing switch has not settled; no request was sent.')
            await asyncio.sleep(.005)

    async def close_idle_connections(self):
        """Retire only idle sockets under a held, independently verified boundary.

        Exact native Codex 0.166.0 restores full context on this clean reconnect.
        We do not strip previous_response_id, rebuild history or replay a turn.
        This alone does not prove detached source tasks have finished.
        """
        if self.state.pending is None:
            raise IntegrationError('Hold request admission before retiring native connections.')
        sockets = tuple(self.sockets)
        if any(slot.connecting or slot.active is not None for slot in sockets):
            raise IntegrationError('Native requests are still running; payer unchanged.')
        for slot in sockets:
            slot.close.set()
        async with asyncio.timeout(2):
            await asyncio.gather(*(slot.done.wait() for slot in sockets))


@asynccontextmanager
async def serve(router):
    """Start a private listener; shipping header mode never puts auth in a URL."""
    import uvicorn

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    sock.setblocking(False)
    router.authority = f'127.0.0.1:{sock.getsockname()[1]}'
    owned_client = router.client is None
    if owned_client:
        router.client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=10),
                                         follow_redirects=False)
    config = uvicorn.Config(router, lifespan='off', access_log=False, log_config=None,
                            log_level='critical', server_header=False, date_header=False,
                            timeout_graceful_shutdown=1, proxy_headers=False)
    server = uvicorn.Server(config)
    server.capture_signals = nullcontext
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    await task
                    raise IntegrationError('Local inference router did not start.')
                await asyncio.sleep(.01)
        prefix = '/' + router.capability if router.auth_mode == 'path' else ''
        yield f'http://{router.authority}{prefix}'
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 2)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        sock.close()
        router.authority = None
        if owned_client:
            await router.client.aclose()
            router.client = None
