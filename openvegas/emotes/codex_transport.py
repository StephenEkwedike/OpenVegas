"""Explicit, POSIX stdio launcher for a NEW owned Codex app-server process.

This is a transport for an app-server frontend, not a replacement interactive
Codex UI. Both protocol directions and stderr are forwarded byte-for-byte. The
frontend still owns initialization, prompts, approvals and rendering. We never
attach to an existing process, read transcripts, or configure hooks/notifiers.

Only the locally reviewed 0.153.4 schema is enabled. Ordinary protocol data is
held transiently in bounded buffers; only projected lifecycle metadata reaches
the existing private emote spool. The caller must NOT log these protocol bytes.
The child may have its own Codex history policy; this wrapper cannot disable or
promise absence of that history. No provider calls occur until the frontend
sends them. Importing this module or declining opt-in launches nothing.

Schema: `codex app-server generate-json-schema` from codex-cli 0.153.4.
Docs: https://learn.chatgpt.com/docs/app-server
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import signal
import stat
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .adapters import supports_codex_app_server
from .events import IDENTITY
from .owned_stream import (
    METHODS,
    OwnedCodexStream,
    OwnedStreamAuthorization,
    OwnedStreamRecord,
    project_codex_lifecycle,
)
from .spool import EventSpool

CHUNK_BYTES = 65536
MAX_LINE_BYTES = 1024 * 1024
VERSION_BYTES = 256
HEARTBEAT_POLL_SECONDS = 1.0
ByteSink = Callable[[bytes], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TransportResult:
    """Content-free outcome; refusal is not an upstream exit code."""

    launched: bool
    returncode: int | None
    reason: str


def _identity(value: object) -> bool:
    return type(value) is str and IDENTITY.fullmatch(value) is not None


def _seconds(value: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0.05 <= value <= 30


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-JSON scalar")


class _Lines:
    """Bound parsing without ever dropping bytes from the actual protocol."""

    def __init__(self, consume, disable, limit=MAX_LINE_BYTES):
        self._consume, self._disable, self._limit = consume, disable, limit
        self._pending = bytearray()
        self.enabled = True

    def disable(self, reason):
        if self.enabled:
            self.enabled = False
            self._pending.clear()
            self._disable(reason)

    def feed(self, data: bytes) -> None:
        offset = 0
        while self.enabled and offset < len(data):
            newline = data.find(b"\n", offset)
            end = len(data) if newline < 0 else newline
            if len(self._pending) + end - offset > self._limit:
                self.disable("oversized_record")
                return
            self._pending.extend(data[offset:end])
            if newline < 0:
                return
            try:
                message = json.loads(
                    self._pending.decode("utf-8"), object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
                if type(message) is not dict:
                    raise ValueError("Expected one protocol object")
            except (ValueError, UnicodeError, RecursionError):
                self.disable("invalid_record")
                return
            self._pending.clear()
            self._consume(message)
            offset = newline + 1

    def eof(self) -> None:
        if self._pending:
            self.disable("incomplete_record")
        self._pending.clear()


class _Lifecycle:
    """One explicitly selected thread, or the first outbound turn/start thread."""

    def __init__(self, session_id, version, spool, thread_id=None):
        self.session_id, self.version, self.spool = session_id, version, spool
        self.thread_id = None
        self.stream_id = uuid4().hex
        self.observer = None
        self.ordinal = 0
        self.reason = ""
        if thread_id is not None:
            self.bind(thread_id)

    def bind(self, thread_id):
        if self.reason or self.observer is not None:
            return
        if not _identity(thread_id):
            self.disable("invalid_thread")
            return
        self.thread_id = thread_id
        self.observer = OwnedCodexStream(OwnedStreamAuthorization(
            owner_id="codex-stdio-launcher", stream_id=self.stream_id,
            thread_id=thread_id, session_id=self.session_id, host_version=self.version,
            owns_stream=True, ordered=True, metadata_only=True,
        ), enabled=True, spool=self.spool)

    def disable(self, reason):
        if not self.reason:
            self.reason = reason
        if self.observer is not None:
            self.observer.close()

    def client(self, message):
        if self.reason:
            return
        method, params = message.get("method"), message.get("params")
        if method not in ("turn/start", "turn/interrupt"):
            return
        if type(params) is not dict:
            self.disable("invalid_client_lifecycle")
            return
        if method == "turn/start" and self.thread_id is None:
            self.bind(params.get("threadId"))
        elif method == "turn/interrupt" and params.get("threadId") == self.thread_id:
            turn_id = params.get("turnId")
            if not _identity(turn_id):
                self.disable("invalid_client_lifecycle")
            elif self.observer is not None:
                # Latch BEFORE forwarding the interrupt, including before START.
                self.observer.cancel(turn_id)

    def server(self, message):
        if self.reason or self.observer is None:
            return
        method = message.get("method")
        if type(method) is not str or method not in METHODS:
            return
        params = message.get("params")
        if type(params) is not dict:
            self.disable("invalid_server_lifecycle")
            return
        thread_id = params.get("threadId")
        if not _identity(thread_id):
            self.disable("invalid_server_lifecycle")
            return
        if thread_id != self.thread_id:
            return  # Subagents and other foreground tabs never animate this pane.
        metadata = project_codex_lifecycle(message, host_version=self.version)
        if metadata is None:
            self.disable("invalid_server_lifecycle")
            return
        self.observer.deliver(OwnedStreamRecord(
            "codex-stdio-launcher", self.stream_id, self.ordinal, metadata,
        ))
        self.ordinal += 1
        if not self.observer.enabled:
            self.disable(self.observer.reason)

    def close(self):
        if self.observer is not None:
            self.observer.close()

    def heartbeat(self):
        if self.reason or self.observer is None:
            return
        turn = self.observer.active_turn
        if turn is not None:
            self.observer.heartbeat(turn=turn)
        if not self.observer.enabled:
            self.disable(self.observer.reason)


async def _stop_owned_process(process, grace):
    """Only the process group created with start_new_session=True is signalled."""
    waiter = asyncio.create_task(process.wait())
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, sig)
            await asyncio.wait({waiter}, timeout=grace)
            # The leader can exit before an owned descendant closes inherited
            # pipes. Retire the residual group even if wait() has completed.
    finally:
        # Close OUR transport, never the caller's streams. A full unread pipe or
        # escaped descendant otherwise leaves asyncio's pipe-dependent wait and
        # destructor alive after the event loop has closed.
        process._transport.close()
        try:
            await asyncio.wait_for(waiter, timeout=grace)
        finally:
            await asyncio.sleep(0)


async def _protected_cleanup(cleanup):
    """A second Ctrl+C must not abandon the first cancellation's owned child."""
    task = asyncio.create_task(cleanup)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _version(executable, *, cwd, env, timeout, grace):
    process = await asyncio.create_subprocess_exec(
        executable, "--version", stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd, env=env, start_new_session=True, limit=VERSION_BYTES + 1,
    )
    try:
        async def read():
            value = bytearray()
            while chunk := await process.stdout.read(VERSION_BYTES + 1 - len(value)):
                value.extend(chunk)
                if len(value) > VERSION_BYTES:
                    return None
            if await process.wait() != 0:
                return None
            match = re.fullmatch(rb"codex-cli (\d+)\.(\d+)\.(\d+)\r?\n?", value)
            return tuple(map(int, match.groups())) if match else None

        return await asyncio.wait_for(read(), timeout=timeout)
    finally:
        await _protected_cleanup(_stop_owned_process(process, grace))


async def run_codex_transport(
    command: Sequence[str], *, enabled: bool = False, session_id: str,
    input_bytes: AsyncIterable[bytes], stdout: ByteSink, stderr: ByteSink,
    thread_id: str | None = None, spool: EventSpool | None = None,
    cwd: str | os.PathLike | None = None, env: Mapping[str, str] | None = None,
    version_timeout: float = 3.0, shutdown_grace: float = 1.0,
) -> TransportResult:
    """Launch exactly `[absolute_codex_path, 'app-server', '--stdio']` on opt-in.

    Sinks receive bytes, not reconstructed JSON. They must provide cancellation-
    cooperative backpressure and must not record transcripts. Both directions
    are required: never tee an unrelated session or have two readers on a pipe.
    Unsupported versions refuse launch, instead of gambling on their protocol.
    Bad/oversized JSON disables cosmetics but still forwards the original bytes.
    No status or emote ANSI is ever written into stdout/stderr by this function.

    The first outbound turn/start selects the watched thread unless thread_id was
    explicitly supplied. Further native threads still work but are not animated.
    Cancellation closes cosmetics before terminating/reaping this owned group;
    EOF/exit code never synthesizes COMPLETE. There is no retry/reconnect/replay.
    The existing select loop renews a validated active turn every five seconds
    while both protocol ends and the owned child are connected. Paused/terminal
    turns never renew; heartbeats imply neither provider progress nor success.
    Native UX and Windows remain uncertified.
    """
    if enabled is not True:
        return TransportResult(False, None, "not_enabled")
    if os.name != "posix":
        return TransportResult(False, None, "unsupported_platform")
    if (
        type(command) not in (tuple, list) or len(command) != 3
        or any(type(arg) is not str or "\0" in arg for arg in command)
        or tuple(command[1:]) != ("app-server", "--stdio")
        or not os.path.isabs(command[0]) or not _identity(session_id)
        or (thread_id is not None and not _identity(thread_id))
        or not _seconds(version_timeout) or not _seconds(shutdown_grace)
    ):
        return TransportResult(False, None, "invalid_configuration")
    try:
        executable = str(Path(command[0]).resolve())
    except (OSError, RuntimeError):
        return TransportResult(False, None, "invalid_configuration")
    try:
        version = await _version(
            executable, cwd=cwd, env=env, timeout=version_timeout, grace=shutdown_grace,
        )
    except (TimeoutError, OSError):
        return TransportResult(False, None, "version_probe_failed")
    if not supports_codex_app_server(version):
        return TransportResult(False, None, "unsupported_version")

    lifecycle = _Lifecycle(session_id, version, spool, thread_id)
    incoming = _Lines(lifecycle.client, lifecycle.disable)
    outgoing = _Lines(lifecycle.server, lifecycle.disable)
    try:
        process = await asyncio.create_subprocess_exec(
            executable, "app-server", "--stdio", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd, env=env, start_new_session=True, limit=CHUNK_BYTES,
        )
    except OSError:
        lifecycle.close()
        return TransportResult(False, None, "launch_failed")

    async def send():
        try:
            async for chunk in input_bytes:
                if type(chunk) is not bytes:
                    raise TypeError("Transport requires bytes")
                for offset in range(0, len(chunk), CHUNK_BYTES):
                    part = chunk[offset:offset + CHUNK_BYTES]
                    incoming.feed(part)
                    process.stdin.write(part)
                    await process.stdin.drain()
        finally:
            incoming.eof()
            process.stdin.close()

    async def receive(reader, sink, parser=None):
        try:
            while chunk := await reader.read(CHUNK_BYTES):
                if parser is not None:
                    parser.feed(chunk)
                await sink(chunk)
        finally:
            if parser is not None:
                parser.eof()
                lifecycle.close()

    tasks = [
        asyncio.create_task(send()),
        asyncio.create_task(receive(process.stdout, stdout, outgoing)),
        asyncio.create_task(receive(process.stderr, stderr)),
    ]
    waiting = asyncio.create_task(process.wait())
    reason = "closed"
    try:
        pending = {*tasks, waiting}
        while pending:
            done, pending = await asyncio.wait(
                pending, timeout=HEARTBEAT_POLL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if any(not task.cancelled() and task.exception() is not None for task in done):
                reason = "transport_error"
                break
            if waiting in done or tasks[0] in done or tasks[1] in done:
                # EOF bounds the backend's lifetime, not how quickly a healthy
                # frontend must consume buffered output after the child exits.
                _, still_pending = await asyncio.wait({waiting}, timeout=shutdown_grace)
                if still_pending:
                    reason = "shutdown_timeout"
                else:
                    results = await asyncio.gather(tasks[1], tasks[2], return_exceptions=True)
                    if any(isinstance(result, BaseException) for result in results):
                        reason = "transport_error"
                break
            # Single lifecycle owner: no timer task, second bridge, or writer.
            # Handle all ready EOF/error barriers above before a lease renewal.
            if (
                process.returncode is None and not tasks[0].done() and not tasks[1].done()
                and not process.stdin.is_closing() and not process.stdout.at_eof()
            ):
                lifecycle.heartbeat()
    finally:
        lifecycle.close()

        async def cleanup():
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _stop_owned_process(process, shutdown_grace)
            await waiting

        await _protected_cleanup(cleanup())
    return TransportResult(True, process.returncode, lifecycle.reason or reason)


async def _stdio(command, session_id, thread_id):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=CHUNK_BYTES)
    input_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(os.dup(0), "rb", buffering=0),
    )
    outputs = []
    try:
        for fd in (1, 2):
            transport, protocol = await loop.connect_write_pipe(
                lambda: asyncio.streams.FlowControlMixin(loop=loop),
                os.fdopen(os.dup(fd), "wb", buffering=0),
            )
            outputs.append(asyncio.StreamWriter(transport, protocol, None, loop))

        async def chunks():
            while data := await reader.read(CHUNK_BYTES):
                yield data

        async def write(writer, data):
            writer.write(data)
            await writer.drain()

        return await run_codex_transport(
            command, enabled=True, session_id=session_id, thread_id=thread_id,
            input_bytes=chunks(), stdout=lambda b: write(outputs[0], b),
            stderr=lambda b: write(outputs[1], b),
        )
    finally:
        input_transport.close()
        for writer in outputs:
            writer.close()


def main(argv=None) -> int:
    """Optional raw-stdio entrypoint for a frontend, not a chat prompt command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="authorize a NEW child process")
    parser.add_argument("--session-id", required=True, help="matching companion watch session")
    parser.add_argument("--thread-id", help="otherwise watch first outgoing turn/start thread")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.enable or os.name != "posix" or any(
        not (stat.S_ISFIFO(os.fstat(fd).st_mode) or stat.S_ISSOCK(os.fstat(fd).st_mode))
        for fd in (0, 1)
    ):
        parser.error("explicit --enable and POSIX frontend stdin/stdout pipes are required")

    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await _stdio(command, args.session_id, args.thread_id)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    try:
        result = asyncio.run(run())
    except (asyncio.CancelledError, KeyboardInterrupt):
        return 130
    except (OSError, ValueError):
        return 2
    # Stdout must remain protocol-only even on refusal. The embedding caller can
    # use the Python result for diagnostics; shell callers get an exit status.
    if result.returncode is None:
        return 2
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
