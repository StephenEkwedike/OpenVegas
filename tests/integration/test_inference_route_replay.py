"""Migration-044 PostgreSQL replay/append atomicity, without provider or payment calls.

The replay helper, migrations, upload service, thread append and HTTP cache paths
are real. Terminal gateway rows are synthetic fixtures, not billing certification.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.inference import AIGateway
from server.services.attachment_history import references
from server.services.file_uploads import FileUploadService
from server.services.inference_replay import (
    InferenceReplayService,
    ReplayClaim,
    command_fingerprint,
)
from server.services.provider_threads import ProviderThreadService

pytestmark = pytest.mark.asyncio
MODEL = "fixture/route-replay-pg-v1"
CONTENT = b"Private synthetic upload bytes must not enter the replay envelope or message history."


@pytest_asyncio.fixture
async def replay_database(database_factory, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_COMPACTION_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_COMPACTION_TRIGGER_MESSAGES", "5")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_COMPACTION_KEEP_RECENT_MESSAGES", "2")
    monkeypatch.setenv("OPENVEGAS_CONTEXT_MAX_MESSAGES", "20")

    async def forbidden(*args, **kwargs):
        raise AssertionError("PostgreSQL replay tests must never call a provider")

    monkeypatch.setattr(AIGateway, "_route_to_provider", forbidden)
    async with database_factory(through=44, max_size=6) as sandbox:
        db = sandbox.db
        assert await db.fetchval("SELECT count(*) FROM schema_migrations") == 44
        assert await db.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE version='044_inference_route_commands'"
        ) == 1
        assert await db.fetchval("SELECT to_regclass('public.inference_route_commands') IS NOT NULL")
        owner, other = str(uuid4()), str(uuid4())
        for user in (owner, other):
            await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user)
        thread = await ProviderThreadService(db).prepare_thread(
            user_id=owner, provider="openrouter", model_id=MODEL,
            thread_id=None, conversation_mode="persistent",
        )
        files = FileUploadService(db)
        pending = await files.upload_init(
            user_id=owner, filename="private-fixture.txt", size_bytes=len(CONTENT),
            mime_type="text/plain", sha256_hex=hashlib.sha256(CONTENT).hexdigest(),
        )
        done = await files.upload_complete(
            user_id=owner, upload_id=pending["upload_id"],
            content_base64=base64.b64encode(CONTENT).decode("ascii"),
        )
        rows = await files.resolve_uploaded_for_inference(user_id=owner, file_ids=[done["file_id"]])
        yield SimpleNamespace(sandbox=sandbox, owner=owner, other=other, thread=thread,
                              refs=references(rows), key=str(uuid4()))


def command(ctx, **changes):
    return {
        "prompt": "Remember this owned upload.", "provider": "openrouter", "model": MODEL,
        "thread_id": ctx.thread.thread_id, "conversation_mode": "persistent", "persist_context": True,
        "enable_tools": False, "enable_web_search": False,
        "attachments": [ctx.refs[0]["file_id"]], "reasoning_effort": "high", **changes,
    }


def response(ctx, **changes):
    return {
        "text": "Visible answer.", "v_cost": "0.001234", "input_tokens": 19, "output_tokens": 7,
        "thread_id": ctx.thread.thread_id, "run_id": "original-route-run",
        "attachments_requested": True, "attachments_effective": True, "attachments_used": True,
        "warnings": [], "tool_calls": [], "diagnostics": {"history_used": 0}, **changes,
    }


async def begin(ctx, *, user=None, incoming=None):
    return await InferenceReplayService(ctx.sandbox.db).begin(
        user_id=user or ctx.owner, idempotency_key=ctx.key,
        command=command(ctx) if incoming is None else incoming,
    )


async def settled_gateway(ctx, claim, *, user=None, key=None, status="succeeded"):
    """Insert only a synthetic immutable gateway receipt, never a wallet charge."""
    request_id = str(uuid4())
    await ctx.sandbox.db.execute(
        "INSERT INTO inference_requests(id,user_id,idempotency_key,payload_hash,status,"
        "inference_source,wallet_funding_source,response_status,response_body_text,"
        "final_charge_v,final_provider_cost_usd,provider_request_id) "
        "VALUES($1::uuid,$2::uuid,$3,$4,$5,'wrapper','external',200,$6,0.001234,0.0001,'synthetic-no-dispatch')",
        request_id, user or claim.user_id, key or claim.gateway_idempotency_key,
        "f" * 64, status, json.dumps({"text": "Synthetic settled gateway output."}),
    )
    return request_id


def append_callback(ctx, *, answer="Visible answer.", kind="visible_text", called=None):
    async def append(tx):
        if called is not None:
            called.append(await tx.fetchval("SELECT txid_current()"))
        await ProviderThreadService(ctx.sandbox.db).append_exchange(
            thread_ctx=ctx.thread, prompt=command(ctx)["prompt"], response_text=answer,
            input_tokens=19, output_tokens=7, persist_context=True,
            attachment_refs=ctx.refs, assistant_kind=kind, tx=tx,
        )
    return append


async def history(ctx):
    return [dict(row) for row in await ctx.sandbox.db.fetch(
        "SELECT id,role,content,created_at FROM provider_thread_messages "
        "WHERE thread_id=$1::uuid ORDER BY created_at,id", ctx.thread.thread_id,
    )]


async def envelope(ctx, claim):
    return dict(await ctx.sandbox.db.fetchrow(
        "SELECT * FROM inference_route_commands WHERE id=$1::uuid AND user_id=$2::uuid",
        claim.request_id, claim.user_id,
    ))


async def test_concurrent_begin_has_one_owner_and_no_billable_envelope_rows(replay_database):
    ctx = replay_database
    outcomes = await asyncio.wait_for(asyncio.gather(
        *(begin(ctx) for _ in range(6)), return_exceptions=True,
    ), timeout=10)
    claims = [outcome for outcome in outcomes if isinstance(outcome, ReplayClaim)]
    assert len(claims) == 1 and claims[0].response is None
    rejected = [outcome for outcome in outcomes if isinstance(outcome, ContractError)]
    assert len(rejected) == 5 and all(exc.code == APIErrorCode.HOLD_CONFLICT for exc in rejected)
    db = await ctx.sandbox.reconnect()
    assert await db.fetchval("SELECT count(*) FROM inference_route_commands") == 1
    assert await db.fetchval("SELECT count(*) FROM inference_requests") == 0
    assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0
    row = await envelope(ctx, claims[0])
    assert row["status"] == "processing" and row["response_status"] is None
    assert row["payload_hash"] == command_fingerprint(command(ctx))
    assert await history(ctx) == []


@pytest.mark.parametrize("change", [
    {"reasoning_effort": "low"}, {"prompt": "Different request"}, {"attachments": []},
])
async def test_same_key_changed_command_conflicts_without_mutating_claim(replay_database, change):
    ctx = replay_database
    first = await begin(ctx)
    before = await envelope(ctx, first)
    with pytest.raises(ContractError) as error:
        await begin(ctx, incoming=command(ctx, **change))
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert await envelope(ctx, first) == before
    assert await history(ctx) == []


async def test_completion_cache_survives_reconnect_expiry_and_appends_only_once(replay_database, monkeypatch):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    original = response(ctx)
    callbacks = []
    actual = await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=gateway_id, response=original,
        append=append_callback(ctx, called=callbacks),
    )
    assert actual == original and len(callbacks) == 1
    db = await ctx.sandbox.reconnect()
    await db.execute("UPDATE chat_file_uploads SET expires_at=now()-interval '1 hour'")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    cached = await begin(ctx)
    assert cached.response == original
    copy_of_response = cached.response
    copy_of_response["diagnostics"]["history_used"] = 999
    assert cached.response == original
    forbidden = AsyncMock(side_effect=AssertionError("Cached command appended twice"))
    again = await InferenceReplayService(db).complete(
        cached, gateway_request_id=gateway_id, response=response(ctx, run_id="replacement"), append=forbidden,
    )
    assert again == original
    forbidden.assert_not_awaited()
    records = await history(ctx)
    assert sorted(row["role"] for row in records) == ["assistant", "user"]
    stored = await envelope(ctx, cached)
    assert stored["status"] == "succeeded" and stored["response_status"] == 200
    assert await db.fetchval("SELECT count(*) FROM inference_requests") == 1
    assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0
    assert CONTENT.decode() not in json.dumps([records, stored], default=str)
    assert base64.b64encode(CONTENT).decode() not in json.dumps([records, stored], default=str)


async def test_concurrent_completions_share_one_append_and_original_response(replay_database):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    callbacks = []
    outcomes = await asyncio.wait_for(asyncio.gather(*(
        InferenceReplayService(ctx.sandbox.db).complete(
            claim, gateway_request_id=gateway_id, response=response(ctx, run_id=f"racer-{index}"),
            append=append_callback(ctx, called=callbacks),
        ) for index in range(4)
    )), timeout=10)
    assert all(item == outcomes[0] for item in outcomes)
    assert len(callbacks) == 1 and len(await history(ctx)) == 2
    assert (await begin(ctx)).response == outcomes[0]


@pytest.mark.parametrize("failure", ["callback", "cancel", "envelope_update"])
async def test_complete_failure_rolls_back_history_timestamp_and_envelope(replay_database, failure):
    import asyncpg

    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    db = ctx.sandbox.db
    before = await envelope(ctx, claim)
    timestamp = await db.fetchval("SELECT updated_at FROM provider_threads WHERE id=$1::uuid", ctx.thread.thread_id)
    if failure == "envelope_update":
        await db.execute("""
            CREATE FUNCTION replay_test_reject_completion() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic replay completion failure' USING ERRCODE='23514'; END $$;
            CREATE TRIGGER replay_test_reject_completion BEFORE UPDATE ON inference_route_commands
            FOR EACH ROW EXECUTE FUNCTION replay_test_reject_completion();
        """)

    async def broken(tx):
        await append_callback(ctx)(tx)
        if failure == "callback":
            raise RuntimeError("Synthetic callback failure after history insert")
        if failure == "cancel":
            raise asyncio.CancelledError()

    with pytest.raises((RuntimeError, asyncio.CancelledError, asyncpg.CheckViolationError)):
        await InferenceReplayService(db).complete(
            claim, gateway_request_id=gateway_id, response=response(ctx), append=broken,
        )
    db = await ctx.sandbox.reconnect()
    assert await history(ctx) == []
    assert await envelope(ctx, claim) == before
    assert await db.fetchval("SELECT updated_at FROM provider_threads WHERE id=$1::uuid", ctx.thread.thread_id) == timestamp
    assert await db.fetchval("SELECT status FROM inference_requests WHERE id=$1::uuid", gateway_id) == "succeeded"
    with pytest.raises(ContractError) as blocked:
        await begin(ctx)
    assert blocked.value.code == APIErrorCode.HOLD_CONFLICT
    if failure == "envelope_update":
        await db.execute("DROP TRIGGER replay_test_reject_completion ON inference_route_commands")
    # The original owner may finish its existing claim; this is not another model call.
    await InferenceReplayService(db).complete(
        claim, gateway_request_id=gateway_id, response=response(ctx), append=append_callback(ctx),
    )
    assert len(await history(ctx)) == 2
    assert await db.fetchval("SELECT count(*) FROM inference_requests") == 1


@pytest.mark.parametrize("failure", ["foreign_gateway", "wrong_gateway_key", "processing", "failed", "owner_token"])
async def test_foreign_unsettled_or_unowned_completion_never_appends(replay_database, failure):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(
        ctx, claim, user=ctx.other if failure == "foreign_gateway" else None,
        key="wrong-derived-key" if failure == "wrong_gateway_key" else None,
        status=failure if failure in {"processing", "failed"} else "succeeded",
    )
    before = await envelope(ctx, claim)
    candidate = replace(claim, owner_token=str(uuid4())) if failure == "owner_token" else claim
    callback = AsyncMock(side_effect=AssertionError("Rejected completion reached append"))
    with pytest.raises(ContractError):
        await InferenceReplayService(ctx.sandbox.db).complete(
            candidate, gateway_request_id=gateway_id, response=response(ctx), append=callback,
        )
    callback.assert_not_awaited()
    assert await history(ctx) == [] and await envelope(ctx, claim) == before


async def test_same_key_is_scoped_to_authenticated_user_and_cannot_read_foreign_cache(replay_database):
    ctx = replay_database
    first, second = await begin(ctx), await begin(ctx, user=ctx.other)
    assert first.request_id != second.request_id
    assert first.gateway_idempotency_key != second.gateway_idempotency_key
    assert second.response is None
    first_gateway = await settled_gateway(ctx, first)
    await InferenceReplayService(ctx.sandbox.db).complete(
        first, gateway_request_id=first_gateway, response=response(ctx, text="Owner-only cached answer"),
        append=append_callback(ctx, answer="Owner-only cached answer"),
    )
    with pytest.raises(ContractError) as pending:
        await begin(ctx, user=ctx.other)
    assert pending.value.code == APIErrorCode.HOLD_CONFLICT
    callback = AsyncMock(side_effect=AssertionError("Cross-user completion reached append"))
    with pytest.raises(ContractError):
        await InferenceReplayService(ctx.sandbox.db).complete(
            replace(first, user_id=ctx.other), gateway_request_id=first_gateway,
            response=response(ctx), append=callback,
        )
    callback.assert_not_awaited()
    assert (await begin(ctx)).response["text"] == "Owner-only cached answer"
    assert (await envelope(ctx, second))["status"] == "processing"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_route_envelopes_are_private_even_for_claimed_owner_jwt(replay_database, role):
    import asyncpg

    ctx = replay_database
    claim = await begin(ctx)
    db = ctx.sandbox.db
    assert await db.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid='inference_route_commands'::regclass")
    assert await db.fetchval("SELECT count(*) FROM pg_policies WHERE tablename='inference_route_commands'") == 0
    await db.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    operations = {
        "SELECT": "SELECT response_body_text FROM inference_route_commands",
        "INSERT": "INSERT INTO inference_route_commands SELECT * FROM inference_route_commands",
        "UPDATE": "UPDATE inference_route_commands SET updated_at=now()",
        "DELETE": "DELETE FROM inference_route_commands",
    }
    for privilege, query in operations.items():
        assert not await db.fetchval("SELECT has_table_privilege($1,'inference_route_commands',$2)", role, privilege)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with db.transaction() as tx:
                await tx.execute(f"SET LOCAL ROLE {role}")
                await tx.execute("SELECT set_config('request.jwt.claim.sub',$1,true)", ctx.owner)
                await tx.fetch(query)
    assert (await envelope(ctx, claim))["status"] == "processing"


@pytest.mark.parametrize("text", ['{"arguments":"visible JSON, not a tool call"}',
                                  '```python\ndef tool_call():\n    return {"arguments": 1}\n```'])
async def test_visible_code_and_json_history_survive_atomic_completion_and_reconnect(replay_database, text):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=gateway_id, response=response(ctx, text=text),
        append=append_callback(ctx, answer=text, kind="visible_text"),
    )
    db = await ctx.sandbox.reconnect()
    messages, loaded, skipped = await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=ctx.thread.thread_id)
    assert (loaded, skipped) == (2, 0)
    by_role = {message["role"]: message for message in messages}
    assert by_role["assistant"] == {"role": "assistant", "content": text}
    assert by_role["user"]["attachment_refs"] == ctx.refs
    assert (await begin(ctx)).response["text"] == text


async def test_native_tool_history_blocks_continuation_without_erasing_cached_response(replay_database):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    original = response(ctx, text="", tool_calls=[{"tool_name": "Read", "provider_call_id": "synthetic-call"}])
    await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=gateway_id, response=original,
        append=append_callback(ctx, answer="", kind="native_tool_request"),
    )
    before = await history(ctx)
    db = await ctx.sandbox.reconnect()
    with pytest.raises(ContractError, match="native tool continuation"):
        await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=ctx.thread.thread_id)
    assert await history(ctx) == before
    assert (await begin(ctx)).response == original


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_http_cached_response_bypasses_history_upload_and_provider_after_reconnect(replay_database, monkeypatch, endpoint):
    from server.routes import inference as routes

    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    original = response(ctx)
    await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=gateway_id, response=original, append=append_callback(ctx),
    )
    db = await ctx.sandbox.reconnect()
    await db.execute("UPDATE chat_file_uploads SET expires_at=now()-interval '1 hour'")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    monkeypatch.setattr(routes, "get_inference_replay_service", lambda: InferenceReplayService(db))
    monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock()))

    def forbidden():
        raise AssertionError("Cached response re-entered history, upload, catalog, or inference")

    for name in ("get_provider_thread_service", "get_catalog", "get_gateway", "get_file_upload_service", "get_llm_mode_service"):
        monkeypatch.setattr(routes, name, forbidden)
    app = FastAPI()
    app.include_router(routes.router, prefix="/inference")
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": ctx.owner}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        result = await client.post(f"/inference/{endpoint}", json={**command(ctx), "idempotency_key": ctx.key})
    assert result.status_code == 200, result.text
    if endpoint == "ask":
        assert result.json() == original
    else:
        events = [json.loads(line.removeprefix("data: ")) for line in result.text.splitlines() if line.startswith("data: ")]
        completed = [event["payload"] for event in events if event["type"] == "response.completed"]
        assert len(completed) == 1 and completed[0]["text"] == original["text"]
        assert completed[0]["v_cost"] == original["v_cost"]
    assert len(await history(ctx)) == 2
    assert await db.fetchval("SELECT count(*) FROM inference_requests") == 1


@pytest.mark.parametrize("status, response_status", [
    ("succeeded", None), ("succeeded", 201), ("processing", 200),
])
async def test_migration_rejects_inconsistent_terminal_status(replay_database, status, response_status):
    import asyncpg

    ctx = replay_database
    claim = await begin(ctx)
    before = await envelope(ctx, claim)
    with pytest.raises(asyncpg.CheckViolationError):
        await ctx.sandbox.db.execute(
            "UPDATE inference_route_commands SET status=$2,response_status=$3 WHERE id=$1::uuid",
            claim.request_id, status, response_status,
        )
    await ctx.sandbox.reconnect()
    assert await envelope(ctx, claim) == before


async def test_migration_44_passes_current_schema_readiness(replay_database):
    from server.services.dependencies import assert_schema_compatible, current_flags

    await assert_schema_compatible(replay_database.sandbox.db, current_flags())


async def test_known_pre_dispatch_abandon_releases_only_original_unstarted_claim(replay_database):
    ctx = replay_database
    service = InferenceReplayService(ctx.sandbox.db)
    first = await begin(ctx)
    assert await service.abandon_before_dispatch(first) is True
    assert await service.abandon_before_dispatch(first) is False
    await ctx.sandbox.reconnect()
    second = await begin(ctx)
    assert second.request_id != first.request_id and second.owner_token != first.owner_token
    assert second.gateway_idempotency_key == first.gateway_idempotency_key
    with pytest.raises(ContractError):
        await InferenceReplayService(ctx.sandbox.db).abandon_before_dispatch(first)
    assert (await envelope(ctx, second))["status"] == "processing"
    assert await ctx.sandbox.db.fetchval("SELECT count(*) FROM inference_requests") == 0
    assert await history(ctx) == []


@pytest.mark.parametrize("key_kind", ["raw", "derived"])
@pytest.mark.parametrize("status", ["processing", "failed", "succeeded"])
async def test_gateway_receipt_blocks_abandon_and_orphan_reclaim(replay_database, key_kind, status):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(
        ctx, claim, key=ctx.key if key_kind == "raw" else claim.gateway_idempotency_key, status=status,
    )
    before = await envelope(ctx, claim)
    with pytest.raises(ContractError) as blocked:
        await InferenceReplayService(ctx.sandbox.db).abandon_before_dispatch(claim)
    assert blocked.value.code == APIErrorCode.HOLD_CONFLICT
    assert await envelope(ctx, claim) == before
    # Simulate a legacy raw-key request or an orphaned derived receipt. This is
    # test setup, not an allowed recovery operation on real settled requests.
    await ctx.sandbox.db.execute("DELETE FROM inference_route_commands WHERE id=$1::uuid", claim.request_id)
    await ctx.sandbox.reconnect()
    with pytest.raises(ContractError) as collision:
        await begin(ctx)
    assert collision.value.code == APIErrorCode.HOLD_CONFLICT
    assert await ctx.sandbox.db.fetchval("SELECT count(*) FROM inference_route_commands") == 0
    assert await ctx.sandbox.db.fetchval("SELECT status FROM inference_requests WHERE id=$1::uuid", gateway_id) == status
    assert await history(ctx) == []


@pytest.mark.parametrize("field", ["owner_token", "user_id"])
async def test_abandon_rejects_foreign_owner_without_mutation(replay_database, field):
    ctx = replay_database
    first, other = await begin(ctx), await begin(ctx, user=ctx.other)
    candidate = replace(first, **{field: ctx.other if field == "user_id" else str(uuid4())})
    before = [await envelope(ctx, claim) for claim in (first, other)]
    with pytest.raises(ContractError):
        await InferenceReplayService(ctx.sandbox.db).abandon_before_dispatch(candidate)
    assert [await envelope(ctx, claim) for claim in (first, other)] == before


async def test_completed_envelope_is_not_abandonable(replay_database):
    ctx = replay_database
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=gateway_id, response=response(ctx), append=append_callback(ctx),
    )
    before = await envelope(ctx, claim)
    with pytest.raises(ContractError):
        await InferenceReplayService(ctx.sandbox.db).abandon_before_dispatch(claim)
    assert await envelope(ctx, claim) == before
    assert (await begin(ctx)).response == response(ctx)
    assert len(await history(ctx)) == 2


async def test_atomic_exchange_order_is_not_determined_by_random_message_ids(replay_database):
    ctx = replay_database
    # Make the normally random tie-breaker deterministic: the assistant UUID
    # sorts before the user UUID. Real insertion/retrieval must preserve order.
    await ctx.sandbox.db.execute("""
        CREATE SEQUENCE replay_test_message_ids;
        ALTER TABLE provider_thread_messages ALTER COLUMN id SET DEFAULT
            (lpad(to_hex(100-nextval('replay_test_message_ids')),32,'0'))::uuid;
    """)
    claim = await begin(ctx)
    gateway_id = await settled_gateway(ctx, claim)
    await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=gateway_id, response=response(ctx), append=append_callback(ctx),
    )
    db = await ctx.sandbox.reconnect()
    messages, loaded, skipped = await ProviderThreadService(db).get_recent_messages_with_stats(
        thread_id=ctx.thread.thread_id,
    )
    assert (loaded, skipped) == (2, 0)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["attachment_refs"] == ctx.refs
    assert messages[1]["content"] == response(ctx)["text"]


async def test_concurrent_commands_append_contiguous_ordered_pairs_on_one_thread(replay_database):
    ctx = replay_database
    turns = [SimpleNamespace(**{**vars(ctx), "key": str(uuid4())}) for _ in range(2)]
    claims = [await begin(turn, incoming=command(turn, prompt=f"user-{index}"))
              for index, turn in enumerate(turns)]
    receipts = [await settled_gateway(ctx, claim) for claim in claims]
    entered = [asyncio.Event(), asyncio.Event()]
    callbacks = []

    async def complete(index):
        async def append(tx):
            callbacks.append(index)
            entered[index].set()
            await ProviderThreadService(ctx.sandbox.db).append_exchange(
                thread_ctx=ctx.thread, prompt=f"user-{index}", response_text=f"answer-{index}",
                input_tokens=19, output_tokens=7, persist_context=True,
                attachment_refs=ctx.refs, assistant_kind="visible_text", tx=tx,
            )
        return await InferenceReplayService(ctx.sandbox.db).complete(
            claims[index], gateway_request_id=receipts[index],
            response=response(ctx, text=f"answer-{index}"), append=append,
        )

    tasks = []
    try:
        # Both independent route transactions enter append while this thread is
        # locked, so completion order must come from the real thread serialization.
        async with ctx.sandbox.db.transaction() as blocker:
            await blocker.execute("SELECT id FROM provider_threads WHERE id=$1::uuid FOR UPDATE", ctx.thread.thread_id)
            tasks = [asyncio.create_task(complete(index)) for index in range(2)]
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=4)
            assert not any(task.done() for task in tasks)
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert sorted(callbacks) == [0, 1]
    assert [result["text"] for result in results] == ["answer-0", "answer-1"]
    db = await ctx.sandbox.reconnect()
    rows = await history(ctx)
    assert len(rows) == 4
    assert all(left["created_at"] < right["created_at"] for left, right in pairwise(rows))
    messages, loaded, skipped = await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=ctx.thread.thread_id)
    assert (loaded, skipped) == (4, 0)
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert {(messages[index]["content"], messages[index + 1]["content"]) for index in (0, 2)} == {
        ("user-0", "answer-0"), ("user-1", "answer-1"),
    }
    for index, turn in enumerate(turns):
        assert (await begin(turn, incoming=command(turn, prompt=f"user-{index}"))).response == results[index]
    assert len(await history(ctx)) == 4


async def test_append_clock_behind_retained_watermark_keeps_strict_thread_order(replay_database):
    from datetime import timedelta

    ctx = replay_database
    await ProviderThreadService(ctx.sandbox.db).append_exchange(
        thread_ctx=ctx.thread, prompt="prior-user", response_text="prior-answer",
        input_tokens=3, output_tokens=2, persist_context=True,
        attachment_refs=ctx.refs, assistant_kind="visible_text",
    )
    # Simulate a backwards wall-clock step without changing the machine clock:
    # the previously committed thread watermark is ahead of PostgreSQL's clock.
    await ctx.sandbox.db.execute("""
        UPDATE provider_thread_messages SET created_at=statement_timestamp()+interval '1 day'
            +CASE WHEN role='assistant' THEN interval '1 microsecond' ELSE interval '0' END
        WHERE thread_id=$1::uuid
    """, ctx.thread.thread_id)
    previous = await history(ctx)
    assert previous[-1]["created_at"] > await ctx.sandbox.db.fetchval("SELECT clock_timestamp()")
    claim = await begin(ctx)
    receipt = await settled_gateway(ctx, claim)
    await InferenceReplayService(ctx.sandbox.db).complete(
        claim, gateway_request_id=receipt, response=response(ctx), append=append_callback(ctx),
    )
    db = await ctx.sandbox.reconnect()
    rows = await history(ctx)
    assert len(rows) == 4 and rows[:2] == previous
    assert all(left["created_at"] < right["created_at"] for left, right in pairwise(rows))
    assert rows[3]["created_at"] - rows[2]["created_at"] == timedelta(microseconds=1)
    messages, loaded, skipped = await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=ctx.thread.thread_id)
    assert (loaded, skipped) == (4, 0)
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "prior-user"), ("assistant", "prior-answer"),
        ("user", command(ctx)["prompt"]), ("assistant", response(ctx)["text"]),
    ]
    assert (await begin(ctx)).response == response(ctx)
