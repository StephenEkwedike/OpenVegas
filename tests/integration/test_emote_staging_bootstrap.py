"""Bootstrap a disposable loopback DB through the real migration runner.

The only test substitution maps the EXACT validated staging DSN to the coordinator's
loopback DSN. The shipped CLI has no localhost or guard-bypass switch.
"""

from urllib.parse import unquote, urlsplit

import pytest
import pytest_asyncio

from tests.test_deployment.test_emote_staging_bootstrap import environment, load_bootstrap, options

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def isolated_bootstrap(integration_environment, monkeypatch):
    import os

    import asyncpg

    dsn = integration_environment  # Already guarded loopback / ov_test_* by conftest.
    module = load_bootstrap()
    env = environment(module)
    db_name = unquote(urlsplit(dsn).path[1:])
    monkeypatch.setattr(module, "DB_NAME", db_name)  # Test-only; no CLI target override exists.
    env["DATABASE_URL"] = (
        "postgresql://postgres:synthetic-only@postgres.railway.internal:5432/" + db_name
    )
    monkeypatch.setattr(os, "environ", env)
    real_connect = asyncpg.connect
    admin = await real_connect(dsn, timeout=5, command_timeout=15, ssl=False)
    locked = await admin.fetchval("SELECT pg_try_advisory_lock(7180260912)")
    if not locked:
        await admin.close()
        pytest.fail("Coordinator disposable DB is in use; do not run concurrently")
    owns = False

    async def loopback_only(url, **kwargs):
        assert url == env["DATABASE_URL"], "Unexpected database target in bootstrap"
        return await real_connect(dsn, timeout=5, command_timeout=60, ssl=False)

    try:
        await module.empty_database(admin)
        owns = True
        monkeypatch.setattr(asyncpg, "connect", loopback_only)
        yield module, admin
    finally:
        if owns:
            # Test-owned schemas only; the production bootstrap never erases anything.
            await admin.execute(
                "DROP SCHEMA IF EXISTS openvegas_staging_bootstrap CASCADE; DROP SCHEMA IF EXISTS auth CASCADE; DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION pg_database_owner;"
            )
        await admin.execute("SELECT pg_advisory_unlock(7180260912)")
        await admin.close()


async def test_fresh_apply_fixture_rerun_readonly_check_and_no_replenishment(isolated_bootstrap):
    module, db = isolated_bootstrap
    assert await module.run(options(module, check=True)) == 1
    assert await db.fetchval("SELECT to_regnamespace('auth')") is None
    assert await module.run(options(module, apply=True, with_fixture_user=True)) == 0
    assert await db.fetchval("SELECT count(*) FROM schema_migrations") == len(
        module.migration_hashes(module.migration_runner())
    )
    assert await db.fetchval("SELECT count(*) FROM auth.users") == 1
    assert (
        await db.fetchval(
            "SELECT balance FROM wallet_accounts WHERE account_id=$1", f"user:{module.FIXTURE_USER}"
        )
        == 100
    )
    assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0
    assert await module.run(options(module, apply=True, with_fixture_user=True)) == 0
    assert await module.run(options(module, check=True)) == 0
    assert (
        await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='starter_grant'")
        == 1
    )
    # Spending is data, not schema drift. Rerunning MUST NOT replenish it.
    async with db.transaction():
        await db.execute(
            """INSERT INTO ledger_entries(debit_account,credit_account,amount,entry_type,reference_id)
          VALUES($1,'store',20,'redeem','staging-smoke-spend')""",
            f"user:{module.FIXTURE_USER}",
        )
        await db.execute(
            "UPDATE wallet_accounts SET balance=balance-20 WHERE account_id=$1",
            f"user:{module.FIXTURE_USER}",
        )
        await db.execute("UPDATE wallet_accounts SET balance=balance+20 WHERE account_id='store'")
    assert await module.run(options(module, apply=True, with_fixture_user=True)) == 0
    assert (
        await db.fetchval(
            "SELECT balance FROM wallet_accounts WHERE account_id=$1", f"user:{module.FIXTURE_USER}"
        )
        == 80
    )
    assert await db.fetchval("SELECT sum(balance) FROM wallet_accounts") == 0


async def test_nonempty_unowned_database_is_refused_and_preserved(isolated_bootstrap):
    module, db = isolated_bootstrap
    await db.execute(
        "CREATE TABLE untouched(value TEXT); INSERT INTO untouched VALUES('preserve-me')"
    )
    with pytest.raises(module.BootstrapError, match="not empty"):
        await module.run(options(module, apply=True))
    assert await db.fetchval("SELECT value FROM untouched") == "preserve-me"
    assert await db.fetchval("SELECT to_regnamespace('auth')") is None


@pytest.mark.parametrize(
    "field",
    ["project_id", "environment_id", "service_id", "database_name", "database_owner", "scope"],
)
async def test_wrong_stamp_refused_before_any_migration_or_fixture(isolated_bootstrap, field):
    module, db = isolated_bootstrap
    assert await module.run(options(module, apply=True)) == 0
    value = "00000000-0000-0000-0000-000000000001" if field.endswith("_id") else "other"
    await db.execute(f"UPDATE openvegas_staging_bootstrap.ownership SET {field}=$1", value)
    with pytest.raises(module.BootstrapError, match="stamp mismatch"):
        await module.run(options(module, apply=True, with_fixture_user=True))
    assert await db.fetchval("SELECT count(*) FROM auth.users") == 0


async def test_schema_drift_refused_without_reset(isolated_bootstrap):
    module, db = isolated_bootstrap
    await module.run(options(module, apply=True))
    await db.execute("ALTER TABLE profiles ADD COLUMN do_not_erase TEXT")
    with pytest.raises(module.BootstrapError, match="schema drift"):
        await module.run(options(module, apply=True))
    assert (
        await db.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='profiles' AND column_name='do_not_erase'"
        )
        == 1
    )


async def test_added_schema_is_drift_and_not_erased(isolated_bootstrap):
    module, db = isolated_bootstrap
    await module.run(options(module, apply=True))
    try:
        await db.execute("CREATE SCHEMA unexpected_staging_test_namespace")
        with pytest.raises(module.BootstrapError, match="schema drift"):
            await module.run(options(module, apply=True))
        assert await db.fetchval("SELECT to_regnamespace('unexpected_staging_test_namespace')")
    finally:
        await db.execute("DROP SCHEMA IF EXISTS unexpected_staging_test_namespace")


async def test_bootstrap_lock_prevents_concurrent_mutation(isolated_bootstrap):
    module, db = isolated_bootstrap
    await db.execute("SELECT pg_advisory_lock($1)", module.LOCK_ID)
    try:
        with pytest.raises(module.BootstrapError, match="holds the lock"):
            await module.run(options(module, apply=True))
        assert await db.fetchval("SELECT to_regnamespace('auth')") is None
    finally:
        await db.execute("SELECT pg_advisory_unlock($1)", module.LOCK_ID)


async def test_migration_hash_conflict_fails_closed(isolated_bootstrap):
    module, db = isolated_bootstrap
    await module.run(options(module, apply=True))
    await db.execute("UPDATE openvegas_staging_bootstrap.migration_plan SET sha256=repeat('0',64)")
    with pytest.raises(module.BootstrapError, match="SQL changed"):
        await module.run(options(module, apply=True))


async def test_interrupted_checkpoint_requires_review_instead_of_adoption(isolated_bootstrap):
    module, db = isolated_bootstrap
    await module.run(options(module, apply=True))
    await db.execute("UPDATE openvegas_staging_bootstrap.ownership SET schema_fingerprint=NULL")
    with pytest.raises(module.BootstrapError, match="Interrupted"):
        await module.run(options(module, apply=True))


async def test_broken_fixture_not_recreated_or_recredited(isolated_bootstrap):
    module, db = isolated_bootstrap
    await module.run(options(module, apply=True, with_fixture_user=True))
    await db.execute("DELETE FROM user_starter_grants WHERE user_id=$1", module.FIXTURE_USER)
    with pytest.raises(module.BootstrapError, match="fixture evidence"):
        await module.run(options(module, apply=True, with_fixture_user=True))
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 1


async def test_fixture_failure_rolls_back_all_fixture_records(isolated_bootstrap, monkeypatch):
    module, db = isolated_bootstrap
    await module.run(options(module, apply=True))
    real_fixture = module.fixture_user

    async def broken(conn, *, create):
        await real_fixture(conn, create=create)
        raise module.BootstrapError("Injected fixture completion failure")

    monkeypatch.setattr(module, "fixture_user", broken)
    with pytest.raises(module.BootstrapError, match="Injected"):
        await module.run(options(module, apply=True, with_fixture_user=True))
    assert await db.fetchval("SELECT count(*) FROM auth.users") == 0
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 0
    assert (
        await db.fetchval("SELECT fixture_created FROM openvegas_staging_bootstrap.ownership")
        is False
    )
