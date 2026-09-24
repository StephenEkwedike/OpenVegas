from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.services import dependencies as deps


@pytest.fixture
def schema(monkeypatch):
    for key in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                "OPENVEGAS_NATIVE_GENERATION_HISTORY"):
        monkeypatch.delenv(key, raising=False)
    checks = SimpleNamespace(migrations=AsyncMock(), tables=AsyncMock(), columns=AsyncMock())
    monkeypatch.setattr(deps, "require_migration_min", checks.migrations)
    monkeypatch.setattr(deps, "require_tables", checks.tables)
    monkeypatch.setattr(deps, "require_columns", checks.columns)
    return checks


def flags():
    return deps.FeatureFlags(False, False, False, False, False, False, False, False)


@pytest.mark.asyncio
async def test_default_off_keeps_existing_schema_gate(schema):
    await deps.assert_schema_compatible(None, flags())
    assert "049_native_task_handoffs" not in [call.args[1] for call in schema.migrations.await_args_list]


@pytest.mark.asyncio
async def test_enabled_requires_additive_handoff_schema(schema, monkeypatch):
    for key in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                "OPENVEGAS_NATIVE_GENERATION_HISTORY"):
        monkeypatch.setenv(key, "1")
    await deps.assert_schema_compatible(None, flags())
    assert "049_native_task_handoffs" in [call.args[1] for call in schema.migrations.await_args_list]
    assert {"native_task_handoffs"} in [call.args[1] for call in schema.tables.await_args_list]
    assert any(("native_task_handoffs", "document_sha256") in call.args[1]
               for call in schema.columns.await_args_list)
    assert any(("native_task_handoffs", "first_dispatch_json") in call.args[1]
               for call in schema.columns.await_args_list)


@pytest.mark.asyncio
async def test_enabled_without_prerequisites_rejects_startup(schema, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    with pytest.raises(RuntimeError, match="requires native generation"):
        await deps.assert_schema_compatible(None, flags())


@pytest.mark.asyncio
async def test_enabled_missing_migration_fails_closed(schema, monkeypatch):
    for key in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                "OPENVEGAS_NATIVE_GENERATION_HISTORY"):
        monkeypatch.setenv(key, "1")

    async def missing(db, migration):
        if migration == "049_native_task_handoffs":
            raise RuntimeError("Handoff migration unavailable")

    schema.migrations.side_effect = missing
    with pytest.raises(RuntimeError, match="migration unavailable"):
        await deps.assert_schema_compatible(None, flags())
