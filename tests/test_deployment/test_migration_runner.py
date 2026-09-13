"""Migration guard/unit orchestration tests. No database is contacted."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_runner_contract", ROOT / "scripts/migrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "supabase/migrations").mkdir(parents=True)
    return module


def options(**kwargs):
    return argparse.Namespace(**{"env_file": None, "apply": False, "allow_remote": False, "confirm_host": None, "through": None, **kwargs})


def migration(runner, name, sql="SELECT 1;"):
    path = runner.ROOT / "supabase/migrations" / name
    path.write_text(sql)
    return path


def test_migration_discovery_excludes_seed_and_supports_through(runner):
    first = migration(runner, "001_first.sql")
    second = migration(runner, "002_second.sql")
    (runner.ROOT / "supabase/seed.sql").write_text("must-not-run")
    assert runner.migration_files() == [first, second]
    assert runner.migration_files(1) == [first]
    with pytest.raises(ValueError, match="existing migration"):
        runner.migration_files(3)


def test_duplicate_migration_numbers_are_rejected(runner):
    migration(runner, "001_first.sql")
    migration(runner, "001_duplicate.sql")
    with pytest.raises(ValueError, match="Duplicate"):
        runner.migration_files()


def test_empty_migration_directory_is_rejected(runner):
    with pytest.raises(ValueError, match="No migration"):
        runner.migration_files()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_apply_is_allowed_without_remote_flags(runner, host):
    os.environ["DATABASE_URL"] = f"postgresql://{host}:5432/local"
    args = options()
    args.apply = True
    assert runner.database_url(args) == os.environ["DATABASE_URL"]


@pytest.mark.parametrize("allow,confirm,accepted", [(False, None, False), (True, None, False), (False, "example.invalid", False), (True, "different.invalid", False), (True, "example.invalid", True)])
def test_remote_apply_requires_both_explicit_guards(runner, allow, confirm, accepted):
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/local"
    args = options()
    args.apply, args.allow_remote, args.confirm_host = True, allow, confirm
    if accepted:
        assert runner.database_url(args) == os.environ["DATABASE_URL"]
    else:
        with pytest.raises(ValueError, match="Remote apply requires"):
            runner.database_url(args)


def test_remote_read_only_check_does_not_require_apply_confirmation(runner):
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/local"
    assert runner.database_url(options()) == os.environ["DATABASE_URL"]


def test_transaction_pooler_apply_is_rejected(runner):
    os.environ["DATABASE_URL"] = "postgresql://127.0.0.1:6543/local"
    args = options()
    args.apply = True
    with pytest.raises(ValueError, match="session-mode"):
        runner.database_url(args)


@pytest.mark.parametrize("query", ["host=example.invalid", "port=6543", "user=other", "host=", "port="])
def test_connection_query_overrides_are_rejected_including_blank_values(runner, query):
    os.environ["DATABASE_URL"] = "postgresql://127.0.0.1/local?" + query
    with pytest.raises(ValueError, match="Unsupported connection query"):
        runner.database_url(options())


def test_allowed_connection_query_options_are_preserved(runner):
    os.environ["DATABASE_URL"] = "postgresql://127.0.0.1/local?sslmode=require&application_name=unit-check"
    assert runner.database_url(options()) == os.environ["DATABASE_URL"]


def test_root_dotenv_is_never_implicitly_loaded(runner):
    (runner.ROOT / ".env").write_text("DATABASE_URL=postgresql://example.invalid/private\n")
    with pytest.raises(ValueError, match="Set DATABASE_URL"):
        runner.database_url(options())


def test_explicit_environment_file_is_selected_without_root_or_shell_fallback(runner):
    (runner.ROOT / ".env").write_text("DATABASE_URL=postgresql://example.invalid/root\n")
    selected = runner.ROOT / "isolated.env"
    selected.write_text("DATABASE_URL=postgresql://127.0.0.1/local\n")
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/shell"
    args = options()
    args.env_file = str(selected)
    assert runner.database_url(args) == "postgresql://127.0.0.1/local"
    selected.write_text("OTHER=synthetic\n")
    with pytest.raises(ValueError, match="Set DATABASE_URL"):
        runner.database_url(args)


class FakeConnection:
    def __init__(self, *, applied=(), journal=True, public_tables=0, auth=True, roles=True, lock=True, fail_sql=None):
        self.applied = applied
        self.journal = journal
        self.public_tables = public_tables
        self.auth = auth
        self.roles = roles
        self.lock = lock
        self.fail_sql = fail_sql
        self.executed = []
        self.transactions = []
        self.exits = []
        self.lock_attempts = 0
        self.closed = []

    async def fetchval(self, query, *args):
        if "pg_try_advisory_lock" in query:
            self.lock_attempts += 1
            return self.lock
        if "auth.users" in query:
            return self.auth
        if "pg_roles" in query:
            return self.roles
        if "to_regclass" in query:
            return self.journal
        if "information_schema.tables" in query:
            return self.public_tables
        raise AssertionError("Unexpected synthetic query")

    async def fetch(self, query):
        assert query == "SELECT version FROM public.schema_migrations"
        return [{"version": version} for version in self.applied]

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if query == self.fail_sql:
            raise RuntimeError("synthetic migration failure")

    def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        connection = self

        class Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, error_type, error, traceback):
                connection.exits.append(error_type is None)
                return False

        return Transaction()

    async def close(self, **kwargs):
        self.closed.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("state,error", [({"auth": False}, "Supabase Auth"), ({"roles": False}, "Supabase Auth"), ({"journal": False, "public_tables": 1}, "without migration journal"), ({"applied": ("999_unknown",)}, "unknown migration")])
async def test_inspection_rejects_unowned_or_incompatible_database(runner, state, error):
    file = migration(runner, "001_first.sql")
    connection = FakeConnection(**state)
    with pytest.raises(ValueError, match=error):
        await runner.inspect(connection, [file])
    assert connection.executed == []


@pytest.mark.asyncio
async def test_inspection_reports_only_pending_known_files(runner):
    first = migration(runner, "001_first.sql")
    second = migration(runner, "002_second.sql")
    pending, applied = await runner.inspect(FakeConnection(applied=(first.stem,)), [first, second])
    assert pending == [second]
    assert applied == {first.stem}


def connect_fake(monkeypatch, connection):
    driver = SimpleNamespace(connect=AsyncMock(return_value=connection))
    monkeypatch.setitem(sys.modules, "asyncpg", driver)
    os.environ["DATABASE_URL"] = "postgresql://127.0.0.1/local"
    return driver


@pytest.mark.asyncio
@pytest.mark.parametrize("already_applied", [False, True])
async def test_default_check_uses_readonly_transaction_and_no_writes(runner, monkeypatch, already_applied):
    file = migration(runner, "001_first.sql")
    connection = FakeConnection(applied=(file.stem,) if already_applied else ())
    driver = connect_fake(monkeypatch, connection)
    assert await runner.run(options()) == (0 if already_applied else 1)
    driver.connect.assert_awaited_once()
    assert connection.transactions == [{"readonly": True}]
    assert connection.executed == []
    assert connection.lock_attempts == 0
    assert connection.closed == [{"timeout": 5}]


@pytest.mark.asyncio
async def test_apply_guard_rejects_before_connection(runner, monkeypatch):
    driver = connect_fake(monkeypatch, FakeConnection())
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/local"
    args = options()
    args.apply = True
    with pytest.raises(ValueError, match="Remote apply"):
        await runner.run(args)
    driver.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_locks_and_uses_individual_transactions_without_seed(runner, monkeypatch):
    first = migration(runner, "001_first.sql", "SELECT 'first';")
    second = migration(runner, "002_second.sql", "SELECT 'second';")
    (runner.ROOT / "supabase/seed.sql").write_text("DO NOT EXECUTE SEED")
    connection = FakeConnection(journal=False)
    connect_fake(monkeypatch, connection)
    args = options()
    args.apply = True
    assert await runner.run(args) == 0
    assert connection.transactions == [{"readonly": True}, {}, {}]
    assert connection.exits == [True, True, True]
    assert connection.lock_attempts == 1
    sql = [query for query, _ in connection.executed]
    assert first.read_text() in sql and second.read_text() in sql
    assert "DO NOT EXECUTE SEED" not in sql
    assert "pg_advisory_unlock" in sql[-1]
    assert connection.closed == [{"timeout": 5}]


@pytest.mark.asyncio
async def test_apply_failure_rolls_back_active_transaction_and_closes_connection(runner, monkeypatch):
    migration(runner, "001_first.sql", "SELECT 'first';")
    migration(runner, "002_fail.sql", "SYNTHETIC FAILURE")
    third = migration(runner, "003_third.sql", "SELECT 'third';")
    connection = FakeConnection(fail_sql="SYNTHETIC FAILURE")
    connect_fake(monkeypatch, connection)
    args = options()
    args.apply = True
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        await runner.run(args)
    assert connection.exits == [True, True, False]
    assert third.read_text() not in [query for query, _ in connection.executed]
    assert "pg_advisory_unlock" in connection.executed[-1][0]
    assert connection.closed == [{"timeout": 5}]


@pytest.mark.asyncio
async def test_lock_contention_never_executes_migrations(runner, monkeypatch):
    migration(runner, "001_first.sql")
    connection = FakeConnection(lock=False)
    connect_fake(monkeypatch, connection)
    sleep = AsyncMock()
    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    args = options()
    args.apply = True
    with pytest.raises(ValueError, match="holds the lock"):
        await runner.run(args)
    assert connection.lock_attempts == 20
    assert sleep.await_count == 20
    assert connection.executed == []
    assert connection.closed == [{"timeout": 5}]


def test_readiness_requires_private_runtime_rls_migration():
    tree = ast.parse((ROOT / "server/services/dependencies.py").read_text())
    schema = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "assert_schema_compatible")
    required = {node.args[1].value for node in ast.walk(schema) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "require_migration_min" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)}
    assert "038_private_runtime_rls" in required
    assert (ROOT / "supabase/migrations/038_private_runtime_rls.sql").is_file()


def test_legacy_terminal_payload_constraint_does_not_validate_old_rows():
    sql = (ROOT / "supabase/migrations/026_agent_tool_cancelled_status_v30.sql").read_text()
    payload_constraint = sql.split("ADD CONSTRAINT ck_tool_terminal_response_payload", 1)[1]
    assert "NOT VALID" in payload_constraint.split("END $$", 1)[0]


def test_result_payload_backfill_excludes_incomplete_legacy_terminal_rows():
    sql = (ROOT / "supabase/migrations/027_agent_tool_result_payload_column_v30_fix.sql").read_text()
    update = sql.split("UPDATE agent_run_tool_calls", 1)[1].split(";", 1)[0]
    for predicate in (
        "WHERE result_payload IS NULL",
        "AND terminal_response_status IS NOT NULL",
        "AND terminal_response_content_type = 'application/json'",
        "AND terminal_response_body_text IS NOT NULL",
    ):
        assert predicate in update
