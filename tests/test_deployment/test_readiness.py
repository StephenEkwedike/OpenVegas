from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server import main
from server.services import dependencies as deps


@pytest.mark.asyncio
async def test_strict_startup_rejects_missing_database(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TEST_MODE", "0")
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "unit-test-secret")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENVEGAS_DB_FAIL_OPEN", "1")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        await deps.init_runtime_deps()


@pytest.mark.asyncio
async def test_production_cannot_start_in_test_mode(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TEST_MODE", "1")
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    with pytest.raises(RuntimeError, match="TEST_MODE"):
        await deps.init_runtime_deps()


@pytest.mark.asyncio
async def test_modern_auth_config_does_not_require_legacy_shared_secret(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TEST_MODE", "0")
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "local")
    monkeypatch.setenv("OPENVEGAS_DB_FAIL_OPEN", "0")
    monkeypatch.setenv("OPENVEGAS_REDIS_REQUIRED", "0")
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "public-test-key")
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        await deps.init_runtime_deps()


def test_placeholder_is_live_but_never_ready(monkeypatch):
    monkeypatch.setattr(deps, "_db", deps._Placeholder())
    client = TestClient(main.app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health").status_code == 200
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "Required application dependencies are not ready"


def test_dependency_error_is_sanitized(monkeypatch):
    monkeypatch.setattr(main, "assert_db_ready", AsyncMock(side_effect=RuntimeError("secret-dsn")))
    response = TestClient(main.app).get("/health/ready")
    assert response.status_code == 503
    assert "secret-dsn" not in response.text


@pytest.mark.asyncio
async def test_required_redis_cannot_use_placeholder(monkeypatch):
    monkeypatch.setattr(deps, "_redis", deps._Placeholder())
    monkeypatch.setenv("OPENVEGAS_REDIS_REQUIRED", "1")
    with pytest.raises(RuntimeError, match="Redis"):
        await deps.assert_redis_ready()
    monkeypatch.setenv("OPENVEGAS_REDIS_REQUIRED", "0")
    await deps.assert_redis_ready()


@pytest.mark.asyncio
async def test_startup_failure_closes_http_and_database_resources(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(main, "init_runtime_deps", AsyncMock(side_effect=RuntimeError("schema failure")))
    cleanup = AsyncMock()
    monkeypatch.setattr(main, "close_runtime_deps", cleanup)
    with pytest.raises(RuntimeError, match="schema failure"):
        async with main.lifespan(main.app):
            pytest.fail("Failed startup must not yield")
    cleanup.assert_awaited_once()
    client.aclose.assert_awaited_once()
    assert deps.get_http_client() is None


@pytest.mark.asyncio
async def test_history_does_not_require_stripe_but_payments_remain_unavailable(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_BILLING_PROVIDER", "stripe")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(deps, "_db", deps._Placeholder())
    service = deps.get_billing_service()
    assert service.provider_mode == "stripe"
    result = await service.list_topup_history(user_id="local-test", limit=10)
    assert result["entries"] == []
    with pytest.raises(deps.BillingError, match="not configured"):
        service.stripe_gateway.create_customer(email="local@test.invalid", name=None)


@pytest.mark.asyncio
async def test_failed_pool_close_still_closes_redis(monkeypatch):
    from types import SimpleNamespace
    pool = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failure")), terminate=lambda: None)
    redis = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(deps, "_db", SimpleNamespace(pool=pool))
    monkeypatch.setattr(deps, "_redis", redis)
    await deps.close_runtime_deps()
    redis.aclose.assert_awaited_once()
    assert isinstance(deps.get_db(), deps._Placeholder)
    assert isinstance(deps.get_redis(), deps._Placeholder)


def _short_deadline_api(recorded):
    import asyncio
    from types import SimpleNamespace

    async def wait_for(awaitable, timeout):
        recorded.append(timeout)
        return await asyncio.wait_for(awaitable, timeout=0.01)

    return SimpleNamespace(wait_for=wait_for)


@pytest.mark.asyncio
async def test_schema_startup_deadline_closes_partial_runtime(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock

    import asyncpg

    deadlines = []
    cancelled = asyncio.Event()

    async def stalled_schema(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    pool = SimpleNamespace(close=AsyncMock(), terminate=Mock())
    client = AsyncMock()
    monkeypatch.setenv("OPENVEGAS_TEST_MODE", "0")
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "synthetic-signing-marker")
    monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1/never-connected")
    monkeypatch.setenv("OPENVEGAS_REDIS_REQUIRED", "0")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(deps, "_db", deps._Placeholder())
    monkeypatch.setattr(deps, "_redis", deps._Placeholder())
    monkeypatch.setattr(deps, "asyncio", _short_deadline_api(deadlines))
    monkeypatch.setattr(deps, "assert_schema_compatible", stalled_schema)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(main, "ensure_qrcode_available", lambda: pytest.fail("Failed schema startup must not reach QR setup"))
    with pytest.raises(TimeoutError):
        async with main.lifespan(main.app):
            pytest.fail("Schema timeout must not enter serving state")
    assert deadlines == [30, 5]
    assert cancelled.is_set()
    pool.close.assert_awaited_once()
    client.aclose.assert_awaited_once()
    assert deps.get_http_client() is None
    assert isinstance(deps.get_db(), deps._Placeholder)


@pytest.mark.asyncio
async def test_schema_readiness_deadline_returns_sanitized_unavailable(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    deadlines = []
    cancelled = asyncio.Event()

    async def stalled_schema(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setenv("OPENVEGAS_TEST_MODE", "0")
    monkeypatch.setattr(main, "current_flags", lambda: SimpleNamespace(human_casino_enabled=False))
    monkeypatch.setattr(main, "get_db", lambda: object())
    monkeypatch.setattr(main, "assert_db_ready", AsyncMock())
    monkeypatch.setattr(main, "assert_redis_ready", AsyncMock())
    monkeypatch.setattr(main, "assert_schema_compatible", stalled_schema)
    monkeypatch.setattr(main, "asyncio", _short_deadline_api(deadlines))
    with pytest.raises(HTTPException) as error:
        await main.health_ready()
    assert deadlines == [10]
    assert cancelled.is_set()
    assert error.value.status_code == 503
    assert error.value.detail == "Required application dependencies are not ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["database", "redis"])
async def test_dependency_readiness_ping_has_five_second_deadline(monkeypatch, dependency):
    import asyncio
    from types import SimpleNamespace

    deadlines = []

    async def stalled(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(deps, "asyncio", _short_deadline_api(deadlines))
    if dependency == "database":
        monkeypatch.setattr(deps, "_db", SimpleNamespace(fetchrow=stalled))
        operation = deps.assert_db_ready
    else:
        monkeypatch.setattr(deps, "_redis", SimpleNamespace(ping=stalled))
        operation = deps.assert_redis_ready
    with pytest.raises(TimeoutError):
        await operation()
    assert deadlines == [5]


@pytest.mark.asyncio
async def test_cleanup_deadlines_reset_first_terminate_pool_and_attempt_redis(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock

    deadlines = []
    closed = []

    async def stalled_close(name):
        assert isinstance(deps.get_db(), deps._Placeholder)
        assert isinstance(deps.get_redis(), deps._Placeholder)
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(name)

    pool = SimpleNamespace(close=lambda: stalled_close("database"), terminate=Mock())
    redis = SimpleNamespace(aclose=lambda: stalled_close("redis"))
    monkeypatch.setattr(deps, "_db", SimpleNamespace(pool=pool))
    monkeypatch.setattr(deps, "_redis", redis)
    monkeypatch.setattr(deps, "asyncio", _short_deadline_api(deadlines))
    await deps.close_runtime_deps()
    assert deadlines == [5, 5]
    assert closed == ["database", "redis"]
    pool.terminate.assert_called_once()
