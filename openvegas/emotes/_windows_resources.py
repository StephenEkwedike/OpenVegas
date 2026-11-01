"""Fail-closed Windows resource reads. No path is reopened after validation.

Only local, fixed-drive paths are supported. See EMOTES_WINDOWS.md for the
security boundary, native test gate, and Microsoft API references.
"""

from __future__ import annotations

import ctypes as C
import ntpath
import os
from contextlib import ExitStack
from dataclasses import dataclass

DWORD = C.c_uint32
HANDLE = C.c_void_p
FILE_SHARE_READ = 0x1
FILE_READ_DATA = 0x1
FILE_TRAVERSE = 0x20
FILE_READ_ATTRIBUTES = 0x80
SYNCHRONIZE = 0x100000
FILE_OPEN = 0x1
FILE_SYNCHRONOUS_IO_NONALERT = 0x20
FILE_OPEN_REPARSE_POINT = 0x200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x40
OBJ_DONT_REPARSE = 0x1000
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_DEVICE = 0x40
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_TYPE_DISK = 1
DRIVE_FIXED = 3
INVALID_HANDLE_VALUE = C.c_void_p(-1).value


class _UnicodeString(C.Structure):
    _fields_ = [("Length", C.c_uint16), ("MaximumLength", C.c_uint16), ("Buffer", C.c_void_p)]


class _ObjectAttributes(C.Structure):
    _fields_ = [
        ("Length", DWORD),
        ("RootDirectory", HANDLE),
        ("ObjectName", C.POINTER(_UnicodeString)),
        ("Attributes", DWORD),
        ("SecurityDescriptor", C.c_void_p),
        ("SecurityQualityOfService", C.c_void_p),
    ]


class _Status(C.Union):
    _fields_ = [("Status", C.c_int32), ("Pointer", C.c_void_p)]


class _IOStatusBlock(C.Structure):
    _fields_ = [("Result", _Status), ("Information", C.c_size_t)]


class _FileInformation(C.Structure):
    _fields_ = [
        ("attributes", DWORD),
        ("creation", DWORD * 2),
        ("access", DWORD * 2),
        ("write", DWORD * 2),
        ("volume", DWORD),
        ("size_high", DWORD),
        ("size_low", DWORD),
        ("links", DWORD),
        ("index_high", DWORD),
        ("index_low", DWORD),
    ]


@dataclass(frozen=True)
class _Info:
    attributes: int
    size: int
    identity: tuple[int, int, int]
    modified: tuple[int, int]


def _component(name: str) -> None:
    # Reject Win32 aliases even though NtCreateFile uses literal component names.
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(
        f"{prefix}{i}" for prefix in ("COM", "LPT") for i in "123456789\u00b9\u00b2\u00b3"
    )
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
        or name.split(".", 1)[0].upper() in devices
        or len(name.encode("utf-16-le", errors="surrogatepass")) > 510
    ):
        raise OSError("Unsafe Windows resource component")


def _root_parts(root) -> tuple[str, list[str]]:
    raw = os.fspath(root).replace("/", "\\")
    drive, tail = ntpath.splitdrive(raw)
    if raw.startswith("\\\\") or ".." in tail.split("\\") or (drive and not tail.startswith("\\")):
        raise OSError("Unsupported Windows resource root")
    for part in tail.split("\\"):
        if part and part != ".":
            _component(part)
    # abspath is lexical, not a filesystem resolve/check used to authorize opens.
    absolute = ntpath.abspath(raw)
    drive, tail = ntpath.splitdrive(absolute)
    if (
        len(drive) != 2
        or drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        or drive[1] != ":"
    ):
        raise OSError("A local Windows drive is required")
    parts = [part for part in tail.split("\\") if part]
    if len(parts) > 256:
        raise OSError("Resource root exceeds directory depth limit")
    for part in parts:
        _component(part)
    return drive + "\\", parts


class _WindowsAPI:
    """Small ctypes boundary, separately injectable from the handle-walk policy."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows resource API unavailable")
        try:
            kernel = C.WinDLL("kernel32", use_last_error=True)
            native = C.WinDLL("ntdll", use_last_error=True)

            def bind(dll, name, args, result):
                function = getattr(dll, name)
                function.argtypes, function.restype = args, result
                return function

            self._create = bind(
                kernel,
                "CreateFileW",
                [C.c_wchar_p, DWORD, DWORD, C.c_void_p, DWORD, DWORD, HANDLE],
                HANDLE,
            )
            self._drive_type = bind(kernel, "GetDriveTypeW", [C.c_wchar_p], DWORD)
            self._open = bind(
                native,
                "NtCreateFile",
                [
                    C.POINTER(HANDLE),
                    DWORD,
                    C.POINTER(_ObjectAttributes),
                    C.POINTER(_IOStatusBlock),
                    C.c_void_p,
                    DWORD,
                    DWORD,
                    DWORD,
                    DWORD,
                    C.c_void_p,
                    DWORD,
                ],
                C.c_int32,
            )
            self._type = bind(kernel, "GetFileType", [HANDLE], DWORD)
            self._info = bind(
                kernel,
                "GetFileInformationByHandle",
                [HANDLE, C.POINTER(_FileInformation)],
                C.c_int32,
            )
            self._read = bind(
                kernel,
                "ReadFile",
                [HANDLE, C.c_void_p, DWORD, C.POINTER(DWORD), C.c_void_p],
                C.c_int32,
            )
            self._close = bind(kernel, "CloseHandle", [HANDLE], C.c_int32)
        except (AttributeError, OSError) as exc:
            raise OSError("Windows secure resource API unavailable") from exc

    def open_drive(self, drive: str) -> int:
        if self._drive_type(drive) != DRIVE_FIXED:
            raise OSError("Only fixed local resource drives are supported")
        handle = self._create(
            "\\\\?\\" + drive,
            FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            FILE_SHARE_READ,
            None,
            3,  # OPEN_EXISTING
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            raise OSError("Cannot open resource drive")
        return handle

    def open_child(self, parent: int, name: str, directory: bool) -> int:
        _component(name)
        buffer = C.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le", errors="surrogatepass"))
        unicode = _UnicodeString(length, length + 2, C.cast(buffer, C.c_void_p))
        attributes = _ObjectAttributes(
            C.sizeof(_ObjectAttributes),
            parent,
            C.pointer(unicode),
            OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
            None,
            None,
        )
        handle, status_block = HANDLE(), _IOStatusBlock()
        access = (
            FILE_READ_ATTRIBUTES | SYNCHRONIZE | (FILE_TRAVERSE if directory else FILE_READ_DATA)
        )
        # Type-neutral open allows inspection/rejection of *every* reparse tag.
        # Retaining read-only sharing also denies normal writers and renames.
        status = self._open(
            C.byref(handle),
            access,
            C.byref(attributes),
            C.byref(status_block),
            None,
            0,
            FILE_SHARE_READ,
            FILE_OPEN,
            FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
        if status < 0:
            raise OSError("Cannot securely open resource component")
        if handle.value in (None, INVALID_HANDLE_VALUE):
            raise OSError("Invalid resource handle")
        return handle.value

    def info(self, handle: int) -> _Info:
        if self._type(handle) != FILE_TYPE_DISK:
            raise OSError("Resource is not a disk file")
        info = _FileInformation()
        if not self._info(handle, C.byref(info)):
            raise OSError("Cannot inspect resource handle")
        return _Info(
            info.attributes,
            (info.size_high << 32) | info.size_low,
            (info.volume, info.index_high, info.index_low),
            tuple(info.write),
        )

    def read(self, handle: int, count: int) -> bytes:
        buffer, read = C.create_string_buffer(count), DWORD()
        if not self._read(handle, buffer, count, C.byref(read), None):
            raise OSError("Cannot read resource handle")
        if read.value > count:
            raise OSError("Invalid resource read count")
        return buffer.raw[: read.value]

    def close(self, handle: int) -> None:
        if not self._close(handle):
            raise OSError("Cannot close resource handle")


def _checked_info(api, handle: int, *, directory: bool) -> _Info:
    info = api.info(handle)
    if (
        info.attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE)
        or bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory
    ):
        raise OSError("Resource has unsafe file attributes")
    return info


def read_windows(root, parts: list[str], limit: int) -> bytes:
    drive, ancestors = _root_parts(root)
    if not parts or len(parts) > 8 or type(limit) is not int or limit < 0:
        raise OSError("Invalid bounded resource read")
    for part in parts:
        _component(part)
    api = _WindowsAPI()
    with ExitStack() as stack:
        handle = api.open_drive(drive)
        stack.callback(api.close, handle)
        _checked_info(api, handle, directory=True)
        for part in ancestors + parts[:-1]:
            handle = api.open_child(handle, part, True)
            stack.callback(api.close, handle)
            _checked_info(api, handle, directory=True)
        handle = api.open_child(handle, parts[-1], False)
        stack.callback(api.close, handle)
        before = _checked_info(api, handle, directory=False)
        if before.size > limit:
            raise OSError("Resource exceeds size limit")
        chunks, remaining = [], limit + 1
        while remaining:
            count = min(remaining, 65536)
            chunk = api.read(handle, count)
            if not chunk:
                break
            if len(chunk) > count:
                raise OSError("Invalid resource read count")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = _checked_info(api, handle, directory=False)
        if len(data) > limit or len(data) != before.size or after != before:
            raise OSError("Resource changed or exceeds size limit")
        return data
