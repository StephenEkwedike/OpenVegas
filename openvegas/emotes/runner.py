"""Explicit subprocess outcome wrapper. Never capture or render child streams."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess

from .bridge import ChatEmoteBridge
from .events import IDENTITY
from .manifest import parse_json
from .spool import EventSpool, _locked, _names, _read, atomic_write

_COUNTER_FILE = "global-v1.json"
_LEGACY_FILE = re.compile(r"[0-9a-f]{64}\.json\Z")


def _generation(fd: int, name: str) -> int:
    raw = parse_json(_read(fd, name, 128), 128)
    value = raw.get("generation")
    if set(raw) != {"generation"} or type(value) is not int or not 0 <= value < 2**53 - 2:
        raise ValueError("Invalid generation reservation")
    return value


def reserve_generation(spool: EventSpool, *, source: str, session_id: str) -> int | None:
    """Reserve ordering for sequential run invocations, without blocking on a lock.

    The sibling private directory is independent of event queue capacity. On
    failure the command still runs, but no unordered cosmetic events are sent.
    One global, at-most-128-byte counter supports unlimited distinct identities;
    session identity remains in Event, not in the reservation filename. First use
    seeds above at most 256 validated legacy per-identity counters. Corrupt,
    oversized, unsafe or unknown legacy state fails closed without a reset.
    Legacy records remain bounded and untouched; subsequent calls read only the
    global counter. Do not mix old per-identity writers with this unpublished v1
    protocol, or manually delete/roll back the counter while old events survive.
    """
    if not all(isinstance(v, str) and IDENTITY.fullmatch(v) for v in (source, session_id)):
        return None
    try:
        directory = spool.directory.parent / "run-generations"
        with _locked(directory) as fd:
            try:
                old = _generation(fd, _COUNTER_FILE)
            except FileNotFoundError:
                names = _names(fd)
                if len(names) > 256 or any(not _LEGACY_FILE.fullmatch(name) for name in names):
                    return None
                old = max((_generation(fd, name) for name in names), default=0)
            generation = old + 1
            atomic_write(fd, _COUNTER_FILE, json.dumps({"generation": generation}).encode("ascii"))
            return generation
    except Exception:  # noqa: BLE001 - ordering failure disables only cosmetics
        return None


def run_command(
    command: tuple[str, ...] | list[str],
    *,
    session_id: str,
    source: str = "openvegas",
    spool: EventSpool | None = None,
) -> int:
    """Run without shell/PTY/capture and return the child's native return code.

    Streams, cwd, environment and foreground process group are inherited. Normal
    exit codes are unchanged; negative return codes denote POSIX signal death.
    The Click entry point maps those to conventional 128+signal shell status.
    This is not a general job-control proxy or an internal coding-turn adapter.
    """
    if not command or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("A nonempty argument vector is required")
    if not all(isinstance(v, str) and IDENTITY.fullmatch(v) for v in (source, session_id)):
        raise ValueError("Invalid source/session identity")
    spool = spool if spool is not None else EventSpool()
    generation = reserve_generation(spool, source=source, session_id=session_id)
    bridge = ChatEmoteBridge(
        session_id,
        publish=spool.publish if generation else lambda _: False,
        source=source,
        initial_generation=(generation or 1) - 1,
    )
    turn = bridge.begin()
    child = None
    interrupted = False
    pending_signal = None
    previous = {}
    try:
        terminal_group = os.name == "posix" and os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        terminal_group = False

    def receive(signum, _frame):
        nonlocal interrupted, pending_signal
        interrupted = True
        # SIGINT from the terminal already reaches the inherited foreground
        # group. Do not send a duplicate interrupt to the child. Other signals
        # commonly target the wrapper PID and are forwarded to its direct child.
        if child is None:
            pending_signal = signum
        elif (signum != signal.SIGINT or not terminal_group) and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    try:
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                previous[sig] = signal.signal(sig, receive)
        if pending_signal is not None:
            bridge.cancel(turn=turn)
            return -pending_signal
        try:
            child = subprocess.Popen(
                list(command),
                shell=False,
                stdin=None,
                stdout=None,
                stderr=None,
                start_new_session=False,
            )
        except FileNotFoundError:
            bridge.finish(False, turn=turn)
            return 127
        except OSError:
            bridge.finish(False, turn=turn)
            return 126
        if pending_signal is not None and child.poll() is None:
            try:
                child.send_signal(pending_signal)
            except ProcessLookupError:
                pass
        while True:
            try:
                code = child.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                bridge.active(turn=turn)
        if interrupted or code < 0:
            bridge.cancel(turn=turn)
        else:
            bridge.finish(code == 0, turn=turn)
        # Keep the companion alive for the authored completion duration. An EXIT
        # here would immediately retire the success effect. The wrapper itself
        # does not sleep or remain alive for cosmetics.
        return code
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
