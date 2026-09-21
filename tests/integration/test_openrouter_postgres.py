"""Real local database/wallet/model switches; supplier HTTP is always mocked."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import AIGateway, InferenceRequest
from openvegas.wallet.ledger import WalletService
from server.services.provider_threads import ProviderThreadService

pytestmark = pytest.mark.asyncio
MODELS = [
    "openai/reviewed-test",
    "anthropic/reviewed-test",
    "google/reviewed-test",
    "mistralai/reviewed-test",
]


async def provision(db, monkeypatch):
    user = str(uuid.uuid4())
    await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user)
    now = datetime.now(UTC)
    review = {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "account_access": True,
        "completion_chat": True,
        "context_window_tokens": 100_000,
        "capabilities": {"tools": True},
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
    }
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({f"openrouter:{m}": review for m in MODELS})
    )
    monkeypatch.setenv("OPENROUTER_LOCAL_FIXTURE", "synthetic-not-a-real-key")
    await db.execute(
        "INSERT INTO provider_credentials(provider,env,key_alias,key_version,status) "
        "VALUES ('openrouter','test','OPENROUTER_LOCAL_FIXTURE','test','active')"
    )
    for model in MODELS:
        await db.execute(
            "INSERT INTO provider_catalog(provider,model_id,display_name,max_tokens,"
            "cost_input_per_1m,cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m) "
            "VALUES ('openrouter',$1,'OpenRouter offline fixture',1024,1,2,100,200)",
            model,
        )
    return user


async def test_openrouter_migration_and_same_supplier_vendor_switches(
    database_factory, monkeypatch
):
    async with database_factory(through=40) as sandbox:
        user = await provision(sandbox.db, monkeypatch)
        await sandbox.migrate(through=41)
        db = sandbox.db
        await db.execute(
            "INSERT INTO profiles(id,username,default_provider) VALUES ($1::uuid,$2,'openrouter')",
            user,
            "router-" + user,
        )
        service, catalog = ProviderThreadService(db), ProviderCatalog(db)
        thread = await service.create_canonical_thread(
            user_id=user,
            provider="openrouter",
            model_id=MODELS[0],
            catalog=catalog,
        )
        await service.append_canonical_exchange(
            user_id=user,
            thread_id=thread.thread_id,
            expected_revision=thread.revision,
            prompt="Remember amber",
            response_text="Remembered amber",
        )
        current_model = MODELS[0]
        for model in MODELS[1:]:
            history = await service.canonical_history(
                user_id=user,
                thread_id=thread.thread_id,
                provider="openrouter",
                model_id=current_model,
            )
            thread = await service.canonical_switch(
                user_id=user,
                thread_id=thread.thread_id,
                provider="openrouter",
                model_id=model,
                catalog=catalog,
                expected_revision=history.revision,
                commit=True,
            )
            assert thread.context_transferred
            current_model = model
        await sandbox.migrate(through=41)
        db = await sandbox.reconnect()
        history = await ProviderThreadService(db).canonical_history(
            user_id=user,
            thread_id=thread.thread_id,
            provider="openrouter",
            model_id=MODELS[-1],
        )
        assert history.messages() == [
            {"role": "user", "content": "Remember amber"},
            {"role": "assistant", "content": "Remembered amber"},
        ]
        assert (
            await db.fetchval("SELECT default_provider FROM profiles WHERE id=$1::uuid", user)
            == "openrouter"
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE version='041_openrouter_provider_context'"
            )
            == 1
        )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reported_usd", ["0.000015", "0.0000154", "0.0000145"])
async def test_openrouter_actual_supplier_cost_retail_charge_and_idempotency(
    database_factory, monkeypatch, stream, reported_usd
):
    async with database_factory(through=41) as sandbox:
        db = sandbox.db
        user = await provision(db, monkeypatch)
        wallet = WalletService(db)
        await wallet.fund_from_card("user:" + user, Decimal(100), "fixture:" + user)
        calls = []

        def supplier(request):
            payload = json.loads(request.content)
            calls.append(payload)
            assert request.url == "https://openrouter.ai/api/v1/chat/completions"
            assert payload["provider"]["allow_fallbacks"] is False
            return httpx.Response(
                200,
                json={
                    "id": "gen-local-fixture",
                    "model": MODELS[0],
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "amber"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                        "cost": float(reported_usd),
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(supplier)) as http:
            gateway = AIGateway(db, wallet, ProviderCatalog(db), http_client=http)
            req = InferenceRequest(
                "user:" + user,
                "openrouter",
                MODELS[0],
                [{"role": "user", "content": "Say amber"}],
                max_tokens=32,
                idempotency_key=str(uuid.uuid4()),
            )
            if stream:
                events = [event async for event in gateway.stream_infer(req)]
                assert [e["type"] for e in events] == ["text_delta", "completed"]
                result = events[-1]["result"]
            else:
                result = await gateway.infer(req)
            again = await gateway.infer(req)
        assert result.text == again.text == "amber"
        assert len(calls) == 1
        assert result.v_cost == Decimal("0.002000")
        stored_usd = Decimal(reported_usd).quantize(Decimal("0.000001"))
        assert result.actual_cost_usd == again.actual_cost_usd == stored_usd
        assert await wallet.get_balance("user:" + user) == Decimal("99.998000")
        assert await db.fetchval("SELECT count(*) FROM inference_usage") == 1
        assert await db.fetchval("SELECT actual_cost_usd FROM inference_usage") == stored_usd
        assert (
            await db.fetchval("SELECT final_provider_cost_usd FROM inference_requests")
            == stored_usd
        )
        assert (
            await db.fetchval(
                "SELECT count(*) FROM inference_preauthorizations WHERE status='reserved'"
            )
            == 0
        )
