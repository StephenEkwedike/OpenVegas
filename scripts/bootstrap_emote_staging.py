#!/usr/bin/env python3
"""Bootstrap ONLY the approved isolated Railway emote technical-smoke database.

Default: offline configuration/plan validation, no connection or writes.
--check: read-only database inspection. --apply requires both explicit confirmations.
Run inside the approved API service's staging environment, never via a prod shell:
  python scripts/bootstrap_emote_staging.py --apply \
    --confirm-environment 7bfd8b2d-dd8a-4052-8004-60abaabd7b00 \
    --confirm-database-host postgres.railway.internal --with-fixture-user

Confirm the actual private DB hostname in Railway first. There is no alternate-host
flag, dotenv loading, reset, deployment, token minting, or external service call.
The tiny auth scaffold is NOT GoTrue, real signup/login, or Auth certification.
Redis's URL is validated but Redis/API/Docker readiness is not certified here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

PROJECT = "af8f0363-bd20-482b-8cec-8e8dfa0c1f0f"
ENVIRONMENT = "7bfd8b2d-dd8a-4052-8004-60abaabd7b00"
SERVICE = "83eb2c27-11bc-4607-9802-5de6fd34599c"
DB_HOST = "postgres.railway.internal"
DB_NAME = "railway"
REDIS_HOST = "redis-j9m5.railway.internal"
SCOPE = "openvegas-emote-technical-staging-v1"
LOCK_ID = 7180260913
CONTROL = "openvegas_staging_bootstrap"
FIXTURE_USER = UUID("32933488-734e-4b34-99a8-a2a0d358c352")
FIXTURE_EMAIL = "emote-staging-smoke@example.invalid"
FIXTURE_USERNAME = "emote-staging-smoke"
FIXTURE_ENTRY = UUID("28d1d4eb-04d6-48bb-b93b-c633df914355")
ROOT = Path(__file__).resolve().parents[1]
AUTH_UID_BODY = "SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid"


class BootstrapError(Exception):
    """Only fixed, non-secret diagnostic messages should reach the operator."""


def validate_environment(env, args):
    required = {
        "RAILWAY_PROJECT_ID": PROJECT,
        "RAILWAY_ENVIRONMENT_ID": ENVIRONMENT,
        "RAILWAY_SERVICE_ID": SERVICE,
        "OPENVEGAS_RUNTIME_ENV": "staging",
        "OPENVEGAS_TEST_MODE": "0",
        "OPENVEGAS_DB_FAIL_OPEN": "0",
    }
    for name, value in required.items():
        if env.get(name) != value:
            raise BootstrapError(f"Required staging identity/setting mismatch: {name}")
    for name, value in env.items():
        if not str(value).strip():
            continue
        upper = name.upper()
        provider = any(
            p in upper
            for p in (
                "OPENAI",
                "ANTHROPIC",
                "OPENROUTER",
                "MISTRAL",
                "GEMINI",
                "GROQ",
                "COHERE",
                "TOGETHER",
                "BEDROCK",
                "DEEPSEEK",
            )
        )
        credential = any(p in upper for p in ("KEY", "SECRET", "TOKEN", "CREDENTIAL", "PASSWORD"))
        if (
            upper.startswith("STRIPE_")
            or upper.endswith(("_API_KEY", "_API_TOKEN"))
            or (provider and credential)
            or upper
            in {
                "GOOGLE_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "HF_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
            }
            or (upper.startswith("SUPABASE_") and upper != "SUPABASE_JWT_SECRET")
            or ("PROVIDER" in upper and credential)
        ):
            raise BootstrapError("Provider/Stripe/Supabase connection credentials are forbidden")
    for name in ("ENV", "ENVIRONMENT", "NODE_ENV"):
        if str(env.get(name, "")).lower() in {"prod", "production"}:
            raise BootstrapError("Conflicting production environment flag")
    for name in (
        "OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED",
        "OPENVEGAS_DEMO_ADMIN_AUTOFUND_ENABLED",
        "OPENVEGAS_BILLING_FAKE_WEBHOOK_ENABLED",
        "OPENVEGAS_WIN_ALWAYS",
    ):
        if env.get(name, "0") != "0":
            raise BootstrapError("Demo/fake financial behavior must remain disabled")
    if args.apply and (
        args.confirm_environment != ENVIRONMENT or args.confirm_database_host != DB_HOST
    ):
        raise BootstrapError(
            "Apply requires exact environment and verified private DB hostname confirmations"
        )
    if args.with_fixture_user and not args.apply:
        raise BootstrapError("Fixture provisioning requires --apply")
    for name, host, port, schemes in (
        ("DATABASE_URL", DB_HOST, 5432, {"postgres", "postgresql"}),
        ("REDIS_URL", REDIS_HOST, 6379, {"redis", "rediss"}),
    ):
        try:
            raw = env.get(name, "")
            parsed = urlsplit(raw)
            valid = (
                parsed.scheme in schemes
                and parsed.hostname == host
                and parsed.port in {None, port}
                and not parsed.query
                and not parsed.fragment
                and parsed.password
                and not any(c.isspace() for c in raw)
            )
            if name == "DATABASE_URL":
                db_name = unquote(parsed.path.removeprefix("/"))
                valid = valid and parsed.username and db_name == DB_NAME
            else:
                valid = valid and parsed.path in {"", "/", "/0"}
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise BootstrapError(
                f"{name} must use its approved private host, port and direct URL; value redacted"
            )
    return env["DATABASE_URL"]


def migration_runner():
    spec = importlib.util.spec_from_file_location(
        "emote_staging_migrations", ROOT / "scripts/migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migration_hashes(runner):
    return {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in runner.migration_files()}


async def validate_roles(conn, *, create=False):
    for role in ("anon", "authenticated"):
        row = await conn.fetchrow(
            """SELECT oid,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
            rolreplication,rolbypassrls FROM pg_roles WHERE rolname=$1""",
            role,
        )
        if row is None and create:
            await conn.execute(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
            continue
        if row is None or any(
            row[k]
            for k in (
                "rolcanlogin",
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise BootstrapError(
                "Auth scaffold role missing or unsafe; no existing role will be modified"
            )
        if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=$1 OR roleid=$1)", row["oid"]
        ):
            raise BootstrapError("Auth scaffold roles have unexpected memberships")


async def empty_database(conn):
    extra = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_namespace
        WHERE nspname NOT IN ('public','information_schema') AND nspname !~ '^pg_')
        OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public')
        OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public')
        OR EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public')""")
    if extra:
        raise BootstrapError("Unstamped database is not empty; refusing adoption or erasure")


async def create_scaffold(conn, identity):
    await empty_database(conn)
    await validate_roles(conn, create=True)
    await conn.execute(f"""
        CREATE SCHEMA {CONTROL};
        REVOKE ALL ON SCHEMA {CONTROL} FROM PUBLIC,anon,authenticated;
        CREATE TABLE {CONTROL}.ownership (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
            scope TEXT NOT NULL,project_id UUID NOT NULL,environment_id UUID NOT NULL,
            service_id UUID NOT NULL,database_name TEXT NOT NULL,database_owner TEXT NOT NULL,
            schema_fingerprint TEXT,fixture_created BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE {CONTROL}.migration_plan (
            version TEXT PRIMARY KEY,sha256 TEXT NOT NULL CHECK(sha256 ~ '^[a-f0-9]{{64}}$')
        );
        REVOKE ALL ON ALL TABLES IN SCHEMA {CONTROL} FROM PUBLIC,anon,authenticated;
        CREATE SCHEMA auth;
        REVOKE ALL ON SCHEMA auth FROM PUBLIC,anon,authenticated;
        CREATE TABLE auth.users(id UUID PRIMARY KEY,email TEXT NOT NULL);
        ALTER TABLE auth.users ENABLE ROW LEVEL SECURITY;
        REVOKE ALL ON auth.users FROM PUBLIC,anon,authenticated;
        CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL STABLE AS $fn${AUTH_UID_BODY}$fn$;
        REVOKE ALL ON FUNCTION auth.uid() FROM PUBLIC,anon,authenticated;
    """)
    await conn.execute(
        f"""INSERT INTO {CONTROL}.ownership
        (scope,project_id,environment_id,service_id,database_name,database_owner)
        VALUES($1,$2,$3,$4,$5,$6)""",
        SCOPE,
        UUID(PROJECT),
        UUID(ENVIRONMENT),
        UUID(SERVICE),
        identity["name"],
        identity["owner"],
    )


async def schema_fingerprint(conn):
    # Data changes (wallet spending, test rows) must not invalidate schema identity.
    rows = await conn.fetch("""
        SELECT 'relation' AS kind,n.nspname||'.'||c.relname AS name,
          jsonb_build_array(c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.relacl::text,
                            pg_get_userbyid(c.relowner))::text AS definition
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('public','auth','openvegas_staging_bootstrap')
        UNION ALL SELECT 'schema',n.nspname,
          jsonb_build_array(n.nspacl::text,pg_get_userbyid(n.nspowner))::text
        FROM pg_namespace n WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
        UNION ALL SELECT 'index',n.nspname||'.'||c.relname,pg_get_indexdef(c.oid)
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('public','auth','openvegas_staging_bootstrap')
        UNION ALL SELECT 'column',n.nspname||'.'||c.relname||'.'||a.attname,
          jsonb_build_array(format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,
                            pg_get_expr(d.adbin,d.adrelid))::text
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE a.attnum>0 AND NOT a.attisdropped AND n.nspname IN ('public','auth','openvegas_staging_bootstrap')
        UNION ALL SELECT 'constraint',n.nspname||'.'||c.conname,pg_get_constraintdef(c.oid)
        FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname IN ('public','auth','openvegas_staging_bootstrap')
        UNION ALL SELECT 'function',n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
          pg_get_functiondef(p.oid)||coalesce(p.proacl::text,'')
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE p.prokind IN ('f','p') AND n.nspname IN ('public','auth','openvegas_staging_bootstrap')
        UNION ALL SELECT 'policy',schemaname||'.'||tablename||'.'||policyname,
          jsonb_build_array(permissive,roles,cmd,qual,with_check)::text
        FROM pg_policies WHERE schemaname IN ('public','auth','openvegas_staging_bootstrap')
        UNION ALL SELECT 'trigger',n.nspname||'.'||c.relname||'.'||t.tgname,pg_get_triggerdef(t.oid)||t.tgenabled::text
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE NOT t.tgisinternal AND n.nspname IN ('public','auth','openvegas_staging_bootstrap')
        ORDER BY 1,2,3
    """)
    return hashlib.sha256(
        json.dumps([list(r) for r in rows], separators=(",", ":")).encode()
    ).hexdigest()


async def validate_stamp(conn, identity, hashes):
    rows = await conn.fetch(f"SELECT * FROM {CONTROL}.ownership")
    if len(rows) != 1:
        raise BootstrapError("Missing or ambiguous staging ownership stamp")
    stamp = rows[0]
    expected = {
        "scope": SCOPE,
        "project_id": UUID(PROJECT),
        "environment_id": UUID(ENVIRONMENT),
        "service_id": UUID(SERVICE),
        "database_name": identity["name"],
        "database_owner": identity["owner"],
    }
    if any(stamp[k] != v for k, v in expected.items()):
        raise BootstrapError("Staging ownership stamp mismatch; refusing writes")
    await validate_roles(conn)
    users = await conn.fetch("""SELECT column_name,data_type,is_nullable FROM information_schema.columns
        WHERE table_schema='auth' AND table_name='users' ORDER BY column_name""")
    if [tuple(r) for r in users] != [("email", "text", "NO"), ("id", "uuid", "NO")]:
        raise BootstrapError("Auth scaffold schema mismatch")
    body = await conn.fetchval("SELECT prosrc FROM pg_proc WHERE oid=to_regprocedure('auth.uid()')")
    if body != AUTH_UID_BODY:
        raise BootstrapError("Auth scaffold function mismatch")
    planned = {
        r["version"]: r["sha256"]
        for r in await conn.fetch(f"SELECT * FROM {CONTROL}.migration_plan")
    }
    if any(hashes.get(version) != digest for version, digest in planned.items()):
        raise BootstrapError(
            "Recorded migration SQL changed or disappeared; manual review required"
        )
    if stamp["schema_fingerprint"] and stamp["schema_fingerprint"] != await schema_fingerprint(
        conn
    ):
        raise BootstrapError("Staging schema drift detected; no automatic repair or reset")
    return stamp, planned


async def fixture_user(conn, *, create):
    stamp = await conn.fetchrow(f"SELECT fixture_created FROM {CONTROL}.ownership")
    account = f"user:{FIXTURE_USER}"
    reference = f"starter_grant:{FIXTURE_USER}:v1"
    if not stamp["fixture_created"]:
        if not create:
            return
        exists = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1)
            OR EXISTS(SELECT 1 FROM profiles WHERE id=$1 OR username=$2)
            OR EXISTS(SELECT 1 FROM wallet_accounts WHERE account_id=$3)""",
            FIXTURE_USER,
            FIXTURE_USERNAME,
            account,
        )
        if exists:
            raise BootstrapError(
                "Synthetic fixture identity already exists without ownership; refusing adoption"
            )
        await conn.execute(
            "INSERT INTO auth.users(id,email) VALUES($1,$2)", FIXTURE_USER, FIXTURE_EMAIL
        )
        await conn.execute(
            "INSERT INTO profiles(id,username,display_name) VALUES($1,$2,'Synthetic staging smoke user')",
            FIXTURE_USER,
            FIXTURE_USERNAME,
        )
        await conn.execute("INSERT INTO wallet_accounts(account_id,balance) VALUES($1,0)", account)
        await conn.execute(
            "SELECT account_id FROM wallet_accounts WHERE account_id=ANY($1::text[]) ORDER BY account_id FOR UPDATE",
            ["fiat_reserve", account],
        )
        await conn.execute(
            """INSERT INTO ledger_entries(id,debit_account,credit_account,amount,entry_type,reference_id)
            VALUES($1,'fiat_reserve',$2,100,'starter_grant',$3)""",
            FIXTURE_ENTRY,
            account,
            reference,
        )
        await conn.execute(
            "UPDATE wallet_accounts SET balance=balance-100 WHERE account_id='fiat_reserve'"
        )
        await conn.execute(
            "UPDATE wallet_accounts SET balance=balance+100 WHERE account_id=$1", account
        )
        await conn.execute(
            "INSERT INTO user_starter_grants(user_id,granted_amount_v,grant_version) VALUES($1,100,'v1')",
            FIXTURE_USER,
        )
        await conn.execute(f"UPDATE {CONTROL}.ownership SET fixture_created=TRUE")
    valid = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM auth.users a JOIN profiles p ON p.id=a.id
        JOIN user_starter_grants g ON g.user_id=a.id JOIN ledger_entries l ON l.id=$4
        WHERE a.id=$1 AND a.email=$2 AND p.username=$3 AND g.granted_amount_v=100 AND g.grant_version='v1'
          AND l.debit_account='fiat_reserve' AND l.credit_account=$5 AND l.amount=100
          AND l.entry_type='starter_grant' AND l.reference_id=$6)""",
        FIXTURE_USER,
        FIXTURE_EMAIL,
        FIXTURE_USERNAME,
        FIXTURE_ENTRY,
        account,
        reference,
    )
    if not valid:
        raise BootstrapError("Synthetic fixture evidence mismatch; no replenishment or repair")


async def run(args):
    url = validate_environment(os.environ, args)
    runner = migration_runner()
    hashes = migration_hashes(runner)
    print(
        f"Approved technical staging: environment={ENVIRONMENT}; DB host={DB_HOST}; migrations={len(hashes)}"
    )
    print("NOT GoTrue, real authentication, provider/payment, or deployment certification.")
    if not args.apply and not args.check:
        print(
            "Dry run only: configuration verified. Database not contacted; use --check to inspect."
        )
        return 0
    import asyncpg

    conn = await asyncpg.connect(url, timeout=10, command_timeout=60, statement_cache_size=0)
    locked = False
    try:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID)
        if not locked:
            raise BootstrapError("Another staging bootstrap holds the lock; retry later")
        identity = await conn.fetchrow("""SELECT current_database() AS name,current_user AS actor,
            pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname=current_database()""")
        if (
            identity["name"] != unquote(urlsplit(url).path[1:])
            or identity["actor"] != identity["owner"]
        ):
            raise BootstrapError("Database name/owner does not match the approved connection")
        async with conn.transaction(readonly=not args.apply):
            exists = await conn.fetchval(
                "SELECT to_regclass('openvegas_staging_bootstrap.ownership') IS NOT NULL"
            )
            if not exists:
                await empty_database(conn)
                if not args.apply:
                    print("Empty, unowned staging DB: explicit --apply required; no writes made.")
                    return 1
                await create_scaffold(conn, identity)
            stamp, planned = await validate_stamp(conn, identity, hashes)
            if exists and stamp["schema_fingerprint"] is None:
                raise BootstrapError(
                    "Interrupted bootstrap has no verified schema checkpoint; manual review required"
                )
            pending, applied = await runner.inspect(conn, runner.migration_files())
            if not applied.issubset(planned):
                raise BootstrapError(
                    "Migration journal has entries outside the owned checksum plan"
                )
            if not args.apply:
                await fixture_user(conn, create=False)
                print(
                    f"Owned schema verified; pending migrations={len(pending)}; fixture={bool(stamp['fixture_created'])}"
                )
                return 1 if pending else 0
            for version, digest in hashes.items():
                await conn.execute(
                    f"INSERT INTO {CONTROL}.migration_plan(version,sha256) VALUES($1,$2) ON CONFLICT DO NOTHING",
                    version,
                    digest,
                )
            # If a file fails, a rerun can finish the runner's per-file atomic work.
            # Old hashes remain immutable by convention and are validated above.
            if pending:
                await conn.execute(f"UPDATE {CONTROL}.ownership SET schema_fingerprint=NULL")
        try:
            result = await runner.run(
                argparse.Namespace(
                    apply=True,
                    check=False,
                    env_file=None,
                    through=None,
                    allow_remote=True,
                    confirm_host=DB_HOST,
                )
            )
        except Exception:
            # Runner uses one transaction per file. Preserve a checkpoint of its
            # committed prefix so an ordinary failed-file rerun can be validated.
            # A hard process kill leaves NULL instead and requires manual review.
            await conn.execute(
                f"UPDATE {CONTROL}.ownership SET schema_fingerprint=$1",
                await schema_fingerprint(conn),
            )
            raise
        if result != 0:
            raise BootstrapError(
                "Migration runner did not complete; rerun only after reviewing the failure"
            )
        if migration_hashes(runner) != hashes:
            raise BootstrapError("Migration files changed during execution")
        async with conn.transaction():
            pending, applied = await runner.inspect(conn, runner.migration_files())
            if pending or applied != set(hashes):
                raise BootstrapError("Migration journal incomplete after apply")
            await fixture_user(conn, create=args.with_fixture_user)
            fingerprint = await schema_fingerprint(conn)
            await conn.execute(f"UPDATE {CONTROL}.ownership SET schema_fingerprint=$1", fingerprint)
        print(
            f"Technical schema ready: {len(hashes)} migrations verified. No sales or provider access enabled."
        )
        if args.with_fixture_user:
            print(
                f"Synthetic fixture user={FIXTURE_USER}; one-time grant=100 V (not replenished). Token generation is separate."
            )
        return 0
    finally:
        try:
            if locked:
                await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
        finally:
            await conn.close(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--confirm-environment")
    parser.add_argument("--confirm-database-host", "--confirm-host", dest="confirm_database_host")
    parser.add_argument("--with-fixture-user", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except BootstrapError as exc:
        print(f"Staging bootstrap refused: {exc}")
    except Exception as exc:  # noqa: BLE001 -- Database/driver messages may expose secrets.
        print(
            f"Staging bootstrap failed ({type(exc).__name__}); details redacted. No automatic reset performed."
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
