"""Resolve a supplied Codex installation for opt-in, native-PID-bound launches."""
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from .credentials import IntegrationError


def _require(ok, reason):
    if not ok:
        raise IntegrationError(f"Cannot verify live Codex: {reason}. No live session started.")


def _target():
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(
        platform.machine().lower())
    _require(sys.platform in ("linux", "darwin") and arch, "unsupported native platform")
    triple = ("aarch64" if arch == "arm64" else "x86_64") + (
        "-apple-darwin" if sys.platform == "darwin" else "-unknown-linux-musl")
    return sys.platform, arch, triple


def _check(path, seen, kind):
    info = path.lstat()
    mode = info.st_mode
    _require(info.st_uid in (0, os.geteuid()) and stat.S_IFMT(mode) == kind,
             "unexpected path type or owner")
    if kind != stat.S_IFLNK:
        # A root-owned sticky temp ancestor is safe only because each child
        # component is separately required to be root- or current-user-owned.
        sticky = (kind == stat.S_IFDIR and info.st_uid == 0 and mode & stat.S_ISVTX
                  and str(path) in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"))
        _require(not mode & 0o022 or sticky, "writable installation path")
        _require(not mode & (stat.S_ISUID | stat.S_ISGID) or kind == stat.S_IFDIR,
                 "privileged executable or file")
    stamp = (info.st_dev, info.st_ino, info.st_uid, mode,
             None if kind == stat.S_IFDIR else (info.st_size, info.st_ctime_ns))
    _require(stamp == seen.setdefault(path, stamp), "installation changed during verification")
    return info


def _file(path, seen):
    for parent in reversed(path.parents):
        _check(parent, seen, stat.S_IFDIR)
    _require(not seen[path.parent][3] & 0o022, "file in shared writable directory")
    return _check(path, seen, stat.S_IFREG)


def _darwin_permissions(seen):
    # macOS ACLs can grant writes not reflected in st_mode. Permit deny-only
    # ACLs (common on home directories); do not interpret discretionary grants.
    if sys.platform == "darwin":
        result = subprocess.run(["/bin/ls", "-lden", *map(str, seen)], env={"LC_ALL": "C"},
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=3, check=True)
        entries = [line for line in result.stdout.splitlines() if re.match(rb"^\s*\d+:", line)]
        _require(all(b" deny " in entry and b" allow " not in entry for entry in entries),
                 "unsupported installation ACL")


def _manifest(path, seen):
    _file(path, seen)
    def unique(pairs):
        value = dict(pairs)
        _require(len(value) == len(pairs), "ambiguous package manifest")
        return value
    with path.open("rb") as stream:
        raw = stream.read(65537)
    _require(len(raw) <= 65536, "oversized package manifest")
    value = json.loads(raw, object_pairs_hook=unique)
    _require(isinstance(value, dict), "invalid package manifest")
    return value


def _package(wrapper, version, target, seen):
    system, arch, triple = target
    root = wrapper.parent.parent
    _require(root.parts[-3:] == ("node_modules", "@openai", "codex")
             and wrapper == root / "bin/codex.js", "unrecognized npm wrapper")
    _file(wrapper, seen)
    _require(os.access(wrapper, os.X_OK), "non-executable npm wrapper")
    alias = f"@openai/codex-{system}-{arch}"
    tagged = f"{version}-{system}-{arch}"
    meta = _manifest(root / "package.json", seen)
    deps = meta.get("optionalDependencies")
    _require(meta.get("name") == "@openai/codex" and meta.get("version") == version
             and meta.get("bin") == {"codex": "bin/codex.js"}
             and isinstance(deps, dict) and deps.get(alias) == f"npm:@openai/codex@{tagged}",
             "wrapper package identity or version mismatch")
    native = root / "node_modules" / alias
    meta = _manifest(native / "package.json", seen)
    _require(meta.get("name") == "@openai/codex" and meta.get("version") == tagged
             and meta.get("os") == [system] and meta.get("cpu") == [arch],
             "native platform package identity or version mismatch")
    return native / "vendor" / triple / "bin/codex"


def _resolve(path, version, target, seen):
    for parent in reversed(path.parents):
        _check(parent, seen, stat.S_IFDIR)
    if path.is_symlink():
        _check(path, seen, stat.S_IFLNK)
        _require(path.name == "codex", "unexpected executable link")
        if path.parent.name == "bin":
            relative = "../lib/node_modules/@openai/codex/bin/codex.js"
        elif path.parent.parts[-2:] == ("node_modules", ".bin"):
            relative = "../@openai/codex/bin/codex.js"
        else:
            raise IntegrationError("Cannot verify live Codex: not an npm bin link.")
        wrapper = Path(os.path.normpath(path.parent / relative))
        _require(os.readlink(path) in (relative, str(wrapper)), "unexpected npm link destination")
        return _package(wrapper, version, target, seen)
    if path.name == "codex.js":
        return _package(path, version, target, seen)
    if "node_modules" in path.parts:
        # Rechecking the resolved npm binary must not bypass package validation.
        _require(len(path.parents) >= 8, "unexpected npm binary location")
        expected = _package(path.parents[6] / "bin/codex.js", version, target, seen)
        _require(path == expected, "unexpected npm binary location")
    _require(path.name in ("codex", "codex-" + target[2]), "unexpected native executable name")
    return path


def _native(path, target):
    system, arch, _ = target
    with path.open("rb") as stream:
        header = stream.read(64)
    if system == "darwin":
        return (len(header) == 64 and header[:4] == b"\xcf\xfa\xed\xfe"
                and struct.unpack_from("<I", header, 4)[0] == (0x100000C if arch == "arm64" else 0x1000007)
                and struct.unpack_from("<I", header, 12)[0] == 2)
    return (len(header) == 64 and header[:7] == b"\x7fELF\x02\x01\x01"
            and struct.unpack_from("<H", header, 16)[0] in (2, 3)
            and struct.unpack_from("<H", header, 18)[0] == (183 if arch == "arm64" else 62))


def _version(path, version):
    with tempfile.TemporaryDirectory(prefix="ov-codex-version-") as tmp:
        tmp = str(Path(tmp).resolve(strict=True))
        env = {"HOME": tmp, "CODEX_HOME": tmp, "TMPDIR": tmp, "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        with subprocess.Popen([str(path), "--version"], cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            try:
                code = proc.wait(timeout=4)
            finally:
                if proc.poll() is None:
                    proc.kill()
            # Wait first: a noisy executable fills the pipe and times out rather
            # than growing memory. Never wait on an inherited stdout writer.
            os.set_blocking(proc.stdout.fileno(), False)
            out = proc.stdout.read(128)
            expected = f"codex-cli {version}".encode()
            return code == 0 and out in (expected, expected + b"\n") and proc.stdout.read(1) == b""


def resolve_native_codex(executable, version):
    """Return a verified absolute POSIX binary path. Never execute a wrapper or search PATH."""
    try:
        _require(isinstance(version, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}", version),
                 "expected an exact numeric version")
        path = Path(executable)
        _require(path.is_absolute() and path.anchor == "/" and ".." not in path.parts,
                 "expected an absolute installation path")
        target, seen = _target(), {}
        binary = _resolve(path, version, target, seen)
        _file(binary, seen)
        _darwin_permissions(seen)
        _require(os.access(binary, os.X_OK) and _native(binary, target), "not an executable for this platform")
        _require(_version(binary, version), "native executable version mismatch")
        for checked, stamp in list(seen.items()):
            _check(checked, seen, stat.S_IFMT(stamp[3]))
        return str(binary)
    except (OSError, TypeError, ValueError, RecursionError, subprocess.SubprocessError):
        raise IntegrationError("Cannot verify live Codex installation or exact native version. No live session started.") from None
