"""Opt-in native front connection with fenced, once-only backend replacement.

Keeps one submitted turn in bounded memory, never on disk. A failed send is
uncertain and is never replayed. Policy verification remains in LiveSwitch.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from copy import deepcopy
from dataclasses import dataclass

from websockets.asyncio.client import unix_connect
from websockets.protocol import State

from .codex_live_transport import (
    _LIMITS,
    _MAX_PENDING,
    _READ,
    _RETRY,
    _TIMEOUT,
    LiveTransport,
    TransportRefused,
    TransportUncertain,
    _frame,
    _path,
    _peer_instance,
    _peer_pid,
    _projection,
    _send,
)

_BOOTSTRAP_FLUSH_SECONDS = 8


@dataclass(frozen=True)
class BackendDetachProof:
    generation: int
    peer_pid: int
    connection_number: int
    endpoint: str
    device: int
    inode: int
    epoch: int
    peer_instance: bytes | str


class StableTransport(LiveTransport):
    """Experimental until real native race, cache and failure acceptance passes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._upstream = self._up_task = None
        self._epoch = 0
        self._hello = None
        self._pending = None
        self._internal = {}
        self._internal_prefix = "ov-internal-" + secrets.token_hex(16) + "-"
        self._serial = 0
        self._deferred = []
        self._deferred_size = 0
        self._rejections = set()
        self._rejecting_ids = set()

    def _front_current(self):
        return (self._client is not None and self._client.state is State.OPEN
                and _peer_pid(self._client) == self._pid
                and _peer_instance(self._client, self._pid) == self._instance)

    def set_intent(self, expected_generation, thread_id):
        token = super().set_intent(expected_generation, thread_id)
        self._sid = thread_id
        return token

    async def _reject_pending(self, message):
        pending, self._pending = self._pending, None
        if pending is not None:
            _, key, _, timer, _ = pending
            if timer is not asyncio.current_task():
                timer.cancel()
            self._rejecting_ids.add(key)
            try:
                if self._client is not None and self._client.state is State.OPEN:
                    await self._refuse(self._client, {"id": key}, message)
            finally:
                self._rejecting_ids.discard(key)

    def _reject_later(self, message):
        # Detach the payload synchronously before opening the original route.
        pending, self._pending = self._pending, None
        if pending is None:
            return
        _, key, _, timer, _ = pending
        timer.cancel()
        self._rejecting_ids.add(key)
        ws = self._client

        async def reject():
            try:
                if ws is not None and ws.state is State.OPEN:
                    await self._refuse(ws, {"id": key}, message)
            except Exception:  # noqa: BLE001 - No raw frame diagnostics.
                self._fail()
            finally:
                self._rejecting_ids.discard(key)

        task = asyncio.create_task(reject())
        self._rejections.add(task)
        task.add_done_callback(self._rejections.discard)

    def release_intent(self, generation, guard):
        if super().release_intent(generation, guard):
            self._reject_later("Not sent: billing switch was refused; original payer unchanged.")
            return True
        return False

    async def thaw(self, generation):
        async with self._lock:
            self._check(generation, "frozen")
            await self._reject_pending("Not sent: billing switch was refused; original payer unchanged.")
            self._proof = None
            self._phase = "open"

    def _fail(self):
        super()._fail()
        self._reject_later("Not sent: billing switch could not be verified.")
        self._deferred.clear()
        self._deferred_size = 0
        for future in self._internal.values():
            if not future.done():
                future.set_exception(TransportUncertain("Native backend disconnected."))

    async def _disconnect_backend(self):
        up, task = self._upstream, self._up_task
        self._upstream = None
        self._epoch += 1  # Old responses cannot enter the successor connection.
        if up is not None:
            await up.close(1000)
        if task is not None:
            async with asyncio.timeout(_TIMEOUT):
                await task
        self._up_task = None

    async def detach(self, generation):
        async with self._lock:
            self._check(generation, "frozen")
            if (self._dirty or self._out or self._in or not self._front_current()
                    or self._hello is None or self._upstream is None):
                raise TransportRefused("Backend fence or native handshake is unverified.")
            self._phase = "detached"
        try:
            await self._disconnect_backend()
            async with self._lock:
                self._check(generation, "detached")
                if self._dirty or not self._front_current():
                    raise TransportUncertain("Front changed during backend retirement.")
                self._proof = BackendDetachProof(
                    generation, self._pid, self._connections, self._source_endpoint,
                    self._inode.st_dev, self._inode.st_ino, self._epoch, self._instance)
                return self._proof
        except BaseException:
            self._fail()
            raise

    def assert_detached(self, proof, endpoint, generation, peer_pid):
        self._check(generation, "detached")
        if (not isinstance(proof, BackendDetachProof) or proof is not self._proof
                or (proof.generation, proof.peer_pid, proof.connection_number,
                    proof.endpoint, proof.device, proof.inode, proof.epoch, proof.peer_instance)
                != (generation, peer_pid, self._connections, self._source_endpoint,
                    self._inode.st_dev, self._inode.st_ino, self._epoch, self._instance)
                or endpoint != self._source_endpoint or peer_pid != self._pid
                or self._upstream is not None or self._up_task is not None
                or self._dirty or self._out or self._in or self._internal
                or not self._front_current()):
            raise TransportRefused("No joined backend-detachment proof for this native peer.")

    async def _connect_backend(self):
        epoch, phase, front, generation = self._epoch, self._phase, self._client, self._generation
        path, inode, endpoint = self._up, self._inode, self._source_endpoint
        up = await unix_connect(path, uri="ws://localhost/", proxy=None, **_LIMITS)
        try:
            async with self._lock:
                if (epoch != self._epoch or generation != self._generation
                        or phase != self._phase or phase not in ("open", "verifying")
                        or front is not self._client or not self._front_current()
                        or self._upstream is not None or endpoint != self._source_endpoint
                        or not os.path.samestat(inode, os.stat(_path(endpoint)))):
                    raise TransportUncertain("Native endpoint or front changed during connection.")
                self._upstream = up
                self._epoch += 1
                self._up_task = asyncio.create_task(self._backend(up, self._epoch))
        except BaseException:
            await up.close()
            raise

    async def _call(self, method, params):
        self._serial += 1
        key = self._internal_prefix + str(self._serial)
        future = asyncio.get_running_loop().create_future()
        self._internal[key] = future
        try:
            await _send(self._upstream, json.dumps({"id": key, "method": method, "params": params}))
            async with asyncio.timeout(_TIMEOUT):
                result = await future
            if "error" in result or not isinstance(result.get("result"), dict):
                raise TransportUncertain("Target initialization or resume failed.")
            return result["result"]
        finally:
            self._internal.pop(key, None)
            if not future.done():
                future.cancel()

    async def attach(self, generation, endpoint, thread_id):
        self.assert_detached(self._proof, self._source_endpoint, generation, self._pid)
        try:
            path, inode = _path(endpoint), os.stat(_path(endpoint))
            if (path == self._up or os.path.samestat(self._inode, inode)
                    or thread_id != self._sid):
                raise TransportUncertain("Fresh target and unchanged thread required.")
            self._proof = None
            self._up, self._inode, self._source_endpoint = path, inode, endpoint
            self._phase = "verifying"
            self._resumed.clear()
            await self._connect_backend()
            await self._call("initialize", deepcopy(self._hello))
            await _send(self._upstream, json.dumps({"method": "initialized", "params": {}}))
            result = await self._call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            self._check(generation, "verifying")
            self._projection = _projection(result)
            if result["thread"]["id"] != thread_id:
                raise TransportUncertain("Target resumed a different native thread.")
            self._resumed.set()
        except BaseException:
            self._fail()
            raise

    async def admit(self, generation, *, commit=None):
        async with self._lock:
            self._check(generation, "verifying")
            self.attached_resume(self._source_endpoint, self._sid, generation, self._pid)
            if not callable(commit):
                raise TransportRefused("An atomic payer commit is required.")
            commit()
            self._generation += 1
            self._phase = "open"
            self._projection = None
            try:
                async with asyncio.timeout(_BOOTSTRAP_FLUSH_SECONDS):
                    for raw in self._deferred:
                        await _send(self._client, raw)
                self._deferred.clear()
                self._deferred_size = 0
                pending, self._pending = self._pending, None
                if pending is not None:
                    raw, key, admitted_generation, timer, deadline = pending
                    timer.cancel()
                    if admitted_generation != generation or key in self._out:
                        raise TransportUncertain("Queued turn identity changed.")
                    if asyncio.get_running_loop().time() >= deadline:
                        await self._refuse(self._client, {"id": key}, "Not sent: billing verification timed out.")
                        return
                    self._out[key] = "turn/start"
                    # From this point any delivery failure is uncertain, never retryable.
                    await _send(self._upstream, raw)
            except TimeoutError:
                self._fail()
                raise TransportUncertain("Native bootstrap or dispatch timed out; no automatic retry.") from None
            except BaseException:
                self._fail()
                raise

    async def _queue(self, raw, frame):
        key = frame["id"]
        if self._pending is not None:
            if key == self._pending[1]:
                raise TransportUncertain("Duplicate queued native request ID.")
            await self._refuse(self._client, frame, "Not sent: one question is already waiting for the billing switch.")
            return

        async def expiry():
            await asyncio.sleep(30)
            async with self._lock:
                if self._pending is not None and self._pending[1] == key:
                    await self._reject_pending("Not sent: billing verification timed out.")

        self._pending = (raw, key, self._generation, asyncio.create_task(expiry()),
                         asyncio.get_running_loop().time() + 30)

    async def _front(self, raw):
        frame = _frame(raw)
        key, method, params = frame.get("id"), frame.get("method"), frame.get("params", {})
        async with self._lock:
            if isinstance(key, str) and key.startswith(self._internal_prefix):
                raise TransportUncertain("Reserved internal RPC ID.")
            if method and key is not None and (key in self._out or len(self._out) >= _MAX_PENDING):
                raise TransportUncertain("Ambiguous or excessive pending native requests.")
            if key is not None and (key in self._rejecting_ids
                    or self._pending is not None and key == self._pending[1]):
                raise TransportUncertain("Native request reused a queued turn ID.")
            fenced = self._intent is not None or self._phase != "open"
            if fenced:
                if (self._phase not in ("failed", "closed") and method == "turn/start"
                        and key is not None and params.get("threadId") == self._sid):
                    await self._queue(raw, frame)
                    return
                if (method == "turn/interrupt" and self._pending is not None
                        and key is not None and params.get("threadId") == self._sid):
                    await self._reject_pending("Not sent: waiting question cancelled.")
                    await self._refuse(self._client, frame, "Not sent: no target turn had started to interrupt.")
                    return
                if (self._phase == "open" and self._intent is not None
                        and method is not None and self._intent_allows(self._intent, method, params)):
                    pass
                else:
                    if method not in _READ:
                        self._dirty = True
                        if self._intent is not None:
                            self._intent.dirty = True
                    await self._refuse(self._client, frame, _RETRY)
                    return
            if self._generation > 1 and method and (
                    method.startswith(("account/login", "accountSession/", "account/chatgptAuthTokens/",
                                       "account/gatewayOAuth/", "attestation/")) or method == "account/logout"):
                await self._refuse(self._client, frame, "Authentication changes cannot cross this managed connection.")
                return
            if method == "initialize":
                if self._hello is not None or not self._bootstrap(method, params) or len(raw) > 8192:
                    raise TransportUncertain("Unsupported or repeated native handshake.")
                self._hello = deepcopy(params)
            if method:
                if key is not None:
                    self._out[key] = method
                if method == "thread/unsubscribe":
                    self._ephemerals.discard(params.get("threadId"))
            elif key not in self._in:
                raise TransportUncertain("Unmatched native client response.")
            else:
                self._in.pop(key)
            await _send(self._upstream, raw)

    async def _backend(self, upstream, epoch):
        try:
            async for raw in upstream:
                async with self._lock:
                    if epoch != self._epoch or upstream is not self._upstream:
                        return
                    frame = _frame(raw)
                    key, method = frame.get("id"), frame.get("method")
                    if not method and key in self._internal:
                        future = self._internal[key]
                        if future.done():
                            raise TransportUncertain("Duplicate internal response.")
                        future.set_result(frame)
                        continue
                    if method:
                        if (self._generation > 1 or self._phase == "verifying") and (
                                method.startswith(("account/login", "accountSession/",
                                                   "account/chatgptAuthTokens/", "account/gatewayOAuth/",
                                                   "attestation/")) or method == "account/logout"):
                            raise TransportUncertain("Authentication cannot cross this managed connection.")
                        if key is not None:
                            if self._phase != "open" or self._intent is not None:
                                raise TransportUncertain("Server requested new work during billing fence.")
                            if key in self._in or len(self._in) >= _MAX_PENDING:
                                raise TransportUncertain("Ambiguous server request.")
                            self._in[key] = method
                        elif self._phase == "frozen":
                            self._dirty = True
                    else:
                        if key not in self._out:
                            raise TransportUncertain("Unmatched native backend response.")
                        operation = self._out.pop(key)
                        if operation == "thread/start":
                            thread = frame.get("result", {}).get("thread", {})
                            if thread.get("ephemeral") is True:
                                sid = thread.get("id")
                                if isinstance(sid, str) and len(self._ephemerals) < _MAX_PENDING:
                                    self._ephemerals.add(sid)
                    if self._phase == "verifying":
                        self._deferred_size += len(raw.encode())
                        if key is not None or len(self._deferred) >= 128 or self._deferred_size > 1024 * 1024:
                            raise TransportUncertain("Unexpected or excessive target bootstrap messages.")
                        self._deferred.append(raw)
                    else:
                        await _send(self._client, raw)
        except Exception:  # noqa: BLE001 - Frames never enter diagnostic text.
            if epoch == self._epoch:
                self._fail()
        finally:
            if epoch == self._epoch and self._phase not in ("closed", "detached"):
                self._fail()
                if self._client is not None:
                    await self._client.close(1011, "Native backend uncertain")

    async def _handle(self, ws):
        claimed = False
        try:
            async with self._lock:
                if (self._pid is None or _peer_pid(ws) != self._pid or self._client is not None
                        or self._phase != "open" or self._instance is not None):
                    await ws.close(1008, "Native writer unavailable")
                    return
                self._instance = _peer_instance(ws, self._pid)
                self._client = ws
                self._connections += 1
                self._released.clear()
                claimed = True
            await self._connect_backend()
            async for raw in ws:
                await self._front(raw)
            if self._phase == "open" and not self._out and not self._in and self._pending is None:
                self._phase = "closed"
            else:
                self._fail()
        except Exception:  # noqa: BLE001 - Never log native frames.
            if claimed:
                self._fail()
        finally:
            if claimed:
                try:
                    await self._reject_pending("Not sent: native front disconnected.")
                finally:
                    try:
                        await self._disconnect_backend()
                    finally:
                        self._client = None
                        self._released.set()
                        self._hello = None
                        self._out.clear()
                        self._in.clear()

    async def __aexit__(self, *args):
        self._phase = "closed"
        try:
            await self._reject_pending("Not sent: managed terminal closed.")
        finally:
            try:
                await super().__aexit__(*args)
            finally:
                await self._disconnect_backend()
                if self._rejections:
                    await asyncio.gather(*self._rejections, return_exceptions=True)
