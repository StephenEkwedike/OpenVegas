"""Real PostgreSQL attachment retention and reasoning recovery, no paid transports.

Uses the guarded loopback-only, empty ov_test_* database fixture and all 43
migrations. Auth identities, files, model reviews and provider responses are
synthetic; backend ownership, SQL transactions, wallet settlement and recovery
are real. This does not certify Supabase Auth or live provider behavior.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from openvegas.contracts.errors import ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.conversation import CanonicalConversation
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.gateway.reconciliation import (
    ReconciliationError, inspect_turn, request_payload_hash, restore_turn,
)
from openvegas.wallet.ledger import WalletService
from server.services.attachment_history import references, resolve_retained
from server.services.file_uploads import FileUploadError, FileUploadService
from server.services.provider_threads import ProviderThreadService

pytestmark = pytest.mark.asyncio
MODEL = "fixture/attachment-reasoning-v1"
CONTENT = b"Synthetic private file bytes are retained only in the owned upload table."
PROMPT = "Remember the amber fixture."


@pytest_asyncio.fixture
async def local_database(database_factory, monkeypatch):
    for key, value in {
        "OPENVEGAS_CONTEXT_ENABLED": "1", "OPENVEGAS_MODEL_SWITCH_ENABLED": "1",
        "OPENVEGAS_CONTEXT_MAX_MESSAGES": "20", "OPENVEGAS_CONTEXT_COMPACTION_ENABLED": "1",
        "OPENVEGAS_CONTEXT_COMPACTION_TRIGGER_MESSAGES": "5",
        "OPENVEGAS_CONTEXT_COMPACTION_KEEP_RECENT_MESSAGES": "2",
        "ATTACHMENT_REASONING_FIXTURE_KEY": "synthetic-never-sent",
    }.items():
        monkeypatch.setenv(key, value)
    now = datetime.now(UTC)
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({f"openrouter:{MODEL}": {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "account_access": True, "completion_chat": True, "context_window_tokens": 100000,
        "max_tokens": 1024, "cost_input_per_1m": "1", "cost_output_per_1m": "2",
        "capabilities": {"reasoning_efforts": ["low", "high", "xhigh"]},
        "supported_parameters": ["max_tokens", "reasoning"],
    }}))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Real provider transports are forbidden in this database suite")

    for method in ("_call_openai", "_call_anthropic", "_call_gemini", "_call_mistral", "_call_openrouter"):
        monkeypatch.setattr(AIGateway, method, forbidden)
    async with database_factory(through=43, max_size=4) as sandbox:
        db = sandbox.db
        assert await db.fetchval("SELECT count(*) FROM schema_migrations") == 43
        assert await db.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE version='043_stripe_emote_adjustments'"
        ) == 1
        owner, other = str(uuid.uuid4()), str(uuid.uuid4())
        for user in (owner, other):
            await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user)
        await db.execute(
            "INSERT INTO provider_catalog(provider,model_id,display_name,max_tokens,"
            "cost_input_per_1m,cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m) "
            "VALUES ('openrouter',$1,'Synthetic local fixture',1024,1,2,100,200)", MODEL,
        )
        await db.execute(
            "INSERT INTO provider_credentials(provider,env,key_alias,key_version,status) "
            "VALUES ('openrouter','test','ATTACHMENT_REASONING_FIXTURE_KEY','test','active')"
        )
        yield SimpleNamespace(sandbox=sandbox, owner=owner, other=other)


async def thread_and_file(ctx):
    db = ctx.sandbox.db
    service, uploads = ProviderThreadService(db), FileUploadService(db)
    thread = await service.prepare_thread(user_id=ctx.owner, provider="openrouter", model_id=MODEL,
                                          thread_id=None, conversation_mode="persistent")
    pending = await uploads.upload_init(user_id=ctx.owner, filename="fixture.txt", size_bytes=len(CONTENT),
                                        mime_type="text/plain", sha256_hex=hashlib.sha256(CONTENT).hexdigest())
    completed = await uploads.upload_complete(user_id=ctx.owner, upload_id=pending["upload_id"],
                                              content_base64=base64.b64encode(CONTENT).decode())
    rows = await uploads.resolve_uploaded_for_inference(user_id=ctx.owner, file_ids=[completed["file_id"]])
    return service, thread, references(rows)


async def append(service, thread, *, refs=None, prompt="hello", answer="answer"):
    await service.append_exchange(thread_ctx=thread, prompt=prompt, response_text=answer,
                                  input_tokens=3, output_tokens=2, persist_context=True,
                                  attachment_refs=refs)


async def stored(db, thread):
    return [dict(row) for row in await db.fetch(
        "SELECT id,role,content,created_at FROM provider_thread_messages WHERE thread_id=$1::uuid ORDER BY id",
        thread.thread_id,
    )]


def inference_app(ctx, monkeypatch, user):
    from server.routes import inference as routes
    db = ctx.sandbox.db
    gateway = SimpleNamespace(infer=AsyncMock(side_effect=AssertionError("Rejected request reached inference")))
    monkeypatch.setattr(routes, "get_catalog", lambda: ProviderCatalog(db))
    monkeypatch.setattr(routes, "get_provider_thread_service", lambda: ProviderThreadService(db))
    monkeypatch.setattr(routes, "get_file_upload_service", lambda: FileUploadService(db))
    monkeypatch.setattr(routes, "get_gateway", lambda: gateway)
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock()))
    monkeypatch.setattr(routes, "get_llm_mode_service", lambda: SimpleNamespace(resolve_for_user=AsyncMock(
        return_value={"effective_mode": "wrapper", "conversation_mode": "persistent"})))
    monkeypatch.setattr(routes, "emit_metric", lambda *a, **k: None)
    monkeypatch.setattr(routes, "emit_run_metrics", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/inference")
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": user}
    return app, gateway


async def test_attachment_exchange_skips_compaction_and_followup_retains_after_reconnect(local_database):
    ctx = local_database
    service, thread, refs = await thread_and_file(ctx)
    await append(service, thread, refs=refs, prompt="")
    for index in range(4):
        await append(service, thread, prompt=f"Followup {index}")
    db = await ctx.sandbox.reconnect()
    records = await stored(db, thread)
    assert len(records) == 10  # Above the enabled five-message compaction trigger.
    assert "conversation_summary_v1" not in json.dumps(records, default=str)
    assert CONTENT.decode() not in json.dumps(records, default=str)
    assert base64.b64encode(CONTENT).decode() not in json.dumps(records, default=str)
    service = ProviderThreadService(db)
    retained, loaded, skipped = await service.get_recent_messages_with_stats(thread_id=thread.thread_id)
    assert loaded == 10 and skipped == 0
    attached = [m for m in retained if "attachment_refs" in m]
    assert attached == [{"role": "user", "content": "", "attachment_refs": refs}]
    resolved = await resolve_retained(refs, user_id=ctx.owner, file_service=FileUploadService(db))
    assert resolved[0]["content_bytes"] == CONTENT
    assert "content_bytes" not in json.dumps(retained)
    await append(service, thread, prompt="Followup after reconnect")
    assert len(await stored(db, thread)) == 12


async def test_old_refs_outside_limit_block_service_and_route_without_dropping(local_database, monkeypatch):
    ctx = local_database
    service, thread, refs = await thread_and_file(ctx)
    await append(service, thread, refs=refs)
    for index in range(11):
        await append(service, thread, prompt=f"Newer {index}")
    db = await ctx.sandbox.reconnect()
    before = await stored(db, thread)
    assert len(before) == 24
    assert not await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM (SELECT content FROM provider_thread_messages "
        "WHERE thread_id=$1::uuid ORDER BY created_at DESC,id DESC LIMIT 21) recent "
        "WHERE content ? 'attachment_refs')", thread.thread_id,
    )
    with pytest.raises(ContractError, match="No files were dropped"):
        await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=thread.thread_id, limit=20)
    app, gateway = inference_app(ctx, monkeypatch, ctx.owner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/inference/ask", json={"provider": "openrouter", "model": MODEL,
                                    "prompt": "Continue", "thread_id": thread.thread_id})
    assert response.status_code == 400 and "No files were dropped" in response.text
    gateway.infer.assert_not_awaited()
    assert await stored(db, thread) == before
    assert await db.fetchval("SELECT count(*) FROM inference_requests") == 0


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "bytes", "digest", "uuid", "oversized"])
async def test_invalid_reference_metadata_cannot_partially_persist(local_database, mutation):
    ctx = local_database
    service, thread, refs = await thread_and_file(ctx)
    await append(service, thread, refs=refs)
    malformed = {
        "empty": [], "duplicate": refs * 2, "bytes": [{**refs[0], "content_bytes": CONTENT.decode()}],
        "digest": [{**refs[0], "sha256": "not-a-sha"}], "uuid": [{**refs[0], "file_id": "../foreign"}],
        "oversized": [{"file_id": str(uuid.uuid4()), "sha256": refs[0]["sha256"]} for _ in range(9)],
    }[mutation]
    db = ctx.sandbox.db
    before = await stored(db, thread)
    updated = await db.fetchval("SELECT updated_at FROM provider_threads WHERE id=$1::uuid", thread.thread_id)
    with pytest.raises(ContractError):
        await append(service, thread, refs=malformed, prompt="Must not persist")
    db = await ctx.sandbox.reconnect()
    assert await stored(db, thread) == before
    assert await db.fetchval("SELECT updated_at FROM provider_threads WHERE id=$1::uuid", thread.thread_id) == updated


async def test_append_transaction_rolls_back_both_messages_when_thread_update_fails(local_database):
    import asyncpg

    ctx = local_database
    service, thread, refs = await thread_and_file(ctx)
    db = ctx.sandbox.db
    await db.execute("""
        CREATE FUNCTION sidecar_reject_thread_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic transaction failure' USING ERRCODE='23514'; END $$;
        CREATE TRIGGER sidecar_thread_update BEFORE UPDATE ON provider_threads
        FOR EACH ROW EXECUTE FUNCTION sidecar_reject_thread_update();
    """)
    with pytest.raises(asyncpg.CheckViolationError):
        await append(service, thread, refs=refs)
    db = await ctx.sandbox.reconnect()
    assert await stored(db, thread) == []
    assert await db.fetchval("SELECT count(*) FROM chat_file_uploads WHERE status='uploaded'") == 1


@pytest.mark.parametrize("corruption", ["digest", "assistant_role"])
async def test_corrupt_persisted_refs_fail_closed_without_rewriting_history(local_database, corruption):
    ctx = local_database
    service, thread, refs = await thread_and_file(ctx)
    await append(service, thread, refs=refs)
    db = ctx.sandbox.db
    payload = {"text": "original", "attachment_refs": refs if corruption == "assistant_role" else [
        {**refs[0], "sha256": "broken"}]}
    await db.execute("UPDATE provider_thread_messages SET role=$2,content=$3::jsonb "
                     "WHERE thread_id=$1::uuid AND role='user'", thread.thread_id,
                     "assistant" if corruption == "assistant_role" else "user", json.dumps(payload))
    before = await stored(db, thread)
    db = await ctx.sandbox.reconnect()
    with pytest.raises(ContractError):
        await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=thread.thread_id)
    assert await stored(db, thread) == before


async def test_retained_file_ownership_digest_expiry_and_thread_route_scope(local_database, monkeypatch):
    ctx = local_database
    service, thread, refs = await thread_and_file(ctx)
    await append(service, thread, refs=refs)
    db = await ctx.sandbox.reconnect()
    uploads = FileUploadService(db)
    with pytest.raises(FileUploadError) as denied:
        await resolve_retained(refs, user_id=ctx.other, file_service=uploads)
    assert denied.value.status_code == 404
    with pytest.raises(ContractError):
        await ProviderThreadService(db).prepare_thread(user_id=ctx.other, provider="openrouter", model_id=MODEL,
                                                      thread_id=thread.thread_id, conversation_mode="persistent")
    app, gateway = inference_app(ctx, monkeypatch, ctx.other)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/inference/ask", json={"provider": "openrouter", "model": MODEL,
                                    "prompt": "Continue", "thread_id": thread.thread_id})
    assert response.status_code == 400
    gateway.infer.assert_not_awaited()
    assert CONTENT.decode() not in response.text
    await db.execute("UPDATE chat_file_uploads SET content_bytes=$2 WHERE id=$1::uuid", refs[0]["file_id"], b"changed")
    with pytest.raises(ContractError, match="changed"):
        await resolve_retained(refs, user_id=ctx.owner, file_service=uploads)
    await db.execute("UPDATE chat_file_uploads SET content_bytes=$2,expires_at=now()-interval '1 second' "
                     "WHERE id=$1::uuid", refs[0]["file_id"], CONTENT)
    with pytest.raises(FileUploadError):
        await resolve_retained(refs, user_id=ctx.owner, file_service=uploads)


async def test_upload_routes_cannot_choose_owner_or_expose_stored_bytes(local_database, monkeypatch):
    from server.routes import files
    ctx = local_database
    _, _, refs = await thread_and_file(ctx)
    monkeypatch.setattr(files, "get_file_upload_service", lambda: FileUploadService(ctx.sandbox.db))
    monkeypatch.setattr(files, "_files_feature_enabled", lambda: True)
    monkeypatch.setattr(files, "emit_metric", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[files.get_current_user] = lambda: {"user_id": ctx.other}
    payload = {"upload_id": refs[0]["file_id"], "content_base64": base64.b64encode(CONTENT).decode()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.post("/files/upload/complete", json=payload)).status_code == 404
        assert (await client.post("/files/upload/complete", json={**payload, "user_id": ctx.owner})).status_code == 422
        app.dependency_overrides[files.get_current_user] = lambda: {"user_id": ctx.owner}
        response = await client.post("/files/upload/complete", json=payload)
    assert response.status_code == 200 and response.json()["file_id"] == refs[0]["file_id"]
    assert CONTENT.decode() not in response.text and "content_bytes" not in response.text


@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_private_history_and_upload_bytes_have_no_browser_table_access(local_database, role):
    import asyncpg

    ctx = local_database
    _, _, _ = await thread_and_file(ctx)
    await ctx.sandbox.db.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    for table in ("provider_threads", "provider_thread_messages", "chat_file_uploads"):
        async with ctx.sandbox.pool.acquire() as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL ROLE {role}")
                    await conn.fetch(f"SELECT * FROM {table}")


async def financial_snapshot(db):
    return [list(map(dict, await db.fetch(query))) for query in (
        "SELECT * FROM inference_requests ORDER BY id", "SELECT * FROM inference_usage ORDER BY id",
        "SELECT * FROM inference_preauthorizations ORDER BY id", "SELECT * FROM ledger_entries ORDER BY id",
        "SELECT * FROM wallet_accounts ORDER BY account_id",
    )]


@pytest.mark.parametrize("effort", [None, "low", "xhigh"])
async def test_canonical_reasoning_hash_settlement_and_recovery_on_real_postgres(local_database, monkeypatch, effort):
    from server.routes import models
    from server.services import dependencies
    ctx, db = local_database, local_database.sandbox.db
    wallet, catalog, service = WalletService(db), ProviderCatalog(db), ProviderThreadService(db)
    await wallet.fund_from_card("user:" + ctx.owner, Decimal("100"), "local-fixture:" + ctx.owner)
    created = await service.create_canonical_thread(user_id=ctx.owner, provider="openrouter", model_id=MODEL, catalog=catalog)
    gateway = AIGateway(db, wallet, catalog)
    supplier = AsyncMock(return_value=InferenceResult("Remembered amber.", 10, 5, completion_status="complete"))
    monkeypatch.setattr(gateway, "_route_to_provider", supplier)
    monkeypatch.setattr(service, "append_canonical_exchange", AsyncMock(side_effect=RuntimeError("synthetic append failure")))
    monkeypatch.setattr(models, "get_catalog", lambda: catalog)
    for name, value in {
        "get_provider_thread_service": service, "get_gateway": gateway,
        "get_fraud_engine": SimpleNamespace(check_inference=AsyncMock()),
        "get_llm_mode_service": SimpleNamespace(resolve_for_user=AsyncMock(return_value={
            "effective_mode": "wrapper", "conversation_mode": "persistent"})),
    }.items():
        monkeypatch.setattr(dependencies, name, lambda value=value: value)
    app = FastAPI()
    app.include_router(models.router)
    app.dependency_overrides[models.get_current_user] = lambda: {"user_id": ctx.other}
    key = str(uuid.uuid4())
    payload = {"thread_id": created.thread_id, "expected_revision": created.revision, "provider": "openrouter",
               "model": MODEL, "prompt": PROMPT, "idempotency_key": key, "reasoning_effort": effort}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.post("/models/conversations/ask", json=payload)).status_code == 409
        supplier.assert_not_awaited()
        app.dependency_overrides[models.get_current_user] = lambda: {"user_id": ctx.owner}
        response = await client.post("/models/conversations/ask", json=payload)
        assert response.status_code == 200 and response.json()["continuity_blocked"]
        assert (await client.post("/models/conversations/ask", json=payload)).status_code == 409
    supplier.assert_awaited_once()
    req = supplier.call_args.args[0]
    assert req.reasoning_effort == effort
    request = await db.fetchrow("SELECT id,payload_hash FROM inference_requests WHERE idempotency_key=$1", key)
    expected = request_payload_hash(CanonicalConversation(), provider="openrouter", model=MODEL, prompt=PROMPT,
                                    max_tokens=1024, reasoning_effort=effort)
    assert request["payload_hash"] == expected == AIGateway._payload_hash(req)
    legacy = {"provider": "openrouter", "model": MODEL, "messages": [{"role": "user", "content": PROMPT}],
              "max_tokens": 1024, "enable_tools": False, "enable_web_search": False, "strict_continuity": True}
    old_hash = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert (expected == old_hash) is (effort is None)
    before = await financial_snapshot(db)
    conflicting = InferenceRequest("user:" + ctx.owner, "openrouter", MODEL, req.messages,
                                   strict_continuity=True, idempotency_key=key,
                                   reasoning_effort="high")
    with pytest.raises(ContractError):
        await gateway.infer(conflicting)
    supplier.assert_awaited_once()
    assert await financial_snapshot(db) == before
    # Recovery is historical verification, not current routing authorization.
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    scope = {"user_id": ctx.owner, "thread_id": created.thread_id, "request_id": str(request["id"])}
    async with ctx.sandbox.pool.acquire() as conn:
        with pytest.raises(ReconciliationError, match="ORIGINAL_REQUEST_HASH_MISMATCH"):
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                await inspect_turn(conn, **scope, prompt=PROMPT, max_tokens=1024, reasoning_effort="high")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            plan = await inspect_turn(conn, **scope, prompt=PROMPT, max_tokens=1024, reasoning_effort=effort)
    assert plan["can_restore"] and plan["settlement_verified"]
    restore_args = dict(scope, prompt=PROMPT, max_tokens=1024, operator_id=str(uuid.uuid4()),
                        expected_plan=plan["plan_token"], reasoning_effort=effort)
    with pytest.raises(ReconciliationError, match="ORIGINAL_REQUEST_HASH_MISMATCH"):
        await restore_turn(db, **{**restore_args, "reasoning_effort": "high"})
    assert await db.fetchval("SELECT count(*) FROM inference_turn_reconciliations") == 0
    assert (await restore_turn(db, **restore_args))["status"] == "restored"
    assert (await restore_turn(db, **restore_args))["status"] == "already_reconciled"
    db = await ctx.sandbox.reconnect()
    assert await financial_snapshot(db) == before
    assert await db.fetchval("SELECT count(*) FROM inference_turn_reconciliations") == 1
    history = await ProviderThreadService(db).canonical_history(user_id=ctx.owner, thread_id=created.thread_id,
                                                               provider="openrouter", model_id=MODEL)
    assert history.messages() == [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": "Remembered amber."}]
    assert "reasoning" not in history.to_json()
