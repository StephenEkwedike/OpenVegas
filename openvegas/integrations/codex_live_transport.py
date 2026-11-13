"""Private, single-native-client Unix transport for supervisor-owned Codex servers.

No route credentials, policy inference, prompt buffering or request retries. All
methods and the synchronous user-visible notice sink run on the same event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import stat
import struct
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State


class TransportRefused(RuntimeError):
    """The requested transition was not performed."""


class TransportUncertain(RuntimeError):
    """Cannot resume ordinary writes; never automatically retry or roll back."""


@dataclass(frozen=True)
class DetachProof:
    generation: int
    peer_pid: int
    connection_number: int


_MAX_FRAME = 4 * 1024 * 1024
_MAX_PENDING = 128
_TIMEOUT = 8
_LIMITS = {
    # Never inherit a root DEBUG logger that could persist native frame bodies.
    "logger": logging.Logger("codex-live-transport", level=logging.CRITICAL + 1),  # noqa: LOG001
    "max_size": _MAX_FRAME,
    "max_queue": 4,
    "write_limit": 65536,
    "compression": None,
    "open_timeout": 3,
    "close_timeout": 2,
    "ping_interval": None,
}
_RETRY = (
    "Not sent: billing transport is switching. Check the current payer, then re-enter your command."
)
_UNCERTAIN = (
    "Native connection lost. Submission status is uncertain. Check native history before retrying."
)
_READ = frozenset(
    (
        "account/read",
        "model/list",
        "configRequirements/read",
        "collaborationMode/list",
        "config/read",
        "plugin/list",
        "experimentalFeature/list",
        "skills/list",
        "thread/read",
        "thread/turns/list",
        "thread/items/list",
    )
)
_SCOPED_READ = frozenset(("thread/read", "thread/turns/list", "thread/items/list"))
_RESUME_KEYS = frozenset(("threadId", "excludeTurns", "initialTurnsPage"))
# During a hook, admit only pinned, non-authorizing reads with known parameters.
_INTENT_READ = {
    "account/read": {"refreshToken"},
    "model/list": {"cursor", "includeHidden", "limit"},
    "configRequirements/read": set(),
    "config/read": {"cwd", "includeLayers"},
    "thread/read": {"threadId", "includeTurns"},
    "thread/turns/list": {"threadId", "cursor", "itemsView", "limit", "sortDirection"},
    "thread/items/list": {"threadId", "turnId", "cursor", "limit", "sortDirection"},
}


class _Intent:
    def __init__(self, sid):
        self.token = object()
        self.sid = sid
        self.dirty = False


_TOP = [
    "cwd",
    "model",
    "reasoningEffort",
    "modelProvider",
    "sandbox",
    "activePermissionProfile",
    "approvalPolicy",
    "approvalsReviewer",
    "runtimeWorkspaceRoots",
    "disabledPluginIds",
    "instructionSources",
]
_THREAD = [
    "id",
    "modelProvider",
    "cliVersion",
    "ephemeral",
    "parentThreadId",
    "status",
    "canAcceptDirectInput",
    "cwd",
    "model",
    "environments",
    "sessionId",
    "forkedFromId",
]


def _projection(result):
    # Only memory retained after relaying the response. Unknown nested objects
    # are refused, not copied: an extension can carry arbitrary user data.
    data = {k: result[k] for k in _TOP}
    thread = {k: result["thread"][k] for k in _THREAD}
    nested = {
        "sandbox": {
            "type",
            "networkAccess",
            "excludeSlashTmp",
            "excludeTmpdirEnvVar",
            "writableRoots",
        },
        "activePermissionProfile": {"id", "extends"},
        "status": {"type", "activeFlags"},
        "environment": {"cwd", "environmentId", "runtimeWorkspaceRoots"},
    }

    def scalar(v):
        return v is None or isinstance(v, (str, bool))

    def leaf(v):
        return scalar(v) or isinstance(v, list) and all(isinstance(i, str) for i in v)

    def shape(k, v):
        return isinstance(v, dict) and set(v) <= nested[k] and all(leaf(x) for x in v.values())

    if (
        not all(leaf(v) for k, v in data.items() if k not in ("sandbox", "activePermissionProfile"))
        or not all(scalar(v) for k, v in thread.items() if k not in ("status", "environments"))
        or not shape("sandbox", data["sandbox"])
        or data["activePermissionProfile"] is not None
        and not shape("activePermissionProfile", data["activePermissionProfile"])
        or not shape("status", thread["status"])
        or not isinstance(thread["environments"], list)
        or not all(shape("environment", e) for e in thread["environments"])
    ):
        raise TransportUncertain("Unsupported native resume metadata.")
    data["thread"] = thread
    if len(json.dumps(data)) > 65536:
        raise TransportUncertain("Excessive native resume metadata.")
    return data


def _peer_pid(ws):
    sock = ws.transport.get_extra_info("socket")
    if sys.platform == "darwin":
        return sock.getsockopt(0, 2)  # SOL_LOCAL / LOCAL_PEERPID
    if sys.platform.startswith("linux"):
        pid, uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return pid if uid == os.geteuid() else None
    return None


def _peer_instance(ws, pid):
    if sys.platform == "darwin":
        # The kernel audit token includes the PID version: PID reuse is not
        # sufficient to become the reconnecting writer. Never log its contents.
        return ws.transport.get_extra_info("socket").getsockopt(0, 6, 32)
    if sys.platform.startswith("linux"):
        # Field 22 follows the parenthesized comm, which can contain spaces.
        return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()[19]
    raise TransportRefused("Unsupported Unix peer identity.")


def _path(endpoint):
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise TransportRefused("A private owned Unix server is required.")
    p = Path(endpoint[7:])
    parent, node = p.parent.lstat(), p.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
        or node.st_uid != os.geteuid()
    ):
        raise TransportRefused("Unsafe native socket or containing directory.")
    if stat.S_ISLNK(node.st_mode):
        backing = p.resolve(strict=True)
        daemon = Path("/tmp").resolve() / f"codex-daemon-{os.geteuid()}"
        info = backing.parent.lstat()
        if (
            sys.platform != "darwin"
            or backing.parent != daemon
            or not re.fullmatch("[0-9a-f]{64}", backing.name)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise TransportRefused("Unverified native socket alias.")
        p, node = backing, backing.lstat()
    if not stat.S_ISSOCK(node.st_mode) or node.st_uid != os.geteuid() or node.st_mode & 0o077:
        raise TransportRefused("Unsafe native socket ownership or mode.")
    return str(p)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous RPC.")
        result[key] = value
    return result


def _frame(raw):
    if not isinstance(raw, str):
        raise TypeError("Expected JSON text.")
    f = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(f, dict):
        raise TypeError("Expected one RPC.")
    has_id = "id" in f
    if has_id and (type(f["id"]) not in (str, int) or len(str(f["id"])) > 128):
        raise ValueError("Invalid RPC ID.")
    if "method" in f:
        if (
            not isinstance(f["method"], str)
            or not 0 < len(f["method"]) <= 180
            or "result" in f
            or "error" in f
            or not isinstance(f.get("params", {}), dict)
        ):
            raise ValueError("Invalid request.")
    elif not has_id or ("result" in f) == ("error" in f):
        raise ValueError("Invalid response.")
    return f


async def _send(ws, data):
    async with asyncio.timeout(3):
        await ws.send(data)


class LiveTransport:
    def __init__(self, source_endpoint, *, on_notice, directory=None):
        if not callable(on_notice):
            raise TypeError("A synchronous user-visible notice sink is required.")
        self._up = _path(source_endpoint)
        self._inode = os.stat(self._up)
        self._source_endpoint = source_endpoint
        self._proof = None
        self._projection = None
        self._notice = on_notice
        self._directory = directory
        self._lock = asyncio.Lock()
        self._route_ready = asyncio.Event()
        self._route_ready.set()
        self._resumed = asyncio.Event()
        self._released = asyncio.Event()
        self._released.set()
        self._pid = None
        self._instance = None
        self._client = None
        self._waiting = None
        self._out = {}
        self._in = {}
        self._dirty = False
        self._intent = None
        self._intent_revision = 0
        self._ephemerals = set()
        self._sid = None
        self._phase = "open"
        self._generation = 1
        self._connections = 0
        self._listener = self._temp = None

    @property
    def generation(self):
        return self._generation

    @property
    def state(self):
        return self._phase

    @property
    def connection_count(self):
        return self._connections

    @property
    def peer_pid(self):
        return self._pid

    async def __aenter__(self):
        self._temp = tempfile.TemporaryDirectory(prefix="ov-live-", dir=self._directory or "/tmp")
        path = Path(self._temp.name) / "t.sock"
        self.endpoint = "unix://" + str(path)
        try:
            self._listener = await unix_serve(self._handle, path=str(path), **_LIMITS)
            path.chmod(0o600)
        except BaseException:
            self._temp.cleanup()
            raise
        return self

    async def __aexit__(self, *_):
        self._intent = None
        self._proof = None
        self._projection = None
        self._phase = "closed"
        self._route_ready.set()
        self._resumed.set()
        if self._listener:
            self._listener.close()
            await self._listener.wait_closed()
        if self._temp:
            self._temp.cleanup()

    def bind_peer(self, pid):
        if self._pid is not None or type(pid) is not int or pid <= 1:
            raise TransportRefused("Bind exactly one owned TUI child PID.")
        self._pid = pid

    def _check(self, generation, phase):
        if self._phase in ("failed", "closed"):
            raise TransportUncertain("Transport is closed to further submissions.")
        if type(generation) is not int or self._generation != generation or self._phase != phase:
            raise TransportRefused("Stale generation or wrong transition state.")

    def set_intent(self, expected_generation, thread_id):
        """Called on the transport loop by the authenticated hook, before any await."""
        self._check(expected_generation, "open")
        if (
            self._intent is not None
            or self._lock.locked()
            or self._client is None
            or self._client.state is not State.OPEN
            or not isinstance(thread_id, str)
            or not 0 < len(thread_id) <= 128
            or self._sid is not None
            and self._sid != thread_id
        ):
            raise TransportRefused("Cannot guard this native switch. Original payer unchanged.")
        self._intent_revision += 1
        self._intent = _Intent(thread_id)
        return self._intent.token

    def release_intent(self, expected_generation, guard):
        if (
            type(expected_generation) is not int
            or expected_generation != self._generation
            or self._phase != "open"
            or self._intent is None
            or self._intent.token is not guard
        ):
            return False
        self._intent = None
        return True

    def _intent_allows(self, guard, method, params):
        if method == "thread/unsubscribe":
            sid = params.get("threadId")
            return (
                set(params) == {"threadId"}
                and isinstance(sid, str)
                and sid != guard.sid
                and sid in self._ephemerals
            )
        return (
            method in _INTENT_READ
            and params.keys() <= _INTENT_READ[method]
            and (method not in _SCOPED_READ or params.get("threadId") == guard.sid)
            and ("refreshToken" not in params or params["refreshToken"] is False)
        )

    async def freeze(self, expected_generation, *, intent=None):
        async with self._lock:
            self._check(expected_generation, "open")
            if (
                intent is not (self._intent.token if self._intent else None)
                or self._intent is not None
                and self._intent.dirty
            ):
                raise TransportRefused(
                    "Switch guard changed or refused new activity; original payer unchanged."
                )
            if not self._client or self._out or self._in:
                raise TransportRefused(
                    "Client absent or native RPCs outstanding; keep original payer."
                )
            self._dirty = False
            self._intent = None
            self._phase = "frozen"

    async def thaw(self, expected_generation):
        async with self._lock:
            self._check(expected_generation, "frozen")
            self._proof = None
            self._phase = "open"

    async def detach(self, expected_generation):
        async with self._lock:
            self._check(expected_generation, "frozen")
            if self._dirty or self._out or self._in or not self._client:
                raise TransportRefused("New activity during the fence; keep original payer.")
            self._notice(
                "Reconnecting native transport. Input is not queued. Check native history before retrying an unacknowledged submission."
            )
            self._phase = "detached"
            self._route_ready.clear()
            ws = self._client
        try:
            async with asyncio.timeout(_TIMEOUT):
                await ws.close(1000)
                await self._released.wait()
                self._check(expected_generation, "detached")
                self._proof = DetachProof(expected_generation, self._pid, self._connections)
                return self._proof
        except BaseException:
            self._fail()
            raise

    def assert_detached(self, proof, source_endpoint, expected_generation, expected_peer_pid):
        if (
            proof is None
            or proof is not self._proof
            or self._phase != "detached"
            or source_endpoint != self._source_endpoint
            or self._generation != expected_generation
            or proof.generation != expected_generation
            or expected_peer_pid != self._pid
            or proof.peer_pid != self._pid
            or proof.connection_number != self._connections
            or self._dirty
            or not self._released.is_set()
            or self._client is not None
            or self._out
            or self._in
        ):
            raise TransportRefused("No current detached transport proof for this source.")

    def attached_resume(self, endpoint, sid, generation, peer_pid):
        self._check(generation, "verifying")
        if (
            endpoint != self._source_endpoint
            or sid != self._sid
            or peer_pid != self._pid
            or self._client is None
            or self._client.state is not State.OPEN
            or _peer_pid(self._client) != peer_pid
            or _peer_instance(self._client, peer_pid) != self._instance
            or self._dirty
            or self._in
            or not self._resumed.is_set()
            or self._projection is None
        ):
            raise TransportRefused("No verified resume for this target, peer and generation.")
        return deepcopy(self._projection)

    async def attach(self, expected_generation, target_endpoint, thread_id):
        self._check(expected_generation, "detached")
        try:
            if self._proof is None or self._dirty:
                raise TransportUncertain("Source detach has not completed.")
            self._proof = None
            path = _path(target_endpoint)
            inode = os.stat(path)
            if (
                path == self._up
                or os.path.samestat(self._inode, inode)
                or not isinstance(thread_id, str)
                or not 0 < len(thread_id) <= 128
            ):
                raise TransportUncertain("Fresh owned target and exact thread ID required.")
            async with self._lock:
                self._check(expected_generation, "detached")
                self._up, self._sid = path, thread_id
                self._inode = inode
                self._source_endpoint = target_endpoint
                self._resumed.clear()
                self._phase = "verifying"
                self._route_ready.set()
            async with asyncio.timeout(_TIMEOUT):
                await self._resumed.wait()
            self._check(expected_generation, "verifying")
        except BaseException:
            self._fail()
            raise

    async def admit(self, expected_generation, *, commit=None):
        async with self._lock:
            self._check(expected_generation, "verifying")
            self.attached_resume(self._source_endpoint, self._sid, expected_generation, self._pid)
            if commit is not None:
                try:
                    commit()
                except BaseException:
                    self._fail()
                    raise
            self._projection = None
            self._generation += 1
            self._phase = "open"

    def _fail(self):
        self._intent = None
        self._proof = None
        self._projection = None
        if self._phase not in ("failed", "closed"):
            self._phase = "failed"
            self._resumed.set()
            self._route_ready.set()
            self._notice(_UNCERTAIN)

    async def abort(self):
        async with self._lock:
            self._fail()
            ws = self._client
        if ws is not None:
            await ws.close(1011, "Transport disabled")

    async def _refuse(self, ws, f, message):
        self._notice(message)
        if "id" not in f:
            raise TransportUncertain("Cannot acknowledge a refused notification.")
        await _send(ws, json.dumps({"id": f["id"], "error": {"code": -32001, "message": message}}))

    def _bootstrap(self, method, params):
        if method == "thread/resume":
            return params.get("threadId") == self._sid and all(
                k in _RESUME_KEYS or v is None for k, v in params.items()
            )
        if method == "account/read":
            return not params.get("refreshToken")
        if method == "initialize":
            return set(params) <= {"clientInfo", "capabilities"}
        if method in _SCOPED_READ:
            return params.get("threadId") == self._sid
        return method in _READ or method in ("initialize", "initialized")

    async def _pipe(self, ws, upstream, incoming, hello=None):
        src, dst = (ws, upstream) if incoming else (upstream, ws)
        sent, reverse = (self._out, self._in) if incoming else (self._in, self._out)

        async def dispatch(raw):
            f = _frame(raw)
            method = f.get("method")
            key = f.get("id")
            params = f.get("params", {})
            seen = self._phase
            revision = self._intent_revision

            def guard_refuses(guard):
                if guard is None or not method:
                    return False
                if not incoming:
                    return key is not None
                return key is None or not self._intent_allows(guard, method, params)

            guarded_out = guard_refuses(self._intent)
            if guarded_out:
                self._intent.dirty = True
            barred = (
                incoming
                and method
                and (
                    seen == "frozen" or seen == "verifying" and not self._bootstrap(method, params)
                )
            )
            if barred:
                self._dirty = True
            async with self._lock:
                phase = self._phase
                if ws is not self._client or phase in ("detached", "closed", "failed"):
                    if incoming and method and key is not None:
                        await self._refuse(ws, f, _RETRY)
                    return
                if method:
                    if key is not None and (key in sent or len(sent) >= _MAX_PENDING):
                        raise TransportUncertain("Ambiguous or excessive pending requests.")
                    if (
                        guarded_out
                        or guard_refuses(self._intent)
                        or incoming
                        and revision != self._intent_revision
                    ):
                        if self._intent is not None:
                            self._intent.dirty = True
                        await self._refuse(src, f, _RETRY)
                        return True
                    if barred or incoming and phase == "frozen":
                        self._dirty = True
                        await self._refuse(ws, f, _RETRY)
                        return True
                    if incoming and phase == "verifying" and not self._bootstrap(method, params):
                        self._dirty = True
                        await self._refuse(ws, f, _RETRY)
                        return True
                    if (
                        incoming
                        and phase == "verifying"
                        and method == "thread/resume"
                        and (self._resumed.is_set() or method in self._out.values())
                    ):
                        self._fail()
                        await self._refuse(ws, f, "Duplicate target resume; handoff stopped.")
                        return
                    if not incoming and phase == "frozen":
                        self._dirty = True
                    # Never carry a login/refresh from one payer into the other.
                    if (phase == "verifying" or self._generation > 1) and (
                        method.startswith(
                            (
                                "account/login",
                                "accountSession/",
                                "account/chatgptAuthTokens/",
                                "account/gatewayOAuth/",
                                "attestation/",
                            )
                        )
                        or method == "account/logout"
                    ):
                        await self._refuse(
                            src, f, "Authentication changes cannot cross this managed connection."
                        )
                        return True
                    if not incoming and key is not None and phase == "verifying":
                        self._fail()
                        await self._refuse(
                            src, f, "Unexpected target request during billing verification."
                        )
                        return
                    if key is not None:
                        sent[key] = method
                    if (
                        incoming
                        and method == "thread/unsubscribe"
                        and isinstance(params.get("threadId"), str)
                    ):
                        self._ephemerals.discard(params["threadId"])
                else:
                    if key not in reverse:
                        raise TransportUncertain("Unmatched native response.")
                    op = reverse.pop(key)
                    if not incoming and op == "thread/start" and isinstance(f.get("result"), dict):
                        thread = f["result"].get("thread")
                        if isinstance(thread, dict) and thread.get("ephemeral") is True:
                            sid = thread.get("id")
                            if (
                                isinstance(sid, str)
                                and 0 < len(sid) <= 128
                                and len(self._ephemerals) < _MAX_PENDING
                            ):
                                self._ephemerals.add(sid)
                    if not incoming and phase == "verifying" and op == "thread/resume":
                        if f.get("result", {}).get("thread", {}).get("id") != self._sid:
                            self._fail()
                            await _send(dst, raw)
                            return
                        self._projection = _projection(f["result"])
                        await _send(dst, raw)
                        self._resumed.set()
                        return True
                await _send(dst, raw)
                return True

        if hello is not None:
            if not await dispatch(hello):
                return
            del hello
        async for raw in src:
            if not await dispatch(raw):
                return
            del raw

    async def _handle(self, ws):
        claimed = False
        hello = None
        try:
            if self._pid is None or _peer_pid(ws) != self._pid:
                await ws.close(1008, "Wrong native peer")
                return
            identity = _peer_instance(ws, self._pid)
            if self._instance is not None and self._instance != identity:
                await ws.close(1008, "Native process identity changed")
                return
            if self._phase == "detached":
                if self._waiting:
                    await ws.close(1008, "Reconnect already waiting")
                    return
                self._waiting = ws
                # Do not bank a submitted action while no backend exists. Keep
                # only the native initialize handshake needed to open a target.
                async with asyncio.timeout(_TIMEOUT):
                    while not self._route_ready.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 0.03)
                        except TimeoutError:
                            continue
                        f = _frame(raw)
                        if (
                            hello is None
                            and f.get("method") == "initialize"
                            and "id" in f
                            and self._bootstrap("initialize", f.get("params", {}))
                        ):
                            hello = raw
                        else:
                            self._dirty = True
                            await self._refuse(ws, f, _RETRY)
                        del raw, f
            async with self._lock:
                if (
                    self._client
                    or self._waiting not in (None, ws)
                    or self._phase not in ("open", "verifying")
                ):
                    await ws.close(1008, "Writer unavailable")
                    return
                claimed = True
                self._client = ws
                self._instance = identity
                self._released.clear()
                self._connections += 1
                self._waiting = None
                path = self._up
            async with unix_connect(path, uri="ws://localhost/", proxy=None, **_LIMITS) as up:
                if not os.path.samestat(self._inode, os.stat(_path(self._source_endpoint))):
                    raise TransportUncertain("Native endpoint replaced.")
                tasks = [
                    asyncio.create_task(self._pipe(ws, up, v, hello if v else None))
                    for v in (True, False)
                ]
                del hello
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    normal_exit = (
                        tasks[0] in done
                        and tasks[0].exception() is None
                        and ws.close_code in (1000, 1005)
                        and not self._out
                        and not self._in
                    )
                    if normal_exit and self._phase == "open":
                        self._phase = "closed"
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ConnectionClosed, OSError, TimeoutError, TypeError, ValueError, TransportUncertain):
            pass
        finally:
            if self._waiting is ws:
                self._waiting = None
                self._fail()
            if claimed:
                async with self._lock:
                    if self._phase not in ("detached", "closed"):
                        self._fail()
                    self._client = None
                    self._intent = None
                    self._ephemerals.clear()
                    self._out.clear()
                    self._in.clear()
                    self._released.set()
