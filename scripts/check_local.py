"""Bounded offline checks and explicitly selected, read-only HTTP probes.

No application module is imported by this script. Existing .env files, user
configuration, credentials, and third-party pytest plugins are not inherited.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FAST_TESTS = (
    "tests/test_deployment/test_local_checks.py",
    "tests/test_cli/test_ui_command_mode.py",
    "tests/test_agent/test_manifest_contract.py",
    "tests/test_auth/test_config_session_storage.py",
    "tests/test_client_network_errors.py",
)
SOURCE_DIRS = {"openvegas", "server", "tests", "scripts", "ui"}
SOURCE_SUFFIXES = {
    ".py", ".html", ".css", ".js", ".ts", ".json", ".patch",
    ".png", ".jpg", ".jpeg", ".svg", ".woff", ".woff2",
}
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".history", "third_party_external",
}
CHECK_FILES = ("scripts/check_local.py", "tests/test_deployment/test_local_checks.py")
PROBES = (
    ("liveness", "/health/live", "live"),
    ("readiness", "/health/ready", "ready"),
    ("browser-ui", "/ui", "html"),
    ("stylesheet", "/ui/assets/theme.css", "css"),
    ("avatar-manifest", "/ui/assets/avatar-manifest.json", "json"),
    ("unauthenticated-api", "/inference/mode", "auth"),
)
PROBE_WORKER = (
    "import runpy,sys; module=runpy.run_path(sys.argv[1]); "
    "raise SystemExit(module['probe_checks'](sys.argv[2], float(sys.argv[3])))"
)

BOOTSTRAP = r'''
import os
from pathlib import Path
import socket
import sys

root = Path(sys.argv[1]).resolve()
os.chdir(root)
sys.path.insert(0, str(root))

def deny_network(*args, **kwargs):
    raise RuntimeError("offline checks prohibit network and database connections")

# Install before pytest imports or collection, not as a fixture after imports.
for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
    setattr(socket, name, deny_network)
for name in ("connect", "connect_ex", "sendto", "sendmsg"):
    if hasattr(socket.socket, name):
        setattr(socket.socket, name, deny_network)

try:
    import pytest
except ImportError:
    print("CHECK_ERROR pytest is not installed in the selected interpreter")
    raise SystemExit(2)

raise SystemExit(pytest.main([
    "-q", "--tb=no", "--no-header", "-p", "no:cacheprovider",
    "-p", "pytest_asyncio.plugin", *sys.argv[2:],
]))
'''


def clean_env(home: Path, snapshot: Path | None = None) -> dict[str, str]:
    """Use an allowlist, never copy the invoking shell's environment."""
    env = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"),
        "TMPDIR": str(home),
        "TEMP": str(home),
        "TMP": str(home),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "OPENVEGAS_DOTENV_OVERRIDE": "0",
        "OPENVEGAS_TEST_MODE": "1",
        "OPENVEGAS_RUNTIME_ENV": "test",
        "OPENVEGAS_ENABLE_TOUCHID": "0",
        "OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if snapshot is not None:
        env["OPENVEGAS_ROOT"] = str(snapshot)
    return env


def allowed_source_file(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return False
    if any(p in EXCLUDED_PARTS or p.startswith(".env") for p in relative.parts):
        return False
    if relative.as_posix() == "pyproject.toml":
        return True
    return (
        len(relative.parts) > 1
        and relative.parts[0] in SOURCE_DIRS
        and relative.suffix.lower() in SOURCE_SUFFIXES
    )


def snapshot_source(source: Path, destination: Path, env: dict[str, str], deadline: float) -> int:
    source = source.resolve()
    if not (source / "pyproject.toml").is_file() or not (source / "server/main.py").is_file():
        raise ValueError("source must be an OpenVegas checkout or source archive")
    if (source / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(source), "ls-files", "--cached", "-z"],
            env=env, capture_output=True, timeout=remaining(deadline), check=False,
        )
        if result.returncode:
            raise ValueError("unable to enumerate tracked source files")
        names = set(os.fsdecode(result.stdout).split("\0")) - {""}
        names.update(CHECK_FILES)
    else:
        # Source archives have no Git index: restrict traversal to product/test trees.
        names = {"pyproject.toml"}
        for directory in sorted(SOURCE_DIRS):
            for current, dirs, files in os.walk(source / directory, followlinks=False):
                remaining(deadline)
                dirs[:] = [d for d in dirs if d not in EXCLUDED_PARTS and not d.startswith(".env")
                           and not (Path(current) / d).is_symlink()]
                names.update((Path(current) / f).relative_to(source).as_posix() for f in files)
    count = 0
    for name in sorted(names):
        remaining(deadline)
        relative = Path(name)
        if not allowed_source_file(relative):
            continue
        origin = source / relative
        if origin.is_symlink() or any(parent.is_symlink() for parent in origin.parents if parent != source):
            continue
        if not origin.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
        count += 1
    return count


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("check deadline exceeded")
    return seconds


def run_bounded(command: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    with subprocess.Popen(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", start_new_session=(os.name == "posix"),
    ) as child:
        try:
            output, _ = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                child.kill()
            child.communicate()
            raise TimeoutError("check subprocess exceeded deadline") from None
        return child.returncode, output


def fast_checks(source: Path, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="openvegas-check-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        snapshot = root / "source"
        env = clean_env(home, snapshot)
        count = snapshot_source(source, snapshot, env, deadline)
        if any(not (snapshot / path).is_file() for path in FAST_TESTS):
            raise ValueError("source snapshot is missing a required fast-check test")
        bootstrap = root / "bootstrap.py"
        bootstrap.write_text(BOOTSTRAP, encoding="utf-8")
        print(f"snapshot: {count} allowlisted files; existing environment/configuration excluded", flush=True)
        code, output = run_bounded(
            [sys.executable, "-I", "-B", str(bootstrap), str(snapshot), *FAST_TESTS],
            cwd=snapshot, env=env, timeout=remaining(deadline),
        )
        # Do not echo captured exceptions, test parameters, tokens, or response bodies.
        summaries = re.findall(r"\b\d+ (?:passed|failed|errors?|skipped|deselected|warnings?)\b", output)
        if summaries:
            print("tests: " + ", ".join(dict.fromkeys(summaries)))
        if "CHECK_ERROR pytest" in output:
            print("FAIL: pytest unavailable; use an existing development interpreter (nothing installed)")
        print(f"{'PASS' if code == 0 else 'FAIL'}: offline fast checks (exit {code})")
        return 0 if code == 0 else 1


def normalize_base_url(value: str, allow_remote: bool) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid probe origin") from None
    if (
        parsed.scheme not in {"http", "https"} or not host
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        or any(ch.isspace() for ch in value) or "\\" in value
    ):
        raise ValueError("probe URL must be an HTTP(S) origin without credentials, path, query, or fragment")
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host.lower() == "localhost"
    if not local and not allow_remote:
        raise ValueError("remote probes require explicit --allow-remote")
    # Pin localhost to loopback; HTTPS must also validate against that IP.
    if host.lower() == "localhost":
        host = "127.0.0.1"
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def response_ok(kind: str, status: int, content_type: str, body: bytes) -> bool:
    if kind == "auth":
        return status in {401, 403}
    if status != 200 or not body:
        return False
    if kind == "html":
        return "text/html" in content_type and b"<html" in body.lower()
    if kind == "css":
        return "text/css" in content_type and b"{" in body
    if "json" not in content_type:
        return False
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if kind == "live":
        return data.get("status") == "up"
    if kind == "ready":
        return data.get("status") == "ready" and data.get("mode") != "test"
    return bool(data)


def probe_checks(base_url: str, timeout: float) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirects())
    deadline = time.monotonic() + timeout
    failed = False
    for label, path, kind in PROBES:
        try:
            request = urllib.request.Request(base_url + path, method="GET", headers={"User-Agent": "OpenVegas-readonly-check/1"})
            try:
                response = opener.open(request, timeout=min(5.0, remaining(deadline)))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                content_type = response.headers.get("Content-Type", "").lower()
                body = response.read(262145)
            ok = len(body) <= 262144 and response_ok(kind, status, content_type, body)
            print(f"{'PASS' if ok else 'FAIL'}: {label} (HTTP {status})", flush=True)
        except (OSError, ValueError, TimeoutError, urllib.error.URLError):
            ok = False
            print(f"FAIL: {label} (connection, TLS, or deadline failure)", flush=True)
        failed |= not ok
    return int(failed)


def timeout_arg(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be between 0 and 30 seconds") from None
    if not 0 < seconds <= 30:
        raise argparse.ArgumentTypeError("timeout must be between 0 and 30 seconds")
    return seconds


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, epilog="No installs, accounts, charges, migrations, or live DB tests. Run with an existing development Python environment.")
    modes = result.add_subparsers(dest="mode")
    fast = modes.add_parser("fast", help="offline selected pytest checks in a temporary clean snapshot (default)")
    fast.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    fast.add_argument("--timeout", type=timeout_arg, default=30.0, help="total deadline in seconds, at most 30")
    probe = modes.add_parser("probe", help="GET-only health, UI/assets, and unauthenticated rejection probes")
    probe.add_argument("--base-url", default="http://127.0.0.1:8000")
    probe.add_argument("--allow-remote", action="store_true", help="explicitly permit contacting a remote HTTP(S) origin")
    probe.add_argument("--timeout", type=timeout_arg, default=30.0, help="total deadline in seconds, at most 30")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.mode in {None, "fast"}:
            return fast_checks(getattr(args, "source", Path(__file__).resolve().parents[1]), getattr(args, "timeout", 30.0))
        origin = normalize_base_url(args.base_url, args.allow_remote)
        with tempfile.TemporaryDirectory(prefix="openvegas-probe-") as temporary:
            home = Path(temporary)
            command = [sys.executable, "-I", "-B", "-c", PROBE_WORKER, str(Path(__file__).resolve()), origin, str(args.timeout)]
            code, output = run_bounded(command, cwd=home, env=clean_env(home), timeout=args.timeout)
            for line in output.splitlines():
                if re.fullmatch(r"(?:PASS|FAIL): [a-z-]+ \((?:HTTP [0-9]{3}|connection, TLS, or deadline failure)\)", line):
                    print(line)
            return 0 if code == 0 else 1
    except (ValueError, TimeoutError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("FAIL: unable to prepare or run checks; no environment or response details emitted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
