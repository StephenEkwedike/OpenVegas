#!/usr/bin/env python3
"""Inspect or explicitly apply the existing OpenVegas SQL migration journal.

Never runs seed.sql, resets a database, or loads the checkout's .env implicitly.
Use a direct/session connection, not a transaction pooler (session advisory lock).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOCK_ID = 7180260911
REFERENCE_038 = "038_provider_credential_alias_defaults"


async def verify_restored_reference_038(conn, applied: set[str]) -> None:
    """Recognize only the retained data-only history from the September restoration.

    This checks the catalog, not past seed execution. Never replay credential
    defaults, inspect key values, or replace the independent private-RLS marker.
    """
    if "038_private_runtime_rls" not in applied:
        raise ValueError("Reference history requires the completed private-RLS restoration")
    if not await conn.fetchval("SELECT to_regclass('public.schema_version') IS NOT NULL"):
        raise ValueError("Reference history is missing its original journal")
    if not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM public.schema_version WHERE version=$1)",
        REFERENCE_038 + ".sql",
    ):
        raise ValueError("Reference history does not match its original journal")
    columns = await conn.fetch("""SELECT a.attname AS name,
        format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS required,
        pg_get_expr(d.adbin,d.adrelid) AS default
        FROM pg_attribute a LEFT JOIN pg_attrdef d
        ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=to_regclass('public.provider_credentials')
        AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""")
    expected = [
        ("id", "uuid", True, "gen_random_uuid()"),
        *[(name, "text", True, None) for name in
          ("provider", "env", "key_alias", "key_version", "status")],
        ("created_at", "timestamp with time zone", True, "now()"),
        ("updated_at", "timestamp with time zone", True, "now()"),
    ]
    if [tuple(r[k] for k in ("name", "type", "required", "default")) for r in columns] != expected:
        raise ValueError("Reference credential catalog columns differ; review before migrating")
    checks = await conn.fetch("""SELECT conname AS name,convalidated AS valid,
        pg_get_constraintdef(oid) AS definition FROM pg_constraint
        WHERE conrelid=to_regclass('public.provider_credentials') ORDER BY conname""")
    expected_checks = [
        ("provider_credentials_pkey", True, "PRIMARY KEY (id)"),
        ("provider_credentials_status_check", True,
         "CHECK ((status = ANY (ARRAY['active'::text, 'unavailable'::text, 'degraded'::text, 'rotating'::text, 'disabled'::text])))"),
    ]
    if [tuple(r[k] for k in ("name", "valid", "definition")) for r in checks] != expected_checks:
        raise ValueError("Reference credential constraints differ; review before migrating")
    indexes = await conn.fetch("""SELECT pg_get_indexdef(indexrelid) AS definition,
        indisvalid AS valid,indisready AS ready FROM pg_index
        WHERE indrelid=to_regclass('public.provider_credentials')
        ORDER BY pg_get_indexdef(indexrelid)""")
    expected_indexes = [
        ("CREATE INDEX idx_provider_credentials_provider_env_status ON public.provider_credentials USING btree (provider, env, status, created_at DESC)", True, True),
        ("CREATE UNIQUE INDEX provider_credentials_pkey ON public.provider_credentials USING btree (id)", True, True),
    ]
    if [tuple(r[k] for k in ("definition", "valid", "ready")) for r in indexes] != expected_indexes:
        raise ValueError("Reference credential indexes differ; review before migrating")
    protected = await conn.fetchval("""SELECT relrowsecurity
        AND NOT has_table_privilege('anon',oid,'SELECT,INSERT,UPDATE,DELETE')
        AND NOT has_table_privilege('authenticated',oid,'SELECT,INSERT,UPDATE,DELETE')
        AND NOT EXISTS(SELECT 1 FROM pg_policy WHERE polrelid=c.oid)
        FROM pg_class c WHERE oid=to_regclass('public.provider_credentials')""")
    if protected is not True:
        raise ValueError("Reference credential access protection differs; review before migrating")


def migration_files(through: int | None = None) -> list[Path]:
    files = sorted((ROOT / "supabase/migrations").glob("[0-9]*_*.sql"))
    if not files:
        raise ValueError("No migration files found")
    versions = [int(path.name.split("_", 1)[0]) for path in files]
    if len(set(versions)) != len(versions):
        raise ValueError("Duplicate migration number; review filenames")
    if through is not None:
        if through not in versions:
            raise ValueError("--through must name an existing migration number")
        files = [path for path in files if int(path.name.split("_", 1)[0]) <= through]
    return files


def database_url(args: argparse.Namespace) -> str:
    if args.env_file:
        from dotenv import dotenv_values
        path = Path(args.env_file)
        if not path.is_file():
            raise ValueError("Environment file does not exist")
        url = str(dotenv_values(path, interpolate=False).get("DATABASE_URL") or "")
    else:
        url = os.getenv("DATABASE_URL", "")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("Malformed DATABASE_URL; connection value redacted") from None
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("Set DATABASE_URL or pass --env-file explicitly")
    if set(parse_qs(parsed.query, keep_blank_values=True)) - {"sslmode", "application_name"}:
        raise ValueError("Unsupported connection query options; host overrides are not allowed")
    if (args.apply and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            and (not args.allow_remote or args.confirm_host != parsed.hostname)):
        raise ValueError("Remote apply requires --allow-remote and --confirm-host HOST after approval")
    if args.apply and port == 6543:
        raise ValueError("Use a direct or session-mode connection for migration locking, not port 6543")
    return url


async def inspect(conn, files: list[Path], *, allow_restored_reference_038=False) -> tuple[list[Path], set[str]]:
    auth_ok = await conn.fetchval("SELECT to_regclass('auth.users') IS NOT NULL AND to_regprocedure('auth.uid()') IS NOT NULL")
    roles_ok = await conn.fetchval("SELECT count(*) = 2 FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
    if not auth_ok or not roles_ok:
        raise ValueError("Supabase Auth schema/roles missing; plain PostgreSQL is not a full local Supabase stack")
    journal = await conn.fetchval("SELECT to_regclass('public.schema_migrations')")
    if journal:
        applied = {row["version"] for row in await conn.fetch("SELECT version FROM public.schema_migrations")}
    else:
        existing = await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
        if existing:
            raise ValueError("Existing public tables without migration journal: audit schema before adopting it")
        applied = set()
    known = {path.stem for path in migration_files()}
    unknown = applied - known
    if unknown == {REFERENCE_038} and allow_restored_reference_038:
        await verify_restored_reference_038(conn, applied)
        unknown = set()
    if unknown:
        raise ValueError("Database has unknown migration versions; reconcile with its owning repository first")
    return [path for path in files if path.stem not in applied], applied


async def run(args: argparse.Namespace) -> int:
    import asyncpg
    url = database_url(args)
    parsed = urlsplit(url)
    print(f"Target host={parsed.hostname} port={parsed.port or 5432}; credentials hidden")
    conn = await asyncpg.connect(url, timeout=10, command_timeout=60, statement_cache_size=0)
    locked = False
    try:
        files = migration_files(args.through)
        if args.apply:
            # Serialize the entire run, while retaining one atomic transaction per migration.
            for _ in range(20):
                locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID)
                if locked:
                    break
                await asyncio.sleep(0.5)
            if not locked:
                raise ValueError("Another migration run holds the lock; retry after it finishes")
        async with conn.transaction(readonly=True):
            pending, applied = await inspect(
                conn, files,
                allow_restored_reference_038=getattr(args, "allow_restored_reference_038", False),
            )
        print(f"Recorded={len(applied)} pending={len(pending)}")
        for path in pending:
            print(f"Pending: {path.name}")
        if not args.apply:
            print("Read-only inspection; historical journal entries do not prove SQL checksums match.")
            return 1 if pending else 0
        await conn.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in pending:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '10s'")
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO public.schema_migrations(version) VALUES ($1) ON CONFLICT DO NOTHING", path.stem)
            print(f"Applied: {path.name}")
        print("Migration run complete. No seed or account provisioning performed.")
        return 0
    finally:
        if locked:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
            finally:
                await conn.close(timeout=5)
        else:
            await conn.close(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Read-only status (default); exit 1 if pending")
    action.add_argument("--apply", action="store_true", help="Apply pending files in atomic transactions")
    parser.add_argument("--env-file", help="Explicit configuration file; existing .env is never auto-loaded")
    parser.add_argument("--through", type=int, help="Stop at this migration number (isolated upgrade testing)")
    parser.add_argument("--allow-remote", action="store_true", help="Allow an approved remote apply")
    parser.add_argument("--confirm-host", help="Exact approved database hostname, required for remote apply")
    parser.add_argument("--allow-restored-reference-038", action="store_true",
                        help="Verify the exact preserved September reference-038 catalog; never ignore unknown history")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        print(f"Migration check failed: {exc}")
    except Exception as exc:  # noqa: BLE001 -- Driver messages may include secrets.
        # Driver exceptions can contain connection details; never echo credentials or SQL data.
        print(f"Migration operation failed ({type(exc).__name__}). Check connectivity/schema; the active file transaction was rolled back.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
