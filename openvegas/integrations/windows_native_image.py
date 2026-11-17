"""Pin the Windows x64 native image while its owner starts the native child.

The hashes cover the executable, not its installation ACLs, plugins or helpers.
Do not use this check as proof of process ownership or billing eligibility.
"""

import hashlib
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path, PureWindowsPath

from openvegas.emotes import _windows_resources as win

from .credentials import IntegrationError

# Executable members of the registry-integrity-verified Windows x64 packages.
_IMAGES = {
    ("codex", "0.169.0"): (
        382529840,
        "ce1945eb440b5481ed1233ebaf32b7056ee7e4e452fa62ffe1cbfb26103cdee0",
    ),
    ("codex", "0.166.0"): (
        371735856,
        "9d563ee4e0441ff9d59d612013e0460ddf8e5709735c2d9f6b98b7ef8ca4eb42",
    ),
    ("claude", "2.1.322"): (
        346496160,
        "345996b4bdbae21d817f750fae2f34cbf8d81224a5444c78136091d121beb298",
    ),
}


def _locations(host, entry):
    raw = os.fspath(entry)
    path = PureWindowsPath(raw)
    if (
        not path.is_absolute()
        or len(path.drive) != 2
        or not path.drive[0].isascii()
        or not path.drive[0].isalpha()
        or path.drive[1] != ":"
        or any(part == ".." for part in path.parts)
    ):
        raise IntegrationError("An absolute local Windows installation path is required.")
    for part in path.parts[1:]:
        win._component(part)
    if path.name.lower() == host + ".exe":
        return [path]
    if path.name.lower() not in (host + ".cmd", host + ".ps1"):
        raise IntegrationError("Unsupported native Windows entry point.")
    # Treat the shim as a location hint. Never read or execute its contents.
    parent = path.parent
    modules = (
        parent.parent
        if parent.name.lower() == ".bin" and parent.parent.name.lower() == "node_modules"
        else parent / "node_modules"
    )
    if host == "codex":
        wrapper = modules / "@openai/codex"
        vendor = PureWindowsPath(
            "@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
        )
    else:
        wrapper = modules / "@anthropic-ai/claude-code"
        vendor = PureWindowsPath("@anthropic-ai/claude-code-win32-x64/claude.exe")
    return [wrapper / "node_modules" / vendor, modules / vendor]


@contextmanager
def _held_image(api, path, size, expected_sha):
    drive, parts = win._root_parts(path)
    with ExitStack() as stack:
        handle = api.open_drive(drive)
        stack.callback(api.close, handle)
        win._checked_info(api, handle, directory=True)
        for i, part in enumerate(parts):
            directory = i + 1 < len(parts)
            handle = api.open_child(handle, part, directory)
            stack.callback(api.close, handle)
            info = win._checked_info(api, handle, directory=directory)
        if not parts or info.size != size:
            raise IntegrationError(
                "Native Windows executable size does not match the reviewed version."
            )
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            count = min(1024 * 1024, remaining)
            chunk = api.read(handle, count)
            if not 0 < len(chunk) <= count:
                raise IntegrationError("Incomplete Windows executable.")
            digest.update(chunk)
            remaining -= len(chunk)
        if (
            api.read(handle, 1)
            or digest.hexdigest() != expected_sha
            or win._checked_info(api, handle, directory=False) != info
        ):
            raise IntegrationError("Native Windows executable differs from the reviewed image.")
        yield str(path)


@contextmanager
def pinned_windows_image(host, executable, version):
    pin = _IMAGES.get((host, version))
    if os.name != "nt" or pin is None:
        raise IntegrationError("No reviewed native Windows image for this host and version.")
    # File existence helps choose the npm layout; it never authorizes execution.
    candidates = [p for p in _locations(host, executable) if Path(p).is_file()]
    if len(candidates) != 1:
        raise IntegrationError("A single installed Windows executable is required.")
    with _held_image(win._WindowsAPI(), candidates[0], *pin) as path:
        yield path


@contextmanager
def installed_windows_image(host, executable):
    """Choose a reviewed size, then authenticate the complete held image before use."""
    candidates = [p for p in _locations(host, executable) if Path(p).is_file()]
    if len(candidates) != 1:
        raise IntegrationError("A single installed Windows executable is required.")
    size = Path(candidates[0]).stat().st_size
    versions = [v for (h, v), pin in _IMAGES.items() if h == host and pin[0] == size]
    if len(versions) != 1:
        raise IntegrationError("Unsupported native Windows version; update OpenVegas. No wallet opened.")
    version = versions[0]
    with pinned_windows_image(host, executable, version) as path:
        yield path, version
