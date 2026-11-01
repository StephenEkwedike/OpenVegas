"""Private Windows state on fixed local drives; no pathname reopen after checks.

Security boundary: current user, SYSTEM and Administrators are trusted. All
ancestors remain open without delete sharing; each private object must have a
protected, owner-only (plus SYSTEM/Administrators) DACL. No ACL repair, junction,
UNC, cloud-placeholder or permissive fallback. Locks do not wait; filesystem
I/O can still have OS-dependent latency. Native Windows CI remains mandatory.

Microsoft API contracts:
https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntcreatefile
https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-lockfileex
https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_rename_info
https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile
"""

from __future__ import annotations

import ctypes as C
import errno
import struct
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from uuid import uuid4

from . import _windows_resources as R

DWORD, HANDLE = R.DWORD, R.HANDLE
READ_CONTROL = 0x20000
DELETE = 0x10000
FILE_OPEN_IF = 3
FILE_CREATE = 2
FILE_DIRECTORY_FILE = 1
FILE_NON_DIRECTORY_FILE = 0x40
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_SCAN = 4096
FULL_CONTROL = 0x1F01FF
TRUSTED_SERVICE_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})


class _Overlapped(C.Structure):
    _fields_ = [
        ("Internal", C.c_size_t),
        ("InternalHigh", C.c_size_t),
        ("Offset", DWORD),
        ("OffsetHigh", DWORD),
        ("hEvent", HANDLE),
    ]


class _Rename(C.Structure):
    _fields_ = [
        ("Flags", DWORD),
        ("RootDirectory", HANDLE),
        ("FileNameLength", DWORD),
        ("FileName", C.c_uint16 * 1),
    ]


class _AclSize(C.Structure):
    _fields_ = [("AceCount", DWORD), ("AclBytesInUse", DWORD), ("AclBytesFree", DWORD)]


@dataclass(frozen=True)
class Security:
    owner: str
    protected: bool
    # (ACE type, flags, mask, SID); unsupported/object/callback ACEs fail closed.
    entries: tuple[tuple[int, int, int, str], ...]


def validate_security(security: Security, user: str) -> None:
    allowed = TRUSTED_SERVICE_SIDS | {user}
    if security.owner != user or not security.protected or not 1 <= len(security.entries) <= 64:
        raise OSError("Unsafe private state owner or ACL")
    user_mask = 0
    for kind, flags, mask, sid in security.entries:
        if kind != 0 or flags != 0 or sid not in allowed:
            raise OSError("Unsafe private state ACL entry")
        if sid == user:
            user_mask |= mask
    if user_mask & FULL_CONTROL != FULL_CONTROL:
        raise OSError("Private state requires current-user full control")


@dataclass(frozen=True)
class PrivateInfo:
    attributes: int
    st_size: int
    st_dev: int
    st_ino: int
    st_mtime_ns: int
    st_nlink: int
    security: Security
    user: str

    @property
    def st_mtime(self):
        return self.st_mtime_ns / 1_000_000_000


def validate_info(info: PrivateInfo, *, directory: bool) -> None:
    if (
        info.attributes & (R.FILE_ATTRIBUTE_REPARSE_POINT | R.FILE_ATTRIBUTE_DEVICE)
        or bool(info.attributes & R.FILE_ATTRIBUTE_DIRECTORY) != directory
        or (not directory and info.st_nlink != 1)
    ):
        raise OSError("Unsafe private state type, reparse point or hard link")
    validate_security(info.security, info.user)


class WindowsStateAPI(R._WindowsAPI):
    def __init__(self):
        super().__init__()
        self.descriptor = C.c_void_p()
        kernel = C.WinDLL("kernel32", use_last_error=True)
        advapi = C.WinDLL("advapi32", use_last_error=True)
        native = C.WinDLL("ntdll", use_last_error=True)

        def bind(dll, name, args, result=C.c_int32):
            try:
                func = getattr(dll, name)
                func.argtypes, func.restype = args, result
                return func
            except (AttributeError, OSError) as exc:
                raise OSError("Windows secure state API unavailable") from exc

        ptr = C.c_void_p
        self._free = bind(kernel, "LocalFree", [ptr], ptr)
        self._process = bind(kernel, "GetCurrentProcess", [], HANDLE)
        self._token_open = bind(advapi, "OpenProcessToken", [HANDLE, DWORD, C.POINTER(HANDLE)])
        self._token_info = bind(
            advapi, "GetTokenInformation", [HANDLE, C.c_int32, ptr, DWORD, C.POINTER(DWORD)]
        )
        self._sid_string = bind(advapi, "ConvertSidToStringSidW", [ptr, C.POINTER(ptr)])
        self._sd_string = bind(
            advapi,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [C.c_wchar_p, DWORD, C.POINTER(ptr), C.POINTER(DWORD)],
        )
        self._security = bind(
            advapi, "GetSecurityInfo", [HANDLE, C.c_int32, DWORD] + [C.POINTER(ptr)] * 5, DWORD
        )
        self._sd_valid = bind(advapi, "IsValidSecurityDescriptor", [ptr])
        self._control = bind(
            advapi, "GetSecurityDescriptorControl", [ptr, C.POINTER(C.c_uint16), C.POINTER(DWORD)]
        )
        self._acl_valid = bind(advapi, "IsValidAcl", [ptr])
        self._acl_info = bind(advapi, "GetAclInformation", [ptr, ptr, DWORD, C.c_int32])
        self._ace = bind(advapi, "GetAce", [ptr, DWORD, C.POINTER(ptr)])
        self._sid_valid = bind(advapi, "IsValidSid", [ptr])
        self._sid_length = bind(advapi, "GetLengthSid", [ptr], DWORD)
        self._dos_error = bind(native, "RtlNtStatusToDosError", [C.c_int32], DWORD)
        self._write = bind(kernel, "WriteFile", [HANDLE, ptr, DWORD, C.POINTER(DWORD), ptr])
        self._set = bind(kernel, "SetFileInformationByHandle", [HANDLE, C.c_int32, ptr, DWORD])
        self._set_native = bind(
            native,
            "NtSetInformationFile",
            [HANDLE, C.POINTER(R._IOStatusBlock), ptr, DWORD, C.c_int32],
        )
        self._lock = bind(
            kernel, "LockFileEx", [HANDLE, DWORD, DWORD, DWORD, DWORD, C.POINTER(_Overlapped)]
        )
        self._unlock = bind(
            kernel, "UnlockFileEx", [HANDLE, DWORD, DWORD, DWORD, C.POINTER(_Overlapped)]
        )
        self._query = bind(
            native,
            "NtQueryDirectoryFile",
            [
                HANDLE,
                HANDLE,
                ptr,
                ptr,
                C.POINTER(R._IOStatusBlock),
                ptr,
                DWORD,
                C.c_int32,
                C.c_ubyte,
                ptr,
                C.c_ubyte,
            ],
        )
        self.user = self._current_user()
        sddl = f"O:{self.user}D:P(A;;FA;;;{self.user})(A;;FA;;;SY)(A;;FA;;;BA)"
        if not self._sd_string(sddl, 1, C.byref(self.descriptor), None):
            raise OSError("Cannot build private state ACL")

    def dispose(self):
        if self.descriptor.value:
            self._free(self.descriptor)
            self.descriptor = C.c_void_p()

    def _sid(self, pointer) -> str:
        if not pointer or not self._sid_valid(pointer):
            raise OSError("Invalid state SID")
        result = C.c_void_p()
        if not self._sid_string(pointer, C.byref(result)):
            raise OSError("Cannot inspect state SID")
        try:
            return C.wstring_at(result)
        finally:
            self._free(result)

    def _current_user(self):
        token = HANDLE()
        if not self._token_open(self._process(), 0x8, C.byref(token)):
            raise OSError("Cannot inspect current user token")
        try:
            needed = DWORD()
            self._token_info(token, 1, None, 0, C.byref(needed))
            if not C.sizeof(HANDLE) <= needed.value <= 65536:
                raise OSError("Invalid current user token size")
            buffer = C.create_string_buffer(needed.value)
            if not self._token_info(token, 1, buffer, needed.value, C.byref(needed)):
                raise OSError("Cannot inspect current user token")
            return self._sid(C.cast(buffer, C.POINTER(HANDLE))[0])
        finally:
            self.close(token)

    def security(self, handle):
        owner, dacl, sd = C.c_void_p(), C.c_void_p(), C.c_void_p()
        status = self._security(
            handle, 1, 0x5, C.byref(owner), None, C.byref(dacl), None, C.byref(sd)
        )
        try:
            if status or not sd or not self._sd_valid(sd) or not dacl or not self._acl_valid(dacl):
                raise OSError("Cannot inspect private state ACL")
            control, revision, size = C.c_uint16(), DWORD(), _AclSize()
            if (
                not self._control(sd, C.byref(control), C.byref(revision))
                or not self._acl_info(dacl, C.byref(size), C.sizeof(size), 2)
                or not 1 <= size.AceCount <= 64
            ):
                raise OSError("Invalid private state ACL")
            entries = []
            for i in range(size.AceCount):
                ace = C.c_void_p()
                if not self._ace(dacl, i, C.byref(ace)):
                    raise OSError("Cannot inspect private state ACE")
                kind, flags, length = struct.unpack("<BBH", C.string_at(ace, 4))
                if kind != 0 or flags != 0 or not 16 <= length <= size.AclBytesInUse:
                    raise OSError("Unsupported private state ACE")
                sid = ace.value + 8
                if not self._sid_valid(sid) or self._sid_length(sid) > length - 8:
                    raise OSError("Invalid private state ACE SID")
                mask = struct.unpack("<I", C.string_at(ace.value + 4, 4))[0]
                entries.append((kind, flags, mask, self._sid(sid)))
            result = Security(self._sid(owner), bool(control.value & 0x1000), tuple(entries))
            validate_security(result, self.user)
            return result
        finally:
            if sd:
                self._free(sd)

    def open_drive(self, drive):
        if self._drive_type(drive) != R.DRIVE_FIXED:
            raise OSError("Private state requires a fixed local drive")
        handle = self._create(
            "\\\\?\\" + drive,
            R.FILE_TRAVERSE | R.FILE_READ_ATTRIBUTES | R.SYNCHRONIZE,
            3,
            None,
            3,
            R.FILE_FLAG_BACKUP_SEMANTICS | R.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle in (None, R.INVALID_HANDLE_VALUE):
            raise OSError("Cannot open private state drive")
        return handle

    def _raise_status(self, status):
        error = self._dos_error(status)
        if error in {2, 3}:
            raise FileNotFoundError(errno.ENOENT, "State entry missing")
        if error in {32, 33}:
            raise BlockingIOError(errno.EAGAIN, "Private state busy")
        raise OSError("Cannot securely open private state entry")

    def child(
        self,
        parent,
        name,
        *,
        directory=False,
        disposition=R.FILE_OPEN,
        writable=False,
        delete=False,
        lock=False,
    ):
        R._component(name)
        buffer = C.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        unicode = R._UnicodeString(length, length + 2, C.cast(buffer, C.c_void_p))
        attrs = R._ObjectAttributes(
            C.sizeof(R._ObjectAttributes),
            parent,
            C.pointer(unicode),
            R.OBJ_CASE_INSENSITIVE | R.OBJ_DONT_REPARSE,
            self.descriptor if disposition != R.FILE_OPEN else None,
            None,
        )
        access = READ_CONTROL | R.FILE_READ_ATTRIBUTES | R.SYNCHRONIZE | R.FILE_READ_DATA
        if directory:
            access |= R.FILE_TRAVERSE
        if writable:
            access |= 0x2
        if delete:
            access |= DELETE
        if lock:
            access |= 0xC0000000  # GENERIC_READ | GENERIC_WRITE for LockFileEx
        # Parents/locks deny delete sharing. Readers allow atomic replacement,
        # but not in-place writes; lock handles must share read/write.
        share = 3 if directory or lock else 5
        options = R.FILE_OPEN_REPARSE_POINT | R.FILE_SYNCHRONOUS_IO_NONALERT
        options |= FILE_DIRECTORY_FILE if directory else FILE_NON_DIRECTORY_FILE
        handle, io = HANDLE(), R._IOStatusBlock()
        status = self._open(
            C.byref(handle),
            access,
            C.byref(attrs),
            C.byref(io),
            None,
            0,
            share,
            disposition,
            options,
            None,
            0,
        )
        if status < 0:
            self._raise_status(status)
        if handle.value in (None, R.INVALID_HANDLE_VALUE):
            raise OSError("Invalid private state handle")
        return handle.value

    def private_info(self, handle, *, directory=False):
        raw = R._FileInformation()
        if self._type(handle) != R.FILE_TYPE_DISK or not self._info(handle, C.byref(raw)):
            raise OSError("Cannot inspect private disk state")
        result = PrivateInfo(
            raw.attributes,
            (raw.size_high << 32) | raw.size_low,
            raw.volume,
            (raw.index_high << 32) | raw.index_low,
            (((raw.write[1] << 32) | raw.write[0]) - 116444736000000000) * 100,
            raw.links,
            self.security(handle),
            self.user,
        )
        validate_info(result, directory=directory)
        return result

    def lock(self, handle):
        overlapped = _Overlapped()
        if not self._lock(handle, 3, 0, 1, 0, C.byref(overlapped)):
            if C.get_last_error() in {33, 997}:
                raise BlockingIOError(errno.EAGAIN, "Private state lock busy")
            raise OSError("Cannot lock private state")
        return overlapped

    def unlock(self, handle, overlapped):
        if not self._unlock(handle, 0, 1, 0, C.byref(overlapped)):
            raise OSError("Cannot unlock private state")

    def names(self, handle, limit):
        result = []
        for i in range(limit + 4):
            io, buffer = R._IOStatusBlock(), C.create_string_buffer(4096)
            status = self._query(
                handle, None, None, None, C.byref(io), buffer, len(buffer), 12, 1, None, i == 0
            )  # FileNamesInformation, single entry
            if status & 0xFFFFFFFF == 0x80000006:  # STATUS_NO_MORE_FILES
                return result
            if status < 0 or not 12 <= io.Information <= len(buffer):
                raise OSError("Cannot enumerate private state")
            following, _, length = struct.unpack("<III", buffer.raw[:12])
            if following or length % 2 or not 0 < length <= io.Information - 12:
                raise OSError("Invalid private state directory entry")
            name = buffer.raw[12 : 12 + length].decode("utf-16-le", errors="strict")
            if name in {".", ".."}:
                continue
            R._component(name)
            result.append(name)
            if len(result) > limit:
                break
        raise OSError("Private state exceeds bounded scan limit")

    def write(self, handle, data):
        count = DWORD()
        if not self._write(handle, data, len(data), C.byref(count), None) or count.value != len(
            data
        ):
            raise OSError("Incomplete private state write")

    def rename(self, handle, parent, name):
        R._component(name)
        encoded = name.encode("utf-16-le")
        buffer = C.create_string_buffer(C.sizeof(_Rename) + len(encoded))
        info = _Rename.from_buffer(buffer)
        # Native FileRenameInformation with NULL RootDirectory and a single
        # component renames within the source handle's directory, not the CWD.
        # The caller retains `parent` without delete sharing throughout. Avoid
        # the Win32 wrapper's pathname conversion; never close/reopen by path.
        info.Flags, info.RootDirectory, info.FileNameLength = 1, None, len(encoded)
        C.memmove(C.addressof(buffer) + _Rename.FileName.offset, encoded, len(encoded))
        io = R._IOStatusBlock()
        status = self._set_native(handle, C.byref(io), buffer, len(buffer), 10)
        if status != 0:
            raise OSError(
                "Cannot atomically publish private state "
                f"(NTSTATUS=0x{status & 0xFFFFFFFF:08X}, Win32={self._dos_error(status)})"
            )

    def delete(self, handle):
        flag = C.c_ubyte(1)
        if not self._set(handle, 4, C.byref(flag), C.sizeof(flag)):
            raise OSError("Cannot remove private state")


@dataclass
class WindowsDirectory:
    api: WindowsStateAPI
    handle: int

    @contextmanager
    def opened(self, name, **kwargs):
        handle = self.api.child(self.handle, name, **kwargs)
        try:
            self.api.private_info(handle)
            yield handle
        finally:
            self.api.close(handle)

    def stat(self, name):
        with self.opened(name, lock=name == ".lock") as handle:
            return self.api.private_info(handle)

    def names(self, limit):
        if type(limit) is not int or not 0 < limit <= MAX_SCAN:
            raise OSError("Invalid private state scan limit")
        return self.api.names(self.handle, limit)

    def read(self, name, limit):
        if type(limit) is not int or not 0 <= limit <= MAX_STATE_BYTES:
            raise OSError("Invalid private state read limit")
        with self.opened(name) as handle:
            before = self.api.private_info(handle)
            if before.st_size > limit:
                raise OSError("State file exceeds size limit")
            parts, remaining = [], limit + 1
            while remaining:
                chunk = self.api.read(handle, min(65536, remaining))
                if not chunk:
                    break
                if len(chunk) > min(65536, remaining):
                    raise OSError("Invalid state read count")
                parts.append(chunk)
                remaining -= len(chunk)
            data = b"".join(parts)
            if (
                len(data) != before.st_size
                or len(data) > limit
                or self.api.private_info(handle) != before
            ):
                raise OSError("Private state changed during read")
            return data

    def unlink(self, name, *, expected=None):
        with self.opened(name, delete=True) as handle:
            current = self.api.private_info(handle)
            if expected is not None and (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) != (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns):
                raise OSError("Private state changed before deletion")
            self.api.delete(handle)

    def atomic_write(self, name, data):
        R._component(name)
        if type(data) is not bytes or len(data) > MAX_STATE_BYTES:
            raise OSError("Invalid private state write size")
        # Refuse an existing unsafe target, even though rename cannot follow it.
        try:
            self.stat(name)
        except FileNotFoundError:
            pass
        temporary = "." + uuid4().hex + ".tmp"
        with self.opened(temporary, writable=True, delete=True, disposition=FILE_CREATE) as handle:
            published = False
            try:
                self.api.write(handle, data)
                if self.api.private_info(handle).st_size != len(data):
                    raise OSError("Incomplete private state write")
                self.api.rename(handle, self.handle, name)
                published = True
            finally:
                if not published:
                    self.api.delete(handle)

    @contextmanager
    def locked(self):
        with self.opened(".lock", disposition=FILE_OPEN_IF, lock=True) as handle:
            if self.api.private_info(handle).st_size:
                raise OSError("Invalid private state lock file")
            token = self.api.lock(handle)
            try:
                yield self
            finally:
                self.api.unlock(handle, token)


@contextmanager
def private_windows_directory(path):
    drive, parts = R._root_parts(path)
    if not parts:
        raise OSError("Drive root cannot be private state")
    api = WindowsStateAPI()
    with ExitStack() as stack:
        stack.callback(api.dispose)
        handle = api.open_drive(drive)
        stack.callback(api.close, handle)
        R._checked_info(api, handle, directory=True)
        for part in parts:
            created = False
            try:
                child = api.child(handle, part, directory=True)
            except FileNotFoundError:
                child = api.child(handle, part, directory=True, disposition=FILE_OPEN_IF)
                created = True
            stack.callback(api.close, child)
            R._checked_info(api, child, directory=True)
            if created:
                api.private_info(child, directory=True)
            handle = child
        api.private_info(handle, directory=True)
        yield WindowsDirectory(api, handle)
