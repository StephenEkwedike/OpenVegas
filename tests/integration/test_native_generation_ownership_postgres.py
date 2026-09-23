"""Migration 045 ownership, real route/gateway/wallet/locks, synthetic supplier.

Requires the guarded loopback-only disposable database_factory. No paid calls,
remote migrations, real credentials or provider certification.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.contracts.errors import ContractError
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import AIGateway, InferenceRequest
from openvegas.wallet.ledger import WalletService
from server.routes import inference as routes
from server.services.file_uploads import FileUploadService
from server.services.inference_replay import InferenceReplayService
from server.services.provider_threads import ProviderThreadService
from tests.integration.test_native_history_postgres import (
    new_run,
    projection,
    propose,
    seed_source,
    seed_user,
)
from tests.integration.test_openrouter_postgres import MODELS, provision

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def owned(database_factory, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_SCOPE", "1")
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_URL", "")
    async with database_factory(through=45, max_size=6) as sandbox:
        assert await sandbox.db.fetchval("SELECT count(*) FROM schema_migrations") == 45
        db = sandbox.db
        user = await provision(db, monkeypatch)
        wallet = WalletService(db)
        await wallet.fund_from_card("user:" + user, Decimal(100), "native-fixture:" + user)
        service = AgentOrchestrationService(db)
        run = await new_run(service, user)
        scope = {"run_id": run.run_id, "runtime_session_id": run.runtime_session_id,
                 **await projection(service, run)}
        command = {"prompt": "Read notes", "provider": "openrouter", "model": MODELS[0],
                   "enable_tools": True, "persist_context": False, "thread_id": None,
                   "conversation_mode": "ephemeral", "native_scope": scope}
        ctx = SimpleNamespace(sandbox=sandbox, db=db, user=user, run=run, service=service,
            scope=scope, command=command, key="first-native-generation", calls=[], emit_calls=False,
            release=None, entered=asyncio.Event(), fail_provider=False, provider_body=None)

        async def supplier(request):
            # Provider entry observes all committed ownership/hold records via a separate connection.
            row = await ctx.db.fetchrow("SELECT * FROM inference_route_commands WHERE native_run_id=$1::uuid", run.run_id)
            assert row is not None and row["gateway_request_id"] is not None
            source = await ctx.db.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid", row["gateway_request_id"])
            assert source["native_route_command_id"] == row["id"]
            assert source["user_id"] == row["user_id"]
            assert str(await ctx.db.fetchval("SELECT native_generation_claim_id FROM agent_runs WHERE id=$1::uuid", run.run_id)) == str(row["id"])
            assert await ctx.db.fetchval("SELECT status FROM inference_preauthorizations WHERE request_id=$1", str(source["id"])) == "reserved"
            payload = json.loads(request.content)
            ctx.calls.append(payload)
            ctx.entered.set()
            if ctx.release is not None:
                await ctx.release.wait()
            if ctx.fail_provider:
                raise httpx.ReadTimeout("Synthetic uncertain supplier response")
            if ctx.provider_body is not None:
                return httpx.Response(200, json=ctx.provider_body)
            message = {"role": "assistant", "content": "Synthetic native answer"}
            if ctx.emit_calls:
                message["tool_calls"] = [{"id": "call-" + str(i), "type": "function", "function": {
                    "name": "call_local_tool", "arguments": json.dumps({"tool_name": "Read",
                    "arguments": {"path": "notes.txt"}, "shell_mode": "read_only", "timeout_sec": 30})}} for i in range(2)]
            return httpx.Response(200, json={"id": "gen-native-local", "model": command["model"],
                "choices": [{"message": message, "finish_reason": "tool_calls" if ctx.emit_calls else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00002}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(supplier)) as http:
            ctx.gateway = AIGateway(db, wallet, ProviderCatalog(db), http_client=http)
            ctx.http = http
            ctx.replay = InferenceReplayService(db)
            monkeypatch.setattr(routes, "get_gateway", lambda: ctx.gateway)
            monkeypatch.setattr(routes, "get_catalog", lambda: ProviderCatalog(ctx.db))
            monkeypatch.setattr(routes, "get_inference_replay_service", lambda: ctx.replay)
            monkeypatch.setattr(routes, "get_provider_thread_service", lambda: ProviderThreadService(ctx.db))
            monkeypatch.setattr(routes, "get_file_upload_service", lambda: FileUploadService(ctx.db))
            monkeypatch.setattr(routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock()))
            monkeypatch.setattr(routes, "get_llm_mode_service", lambda: SimpleNamespace(resolve_for_user=AsyncMock(
                return_value={"effective_mode": "wrapper", "conversation_mode": "persistent"})))
            app = FastAPI()
            app.include_router(routes.router, prefix="/inference")
            ctx.auth_user = user
            app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": ctx.auth_user}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://native-test") as client:
                ctx.client = client
                yield ctx


async def post(ctx, endpoint="ask", **changes):
    return await ctx.client.post("/inference/" + endpoint,
                                 json={**ctx.command, "idempotency_key": ctx.key, **changes})


def payload(response, endpoint="ask"):
    assert response.status_code == 200, response.text
    if endpoint == "ask":
        assert "error" not in response.json(), response.text
        return response.json()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert not any(event["type"] == "response.error" for event in events), response.text
    return next(event["payload"] for event in events if event["type"] == "response.completed")


async def begin(ctx, **changes):
    return await ctx.replay.begin(user_id=ctx.user, idempotency_key=ctx.key,
                                  command={**ctx.command, **changes})


def request(ctx, claim):
    req = InferenceRequest("user:" + ctx.user, "openrouter", ctx.command["model"],
        [{"role": "user", "content": ctx.command["prompt"]}], enable_tools=True,
        idempotency_key=claim.gateway_idempotency_key)
    req._native_generation_claim = claim.native_claim
    return req


async def no_append(tx):
    pass


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_actual_route_gateway_commits_before_dispatch_and_replays_after_restart(owned, monkeypatch, endpoint):
    c = owned
    first = payload(await post(c, endpoint), endpoint)
    assert len(c.calls) == 1
    assert first["completion_status"] == "complete"
    assert first["native_generation"] == {"scope_version": 1, "run_id": c.run.run_id,
        "runtime_session_id": c.run.runtime_session_id,
        "inference_request_id": str(await c.db.fetchval("SELECT id FROM inference_requests")),
        "original_turn_scope_verified": True, "continuation_supported": False}
    assert first["thread_id"] is None and first["context_enabled"] is False
    assert await c.db.fetchval("SELECT count(*) FROM provider_thread_messages") == 0
    # New process/service pool, old projection, disabled feature and expired review.
    c.db = await c.sandbox.reconnect()
    c.replay = InferenceReplayService(c.db)
    c.gateway = AIGateway(c.db, WalletService(c.db), ProviderCatalog(c.db), http_client=c.http)
    await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", c.run.run_id)
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_SCOPE", "0")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    replay = payload(await post(c))
    assert replay["native_generation"] == first["native_generation"]
    assert replay["text"] == first["text"] and replay["completion_status"] == "complete"
    assert len(c.calls) == 1 and await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1


@pytest.mark.parametrize("bad", ["user", "session", "run", "version", "signature"])
async def test_owner_session_scope_and_projection_cannot_change(owned, bad):
    c = owned
    payload(await post(c))
    scope = dict(c.scope)
    if bad == "user":
        c.auth_user = await seed_user(c.db)
    else:
        field = {"session": "runtime_session_id", "run": "run_id", "version": "expected_run_version",
                 "signature": "expected_valid_actions_signature"}[bad]
        scope[field] = 900 if bad == "version" else "sha256:" + "0" * 64 if bad == "signature" else str(uuid4())
    response = await post(c, native_scope=scope)
    assert response.status_code == 409
    assert len(c.calls) == 1
    if bad == "user":
        c.auth_user = c.user
    assert (await post(c, native_scope=None)).status_code == 409


async def test_distinct_key_run_fence_is_serialized_by_real_run_lock(owned):
    c = owned
    async with c.db.transaction() as tx:
        await tx.fetchrow("SELECT id FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        tasks = [asyncio.create_task(c.replay.begin(user_id=c.user, idempotency_key="race-" + str(i), command=c.command)) for i in range(2)]
        await asyncio.sleep(0.05)
        assert all(not task.done() for task in tasks)
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 4)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ContractError) for result in results) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 1
    assert not c.calls


@pytest.mark.parametrize("change", ["runtime_session_id", "workspace_root", "workspace_fingerprint", "git_root"])
async def test_registration_is_immutable_from_claim_including_null_git_transition(owned, change):
    c = owned
    claim = await begin(c)
    args = {**c.run.scope(), "workspace_root": "/synthetic/native-workspace",
            "workspace_fingerprint": "sha256:" + "a" * 64, "git_root": None}
    await c.service.register_workspace(**args)
    args[change] = (str(uuid4()) if change == "runtime_session_id" else
                    "sha256:" + "b" * 64 if change == "workspace_fingerprint" else "/changed")
    with pytest.raises(ContractError, match="immutable"):
        await c.service.register_workspace(**args)
    assert await c.replay.abandon_before_dispatch(claim)
    await c.service.register_workspace(**args)
    assert await c.db.fetchval("SELECT native_generation_claim_id FROM agent_runs") is None


@pytest.mark.parametrize("state", ["cancel", "expired", "stale"])
async def test_lifecycle_rechecked_between_route_claim_and_reservation(owned, state):
    c = owned
    claim = await begin(c)
    sql = {"cancel": "cancel_requested_at=now()", "expired": "expires_at=now()-interval '1 second'",
           "stale": "version=version+1"}[state]
    await c.db.execute("UPDATE agent_runs SET " + sql + " WHERE id=$1::uuid", c.run.run_id)
    with pytest.raises(ContractError):
        await c.gateway.infer(request(c, claim))
    assert not c.calls
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 0


async def test_linked_scope_survives_uncertain_provider_failure_and_never_retries(owned):
    c = owned
    claim = await begin(c)
    req = request(c, claim)
    c.fail_provider = True
    with pytest.raises(ContractError):
        await c.gateway.infer(req)
    assert len(c.calls) == 1
    await c.db.execute("UPDATE inference_requests SET updated_at=now()-interval '1 day'")
    for operation in [lambda: c.gateway.infer(req), lambda: begin(c), lambda: c.replay.abandon_before_dispatch(claim)]:
        with pytest.raises(ContractError):
            await operation()
    req._native_generation_claim = None
    with pytest.raises(ContractError):
        await c.gateway.infer(req)
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT gateway_request_id IS NOT NULL FROM inference_route_commands")


async def test_account_scope_cannot_be_inferred_from_idempotency_key(owned):
    c = owned
    claim = await begin(c)
    req = request(c, claim)
    req.account_id = "agent:" + c.user
    with pytest.raises(ContractError):
        await c.gateway.infer(req)
    assert not c.calls


async def test_multicall_generation_cannot_split_runs_or_accept_unscoped_proposals(owned):
    c = owned
    c.emit_calls = True
    output = payload(await post(c))
    calls = output["tool_calls"]
    assert len(calls) == 2
    source = SimpleNamespace(request_id=output["native_generation"]["inference_request_id"], call=calls[0])
    first = await propose(c.service, c.run, source, idempotency_key="native-proposal")
    assert first.status_code == 200
    # Current projection can advance; exact proposal replay retains original identity.
    frozen = await projection(c.service, c.run)
    await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", c.run.run_id)
    replay = await propose(c.service, c.run, source, idempotency_key="native-proposal", **frozen)
    assert replay.payload == first.payload
    other = await new_run(c.service, c.user)
    source.call = calls[1]
    with pytest.raises(ContractError, match="another run"):
        await propose(c.service, other, source)
    with pytest.raises(ContractError, match="original scoped"):
        await propose(c.service, c.run, source, native_inference_request_id=None, native_provider_call_id=None)
    legacy = await seed_source(c.db, c.user)
    with pytest.raises(ContractError, match="original scope"):
        await propose(c.service, c.run, legacy)
    second = await propose(c.service, c.run, source)
    assert second.status_code == 200
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 2
    bindings = await c.db.fetch("SELECT content_json FROM agent_chat_turns")
    assert all(json.loads(row["content_json"])["generation_scope"] == {"scope_version": 1, **c.scope} for row in bindings)


class LostAck:
    def __init__(self, db, skip=0):
        self.db, self.armed, self.skip = db, True, skip

    @asynccontextmanager
    async def transaction(self):
        async with self.db.transaction() as tx:
            yield tx
        if self.skip:
            self.skip -= 1
            return
        if self.armed:
            self.armed = False
            raise ConnectionError("Synthetic lost commit acknowledgement")

    def __getattr__(self, name):
        return getattr(self.db, name)


@pytest.mark.parametrize("boundary", ["begin", "reservation", "settlement", "complete"])
async def test_lost_ack_never_reopens_generation(owned, boundary):
    c = owned
    if boundary == "begin":
        c.replay = InferenceReplayService(LostAck(c.db))
        with pytest.raises(ConnectionError):
            await begin(c)
    else:
        claim = await begin(c)
        if boundary in {"reservation", "settlement"}:
            c.gateway.db = LostAck(c.db, skip=1 if boundary == "settlement" else 0)
            with pytest.raises(ConnectionError):
                await c.gateway.infer(request(c, claim))
            assert await c.db.fetchval("SELECT gateway_request_id IS NOT NULL FROM inference_route_commands")
            if boundary == "settlement":
                assert await c.db.fetchval("SELECT status FROM inference_requests") == "succeeded"
                assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1
        else:
            result = await c.gateway.infer(request(c, claim))
            c.replay = InferenceReplayService(LostAck(c.db))
            with pytest.raises(ConnectionError):
                await c.replay.complete(claim, gateway_request_id=result.inference_request_id,
                                        response={"text": "exact-result"}, append=no_append)
    c.replay = InferenceReplayService(await c.sandbox.reconnect())
    c.db = c.sandbox.db
    if boundary == "complete":
        assert (await begin(c)).response == {"text": "exact-result"}
        assert len(c.calls) == 1
    else:
        with pytest.raises(ContractError):
            await begin(c)
        assert len(c.calls) == (1 if boundary == "settlement" else 0)


async def test_default_off_preserves_legacy_and_keeps_private_schema(owned, monkeypatch):
    c = owned
    monkeypatch.delenv("OPENVEGAS_NATIVE_GENERATION_SCOPE")
    assert (await post(c)).status_code == 409
    assert not c.calls and await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 0
    assert await c.db.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid='inference_route_commands'::regclass")
    for role in ("anon", "authenticated"):
        assert not await c.db.fetchval("SELECT has_table_privilege($1,'inference_route_commands','SELECT')", role)


@pytest.mark.parametrize("web,attachment", [(False, True), (True, False), (True, True)])
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_reviewed_first_generation_media_uses_real_preflight_gateway_and_replay(owned, monkeypatch, web, attachment, endpoint):
    import base64
    import hashlib

    from tests.test_models import test_openrouter_attachments as files
    from tests.test_models import test_openrouter_web_gateway as search

    c = owned
    config = search.catalog_row()
    review = search.review()
    file_review = files.review()
    review.update({name: value for name, value in config.items() if "_per_1m" in name})
    review.update(max_tokens=1024, context_window_tokens=16384,
                  attachments=file_review["attachments"], observed_pricing=file_review["observed_pricing"])
    review["web_search"]["execution"]["context_window_tokens"] = 16384
    review["attachments"]["provider"] = "fixture/endpoint"
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + search.MODEL: review}))
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_WEB_SEARCH", "1")
    await c.db.execute("INSERT INTO provider_catalog(provider,model_id,display_name,max_tokens,"
        "cost_input_per_1m,cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m) "
        "VALUES('openrouter',$1,'Native media fixture',1024,1,2,10,20)", search.MODEL)
    c.command["model"] = search.MODEL
    c.command["enable_web_search"] = web
    if web:
        c.provider_body = search.response()
    if attachment:
        uploads = FileUploadService(c.db)
        content = b"Complete owned synthetic content, not a preview."
        initialized = await uploads.upload_init(user_id=c.user, filename="owned.txt", size_bytes=len(content),
            mime_type="text/plain", sha256_hex=hashlib.sha256(content).hexdigest())
        uploaded = await uploads.upload_complete(user_id=c.user, upload_id=initialized["upload_id"],
            content_base64=base64.b64encode(content).decode())
        c.command["attachments"] = [uploaded["file_id"]]
    first = payload(await post(c, endpoint), endpoint)
    assert first["native_generation"]["original_turn_scope_verified"] is True
    assert len(c.calls) == 1
    if attachment:
        assert "Complete owned synthetic content" in json.dumps(c.calls[0]["messages"])
        await c.db.execute("UPDATE chat_file_uploads SET expires_at=now()-interval '1 second'")
    if web:
        assert first["web_search_used"] is True
        assert await c.db.fetchval("SELECT response_body_text FROM inference_requests")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    recovered = payload(await post(c))
    assert recovered["native_generation"] == first["native_generation"]
    assert len(c.calls) == 1


@pytest.mark.parametrize("in_flight", [False, True])
@pytest.mark.parametrize("original,replacement", [(None, ""), ("", None)])
async def test_empty_and_null_registration_never_rotate_after_claim(owned, original, replacement, in_flight):
    c = owned
    args = {**c.run.scope(), "workspace_root": "/synthetic/native-workspace",
            "workspace_fingerprint": "sha256:" + "a" * 64, "git_root": original}
    await c.service.register_workspace(**args)
    pending = None
    if in_flight:
        c.release = asyncio.Event()
        pending = asyncio.create_task(post(c))
        await asyncio.wait_for(c.entered.wait(), 4)
    else:
        await begin(c)
    try:
        with pytest.raises(ContractError, match="immutable"):
            await c.service.register_workspace(**{**args, "git_root": replacement})
        assert await c.db.fetchval("SELECT git_root FROM agent_runs WHERE id=$1::uuid", c.run.run_id) == original
    finally:
        if pending is not None:
            c.release.set()
            result = await asyncio.wait_for(pending, 4)
            payload(result)
    if in_flight:
        payload(await post(c))
        assert len(c.calls) == 1


async def test_cancellation_after_dispatch_retains_settlement_but_forbids_native_execution(owned):
    c = owned
    c.emit_calls = True
    c.release = asyncio.Event()
    task = asyncio.create_task(post(c))
    await asyncio.wait_for(c.entered.wait(), 4)
    try:
        await c.db.execute("UPDATE agent_runs SET cancel_requested_at=now() WHERE id=$1::uuid", c.run.run_id)
    finally:
        c.release.set()
    output = payload(await asyncio.wait_for(task, 4))
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1
    source = SimpleNamespace(request_id=output["native_generation"]["inference_request_id"], call=output["tool_calls"][0])
    with pytest.raises(ContractError, match="inactive or cancelling"):
        await propose(c.service, c.run, source)
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 0


async def test_fully_grant_covered_generation_can_bind_zero_charge_settlement(owned):
    c = owned
    order = str(uuid4())
    await c.db.execute("INSERT INTO store_orders(id,user_id,item_id,cost_v,status,idempotency_key,idempotency_payload_hash) "
                      "VALUES($1::uuid,$2::uuid,'native-test-grant',0,'fulfilled',$3,$4)", order, c.user, order, "a" * 64)
    await c.db.execute("INSERT INTO inference_token_grants(user_id,source_order_id,provider,model_id,tokens_total,tokens_remaining) "
                      "VALUES($1::uuid,$2::uuid,'openrouter',$3,100000,100000)", c.user, order, c.command["model"])
    c.emit_calls = True
    result = payload(await post(c))
    assert Decimal(result["v_cost"]) == 0
    assert await c.db.fetchval("SELECT status FROM inference_preauthorizations") == "refunded"
    assert await c.db.fetchval("SELECT count(*) FROM inference_grant_usages") == 1
    source = SimpleNamespace(request_id=result["native_generation"]["inference_request_id"], call=result["tool_calls"][0])
    bound = await propose(c.service, c.run, source)
    assert bound.status_code == 200
    assert len(c.calls) == 1
