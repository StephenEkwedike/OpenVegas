"""Real local settlement/rollback/concurrency; no real provider requests."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.conversation import CanonicalConversation
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.gateway.reconciliation import ReconciliationError, inspect_turn, restore_turn
from openvegas.wallet.ledger import WalletService
from server.services.provider_threads import ProviderThreadService

pytestmark = pytest.mark.asyncio
MODEL = "reconciliation-offline-fixture"
PROMPT = "Remember amber."


async def settled_pending(sandbox, monkeypatch):
    db = sandbox.db
    user, key = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user)
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setenv("RECONCILIATION_FIXTURE_KEY", "synthetic-never-sent")
    now = datetime.now(UTC)
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON",
        json.dumps(
            {
                f"openai:{MODEL}": {
                    "reviewed_at": (now - timedelta(hours=1)).isoformat(),
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                    "context_window_tokens": 100000,
                    "account_access": True,
                    "completion_chat": True,
                }
            }
        ),
    )
    await db.execute(
        "INSERT INTO provider_credentials(provider,env,key_alias,key_version,status) "
        "VALUES ('openai','test','RECONCILIATION_FIXTURE_KEY','test','active')"
    )
    await db.execute(
        "INSERT INTO provider_catalog(provider,model_id,display_name,max_tokens,"
        "cost_input_per_1m,cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m) "
        "VALUES ('openai',$1,'Offline reconciliation fixture',1024,1,1,100,100)",
        MODEL,
    )
    wallet, catalog = WalletService(db), ProviderCatalog(db)
    await wallet.fund_from_card("user:" + user, Decimal(100), "fixture:" + user)
    service = ProviderThreadService(db)
    thread = await service.create_canonical_thread(
        user_id=user,
        provider="openai",
        model_id=MODEL,
        catalog=catalog,
    )
    await service._mark_canonical_pending(
        user_id=user,
        thread_id=thread.thread_id,
        canonical=CanonicalConversation(),
        request_key=key,
    )
    gateway = AIGateway(db, wallet, catalog)
    calls = []

    async def provider(*args):
        calls.append(1)
        return InferenceResult("Remembered amber.", 10, 5, completion_status="complete")

    monkeypatch.setattr(gateway, "_route_to_provider", provider)
    await gateway.infer(
        InferenceRequest(
            "user:" + user,
            "openai",
            MODEL,
            [{"role": "user", "content": PROMPT}],
            max_tokens=1024,
            idempotency_key=key,
            enable_tools=False,
            enable_web_search=False,
            strict_continuity=True,
        )
    )
    request = str(
        await db.fetchval(
            "SELECT id FROM inference_requests WHERE idempotency_key=$1",
            key,
        )
    )
    assert calls == [1]
    return {"user_id": user, "thread_id": thread.thread_id, "request_id": request}


async def inspect(sandbox, scope):
    async with (
        sandbox.pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        return await inspect_turn(conn, **scope, prompt=PROMPT, max_tokens=1024)


async def snapshot(db):
    return [
        list(map(dict, await db.fetch(sql)))
        for sql in (
            "SELECT * FROM ledger_entries ORDER BY id",
            "SELECT * FROM inference_usage ORDER BY id",
            "SELECT * FROM inference_requests ORDER BY id",
            "SELECT * FROM wallet_accounts ORDER BY account_id",
        )
    ]


async def test_reconcile_committed_result_concurrent_replay_and_reconnect(
    database_factory, monkeypatch
):
    async with database_factory(through=42) as sandbox:
        scope = await settled_pending(sandbox, monkeypatch)
        before = await snapshot(sandbox.db)
        report = await inspect(sandbox, scope)
        assert report["can_restore"] and report["settlement_verified"]
        assert not report["provider_invoice_verified"]
        args = dict(
            scope,
            prompt=PROMPT,
            max_tokens=1024,
            expected_plan=report["plan_token"],
            operator_id=str(uuid.uuid4()),
        )
        results = await asyncio.gather(*(restore_turn(sandbox.db, **args) for _ in range(4)))
        assert sorted(r["status"] for r in results) == ["already_reconciled"] * 3 + ["restored"]
        await sandbox.reconnect()
        assert await snapshot(sandbox.db) == before
        assert await sandbox.db.fetchval("SELECT count(*) FROM inference_turn_reconciliations") == 1
        history = await ProviderThreadService(sandbox.db).canonical_history(
            user_id=scope["user_id"],
            thread_id=scope["thread_id"],
            provider="openai",
            model_id=MODEL,
        )
        assert history.messages() == [
            {"role": "user", "content": PROMPT},
            {"role": "assistant", "content": "Remembered amber."},
        ]
        assert (await inspect(sandbox, scope))["status"] == "already_reconciled"
        # Match Supabase's schema visibility, then test the private table's ACL
        # rather than accidentally passing/failing on a hidden search_path schema.
        await sandbox.pool.execute("GRANT USAGE ON SCHEMA public TO authenticated")
        async with sandbox.pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            with pytest.raises(Exception, match="permission denied"):
                await conn.fetch("SELECT * FROM inference_turn_reconciliations")


async def test_reconcile_audit_failure_rolls_back_history(database_factory, monkeypatch):
    async with database_factory(through=42) as sandbox:
        scope = await settled_pending(sandbox, monkeypatch)
        report = await inspect(sandbox, scope)
        before = await snapshot(sandbox.db)
        content = await sandbox.db.fetchval("SELECT content FROM provider_thread_messages")
        await sandbox.db.execute("""
            CREATE FUNCTION reject_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$;
            CREATE TRIGGER reject_receipt BEFORE INSERT ON inference_turn_reconciliations
            FOR EACH ROW EXECUTE FUNCTION reject_receipt();
        """)
        with pytest.raises(Exception, match="synthetic audit failure"):
            await restore_turn(
                sandbox.db,
                **scope,
                prompt=PROMPT,
                max_tokens=1024,
                expected_plan=report["plan_token"],
                operator_id=str(uuid.uuid4()),
            )
        assert await snapshot(sandbox.db) == before
        assert await sandbox.db.fetchval("SELECT content FROM provider_thread_messages") == content
        assert await sandbox.db.fetchval("SELECT count(*) FROM inference_turn_reconciliations") == 0


@pytest.mark.parametrize("status", ["processing", "failed"])
async def test_reconcile_unknown_remote_outcome_never_retries_or_changes_balance(
    database_factory,
    monkeypatch,
    status,
):
    async with database_factory(through=42) as sandbox:
        scope = await settled_pending(sandbox, monkeypatch)
        await sandbox.db.execute("UPDATE inference_requests SET status=$1", status)
        before = await snapshot(sandbox.db)
        report = await inspect(sandbox, scope)
        assert report["reason"] == "UNCONFIRMED_PROVIDER_OUTCOME"
        assert report["status"] == "blocked" and not report["can_restore"]
        with pytest.raises(ReconciliationError):
            await restore_turn(
                sandbox.db,
                **scope,
                prompt=PROMPT,
                max_tokens=1024,
                expected_plan="0" * 64,
                operator_id=str(uuid.uuid4()),
            )
        assert await snapshot(sandbox.db) == before


async def test_reconcile_changed_plan_or_prompt_refused(database_factory, monkeypatch):
    async with database_factory(through=42) as sandbox:
        scope = await settled_pending(sandbox, monkeypatch)
        before = await snapshot(sandbox.db)
        report = await inspect(sandbox, scope)
        for prompt, token in ((PROMPT, "0" * 64), ("Forged prompt", report["plan_token"])):
            with pytest.raises(ReconciliationError):
                await restore_turn(
                    sandbox.db,
                    **scope,
                    prompt=prompt,
                    max_tokens=1024,
                    expected_plan=token,
                    operator_id=str(uuid.uuid4()),
                )
        assert await snapshot(sandbox.db) == before
