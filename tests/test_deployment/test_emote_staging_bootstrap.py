"""Fail-closed staging scope tests. No database or live service calls."""

import argparse
import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "isolated_staging_bootstrap", ROOT / "scripts/bootstrap_emote_staging.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(module):
    return {
        "RAILWAY_PROJECT_ID": module.PROJECT,
        "RAILWAY_ENVIRONMENT_ID": module.ENVIRONMENT,
        "RAILWAY_SERVICE_ID": module.SERVICE,
        "OPENVEGAS_RUNTIME_ENV": "staging",
        "OPENVEGAS_TEST_MODE": "0",
        "OPENVEGAS_DB_FAIL_OPEN": "0",
        "DATABASE_URL": "postgresql://postgres:synthetic-only@postgres.railway.internal:5432/railway",
        "REDIS_URL": f"redis://default:synthetic-only@{module.REDIS_HOST}:6379",
        "SUPABASE_JWT_SECRET": "synthetic-staging-only-not-a-real-signing-secret",
    }


def options(module, **changes):
    return argparse.Namespace(
        **{
            "apply": False,
            "check": False,
            "with_fixture_user": False,
            "confirm_environment": module.ENVIRONMENT,
            "confirm_database_host": module.DB_HOST,
            **changes,
        }
    )


@pytest.fixture
def module(monkeypatch):
    module = load_bootstrap()
    monkeypatch.setattr(os, "environ", environment(module))
    return module


@pytest.mark.parametrize(
    "name",
    [
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_SERVICE_ID",
        "OPENVEGAS_RUNTIME_ENV",
        "OPENVEGAS_TEST_MODE",
        "OPENVEGAS_DB_FAIL_OPEN",
    ],
)
@pytest.mark.parametrize("value", [None, "production", "1", ""])
def test_exact_scope_and_runtime_settings_required(module, name, value):
    env = environment(module)
    if value is None:
        env.pop(name)
    else:
        env[name] = value
    with pytest.raises(module.BootstrapError, match="identity/setting"):
        module.validate_environment(env, options(module, apply=True))


@pytest.mark.parametrize(
    "name",
    [
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "MISTRAL_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "OPENVEGAS_PROVIDER_CREDENTIALS",
        "OPENVEGAS_OPENROUTER_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "REPLICATE_API_TOKEN",
        "HF_TOKEN",
        "FUTURE_VENDOR_API_KEY",
    ],
)
def test_no_provider_or_payment_credential_can_enter(module, name):
    env = environment(module)
    env[name] = "do-not-echo-this-private-value"
    with pytest.raises(module.BootstrapError, match="forbidden") as exc:
        module.validate_environment(env, options(module))
    assert "do-not-echo" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:x@127.0.0.1/railway",
        "postgresql://postgres:x@db.prod.supabase.co/railway",
        "postgresql://postgres:x@postgres.railway.internal.evil/railway",
        "postgresql://postgres:x@postgres.railway.internal:6543/railway",
        "postgresql://postgres:x@postgres.railway.internal/railway?host=prod.invalid",
        "postgresql://postgres:x@postgres.railway.internal/railway?sslmode=require",
        "postgresql://postgres:x@postgres.railway.internal/railway#fragment",
        "postgresql://postgres:x@postgres.railway.internal/postgres",
        "postgresql://postgres:x@postgres.railway.internal/template1",
        "postgresql://postgres:x@postgres.railway.internal/another_database",
        "postgresql://postgres@postgres.railway.internal/railway",
        "postgresql://postgres:x@postgres.railway.internal/",
        "postgresql://postgres:x@postgres.railway.internal/a%2Fb",
    ],
)
def test_database_target_overrides_fail_closed(module, url):
    env = environment(module)
    env["DATABASE_URL"] = url
    with pytest.raises(module.BootstrapError, match="DATABASE_URL"):
        module.validate_environment(env, options(module, apply=True))


@pytest.mark.parametrize(
    "url",
    [
        "redis://default:x@prod.invalid:6379",
        "redis://default:x@redis-j9m5.railway.internal:6380",
        "redis://default:x@redis-j9m5.railway.internal/1",
        "redis://redis-j9m5.railway.internal",
        "redis://default:x@redis.railway.internal:6379",
    ],
)
def test_redis_target_cannot_reuse_prod(module, url):
    env = environment(module)
    env["REDIS_URL"] = url
    with pytest.raises(module.BootstrapError, match="REDIS_URL"):
        module.validate_environment(env, options(module))


@pytest.mark.parametrize(
    "flag,value",
    [
        ("confirm_environment", None),
        ("confirm_environment", "wrong"),
        ("confirm_database_host", None),
        ("confirm_database_host", "prod.invalid"),
    ],
)
def test_apply_requires_both_confirmation_values(module, flag, value):
    with pytest.raises(module.BootstrapError, match="confirmations"):
        module.validate_environment(
            environment(module), options(module, apply=True, **{flag: value})
        )


def test_fixture_is_never_implicitly_created_by_check(module):
    with pytest.raises(module.BootstrapError, match="requires --apply"):
        module.validate_environment(
            environment(module), options(module, check=True, with_fixture_user=True)
        )


def test_offline_plan_does_not_connect_or_print_secrets(module, monkeypatch, capsys):
    import asyncpg

    connect = AsyncMock(side_effect=AssertionError("No network during plan"))
    monkeypatch.setattr(asyncpg, "connect", connect)
    assert module.main([]) == 0
    connect.assert_not_called()
    output = capsys.readouterr().out
    assert "Database not contacted" in output and "NOT GoTrue" in output
    assert "synthetic-only" not in output and "postgresql://" not in output


def test_ambient_dotenv_is_not_loaded(module, monkeypatch, tmp_path, capsys):
    (tmp_path / ".env").write_text("STRIPE_SECRET_KEY=forbidden-secret\n")
    monkeypatch.chdir(tmp_path)
    assert module.main([]) == 0
    assert "forbidden-secret" not in capsys.readouterr().out


def test_database_errors_are_redacted(module, monkeypatch, capsys):
    import asyncpg

    monkeypatch.setattr(
        asyncpg, "connect", AsyncMock(side_effect=RuntimeError("private-password-and-row"))
    )
    assert module.main(["--check"]) == 2
    output = capsys.readouterr().out
    assert "RuntimeError" in output and "private-password" not in output


@pytest.mark.asyncio
async def test_nonempty_database_is_never_adopted(module):
    conn = AsyncMock()
    conn.fetchval.return_value = True
    with pytest.raises(module.BootstrapError, match="not empty"):
        await module.empty_database(conn)
    conn.execute.assert_not_called()


def test_no_deploy_dotenv_token_or_reset_code(module):
    import ast

    tree = ast.parse(Path(module.__file__).read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imports.intersection({"stripe", "httpx", "requests", "dotenv", "subprocess", "jwt"})
    assert "DROP SCHEMA" not in Path(module.__file__).read_text()
    assert "TRUNCATE" not in Path(module.__file__).read_text()
