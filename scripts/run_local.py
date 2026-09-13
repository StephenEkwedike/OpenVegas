#!/usr/bin/env python3
"""Run the host API or CLI with only the explicit, loopback development config."""

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def local_environment(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("Create .env.local with scripts/setup_local.py first")
    values = {key: value for key, value in dotenv_values(path, interpolate=False).items()
              if value is not None}
    schemes = {"DATABASE_URL": {"postgres", "postgresql"}, "SUPABASE_URL": {"http", "https"},
               "OPENVEGAS_BACKEND_URL": {"http", "https"}, "REDIS_URL": {"redis", "rediss"}}
    for key, allowed_schemes in schemes.items():
        try:
            parsed = urlsplit(values.get(key, ""))
            port = parsed.port
            authority = unquote(parsed.netloc.rsplit("@", 1)[-1])
        except ValueError:
            raise ValueError(f"{key} has an invalid local service address") from None
        if (parsed.scheme not in allowed_schemes or "," in authority or parsed.fragment
                or (port is not None and port < 1)
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError(f"{key} must select the isolated loopback service")
        allowed_query = {"sslmode", "application_name"} if key == "DATABASE_URL" else set()
        if set(parse_qs(parsed.query, keep_blank_values=True)) - allowed_query:
            raise ValueError(f"{key} has unsupported connection query options")
    if values.get("STRIPE_SECRET_KEY") and not values["STRIPE_SECRET_KEY"].startswith("sk_test_"):
        raise ValueError("Local launcher permits Stripe test keys only")
    home = ROOT / ".local" / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {key: os.environ[key] for key in ("PATH", "TERM", "COLORTERM", "LANG", "LC_ALL",
           "SYSTEMROOT", "WINDIR", "TMPDIR") if key in os.environ}
    env.update(values)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "OPENVEGAS_ENV_FILE": str(path.resolve()), "OPENVEGAS_ROOT": str(ROOT),
        "OPENVEGAS_DOTENV_OVERRIDE": "0", "OPENVEGAS_RUNTIME_ENV": "development",
        "OPENVEGAS_TEST_MODE": "0", "OPENVEGAS_DB_FAIL_OPEN": "0",
        "OPENVEGAS_ENABLE_TOUCHID": "0", "OPENVEGAS_FORCE_CONFIG_REFRESH_STORAGE": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    })
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("server", "cli"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        env = local_environment(ROOT / ".env.local")
    except ValueError as exc:
        parser.error(str(exc))
    if args.mode == "server":
        command = [sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
                   "--port", "8000", *args.arguments]
    else:
        command = [sys.executable, "-m", "openvegas.cli", *args.arguments]
    os.chdir(ROOT)
    os.execve(sys.executable, command, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
