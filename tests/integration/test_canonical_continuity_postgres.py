"""Disposable PostgreSQL continuity, persistence and real wallet settlement tests.

Uses the existing guarded integration fixtures. All provider transports are stubs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.contracts.errors import ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.conversation import ContinuityError
from openvegas.gateway.inference import AIGateway, InferenceResult
from openvegas.wallet.ledger import WalletService
from server.services.provider_threads import ProviderThreadService

pytestmark = pytest.mark.asyncio
MODEL = "continuity-local"


@pytest.fixture(autouse=True)
def controlled_providers(monkeypatch, integration_environment):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setenv("CONTINUITY_LOCAL_KEY", "synthetic-no-network")
    now = datetime.now(UTC)
    review = {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "context_window_tokens": 200000,
        "account_access": True,
        "completion_chat": True,
    }
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON",
        json.dumps({f"{p}:{MODEL}": review for p in ("openai", "anthropic", "mistral", "gemini")}),
    )

    async def denied(*args, **kwargs):
        raise AssertionError("A real provider transport is forbidden")

    for name in ("_call_openai", "_call_anthropic", "_call_gemini", "_call_mistral"):
        monkeypatch.setattr(AIGateway, name, denied)


async def provision(db):
    user = str(uuid.uuid4())
    await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user)
    for provider in ("openai", "anthropic", "mistral", "gemini"):
        await db.execute(
            "INSERT INTO provider_catalog(provider,model_id,display_name,max_tokens,"
            "cost_input_per_1m,cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m) "
            "VALUES ($1,$2,'Continuity local fixture',1024,1,1,100,100)",
            provider,
            MODEL,
        )
        await db.execute(
            "INSERT INTO provider_credentials(provider,env,key_alias,key_version,status) "
            "VALUES ($1,'test','CONTINUITY_LOCAL_KEY','test','active')",
            provider,
        )
    service = ProviderThreadService(db)
    catalog = ProviderCatalog(db)
    created = await service.create_canonical_thread(
        user_id=user,
        provider="openai",
        model_id=MODEL,
        catalog=catalog,
    )
    return user, service, catalog, created


def arguments(user, catalog, created, gateway):
    return {
        "user_id": user,
        "catalog": catalog,
        "gateway": gateway,
        "thread_id": created.thread_id,
        "provider": "openai",
        "model_id": MODEL,
        "expected_revision": created.revision,
        "prompt": "Remember the local fixture.",
        "idempotency_key": str(uuid.uuid4()),
    }


@pytest.mark.parametrize("target", ["anthropic", "gemini"])
async def test_continuity_reconnect_fork_order_scope_and_rollback(database_factory, target):
    async with database_factory(max_size=2) as sandbox:
        user, service, catalog, created = await provision(sandbox.db)
        gateway = SimpleNamespace(
            infer=AsyncMock(
                return_value=InferenceResult("Remembered.", 10, 5, completion_status="complete")
            )
        )
        result = await service.infer_canonical(**arguments(user, catalog, created, gateway))
        db = await sandbox.reconnect()
        service, catalog = ProviderThreadService(db), ProviderCatalog(db)
        fork = await service.canonical_switch(
            user_id=user,
            thread_id=created.thread_id,
            provider=target,
            model_id=MODEL,
            catalog=catalog,
            commit=True,
            expected_revision=result["revision"],
        )
        restored = await service.canonical_history(
            user_id=user, thread_id=fork.thread_id, provider=target, model_id=MODEL
        )
        assert restored.messages() == [
            {"role": "user", "content": "Remember the local fixture."},
            {"role": "assistant", "content": "Remembered."},
        ]
        with pytest.raises(ContractError):
            await service.canonical_switch(
                user_id=str(uuid.uuid4()),
                thread_id=fork.thread_id,
                provider="openai",
                model_id=MODEL,
                catalog=catalog,
            )
        with pytest.raises(ContinuityError, match="History changed"):
            await service.canonical_switch(
                user_id=user,
                thread_id=fork.thread_id,
                provider="openai",
                model_id=MODEL,
                catalog=catalog,
                commit=True,
                expected_revision="0" * 64,
            )
        assert await db.fetchval("SELECT count(*) FROM provider_threads") == 2
        assert await db.fetchval("SELECT count(*) FROM provider_thread_messages") == 2


async def test_continuity_real_concurrent_turn_exclusion_and_cancel_marker(database_factory):
    async with database_factory(max_size=2) as sandbox:
        user, service, catalog, created = await provision(sandbox.db)
        entered = asyncio.Event()

        async def pending(req):
            entered.set()
            await asyncio.Event().wait()

        gateway = SimpleNamespace(infer=AsyncMock(side_effect=pending))
        task = asyncio.create_task(
            service.infer_canonical(**arguments(user, catalog, created, gateway))
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            # Pool connections are not held over provider I/O, even with max_size=2.
            with pytest.raises(ContinuityError, match="possibly billed"):
                await asyncio.wait_for(
                    service.canonical_switch(
                        user_id=user,
                        thread_id=created.thread_id,
                        provider="anthropic",
                        model_id=MODEL,
                        catalog=catalog,
                    ),
                    3,
                )
            second = SimpleNamespace(infer=AsyncMock())
            with pytest.raises(ContinuityError, match="possibly billed"):
                await service.infer_canonical(**arguments(user, catalog, created, second))
            second.infer.assert_not_awaited()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        db = await sandbox.reconnect()
        with pytest.raises(ContinuityError, match="possibly billed"):
            await ProviderThreadService(db).canonical_history(
                user_id=user, thread_id=created.thread_id, provider="openai", model_id=MODEL
            )


async def test_continuity_real_settlement_duplicate_and_partial_output(
    database_factory, monkeypatch
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, service, catalog, created = await provision(db)
        wallet = WalletService(db)
        await wallet.fund_from_card("user:" + user, Decimal(100), "local-funding:" + user)
        gateway = AIGateway(db, wallet, catalog)
        calls = []

        async def provider(*args):
            calls.append(1)
            return InferenceResult("Partial answer", 10, 1024, completion_status="incomplete")

        monkeypatch.setattr(gateway, "_route_to_provider", provider)
        args = arguments(user, catalog, created, gateway)
        result = await service.infer_canonical(**args)
        assert result["continuity_blocked"] and result["revision"] is None
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        assert (
            await db.fetchval(
                "SELECT count(*) FROM inference_preauthorizations WHERE status='reserved'"
            )
            == 0
        )
        balance = await wallet.get_balance("user:" + user)
        assert balance < Decimal(100)
        with pytest.raises(ContinuityError, match="possibly billed"):
            await service.infer_canonical(**args)
        assert len(calls) == 1 and await wallet.get_balance("user:" + user) == balance


async def test_continuity_cancel_after_real_committed_settlement_cannot_charge_again(
    database_factory, monkeypatch
):
    async with database_factory() as sandbox:
        db = sandbox.db
        user, service, catalog, created = await provision(db)
        wallet = WalletService(db)
        await wallet.fund_from_card("user:" + user, Decimal(100), "local-funding:" + user)
        gateway = AIGateway(db, wallet, catalog)
        monkeypatch.setattr(
            gateway,
            "_route_to_provider",
            AsyncMock(
                return_value=InferenceResult(
                    "Completed but acknowledgement lost", 10, 5, completion_status="complete"
                )
            ),
        )
        finalize = gateway._finalize_inference_execution

        async def commit_then_cancel(*args):
            await finalize(*args)
            raise asyncio.CancelledError

        monkeypatch.setattr(gateway, "_finalize_inference_execution", commit_then_cancel)
        args = arguments(user, catalog, created, gateway)
        with pytest.raises(asyncio.CancelledError):
            await service.infer_canonical(**args)
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        balance = await wallet.get_balance("user:" + user)
        with pytest.raises(ContinuityError, match="possibly billed"):
            await service.infer_canonical(**{**args, "idempotency_key": str(uuid.uuid4())})
        assert await wallet.get_balance("user:" + user) == balance
        gateway._route_to_provider.assert_awaited_once()


async def test_continuity_mistral_current_schema_fails_atomically(database_factory):
    async with database_factory(through=39) as sandbox:
        user, service, catalog, created = await provision(sandbox.db)
        with pytest.raises(ContinuityError, match="operator migration required"):
            await service.canonical_switch(
                user_id=user,
                thread_id=created.thread_id,
                provider="mistral",
                model_id=MODEL,
                catalog=catalog,
                commit=True,
                expected_revision=created.revision,
            )
        assert await sandbox.db.fetchval("SELECT count(*) FROM provider_threads") == 1
        assert await sandbox.db.fetchval("SELECT count(*) FROM provider_thread_messages") == 1


async def test_continuity_mistral_through40_fork_order_and_profile_default(database_factory):
    async with database_factory(through=40) as sandbox:
        db = sandbox.db
        user, service, catalog, created = await provision(db)
        expected = []
        revision = created.revision
        for prompt, response in (("First question", "First answer"), ("Second question", "Second answer")):
            await service.append_canonical_exchange(
                user_id=user,
                thread_id=created.thread_id,
                expected_revision=revision,
                prompt=prompt,
                response_text=response,
            )
            history = await service.canonical_history(
                user_id=user, thread_id=created.thread_id, provider="openai", model_id=MODEL
            )
            revision = history.revision
            expected.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response},
            ])
        fork = await service.canonical_switch(
            user_id=user,
            thread_id=created.thread_id,
            provider="mistral",
            model_id=MODEL,
            catalog=catalog,
            commit=True,
            expected_revision=revision,
        )
        await db.execute(
            "INSERT INTO profiles(id, username, default_provider) VALUES ($1::uuid, $2, 'mistral')",
            user,
            "continuity-" + user,
        )
        db = await sandbox.reconnect()
        service = ProviderThreadService(db)
        restored = await service.canonical_history(
            user_id=user, thread_id=fork.thread_id, provider="mistral", model_id=MODEL
        )
        original = await service.canonical_history(
            user_id=user, thread_id=created.thread_id, provider="openai", model_id=MODEL
        )
        assert restored.messages() == original.messages() == expected
        assert restored.revision == original.revision == fork.revision == revision
        assert fork.thread_id != created.thread_id and fork.context_transferred is True
        assert await db.fetchval(
            "SELECT default_provider FROM profiles WHERE id=$1::uuid", user
        ) == "mistral"
        assert await db.fetchval("SELECT count(*) FROM provider_threads") == 2
        assert await db.fetchval("SELECT count(*) FROM provider_thread_messages") == 2
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0


async def test_continuity_mistral_migration40_upgrade_and_rerun_preserve_data(database_factory):
    async with database_factory(through=39) as sandbox:
        db = sandbox.db
        user, service, catalog, created = await provision(db)
        await db.execute(
            "INSERT INTO profiles(id, username, default_provider) VALUES ($1::uuid, $2, 'openai')",
            user,
            "continuity-" + user,
        )

        async def persisted_rows():
            rows = []
            for table in ("profiles", "provider_threads", "provider_thread_messages"):
                rows.append(await sandbox.db.fetch(f"SELECT * FROM {table} ORDER BY id"))
            return rows

        before_upgrade = await persisted_rows()
        await sandbox.migrate(through=40)
        assert await persisted_rows() == before_upgrade
        await db.execute(
            "UPDATE profiles SET default_provider='mistral' WHERE id=$1::uuid", user
        )
        fork = await service.canonical_switch(
            user_id=user,
            thread_id=created.thread_id,
            provider="mistral",
            model_id=MODEL,
            catalog=catalog,
            commit=True,
            expected_revision=created.revision,
        )
        before_rerun = await persisted_rows()
        journal = await db.fetch("SELECT * FROM schema_migrations ORDER BY version")
        assert sum(row["version"] == "040_mistral_provider_context" for row in journal) == 1
        await sandbox.migrate(through=40)
        # Exercise the SQL itself too, not only the runner's already-applied skip.
        migration = (
            Path(__file__).resolve().parents[2]
            / "supabase/migrations/040_mistral_provider_context.sql"
        )
        async with db.transaction() as tx:
            await tx.execute(migration.read_text(encoding="utf-8"))
        db = await sandbox.reconnect()
        assert await persisted_rows() == before_rerun
        assert await db.fetch("SELECT * FROM schema_migrations ORDER BY version") == journal
        history = await ProviderThreadService(db).canonical_history(
            user_id=user, thread_id=fork.thread_id, provider="mistral", model_id=MODEL
        )
        assert history.messages() == [] and history.revision == created.revision
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0


async def test_continuity_full_input_cost_reserved_before_provider(database_factory, monkeypatch):
    from openvegas.wallet.ledger import InsufficientBalance

    async with database_factory() as sandbox:
        db = sandbox.db
        user, service, catalog, created = await provision(db)
        wallet = WalletService(db)
        await wallet.fund_from_card("user:" + user, Decimal("0.5"), "local-funding:" + user)
        gateway = AIGateway(db, wallet, catalog)
        provider = AsyncMock()
        monkeypatch.setattr(gateway, "_route_to_provider", provider)
        args = arguments(user, catalog, created, gateway)
        args["prompt"] = "x" * 10000
        with pytest.raises(InsufficientBalance):
            await service.infer_canonical(**args)
        provider.assert_not_awaited()
        assert await wallet.get_balance("user:" + user) == Decimal("0.5")
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0
        history = await service.canonical_history(user_id=user, thread_id=created.thread_id,
            provider="openai", model_id=MODEL)
        assert history.revision == created.revision
