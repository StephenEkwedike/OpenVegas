"""Offline installed-distribution gate, NOT frozen-binary or native UX certification.

Usage: python scripts/verify_emote_release.py --python /venv/bin/python --json report.json
Install a built wheel first. This script never installs, builds, or downloads anything.
The target interpreter runs with -I -B, outside the checkout, with an empty home.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

PACKS = (
    "beat-maker", "bicycle-finish", "pixel-courier", "skyline-dunk",
    "three-point-glow", "visor-explorer",
)
ASSETS = ("manifest.json", "sheet.png", "provenance.json")
LIMITATION = (
    "Installed Python distribution only; not shipping frozen/npm binaries, "
    "native terminal UX, external host hooks, art approval, or commerce certification. "
    "Auth discovery uses synthetic public HTTP data, not live discovery or login/signup."
)


def clean_environment(home: Path) -> dict[str, str]:
    # Do not forward provider credentials, dotenv selectors, or Python path overrides.
    home = home.resolve()
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    if sys.platform == "darwin":
        # CoreFoundation otherwise injects this at startup, breaking exact equality.
        env["__CF_USER_TEXT_ENCODING"] = f"0x{os.getuid():X}:0x0:0x0"
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "localappdata"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TMPDIR": str(home), "TMP": str(home), "TEMP": str(home),
        "PATH": os.defpath, "LANG": "C.UTF-8", "NO_COLOR": "1", "TERM": "dumb",
        "OPENVEGAS_TEST_MODE": "1", "OPENVEGAS_RUNTIME_ENV": "test",
        "OPENVEGAS_DOTENV_OVERRIDE": "0", "OPENVEGAS_ENABLE_TOUCHID": "0",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    })
    return env


def install_guard() -> list[str]:
    violations: list[str] = []

    def audit(event, args):
        # gethostname reads local OS identity; unlike DNS lookups it does no network I/O.
        forbidden = event.startswith("socket.") and event not in {
            "socket.__new__", "socket.gethostname",
        }
        forbidden |= event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = Path(os.fsdecode(args[0])).name.lower()
            forbidden |= name == "env.md" or name == ".env" or name.startswith(".env.")
        if forbidden:
            violations.append(event)
            raise RuntimeError("Offline verification blocked a forbidden operation")

    sys.addaudithook(audit)
    return violations


def check_record(dist, relative: str) -> Path:
    entries = {str(item): item for item in dist.files or ()}
    entry = entries.get(relative)
    if entry is None or entry.hash is None or entry.hash.mode != "sha256":
        raise ValueError(f"Missing SHA-256 installed RECORD entry: {relative}")
    path = Path(dist.locate_file(entry))
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or symlinked installed file: {relative}")
    limit = 16 * 1024 * 1024 if path.suffix == ".png" else 64 * 1024
    if path.stat().st_size > limit:
        raise ValueError(f"Oversized installed resource: {relative}")
    data = path.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    if digest != entry.hash.value or len(data) != entry.size:
        raise ValueError(f"Installed RECORD mismatch: {relative}")
    return path


@contextlib.contextmanager
def forbid_auth_writes(violations: list[str]):
    # Scoped because later CLI checks may legitimately create temporary state.
    active = True

    def audit(event, args):
        if not active:
            return
        forbidden = event in {
            "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod",
            "os.chown", "os.link", "os.symlink", "os.truncate", "os.utime",
        }
        if event == "open":
            mode, flags = args[1:3]
            forbidden |= bool(mode and any(flag in mode for flag in "wax+"))
            forbidden |= bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT |
                                       os.O_TRUNC | os.O_APPEND))
        if forbidden:
            violations.append(event)
            raise RuntimeError("Auth discovery must not write HOME or configuration")

    sys.addaudithook(audit)
    try:
        yield
        if violations:
            raise RuntimeError("Auth discovery swallowed a forbidden write")
    finally:
        active = False


def check_auth_discovery(dist, write_violations: list[str]) -> dict:
    home = Path.home().resolve()
    if any(home.iterdir()):
        raise ValueError("Auth discovery requires an empty HOME")
    if os.environ != clean_environment(home):
        raise ValueError("Auth discovery requires the credential-free environment allowlist")
    with forbid_auth_writes(write_violations):
        modules = {}
        for name in ("auth", "auth_config", "config", "telemetry"):
            expected = check_record(dist, f"openvegas/{name}.py").resolve()
            module = importlib.import_module(f"openvegas.{name}")
            if Path(module.__file__).resolve() != expected:
                raise ValueError("Source shadowing an installed authentication module")
            modules[name] = module
        auth, config = modules["auth"], modules["config"]
        if config.CONFIG_FILE != home / ".openvegas" / "config.json":
            raise ValueError("Authentication config escaped the isolated HOME")
        import httpx

        from supabase import Client, create_client

        if auth.create_client is not create_client:
            raise ValueError("Authentication must use the real Supabase client constructor")
        public_url = "https://auth.example.test"
        public_key = "sb_publishable_synthetic_offline_fixture"
        calls = []

        def public_fixture(url, **kwargs):
            calls.append(url)
            if url != config.FALLBACK_DEFAULT_BACKEND_URL.rstrip("/") + "/auth/config":
                raise ValueError("Discovery did not use the default public backend")
            if kwargs != {"timeout": 10, "follow_redirects": False}:
                raise ValueError("Unexpected discovery request options")
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "supabase_url": public_url, "supabase_anon_key": public_key,
            })

        original_get = auth.httpx.get
        try:
            auth.httpx.get = public_fixture
            instance = auth.SupabaseAuth()
        finally:
            auth.httpx.get = original_get
        if len(calls) != 1 or not isinstance(instance.client, Client):
            raise ValueError("Discovery must construct one real Supabase client")
        if (str(instance.client.supabase_url).rstrip("/") != public_url or
                instance.client.supabase_key != public_key):
            raise ValueError("Client did not use the discovered public settings")
        if any(home.iterdir()):
            raise ValueError("Auth discovery changed the empty HOME")
    return {
        "synthetic_http_fixture": True, "real_supabase_client_constructor": True,
        "discovery_requests": 1, "manual_configuration": False,
        "home_unchanged": True, "installed_modules": sorted(modules),
    }


def run_dispatch(dist, kind: str, args: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    previous = sys.argv
    sys.argv = ["openvegas", *args]
    code = 0
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                if kind == "module":
                    runpy.run_module("openvegas.cli", run_name="__main__")
                else:
                    entries = [ep for ep in dist.entry_points
                               if ep.group == "console_scripts" and ep.name == "openvegas"]
                    if len(entries) != 1 or entries[0].value != "openvegas.cli:cli":
                        raise ValueError("Missing or unexpected openvegas console dispatcher")
                    entries[0].load()()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        sys.argv = previous
    return code, output.getvalue()


def probe() -> dict:
    violations = install_guard()
    result = {
        "schema": 1, "status": "fail",
        "platform": {"win32": "Windows", "darwin": "Darwin", "linux": "Linux"}.get(
            sys.platform, sys.platform
        ),
        "python": ".".join(map(str, sys.version_info[:3])), "scope": LIMITATION,
        "checks": [], "failures": [],
    }

    def check(name, action):
        try:
            detail = action()
            result["checks"].append({"name": name, "status": "pass", "detail": detail})
            return True
        except Exception as exc:  # noqa: BLE001 - collect all failed gates, never pass on errors
            # No arbitrary exception payloads: imports can include sensitive environment data.
            result["checks"].append({"name": name, "status": "fail", "error": type(exc).__name__})
            result["failures"].append(name)
            return False

    def installed():
        dist = importlib.metadata.distribution("openvegas")
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            raise ValueError("Editable installs cannot certify installed package contents")
        expected = check_record(dist, "openvegas/__init__.py").resolve()
        import openvegas
        if Path(openvegas.__file__).resolve() != expected:
            raise ValueError("Source shadowing the installed distribution")
        result["distribution_version"] = dist.version
        return "Non-editable distribution; imported package matches installed RECORD"

    if not check("installed-origin", installed):
        return result
    dist = importlib.metadata.distribution("openvegas")
    result["auth_write_violations"] = []
    check("clean-home-public-auth-discovery",
          lambda: check_auth_discovery(dist, result["auth_write_violations"]))

    def inventory():
        from openvegas.emotes.resources import PackRepository
        names = PackRepository().names()
        if set(names) != set(PACKS):
            raise ValueError("Installed pack inventory must contain exactly the six release packs")
        return names

    check("six-pack-inventory", inventory)
    for name in PACKS:
        def asset_check(name=name):
            from openvegas.emotes.manifest import parse_json
            from openvegas.emotes.resources import PackRepository
            for filename in ASSETS:
                check_record(dist, f"openvegas/emotes/assets/{name}/{filename}")
            root = Path(dist.locate_file(f"openvegas/emotes/assets/{name}"))
            provenance = parse_json((root / "provenance.json").read_bytes())
            if not provenance.get("source_sha256") or not provenance.get("approval"):
                raise ValueError("Incomplete provenance")
            pack = PackRepository().load(name)
            if pack.manifest.pack_id != f"openvegas.{name}" or pack.manifest.sheet != "sheet.png":
                raise ValueError("Pack identity or sheet mismatch")
            used = {pack.manifest.reduced_motion_frame}
            used.update(i for clip in pack.manifest.animations.values() for i in clip.frames)
            for index in sorted(used):
                with pack.frame(index) as frame:
                    if frame.size != (pack.manifest.width, pack.manifest.height):
                        raise ValueError("Decoded frame geometry mismatch")
            return {"pack_id": pack.manifest.pack_id, "version": pack.manifest.version,
                    "frames": len(used), "sheet_sha256": pack.manifest.sha256}

        check(f"pack:{name}", asset_check)

    for kind in ("module", "console-entry-point"):
        def doctor(kind=kind):
            code, output = run_dispatch(dist, kind, ["emote", "doctor"])
            if code != 0 or any(f"Pack {name}: valid;" not in output for name in PACKS):
                raise ValueError("Doctor failed or omitted a required pack")
            return "All six packs reported valid"

        check(f"{kind}:emote-doctor", doctor)
        for name in PACKS:
            def artist(name=name, kind=kind):
                root = str(dist.locate_file(f"openvegas/emotes/assets/{name}"))
                code, output = run_dispatch(dist, kind, ["emote", "artist", "validate", root])
                if code != 0 or f"Valid: openvegas.{name} " not in output:
                    raise ValueError("Artist validation dispatcher failed")
                return "Installed artist validation dispatched"

            check(f"{kind}:artist:{name}", artist)

    if violations:
        result["failures"].append("offline-guard")
    result["guard_violations"] = sorted(set(violations))
    result["status"] = "fail" if result["failures"] else "pass"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Interpreter with the wheel installed")
    parser.add_argument("--json", type=Path, help="Write machine-readable evidence")
    parser.add_argument("--timeout", type=float, default=120, help="Probe deadline in seconds")
    parser.add_argument("--_probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._probe:
        result = probe()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "pass" else 1
    if not 0 < args.timeout <= 600:
        parser.error("--timeout must be in (0, 600]")
    try:
        with tempfile.TemporaryDirectory(prefix="openvegas-release-") as scratch:
            home = Path(scratch)
            completed = subprocess.run(
                [str(Path(args.python).absolute()), "-I", "-B", str(Path(__file__).resolve()), "--_probe"],
                cwd=home, env=clean_environment(home), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=args.timeout, check=False,
            )
            result = json.loads(completed.stdout)
            if completed.returncode != 0 and result.get("status") == "pass":
                raise ValueError("Probe exit status disagrees with report")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result = {"schema": 1, "status": "fail", "scope": LIMITATION,
                  "failures": ["probe-launch-timeout-or-invalid-report"]}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
