"""Early pytest safety boundary, not a sandbox against deliberately bypassing tests."""

import atexit
import ipaddress
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


def safe_database_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        return bool(
            parsed.scheme in {"postgres", "postgresql"}
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and re.fullmatch(r"ov_test_[A-Za-z0-9_]+", unquote(parsed.path.lstrip("/")))
            and not parsed.query and not parsed.fragment
            and (port is None or 0 < port <= 65535)
        )
    except (ValueError, TypeError):
        return False


def install():
    """Run before application imports/collection; errors never include env values."""
    for name, value in os.environ.items():
        if name.startswith("STRIPE_") and value.strip().startswith(("sk_live_", "rk_live_")):
            raise ValueError("pytest safety: inherited live Stripe credential rejected")
    for name in ("DATABASE_URL", "OPENVEGAS_INTEGRATION_DATABASE_URL"):
        value = os.environ.get(name, "").strip()
        if value and not safe_database_url(value):
            raise ValueError("pytest safety: require loopback ov_test_* database without overrides")
    if any(os.environ.get(name) for name in ("PGHOST", "PGHOSTADDR", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE")):
        raise ValueError("pytest safety: inherited libpq connection overrides rejected")
    for name in list(os.environ):
        if name.startswith(("STRIPE_", "SUPABASE_")) or name in {"PGPASSWORD", "PGPASSFILE"}:
            os.environ.pop(name, None)
    # Override, rather than merely disabling dotenv override: otherwise absent
    # variables can still be populated from a developer's real checkout .env.
    fd, path = tempfile.mkstemp(prefix="ov-pytest-empty-", suffix=".env")
    os.close(fd)
    atexit.register(lambda: Path(path).unlink(missing_ok=True))
    os.environ.update(OPENVEGAS_ENV_FILE=path, OPENVEGAS_DOTENV_OVERRIDE="0",
                      OPENVEGAS_TEST_MODE="1", OPENVEGAS_RUNTIME_ENV="test",
                      PYTHON_DOTENV_DISABLED="1", REDIS_URL="")

    def local(address):
        try:
            return ipaddress.ip_address(address).is_loopback
        except (ValueError, TypeError):
            return False

    original_resolve = socket.getaddrinfo
    def resolve(host, *args, **kwargs):
        # Never resolve external names; pin localhost rather than trusting DNS.
        if host in ("localhost", b"localhost"):
            host = "127.0.0.1"
        if host is not None and not local(host.decode() if isinstance(host, bytes) else host):
            raise RuntimeError("pytest safety: external network access blocked")
        return original_resolve(host, *args, **kwargs)
    socket.getaddrinfo = resolve
    for method in ("connect", "connect_ex", "sendto", "sendmsg"):
        original = getattr(socket.socket, method, None)
        if original is None:
            continue
        def guarded(self, *args, _method=method, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                address = args[0] if _method in ("connect", "connect_ex") else (
                    args[-1] if _method == "sendto" else kwargs.get("address", args[3] if len(args) > 3 else None)
                )
                if address is not None and (not isinstance(address, tuple) or not local(address[0])):
                    raise RuntimeError("pytest safety: external network access blocked")
            return _original(self, *args, **kwargs)
        setattr(socket.socket, method, guarded)
