"""Bounded POSIX native writes over runtime-observed bytes, not disk attestation.

Locks serialize cooperating writers only. POSIX recheck+rename is NOT a CAS
against an uncooperative process, and a readback cannot guarantee future state.
No shell, subprocess, legacy patching, retry, rollback, mkdir or original chmod.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import threading
import time
import unicodedata
import uuid
from asyncio import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from openvegas.agent.native_mutation import (
    NativeMutationError,
    apply_exact_patch,
    validate_plan,
    validate_relative_path,
)

MAX_SOURCE_BYTES = 32768
LOCK_TIMEOUT_SEC = 5.0
_COOPERATING_LOCK = threading.RLock()
_REASONS = frozenset({
    "native_runtime_unsupported", "native_runtime_path", "native_runtime_unsafe_file",
    "native_runtime_metadata", "native_runtime_source_limit", "native_runtime_source_encoding",
    "native_runtime_source_changed", "native_runtime_parent_changed", "native_runtime_busy",
    "native_runtime_plan_invalid", "native_runtime_io", "native_runtime_create_collision",
    "native_runtime_readback_mismatch", "native_runtime_interrupted", "native_runtime_temp_changed",
})


class RuntimeWriteError(NativeMutationError):
    """Fixed, content-free reasons; compatible with the pure helper error family."""

    def __init__(self, reason: str):
        super().__init__()
        self.reason_code = reason if reason in _REASONS else "native_runtime_io"
        self.args = (self.reason_code,)


def _fail(reason: str) -> None:
    raise RuntimeWriteError(reason)


def _supported() -> None:
    if (os.name != "posix" or sys.platform not in {"darwin", "linux"}
            or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
            or not {os.open, os.stat, os.link, os.unlink, os.rename} <= os.supports_dir_fd):
        _fail("native_runtime_unsupported")


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino


def _version(info: os.stat_result) -> tuple:
    return (_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_nlink, info.st_uid, info.st_gid, getattr(info, "st_flags", 0))


def _ordinary(info: os.stat_result) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | stat.S_IWOTH)
            or not mode & stat.S_IWUSR or getattr(info, "st_flags", 0)):
        _fail("native_runtime_unsafe_file")


def _metadata(fd: int) -> None:
    # ACLs/xattrs would otherwise be silently discarded by replacing an inode.
    if sys.platform == "linux":
        if not hasattr(os, "listxattr") or os.listxattr(fd):
            _fail("native_runtime_metadata")
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_free.argtypes = [ctypes.c_void_p]
    ctypes.set_errno(0)
    acl = libc.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED, Darwin sys/acl.h.
    error = ctypes.get_errno()
    if acl:
        libc.acl_free(acl)
        _fail("native_runtime_metadata")
    if error not in {0, errno.ENOENT}:
        _fail("native_runtime_metadata")
    libc.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.flistxattr.restype = ctypes.c_ssize_t
    size = libc.flistxattr(fd, None, 0, 0)
    if not 0 <= size <= 65536:
        _fail("native_runtime_metadata")
    if size:
        buffer = ctypes.create_string_buffer(size)
        count = libc.flistxattr(fd, buffer, size, 0)
        if not 0 <= count <= size:
            _fail("native_runtime_metadata")
        # macOS automatically adds this OS-managed origin marker even to a new
        # plain temp file. It is not a permission/ACL and is not copied/rewritten.
        if set(buffer.raw[:count].split(b"\0")) - {b"", b"com.apple.provenance"}:
            _fail("native_runtime_metadata")


class _Anchor:
    def __init__(self, root: str, relative: str):
        self.fds: list[int] = []
        self.links: list[tuple[int, str, int, tuple]] = []
        if (type(root) is not str or not root.startswith("/") or root.startswith("//")
                or "\0" in root or len(root.encode("utf-8")) > 4096):
            _fail("native_runtime_path")
        roots = root.rstrip("/").split("/")[1:]
        if not roots or any(part in {"", ".", ".."} for part in roots):
            _fail("native_runtime_path")
        components = roots + relative.split("/")[:-1]
        if len(components) > 128:
            _fail("native_runtime_path")
        self.leaf = relative.split("/")[-1]
        try:
            current = os.open("/", self._flags())
            self.fds.append(current)
            for part in components:
                child = os.open(part, self._flags(), dir_fd=current)
                self.fds.append(child)
                self.links.append((current, part, child, _identity(os.fstat(child))))
                current = child
            self.parent = current
            self.verify()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def verify(self) -> None:
        for parent, name, child, identity in self.links:
            try:
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                _fail("native_runtime_parent_changed")
            if (not stat.S_ISDIR(linked.st_mode) or _identity(linked) != identity
                    or _identity(os.fstat(child)) != identity):
                _fail("native_runtime_parent_changed")

    def close(self) -> None:
        failed = False
        while self.fds:
            try:
                os.close(self.fds.pop())
            except OSError:
                failed = True
        if failed:
            _fail("native_runtime_io")


@contextmanager
def _locked(anchor: _Anchor):
    import fcntl

    if not _COOPERATING_LOCK.acquire(timeout=LOCK_TIMEOUT_SEC):
        _fail("native_runtime_busy")
    held = False
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(anchor.parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _fail("native_runtime_busy")
                time.sleep(0.01)
        anchor.verify()
        yield
    finally:
        try:
            if held:
                fcntl.flock(anchor.parent, fcntl.LOCK_UN)
        finally:
            _COOPERATING_LOCK.release()


@dataclass(frozen=True)
class _Snapshot:
    data: bytes | None
    info: os.stat_result | None

    def proof(self) -> dict:
        return {"exists": self.data is not None,
                "sha256": hashlib.sha256(self.data).hexdigest() if self.data is not None else None,
                "bytes": len(self.data) if self.data is not None else 0}


def _text(data: bytes) -> str:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError:
        _fail("native_runtime_source_encoding")
    if any((ord(char) < 32 and char not in "\r\n\t") or 127 <= ord(char) <= 159 or unicodedata.category(char) == "Cf" and char != "\ufeff" for char in text):
        _fail("native_runtime_source_encoding")
    return text


def _read(anchor: _Anchor) -> _Snapshot:
    anchor.verify()
    try:
        fd = os.open(anchor.leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0), dir_fd=anchor.parent)
    except FileNotFoundError:
        anchor.verify()
        return _Snapshot(None, None)
    try:
        before = os.fstat(fd)
        _ordinary(before)
        _metadata(fd)
        if before.st_size > MAX_SOURCE_BYTES:
            _fail("native_runtime_source_limit")
        content = bytearray()
        while True:
            block = os.read(fd, min(4096, MAX_SOURCE_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
            if len(content) > MAX_SOURCE_BYTES:
                _fail("native_runtime_source_limit")
        after = os.fstat(fd)
        linked = os.stat(anchor.leaf, dir_fd=anchor.parent, follow_symlinks=False)
        if _version(before) != _version(after) or _version(after) != _version(linked):
            _fail("native_runtime_source_changed")
        data = bytes(content)
        _text(data)
        anchor.verify()
        return _Snapshot(data, after)
    finally:
        os.close(fd)


def capture_source(workspace_root: str, path: str) -> dict:
    """Observe one complete bounded UTF-8 file, with absence distinct from empty."""
    anchor = None
    try:
        _supported()
        relative = validate_relative_path(path)
        anchor = _Anchor(workspace_root, relative)
        with _locked(anchor):
            captured = _read(anchor)
            return {"exists": captured.data is not None,
                    "content_utf8": _text(captured.data) if captured.data is not None else None}
    except NativeMutationError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, AttributeError):
        raise RuntimeWriteError("native_runtime_io") from None
    finally:
        if anchor is not None:
            anchor.close()


def _matches(snapshot: _Snapshot, plan: Any) -> bool:
    return snapshot.proof() == {"exists": plan.before_exists, "sha256": plan.before_sha256,
                                "bytes": plan.before_bytes}


def _same(left: _Snapshot, right: _Snapshot) -> bool:
    return (left.data == right.data and
            (_version(left.info) if left.info is not None else None)
            == (_version(right.info) if right.info is not None else None))


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "native temporary write failed")
        offset += written


def _unlink_owned(anchor: _Anchor, name: str, identity: tuple) -> bool:
    try:
        current = os.stat(name, dir_fd=anchor.parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(current.st_mode) or _identity(current) != identity:
        return False
    os.unlink(name, dir_fd=anchor.parent)
    return True


def execute_native_mutation(workspace_root: str, plan_document: dict) -> dict:
    """Execute one approved pure plan; approval/ID checks belong to the caller.

    A failed operation after a possible commit is UNKNOWN, never retried/undone.
    Cancelling an async caller cannot stop an in-flight worker thread; a lost
    result must be classified as unknown by that caller, not executed again.
    Safe proof contains no source, target content, patch, exception or temp path.
    """
    proof = {"kind": "runtime_observed_file_v1", "contract_sha256": None,
             "relative_path": None, "observed_before": None, "observed_after": None,
             "outcome": "not_applied", "reason": None}
    anchor = None
    temporary = None
    temp_identity = None
    fd = None
    possible_commit = False
    try:
        _supported()
        plan = validate_plan(plan_document)
        proof.update(contract_sha256=plan.contract_sha256, relative_path=plan.relative_path)
        anchor = _Anchor(workspace_root, validate_relative_path(plan.relative_path))
        with _locked(anchor):
            before = _read(anchor)
            proof["observed_before"] = before.proof()
            if not _matches(before, plan):
                _fail("native_runtime_source_changed")
            target = apply_exact_patch(before.data or b"", plan.patch, plan.relative_path, plan.before_exists)
            if (target != plan.content_utf8.encode("utf-8") or len(target) != plan.after_bytes
                    or hashlib.sha256(target).hexdigest() != plan.after_sha256):
                _fail("native_runtime_plan_invalid")
            if plan.no_change:
                after = _read(anchor)
                proof["observed_after"] = after.proof()
                if not _same(before, after):
                    _fail("native_runtime_source_changed")
                proof["outcome"] = "no_change"
            else:
                temporary = ".openvegas-native-" + uuid.uuid4().hex + ".tmp"
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
                             | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=anchor.parent)
                temp_info = os.fstat(fd)
                temp_identity = _identity(temp_info)
                _ordinary(temp_info)
                _metadata(fd)
                if before.info is not None and (temp_info.st_uid, temp_info.st_gid) != (before.info.st_uid, before.info.st_gid):
                    _fail("native_runtime_metadata")
                _write_all(fd, target)
                os.fchmod(fd, stat.S_IMODE(before.info.st_mode) if before.info is not None else 0o600)
                os.fsync(fd)
                current = _read(anchor)
                if not _same(before, current):
                    _fail("native_runtime_source_changed")
                anchor.verify()
                linked_temp = os.stat(temporary, dir_fd=anchor.parent, follow_symlinks=False)
                if _identity(linked_temp) != temp_identity or _version(linked_temp) != _version(os.fstat(fd)):
                    _fail("native_runtime_temp_changed")
                _ordinary(linked_temp)
                _metadata(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                checked = bytearray()
                while len(checked) <= MAX_SOURCE_BYTES:
                    part = os.read(fd, min(4096, MAX_SOURCE_BYTES + 1 - len(checked)))
                    if not part:
                        break
                    checked.extend(part)
                if bytes(checked) != target:
                    _fail("native_runtime_temp_changed")
                if not _same(before, _read(anchor)):
                    _fail("native_runtime_source_changed")
                anchor.verify()
                possible_commit = True
                if before.data is None:
                    try:
                        os.link(temporary, anchor.leaf, src_dir_fd=anchor.parent,
                                dst_dir_fd=anchor.parent, follow_symlinks=False)
                    except FileExistsError:
                        possible_commit = False
                        _fail("native_runtime_create_collision")
                    if not _unlink_owned(anchor, temporary, temp_identity):
                        _fail("native_runtime_temp_changed")
                else:
                    os.replace(temporary, anchor.leaf, src_dir_fd=anchor.parent, dst_dir_fd=anchor.parent)
                os.fsync(anchor.parent)
                after = _read(anchor)
                proof["observed_after"] = after.proof()
                if after.data != target:
                    _fail("native_runtime_readback_mismatch")
                proof["outcome"] = "applied"
    except (Exception, KeyboardInterrupt, CancelledError) as error:  # noqa: BLE001 - any local fault needs a bounded, content-free outcome.
        proof["outcome"] = "unknown" if possible_commit else "not_applied"
        reason = getattr(error, "reason_code", None)
        proof["reason"] = (reason if reason in _REASONS else
                           "native_runtime_interrupted" if isinstance(error, (KeyboardInterrupt, CancelledError)) else
                           "native_runtime_plan_invalid" if isinstance(error, NativeMutationError) else "native_runtime_io")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                proof["outcome"] = "unknown" if possible_commit else "not_applied"
                proof["reason"] = "native_runtime_io"
        if anchor is not None:
            try:
                if (temporary is not None and temp_identity is not None
                        and not _unlink_owned(anchor, temporary, temp_identity)):
                    proof["outcome"] = "unknown" if possible_commit else "not_applied"
                    proof["reason"] = "native_runtime_temp_changed"
            except OSError:
                proof["outcome"] = "unknown" if possible_commit else "not_applied"
                proof["reason"] = "native_runtime_io"
            finally:
                try:
                    anchor.close()
                except OSError:
                    proof["outcome"] = "unknown" if possible_commit else "not_applied"
                    proof["reason"] = "native_runtime_io"
                except RuntimeWriteError:
                    proof["outcome"] = "unknown" if possible_commit else "not_applied"
                    proof["reason"] = "native_runtime_io"
    return proof
