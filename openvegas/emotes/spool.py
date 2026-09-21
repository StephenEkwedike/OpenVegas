"""Private, bounded event spool. No transcript payloads or terminal output.

The lock is nonblocking; publication has no retry, sleep, or fsync. Filesystem
operations still have OS-dependent latency, so this is not a hard realtime API.
Windows uses a protected-ACL, handle-relative backend; POSIX uses descriptors.
"""

from __future__ import annotations

import os
import re
import stat
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import click

from ._windows_state import (
    PrivateInfo,
    WindowsDirectory,
    private_windows_directory,
    validate_info,
)
from .events import MAX_EVENT_BYTES, Event, Phase

MAX_QUEUE = 256
MAX_SCAN = MAX_QUEUE * 4
STALE_SECONDS = 60.0
EVENT_FILE = re.compile(r"[0-9a-f]{32}\.json\Z")
TEMP_FILE = re.compile(r"\.[0-9a-f]{32}\.tmp\Z")


class SpoolError(ValueError):
    pass


def default_state_dir() -> Path:
    return Path(click.get_app_dir("openvegas")) / "emotes"


def default_spool_dir() -> Path:
    return default_state_dir() / "events"


def _private(info: os.stat_result, *, directory: bool) -> None:
    if isinstance(info, PrivateInfo):
        validate_info(info, directory=directory)
        return
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise SpoolError("Unsafe state directory or file permissions")


@contextmanager
def private_directory(path: Path):
    if os.name == "nt":
        with private_windows_directory(path) as directory:
            yield directory
        return
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise SpoolError("Private POSIX spool unavailable on this platform")
    path = Path(path)
    if ".." in path.parts or len(path.parts) > 256:
        raise SpoolError("Unsafe private state path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    missing = []
    anchor = path
    with ExitStack() as stack:
        # Existing OS/home prefixes can be non-private. Retain that anchor, then
        # create each missing component privately, never chmod an existing path.
        while True:
            try:
                fd = os.open(anchor, flags)
                stack.callback(os.close, fd)
                break
            except FileNotFoundError:
                if anchor.parent == anchor or not anchor.name:
                    raise
                missing.append(anchor.name)
                anchor = anchor.parent
        for component in reversed(missing):
            try:
                os.mkdir(component, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass  # Open and validate a concurrent creator's directory too.
            fd = os.open(component, flags, dir_fd=fd)
            stack.callback(os.close, fd)
            _private(os.fstat(fd), directory=True)
        _private(os.fstat(fd), directory=True)
        yield fd


@contextmanager
def _locked(path: Path):
    with private_directory(path) as directory:
        if isinstance(directory, WindowsDirectory):
            with directory.locked():
                yield directory
            return
        try:
            import fcntl
        except ImportError:
            raise SpoolError("Private event locking unavailable on this platform") from None

        lock = os.open(
            ".lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            _private(os.fstat(lock), directory=False)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield directory
        finally:
            os.close(lock)


def state_names(directory, *, limit: int = MAX_SCAN, include_lock: bool = True) -> list[str]:
    """Bounded names only, never DirEntry/path objects to reopen on Windows."""
    if isinstance(directory, WindowsDirectory):
        names = directory.names(limit + (not include_lock))
        if not include_lock:
            names = [name for name in names if name != ".lock"]
        if len(names) > limit:
            raise SpoolError("Spool exceeds bounded scan limit")
        return names
    names = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not include_lock and entry.name == ".lock":
                continue
            names.append(entry.name)
            if len(names) > limit:
                raise SpoolError("Spool exceeds bounded scan limit")
    return names


def _names(directory) -> list[str]:
    return state_names(directory, include_lock=False)


def state_stat(directory, name: str):
    """Return checked private file metadata from a no-follow open on Windows."""
    if isinstance(directory, WindowsDirectory):
        return directory.stat(name)
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    _private(info, directory=False)
    return info


def state_unlink(directory, name: str, *, expected=None) -> None:
    """Delete by validated handle on Windows; optional expected identity for CAS."""
    if isinstance(directory, WindowsDirectory):
        directory.unlink(name, expected=expected)
        return
    if expected is not None:
        current = state_stat(directory, name)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            raise SpoolError("Private state changed before deletion")
    os.unlink(name, dir_fd=directory)


def _read(directory: int, name: str, limit: int) -> bytes:
    if isinstance(directory, WindowsDirectory):
        return directory.read(name, limit)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        _private(os.fstat(fd), directory=False)
        if os.fstat(fd).st_size > limit:
            raise SpoolError("State file exceeds size limit")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise SpoolError("State file exceeds size limit")
        return data
    finally:
        os.close(fd)


def atomic_write(directory: int, name: str, data: bytes) -> None:
    if isinstance(directory, WindowsDirectory):
        directory.atomic_write(name, data)
        return
    temporary = "." + uuid4().hex + ".tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise SpoolError("Incomplete state write")
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _cleanup(directory: int, incoming: Event | None = None) -> int:
    """Under flock, discard stale whole turns and make bounded space for new ones.

    Never synthesize START or success. Eviction removes a whole turn rather than
    leaving its terminal event to masquerade as another turn's completion.
    """
    names = _names(directory)
    remaining = set(names)
    groups = {}
    now = time.time()

    def remove(name):
        state_unlink(directory, name)
        remaining.discard(name)

    for name in names:
        if TEMP_FILE.fullmatch(name):
            info = state_stat(directory, name)
            if now - info.st_mtime >= STALE_SECONDS:
                remove(name)
            continue
        if not EVENT_FILE.fullmatch(name):
            continue
        try:
            event = Event.from_bytes(_read(directory, name, MAX_EVENT_BYTES))
            info = state_stat(directory, name)
        except (OSError, ValueError):
            remove(name)
            continue
        key = (*event.key, event.generation)
        groups.setdefault(key, []).append((name, event, info.st_mtime))

    for key, rows in list(groups.items()):
        if now - max(row[2] for row in rows) >= STALE_SECONDS:
            for name, _, _ in rows:
                remove(name)
            del groups[key]

    if incoming is None:
        return len(remaining)
    incoming_key = (*incoming.key, incoming.generation)
    # A heartbeat supersedes older BUSY messages only; keep START and all lifecycle
    # edges, and never replace a newer heartbeat with an out-of-order arrival.
    if incoming.phase == Phase.BUSY:
        for name, event, _ in groups.get(incoming_key, []):
            if event.phase == Phase.BUSY and event.sequence < incoming.sequence:
                remove(name)
    if len(remaining) >= MAX_QUEUE:
        candidates = sorted(
            ((key, rows) for key, rows in groups.items() if key != incoming_key),
            key=lambda pair: max(row[2] for row in pair[1]),
        )
        for _, rows in candidates:
            for name, _, _ in rows:
                if name in remaining:
                    remove(name)
            if len(remaining) < MAX_QUEUE:
                break
    if len(remaining) >= MAX_QUEUE and incoming.phase in {
        Phase.COMPLETE,
        Phase.CANCEL,
        Phase.ERROR,
        Phase.EXIT,
    }:
        # Prefer a real terminal edge over redundant busy/repeated-start records.
        rows = sorted(groups.get(incoming_key, []), key=lambda row: row[1].sequence)
        kept_start = False
        for name, event, _ in rows:
            if event.phase == Phase.START and not kept_start:
                kept_start = True
                continue
            if name in remaining and event.phase in {Phase.BUSY, Phase.START}:
                remove(name)
                if len(remaining) < MAX_QUEUE:
                    break
    return len(remaining)


def publish_event(event: Event, *, directory: str | Path | None = None) -> bool:
    """Silent, best-effort metadata publisher; false means dropped, never retry here."""
    try:
        if not isinstance(event, Event):
            return False
        data = event.to_bytes()
        Event.from_bytes(data)
        with _locked(Path(directory) if directory is not None else default_spool_dir()) as fd:
            if _cleanup(fd, event) >= MAX_QUEUE:
                return False
            atomic_write(fd, uuid4().hex + ".json", data)
        return True
    except Exception:  # noqa: BLE001 - publisher cannot interrupt the host lifecycle
        return False


class EventSpool:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory is not None else default_spool_dir()

    def publish(self, event: Event) -> bool:
        return publish_event(event, directory=self.directory)

    def drain(self, *, source: str, session_id: str) -> list[Event]:
        """Consume selected-session events; clean stale whole turns globally.

        Invalid bounded event files are discarded. No body is returned or logged.
        """
        events = []
        try:
            with _locked(self.directory) as fd:
                _cleanup(fd)
                for name in _names(fd):
                    if not EVENT_FILE.fullmatch(name):
                        continue
                    try:
                        event = Event.from_bytes(_read(fd, name, MAX_EVENT_BYTES))
                    except (OSError, ValueError):
                        state_unlink(fd, name)
                        continue
                    if (event.source, event.session_id) == (source, session_id):
                        events.append(event)
                        state_unlink(fd, name)
        except (OSError, ValueError):
            return []
        return sorted(events, key=lambda e: (e.generation, e.sequence))
