"""Windows session ownership. Only a generation number is stored on disk."""

import ctypes
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from openvegas.emotes._windows_state import private_windows_directory

from .credentials import IntegrationError


def _local_appdata():
    # Environment variables can differ between two launchers for the same user.
    # Use the current user's OS-known folder, not HOME, TEMP, or LOCALAPPDATA.
    shell = ctypes.WinDLL("shell32.dll", winmode=0x800)
    ole = ctypes.WinDLL("ole32.dll", winmode=0x800)
    get = shell.SHGetKnownFolderPath
    get.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get.restype = ctypes.c_int32
    free = ole.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    guid = ctypes.create_string_buffer(UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)
    ptr = ctypes.c_void_p()
    try:
        if get(guid, 0, None, ctypes.byref(ptr)) or not ptr.value:
            raise OSError("Windows local user data directory unavailable")
        return Path(ctypes.wstring_at(ptr.value))
    finally:
        free(ptr)


@contextmanager
def acquire(name, directory):
    root = (
        Path(directory) if directory is not None else _local_appdata() / "OpenVegas-Native-Leases"
    )
    # The directory and its lock are permanent. Replacing either while another
    # process holds it would allow a second owner on a different file.
    with (
        private_windows_directory(root),
        private_windows_directory(root / name) as state,
        state.locked(),
    ):
        try:
            previous = state.read("generation", 20)
        except FileNotFoundError:
            generation = 1
        else:
            if not previous.isdigit() or previous.startswith(b"0"):
                raise IntegrationError("Invalid Windows session generation.")
            generation = int(previous) + 1
        if generation >= 2**63:
            raise IntegrationError("Windows session generation exhausted.")
        encoded = str(generation).encode("ascii")
        state.atomic_write("generation", encoded)
        if state.read("generation", 20) != encoded:
            raise IntegrationError("Windows session generation was not persisted.")
        yield generation
