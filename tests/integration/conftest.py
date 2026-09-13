"""Opt-in, disposable PostgreSQL integration fixtures. No Supabase Auth claims."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import pytest_asyncio


def _integration_dsn() -> str:
    dsn = os.environ.get("OPENVEGAS_INTEGRATION_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("Set OPENVEGAS_INTEGRATION_DATABASE_URL for disposable local DB tests")
    try:
        parts = urlsplit(dsn)
        valid = (
            parts.scheme in {"postgres", "postgresql"}
            and parts.hostname in {"127.0.0.1", "localhost", "::1"}
            and re.fullmatch(r"ov_test_[A-Za-z0-9_]+", unquote(parts.path.lstrip("/")))
            and not parts.query
            and not parts.fragment
        )
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        pytest.fail(
            "Integration DSN rejected: require loopback, database ov_test_*, "
            "and no query/fragment overrides; DSN is intentionally redacted",
            pytrace=False,
        )
    return dsn


@pytest.fixture
def integration_environment(monkeypatch):
    dsn = _integration_dsn()
    for name in tuple(os.environ):
        if name.startswith(("STRIPE_", "SUPABASE_", "OPENVEGAS_WIN_ALWAYS", "OPENVEGAS_DEMO_")):
            monkeypatch.delenv(name, raising=False)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "DATABASE_URL": dsn,
        "REDIS_URL": "",
        "OPENVEGAS_DOTENV_OVERRIDE": "0",
        "OPENVEGAS_RUNTIME_ENV": "test",
        "OPENVEGAS_TEST_MODE": "0",
        "OPENVEGAS_DB_FAIL_OPEN": "0",
        "OPENVEGAS_WIN_ALWAYS": "0",
        "OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED": "0",
        "OPENVEGAS_DEMO_ADMIN_AUTOFUND_ENABLED": "0",
        "OPENVEGAS_BILLING_PROVIDER": "stripe",
        "OPENVEGAS_BILLING_FAKE_WEBHOOK_ENABLED": "0",
        "STRIPE_SECRET_KEY": "sk_test_local_signature_only_no_network",
        "STRIPE_WEBHOOK_SECRET": "whsec_local_integration_signature_only",
        "V_PER_USD": "100",
        "TOPUP_STRIPE_LATE_SETTLEMENT_WINDOW_SEC": "259200",
        "WRAPPER_REWARDS_ENABLED": "0",
    }.items():
        monkeypatch.setenv(name, value)
    return dsn


@pytest.fixture
def migration_runner(integration_environment):
    default = Path(__file__).resolve().parents[2] / "scripts" / "migrate.py"
    path = os.environ.get("OPENVEGAS_INTEGRATION_MIGRATION_RUNNER", str(default)).strip()
    file = Path(path).expanduser().resolve()
    if not file.is_file():
        pytest.fail("Configured integration migration runner is not a file", pytrace=False)

    async def apply(dsn, *, through=38):
        # Exercise the coordinator's actual CLI, guards, lock, and journal handling.
        env = dict(os.environ, DATABASE_URL=dsn, PYTHONDONTWRITEBYTECODE="1")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(file), "--apply", "--through", str(through),
            env=env, cwd=file.parent.parent,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            pytest.fail("Local migration runner timed out", pytrace=False)
        assert proc.returncode == 0, stdout.decode(errors="replace")

    return apply


class DatabaseSandbox:
    def __init__(self, dsn, pool, adapter, max_size, runner):
        self._dsn = dsn
        self.pool = pool
        self.db = adapter(pool)
        self._adapter = adapter
        self._max_size = max_size
        self._runner = runner

    async def migrate(self, *, through=38):
        await self._runner(self._dsn, through=through)

    async def reconnect(self):
        """Discard every connection and runtime service's connection pool."""
        import asyncpg

        await self.pool.close()
        self.pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=self._max_size,
            timeout=5, command_timeout=5, ssl=False,
        )
        self.db = self._adapter(self.pool)
        return self.db


@pytest_asyncio.fixture
async def database_factory(integration_environment, migration_runner):
    import asyncpg
    from server.services.dependencies import PostgresDB

    # Never create another database or connect to a different database name.
    dsn = integration_environment
    parts = urlsplit(dsn)
    try:
        admin = await asyncpg.connect(dsn, timeout=5, command_timeout=10, ssl=False)
    except Exception:
        pytest.fail("Cannot connect to isolated PostgreSQL; DSN redacted", pytrace=False)

    locked = False
    try:
        actual_name = await admin.fetchval("SELECT current_database()")
        if actual_name != unquote(parts.path.lstrip("/")):
            pytest.fail("Integration server returned an unexpected database name", pytrace=False)
        locked = await admin.fetchval("SELECT pg_try_advisory_lock(7180260912)")
        if not locked:
            pytest.fail("Another integration suite owns the disposable database", pytrace=False)
    except BaseException:
        await admin.close()
        raise

    @asynccontextmanager
    async def create(*, through=38, max_size=20):
        existing = await admin.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')
            ) OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='auth')
        """)
        if existing:
            pytest.fail("Disposable DB must be empty; refusing to erase pre-existing schemas/data", pytrace=False)
        sandbox = None
        owns_schemas = False
        try:
            roles_ready = await admin.fetchval(
                "SELECT count(*)=2 FROM pg_roles WHERE rolname IN ('anon','authenticated')"
            )
            if not roles_ready:
                pytest.fail("Coordinator test cluster must provide anon/authenticated roles", pytrace=False)
            # Minimal relational scaffold, not a GoTrue/Supabase Auth implementation.
            await admin.execute("""
                CREATE SCHEMA auth;
                CREATE TABLE auth.users (id UUID PRIMARY KEY, email TEXT);
                CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL STABLE AS $$
                    SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid
                $$;
            """)
            owns_schemas = True
            await migration_runner(dsn, through=through)
            pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=max_size,
                timeout=5, command_timeout=5, ssl=False,
            )
            sandbox = DatabaseSandbox(dsn, pool, PostgresDB, max_size, migration_runner)
            yield sandbox
        finally:
            if sandbox is not None:
                await sandbox.pool.close()
            if owns_schemas:
                async with admin.transaction():
                    await admin.execute("DROP SCHEMA public CASCADE; DROP SCHEMA auth CASCADE")
                    await admin.execute("CREATE SCHEMA public AUTHORIZATION pg_database_owner")

    try:
        yield create
    finally:
        if locked:
            await admin.execute("SELECT pg_advisory_unlock(7180260912)")
        await admin.close()
