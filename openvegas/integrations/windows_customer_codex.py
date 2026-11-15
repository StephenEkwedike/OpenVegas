"""A single authenticated native TUI over a retained Windows stdio app-server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass

import click
from websockets.asyncio.server import serve
from websockets.protocol import State

from .billing_host import exact_model, session_id
from .codex_handoff_policy import _identity
from .credentials import IntegrationError
from .inference_router import MAX_BODY
from .inference_router_adapter import VERSIONS, ProviderAdapter
from .inference_router_windows import WindowsTCPPeerAuth
from .use import parse_command
from .windows_customer import Check, CheckRefused, Selection, checking, require

MAX = 4 * 1024 * 1024
PREFIX = "ov-win-rpc-"
ORIGINS = {
    "apiKey": "https://api.openai.com/v1",
    "chatgpt": "https://chatgpt.com/backend-api/codex",
}


def _object(raw):
    def unique(pairs):
        value = dict(pairs)
        require(len(value) == len(pairs))
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise IntegrationError(
            "Native wallet request has no unambiguous thread identity."
        ) from None
    require(isinstance(value, dict))
    return value


def _root_proof(root, direct, metadata):
    require(session_id(root) and len(direct) <= 1 and len(metadata) <= 1)
    claims = list(direct)
    if metadata:
        require(isinstance(metadata[0], str) and len(metadata[0]) <= 16384)
        claims.append(_object(metadata[0]).get("thread_id"))
    require(bool(claims) and all(value == root for value in claims))


@dataclass(frozen=True, repr=False)
class RootAdapter(ProviderAdapter):
    root: object

    def request_plan(self, method, path, headers, mode, wallet_token=None):
        pairs = tuple(headers.items() if isinstance(headers, dict) else headers)
        plan = super().request_plan(method, path, pairs, mode, wallet_token)
        if mode == "v" and plan.response_kind != "codex_catalog_envelope":
            # Checked after the admission wait, before acquiring/sending the upstream lease.
            _root_proof(
                self.root(),
                [v for k, v in pairs if k.lower() == "thread-id"],
                [v for k, v in pairs if k.lower() == "x-codex-turn-metadata"],
            )
        return plan


class RootIngress:
    """A root-bound handshake is not authority for a sibling's next frame."""

    def __init__(self, inner, root):
        self.inner, self.root = inner, root

    def __getattr__(self, name):
        return getattr(self.inner, name)

    authority = property(lambda s: s.inner.authority, lambda s, v: setattr(s.inner, "authority", v))
    client = property(lambda s: s.inner.client, lambda s, v: setattr(s.inner, "client", v))

    async def __call__(self, scope, receive, send):
        async def bound():
            event = await receive()
            if event["type"] == "websocket.receive" and self.inner.state.mode == "v":
                raw = event.get("text") if event.get("text") is not None else event.get("bytes")
                require(isinstance(raw, (str, bytes)) and len(raw) <= MAX_BODY)
                body = _object(raw)
                meta = body.get("client_metadata")
                require(body.get("type") == "response.create" and isinstance(meta, dict))
                _root_proof(
                    self.root(),
                    [meta.get("thread_id")],
                    [meta["x-codex-turn-metadata"]] if "x-codex-turn-metadata" in meta else [],
                )
            return event

        return await self.inner(scope, bound if scope["type"] == "websocket" else receive, send)


@asynccontextmanager
async def server_process(exe, env, cwd, overlay=()):
    p = await asyncio.create_subprocess_exec(
        exe,
        *overlay,
        "--no-daemon",
        "app-server",
        "--listen",
        "stdio://",
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        limit=MAX,
    )
    try:
        yield p
    finally:
        p.stdin.close()

        # A paused/full stdout pipe can prevent an asyncio process wait from
        # finishing, even after exit. All protocol readers have retired here.
        async def drain():
            while await p.stdout.read(65536):
                pass

        discard = asyncio.create_task(drain())
        try:
            try:
                await asyncio.wait_for(p.wait(), 3)
            except TimeoutError:
                if p.returncode is None:
                    p.kill()
                await p.wait()
        finally:
            discard.cancel()
            await asyncio.gather(discard, return_exceptions=True)


class NativeThreads:
    """IDs only. No ambient thread discovery or inspection of another chat."""

    def __init__(self):
        self.pending = {}
        self.roots, self.cold, self.used = set(), set(), set()
        self.revision = 0
        self.ephemeral_work = 0

    def fence(self, bounded):
        # A known ephemeral cannot spend against a root-bound provider. Its
        # turn notifications aren't root work; its lifecycle still matters.
        return bounded, self.revision - (self.ephemeral_work if bounded else 0)

    def sent(self, msg):
        method, key = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method in ("thread/start", "thread/resume", "thread/fork", "thread/unsubscribe"):
            require(key is not None and key not in self.pending and len(self.pending) < 128)
            self.pending[key] = (method, params.get("threadId"))
            self.revision += 1
        elif method in ("turn/start", "turn/steer"):
            self.work(params.get("threadId"))

    def work(self, sid):
        if session_id(sid):
            require(sid in self.used or len(self.used) < 256)
            self.used.add(sid)
            self.revision += 1
            if sid in self.cold:
                self.ephemeral_work += 1

    def received(self, msg):
        method = msg.get("method")
        if method is not None:
            if method in ("turn/started", "turn/completed"):
                self.work((msg.get("params") or {}).get("threadId"))
            return
        item = self.pending.pop(msg.get("id"), None)
        if item is None:
            return
        self.revision += 1
        if "error" in msg:
            return
        op, previous = item
        if op == "thread/unsubscribe":
            self.roots.discard(previous)
            return
        thread = msg["result"]["thread"]
        sid = thread["id"]
        require(session_id(sid) and len(self.roots | self.cold | {sid}) <= 256)
        if thread.get("ephemeral") is True:
            require(sid not in self.roots)
            self.cold.add(sid)
        elif thread.get("ephemeral") is False and thread.get("parentThreadId") is None:
            require(sid not in self.cold)
            self.roots.add(sid)

    def verify(self, sid, loaded, *, bounded=False):
        ids = loaded.get("data")
        require(
            not self.pending
            and sid in self.roots
            and isinstance(ids, list)
            and 0 < len(ids) <= 32
            and all(session_id(i) for i in ids)
            and len(set(ids)) == len(ids)
            and sid in ids
            and loaded.get("nextCursor") is None
            and set(ids) - {sid} <= (self.cold if bounded else self.cold - self.used)
        )


class Pipe:
    """Supervisor RPC results never reach the TUI. Native RPCs pass unchanged."""

    def __init__(self, process):
        self.p = process
        self.waiters = {}
        self.client = None
        self.write_lock = asyncio.Lock()
        self.threads = NativeThreads()

    async def send(self, message):
        raw = json.dumps(
            message, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode()
        require(len(raw) <= MAX)
        async with self.write_lock:
            self.threads.sent(message)
            self.p.stdin.write(raw + b"\n")
            await self.p.stdin.drain()

    async def call(self, method, params):
        key = PREFIX + secrets.token_hex(16)
        f = self.waiters[key] = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(6):
                await self.send({"id": key, "method": method, "params": params})
                return await f
        finally:
            self.waiters.pop(key, None)

    async def read(self):
        try:
            while raw := await self.p.stdout.readline():
                require(len(raw) <= MAX and raw.endswith(b"\n"))
                packet = json.loads(raw)
                self.threads.received(packet)
                key = packet.get("id")
                if isinstance(key, str) and key.startswith(PREFIX):
                    require(not packet.get("method"))
                    future = self.waiters.get(key)
                    if future is not None and not future.done():
                        if "result" in packet and "error" not in packet:
                            future.set_result(packet["result"])
                        else:
                            future.set_exception(IntegrationError("Native metadata read refused."))
                elif self.client:
                    await self.client.send(raw.decode("utf-8"))
                else:
                    require(key is None)
        finally:
            for future in self.waiters.values():
                if not future.done():
                    future.set_exception(IntegrationError("Native metadata connection ended."))


async def route(pipe, cwd, expected=None):
    cfg = (await pipe.call("config/read", {"cwd": cwd, "includeLayers": True}))["config"]
    require(
        cfg.get("model_provider") in (None, "openai")
        and not (cfg.get("model_providers") or {}).get("openai")
        and cfg.get("openai_base_url")
        in ((None, "", ORIGINS["apiKey"]) if expected is None else (expected,))
    )
    kind = (await pipe.call("account/read", {"refreshToken": False}))["account"]["type"]
    require(kind in ORIGINS)
    return ORIGINS[kind]


async def original_upstream(exe, env, cwd):
    require(
        not any(
            k in env
            for k in (
                "CODEX_CONFIG",
                "CODEX_PROVIDER",
                "OPENAI_BASE_URL",
                "CHATGPT_BASE_URL",
                "CODEX_APP_SERVER_ADDRESS",
            )
        )
    )
    async with server_process(exe, env, cwd) as p:
        pipe = Pipe(p)
        reader = asyncio.create_task(pipe.read())
        try:
            await pipe.call(
                "initialize",
                {
                    "clientInfo": {"name": "openvegas", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await pipe.send({"method": "initialized", "params": {}})
            return await route(pipe, cwd)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def action_for(method, params):
    if method not in ("turn/start", "turn/steer"):
        return None
    items = params.get("input")
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or items[0].get("type") != "text"
    ):
        return None
    text = items[0].get("text")
    if not isinstance(text, str) or items[0].get("text_elements") or items[0].get("textElements"):
        return None
    if text.strip(" \t").lower() == "openvegas status":
        return "status"
    parsed = parse_command(text)
    return parsed[0] if parsed and parsed[0] in ("v", "host") else None


def root_bounded(session):
    return (
        isinstance(session.router, RootIngress)
        and isinstance(session.adapter, RootAdapter)
        and session.router.inner.adapter is session.adapter
        and session.router.root is session.adapter.root
    )


def _drive_key(value):
    if not isinstance(value, str):
        return None
    if value.startswith("\\\\?\\"):
        value = value[4:]
        if "/" in value:
            return None
    else:
        value = value.replace("/", "\\")
    if not re.fullmatch(r"[A-Za-z]:\\.*", value):
        return None
    for part in value[3:].split("\\") if len(value) > 3 else ():
        # Only equate ordinary DOS paths with their unambiguous verbatim spelling.
        # In particular, stripping a prefix from a device or trailing-dot name
        # can change what it opens. Never resolve links or fold directory case.
        if (
            not part
            or part.endswith((" ", "."))
            or re.search(r'[\x00-\x1f<>:"|?*]', part)
            or re.fullmatch(
                r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9\u00b9\u00b2\u00b3]",
                part.partition(".")[0].rstrip(" "),
                re.ASCII | re.IGNORECASE,
            )
        ):
            return None
    return value[0].upper() + value[1:]


def _root_identity(thread, sid, cwd):
    # The shared identity verifier compares strings. The protocol types differ:
    # thread.cwd is AbsolutePathBuf; environment cwd and roots are legacy strings.
    # Preserve its policy checks; compare all three original paths before adapting
    # their spelling. No directory lookup or interpretation of other workspaces.
    if os.name != "nt" and cwd.startswith("/") and not cwd.startswith("//"):
        return _identity(thread, sid)
    with checking(Check.WORKSPACE):
        key = _drive_key(cwd)
        envs = thread["environments"]
        require(key is not None and isinstance(envs, list) and len(envs) == 1)
        env = envs[0]
        roots = env["runtimeWorkspaceRoots"]
        require(isinstance(roots, list) and len(roots) == 1)
        require(all(_drive_key(p) == key for p in (thread["cwd"], env["cwd"], roots[0])))
    return _identity(
        {
            **thread,
            "cwd": cwd,
            "environments": [{**env, "cwd": cwd, "runtimeWorkspaceRoots": [cwd]}],
        },
        sid,
    )


async def selection(pipe, owner, params):
    sid = params.get("threadId")
    with checking(Check.ROUTE):
        require(await route(pipe, owner.cwd, owner.session._url) == owner.session.adapter.host_base)
    with checking(Check.INVENTORY):
        loaded = await pipe.call("thread/loaded/list", {"limit": 32})
        pipe.threads.verify(sid, loaded, bounded=root_bounded(owner.session))
    with checking(Check.IDENTITY):
        thread = (await pipe.call("thread/read", {"threadId": sid, "includeTurns": False}))[
            "thread"
        ]
        native = _root_identity(thread, sid, owner.cwd)
    # The TUI may have picked a new model for this turn without changing
    # thread/read. Use only explicit, agreeing native turn selections.
    with checking(Check.MODEL):
        names = [
            v
            for v in (
                params.get("model"),
                ((params.get("collaborationMode") or {}).get("settings") or {}).get("model"),
            )
            if v is not None
        ]
        require(not names or (all(exact_model(v) for v in names) and len(set(names)) == 1))
    return Selection(sid, names[0] if names else native.model, native.cwd, owner.session._url)


def picker_write(method, params):
    """Forward the native picker's own model/effort writes, not routing or policy edits."""
    if params.get("filePath") is not None:
        return False
    if method == "config/value/write":
        edits = [params]
    elif method == "config/batchWrite":
        edits = params.get("edits")
    else:
        return False
    return (
        isinstance(edits, list)
        and 0 < len(edits) <= 2
        and all(
            isinstance(edit, dict)
            and edit.get("keyPath") in ("model", "model_reasoning_effort")
            and (edit.get("value") is None or isinstance(edit["value"], str))
            for edit in edits
        )
    )


async def bridge(ws, pipe, owner, tui):
    def guard():
        require(ws.state is State.OPEN and tui.poll() is None and pipe.p.returncode is None)

    async def reply(key, message):
        # An intercepted turn did not start. Do not invent a completed native turn.
        await ws.send(json.dumps({"id": key, "error": {"code": -32000, "message": message}}))

    async def control(key, action, params):
        def fence():
            return pipe.threads.fence(root_bounded(owner.session))

        revision = fence()

        def stable():
            with checking(Check.FENCE):
                guard()
                require(fence() == revision)

        try:
            message = await owner.command(
                action, observe=lambda: selection(pipe, owner, params), guard=stable
            )
        except CheckRefused as exc:
            message = (
                "Local control unconfirmed. Check status; no automatic retry. Control check: "
                + exc.step.value
                + "."
            )
        except Exception:  # noqa: BLE001 - Never expose native frames or credential errors.
            message = "Local control unconfirmed. Check status; no automatic retry."
        await reply(key, message)

    reader = asyncio.create_task(pipe.read())
    pending = None

    async def front():
        nonlocal pending
        async for raw in ws:
            msg = json.loads(raw)
            key, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
            require(not (isinstance(key, str) and key.startswith(PREFIX)))
            if pending is not None and pending.done():
                await pending
                pending = None
            action = action_for(method, params)
            if action:
                require(key is not None)
                if pending is not None or method == "turn/steer":
                    await reply(key, "Local control busy. Finish or cancel the turn, then retry.")
                else:
                    pending = asyncio.create_task(control(key, action, params))
            elif method and method != "turn/interrupt" and pending is not None and key is not None:
                await reply(key, "Local control pending; request not sent. Retry explicitly.")
            elif method and (
                method.startswith(("account/login", "account/logout", "config/write"))
                or (
                    method in ("config/value/write", "config/batchWrite")
                    and not picker_write(method, params)
                )
                or (
                    owner.session.state.mode == "v"
                    and (
                        method in ("thread/start", "thread/resume", "thread/fork")
                        or (
                            method in ("turn/start", "turn/steer")
                            and params.get("threadId") != owner.sid
                        )
                    )
                )
            ):
                await reply(key, "Return to host billing and exit managed mode for this operation.")
            else:
                if method in ("thread/start", "thread/resume", "thread/fork"):
                    require(
                        await route(pipe, owner.cwd, owner.session._url)
                        == owner.session.adapter.host_base
                    )
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
        await asyncio.gather(
            *(t for t in (writer, reader, pending) if t is not None), return_exceptions=True
        )


async def run(owner, exe, env):
    session = owner.session
    async with (
        session.launch(
            version=VERSIONS["codex"], env=env, args=[], effective_policy_verified=True
        ) as (child_env, overlay),
        server_process(exe, child_env, owner.cwd, overlay) as server,
    ):
        # asyncio owns the process; the Windows verifier duplicates the actual
        # CreateProcess handle. Never re-open by a PID from a frame or process list.
        session.bind_native(server._transport.get_extra_info("subprocess"))
        peer = WindowsTCPPeerAuth()
        secret = secrets.token_urlsafe(32)
        tui = None
        claimed = False
        failed = asyncio.Event()

        async def accept(ws):
            nonlocal claimed
            if (
                ws.request.path != "/"
                or ws.request.headers.get_all("Authorization") != ["Bearer " + secret]
                or not await peer({"client": ws.remote_address, "server": ws.local_address})
                or claimed
            ):
                await ws.close(1008)
                return
            claimed = True
            pipe = Pipe(server)
            pipe.client = ws
            try:
                await bridge(ws, pipe, owner, tui)
            except Exception:  # noqa: BLE001 - Abort the owned UI without logging native frames.
                failed.set()

        try:
            async with serve(accept, "127.0.0.1", 0, max_size=MAX, compression=None) as listener:
                url = "ws://127.0.0.1:" + str(listener.sockets[0].getsockname()[1])
                click.echo(
                    "Windows managed Codex. Type openvegas use v, openvegas use host, or openvegas status.",
                    err=True,
                )
                tui = subprocess.Popen(  # noqa: ASYNC220 - Retain CreateProcess handle before yielding.
                    [exe, "--remote", url, "--remote-auth-token-env", "OV_CODEX_UI_TOKEN"],
                    env={**env, "OV_CODEX_UI_TOKEN": secret},
                    cwd=owner.cwd,
                    shell=False,
                )
                try:
                    peer.bind(tui)
                    while tui.poll() is None:
                        require(server.returncode is None and not failed.is_set())
                        await asyncio.sleep(0.05)
                    return tui.returncode
                finally:
                    if tui.poll() is None:
                        tui.kill()
                        await asyncio.to_thread(tui.wait)
        finally:
            peer.close()
