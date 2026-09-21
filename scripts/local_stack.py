#!/usr/bin/env python3
"""Start the isolated local backend in order, with bounded readiness checks."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class StartupError(RuntimeError):
    """A safe, credential-free message suitable for the terminal."""


def tool_environment() -> dict[str, str]:
    # Neither shell credentials nor COMPOSE_* overrides select the local stack.
    return {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "USER",
            "TERM",
            "COLORTERM",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SYSTEMROOT",
            "WINDIR",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
        )
        if key in os.environ
    }


def local_config() -> None:
    from run_local import local_environment

    try:
        env = local_environment(ROOT / ".env.local")
        ports = {
            "DATABASE_URL": 54322,
            "SUPABASE_URL": 54321,
            "SUPABASE_PUBLIC_URL": 54321,
            "OPENVEGAS_BACKEND_URL": 8000,
            "OPENVEGAS_API_URL": 8000,
            "REDIS_URL": 16379,
        }
        for key, port in ports.items():
            address = urlsplit(env.get(key, ""))
            if address.hostname not in {"localhost", "127.0.0.1", "::1"} or address.port != port:
                raise ValueError(f"{key} must use the local stack on port {port}")
        if not env.get("SUPABASE_ANON_KEY"):
            raise ValueError("SUPABASE_ANON_KEY is missing from .env.local")
    except ValueError as exc:
        raise StartupError(str(exc)) from None


def compose(*arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / ".env.local"),
        "-f",
        str(ROOT / "compose.yaml"),
        "-p",
        "ov-restoration",
        *arguments,
    ]


def stop_child(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


class Runner:
    def __init__(self) -> None:
        folder = ROOT / ".local" / "startup"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs = Path(tempfile.mkdtemp(prefix="run-", dir=folder))
        self.number = 0

    def step(self, name: str, command: list[str], *, timeout: float, hint: str = "") -> None:
        self.number += 1
        log = self.logs / f"{self.number:02d}.log"
        print(f"[{self.number}] {name}...", flush=True)
        started = time.monotonic()
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=tool_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
            except OSError:
                raise StartupError(f"Cannot launch {name}. Check the installed tools.") from None
            try:
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise StartupError(f"{name} timed out. {hint} Private log: {log}")
                    try:
                        result = process.wait(timeout=min(15, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        print(
                            f"    Still waiting for {name.lower()} ({int(time.monotonic() - started)}s)...",
                            flush=True,
                        )
                if result:
                    raise StartupError(f"{name} failed (exit {result}). {hint} Private log: {log}")
            finally:
                stop_child(process)
        print(f"    OK ({time.monotonic() - started:.1f}s)", flush=True)


def prerequisites() -> None:
    for tool in ("docker", "supabase"):
        if shutil.which(tool) is None:
            raise StartupError(f"{tool} is not installed/on PATH. See docs/LOCAL_DEVELOPMENT.md.")
    try:
        import asyncpg  # noqa: F401
        import dotenv  # noqa: F401
    except ImportError:
        raise StartupError(
            "Install local dependencies: .venv/bin/python -m pip install -c requirements.lock -e '.[server,dev]'"
        ) from None


def launch(runner: Runner, *, build: bool, apply_migrations: bool = False) -> None:
    runner.step(
        "Docker engine",
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        timeout=30,
        hint="Start Docker Desktop and retry ./dev/start.",
    )
    print(
        "Use a trusted/firewalled network: local Supabase development ports are not loopback-only.",
        flush=True,
    )
    runner.step(
        "Supabase database, login and email",
        ["supabase", "start", "--workdir", str(ROOT / "dev")],
        timeout=600,
        hint="Do not interrupt database recovery; retry ./dev/start after checking the log.",
    )
    if not (ROOT / ".env.local").is_file():
        runner.step(
            "Create isolated local configuration",
            [sys.executable, str(ROOT / "scripts/setup_local.py")],
            timeout=40,
        )
    local_config()
    action = "--apply" if apply_migrations else "--check"
    runner.step(
        "Local database schema",
        [
            sys.executable,
            str(ROOT / "scripts/migrate.py"),
            "--env-file",
            str(ROOT / ".env.local"),
            action,
        ],
        timeout=180,
        hint="If migrations are pending, review them then run ./dev/start --migrate. No reset or seed is run.",
    )
    runner.step(
        "Build and start API + Redis" if build else "Start API + Redis",
        compose(
            "up", "-d", "--build" if build else "--no-build", "--wait", "--wait-timeout", "120"
        ),
        timeout=900,
        hint="Check docker compose logs --tail=60 app. No ready banner is shown on failure.",
    )
    runner.step(
        "Verify local pages, database readiness and authentication boundary",
        [sys.executable, str(ROOT / "scripts/check_local.py"), "probe"],
        timeout=40,
    )
    show_menu()


def show_menu() -> None:
    print(
        """
OpenVegas LOCAL is ready

Browser
  Website:          http://127.0.0.1:8000/ui
  Sign up:          http://127.0.0.1:8000/ui/login?mode=signup
  Login:            http://127.0.0.1:8000/ui/login
  Balance:          http://127.0.0.1:8000/ui/balance
  Email inbox:      http://127.0.0.1:54324  (local confirmation emails)

Terminal (in this repository; a second window is fine)
  ./dev/openvegas signup              Create a LOCAL account
  ./dev/openvegas login               Log in to that local account
  ./dev/openvegas ui                  Guided terminal games
  ./dev/openvegas chat                Coding/chat UI; AI needs provider configuration
  ./dev/openvegas balance             View local credits
  ./dev/openvegas whoami              Check the signed-in local account

Developer / operations
  API explorer:     http://127.0.0.1:8000/docs
  Health:           http://127.0.0.1:8000/health/ready
  ./dev/openvegas ops diagnostics     Login required; runtime diagnostics
  docker compose ps                  Container status
  docker compose logs -f app          API logs (Ctrl+C stops log viewing only)
  ./dev/start --status                Recheck readiness and show this list
  ./dev/start --stop                  Stop this local stack; keep saved data

Use ./dev/openvegas, not bare openvegas, for LOCAL testing.
Production .env, accounts and your normal CLI login are unchanged.
Local accounts are separate; confirmation messages arrive in the local inbox.
No provider keys or live payments are enabled by this launcher.
Supabase Studio is disabled; the API explorer is not an admin login bypass.
""",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--status", action="store_true", help="Read-only readiness check and access menu"
    )
    actions.add_argument(
        "--stop", action="store_true", help="Stop only this stack, retaining all local data"
    )
    parser.add_argument(
        "--no-build", action="store_true", help="Reuse the previously built API image"
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Explicitly apply pending migrations to the isolated local database",
    )
    args = parser.parse_args(argv)
    if (args.status or args.stop) and (args.no_build or args.migrate):
        parser.error("--no-build and --migrate apply only when starting")
    try:
        prerequisites()
        runner = Runner()
        if args.stop:
            runner.step("Stop API + Redis", compose("stop"), timeout=90)
            runner.step(
                "Stop Supabase and retain its data",
                ["supabase", "stop", "--workdir", str(ROOT / "dev")],
                timeout=120,
            )
            print("Local stack stopped. Data retained; restart with ./dev/start.")
        elif args.status:
            local_config()
            runner.step(
                "Local readiness",
                [sys.executable, str(ROOT / "scripts/check_local.py"), "probe"],
                timeout=40,
            )
            show_menu()
        else:
            launch(runner, build=not args.no_build, apply_migrations=args.migrate)
    except KeyboardInterrupt:
        print(
            "\nStartup/check interrupted. No later steps were run. Retry ./dev/start, or use ./dev/start --stop to stop retained services."
        )
        return 130
    except StartupError as exc:
        print(f"\nLocal stack is NOT ready: {exc}")
        return 1
    except OSError as exc:
        print(
            f"\nLocal launcher failed ({type(exc).__name__}); check local tool/file permissions. No ready banner was issued."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
