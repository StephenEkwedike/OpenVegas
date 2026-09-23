"""Revision-fenced same-model native continuation, real route/gateway/wallet/locks, synthetic supplier.

Requires the guarded loopback-only disposable database_factory. No paid calls,
remote migrations, real credentials or provider certification.
"""
from __future__ import annotations

import asyncio
import json
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
from openvegas.gateway.inference import AIGateway
from openvegas.wallet.ledger import WalletService
from server.routes import inference as routes
from server.services.file_uploads import FileUploadService
from server.services.inference_replay import InferenceReplayService
from server.services.provider_threads import ProviderThreadService
from tests.integration.test_native_history_postgres import (
    new_run,
    projection,
)
from tests.integration.test_openrouter_postgres import MODELS, provision

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def continuation_db(database_factory, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_SCOPE", "1")
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "1")
    monkeypatch.setenv("OPENVEGAS_CAPABILITY_OVERRIDES_URL", "")
    async with database_factory(through=47, max_size=6) as sandbox:
        assert await sandbox.db.fetchval("SELECT count(*) FROM schema_migrations") == 47
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
                   "conversation_mode": "ephemeral", "native_scope": scope, "native_history": True}
        ctx = SimpleNamespace(sandbox=sandbox, db=db, user=user, run=run, service=service,
            scope=scope, command=command, key="first-native-generation", calls=[], emit_calls=True,
            release=None, entered=asyncio.Event(), fail_provider=False, provider_body=None)

        async def supplier(request):
            # Provider entry observes all committed ownership/hold records via a separate connection.
            row = await ctx.db.fetchrow("SELECT * FROM inference_route_commands WHERE native_run_id=$1::uuid ORDER BY native_history_revision DESC LIMIT 1", run.run_id)
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
            message = {"role": "assistant", "content": "Synthetic native answer",
                       "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-private-fixture", "format": "google-gemini-v1", "index": 0}]}
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


async def complete_tools(c, result, *, count=None, status="succeeded"):
    from tests.integration.test_native_history_postgres import Source, callback, propose_and_start
    calls = result["tool_calls"] if count is None else result["tool_calls"][:count]
    for call in calls:
        source = Source(result["native_generation"]["inference_request_id"], result["provider_request_id"], call, {})
        tool = await propose_and_start(c.service, c.run, source)
        finished = await callback(c.service, c.run, tool, status=status,
                                  payload={"ok": True}, stdout="exact result " + call["provider_call_id"])
        assert finished.status_code == (409 if status == "timed_out" else 200), finished.payload


async def followup(c, first, **changes):
    c.scope = {"run_id": c.run.run_id, "runtime_session_id": c.run.runtime_session_id,
               **await projection(c.service, c.run)}
    return {**c.command, "native_scope": c.scope,
            "idempotency_key": "next:" + first["native_generation"]["inference_request_id"],
            "prompt": "CLIENT HISTORY MUST NOT BE SENT",
            "native_continuation": {
                "previous_inference_request_id": first["native_generation"]["inference_request_id"],
                "expected_history_revision": first["native_generation"]["history_revision"],
            }, **changes}


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_actual_route_continues_exact_native_envelope_and_all_results(continuation_db, monkeypatch, endpoint):
    c = continuation_db
    first_response = await post(c)
    first = payload(first_response)
    assert first["native_generation"]["history_revision"] == 0
    assert first["native_generation"]["continuation_supported"] is True
    assert "opaque-private-fixture" not in first_response.text
    original = json.loads(await c.db.fetchval("SELECT assistant_message_json FROM native_generation_envelopes"))
    await complete_tools(c, first)
    command = await followup(c, first)
    c.emit_calls = False
    second_response = await c.client.post("/inference/" + endpoint, json=command)
    second = payload(second_response, endpoint)
    assert second["native_generation"]["history_revision"] == 1
    assert second["native_generation"]["continuation_supported"] is False
    assert len(c.calls) == 2
    assert c.calls[1]["messages"][:len(c.calls[0]["messages"])] == c.calls[0]["messages"]
    assert c.calls[1]["messages"][-3] == original
    tools = c.calls[1]["messages"][-2:]
    assert [tool["tool_call_id"] for tool in tools] == [call["provider_call_id"] for call in first["tool_calls"]]
    assert all(tool["role"] == "tool" and "exact result " in tool["content"] for tool in tools)
    assert "CLIENT HISTORY MUST NOT BE SENT" not in json.dumps(c.calls[1])
    assert {k: v for k, v in c.calls[1].items() if k != "messages"} == {k: v for k, v in c.calls[0].items() if k != "messages"}
    assert "opaque-private-fixture" not in second_response.text
    c.db = await c.sandbox.reconnect()
    c.replay = InferenceReplayService(c.db)
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_SCOPE", "0")
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "0")
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    replay = payload(await c.client.post("/inference/ask", json=command))
    assert replay["native_generation"] == second["native_generation"]
    assert (await post(c)).text == first_response.text
    assert len(c.calls) == 2
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 2


@pytest.mark.parametrize("kind", ["all_missing", "one_missing", "timeout", "cancelled"])
async def test_continuation_requires_all_emitted_calls_accepted_once(continuation_db, kind):
    c = continuation_db
    first = payload(await post(c))
    if kind == "one_missing":
        await complete_tools(c, first, count=1)
    if kind == "timeout":
        await complete_tools(c, first, status="timed_out")
    if kind == "cancelled":
        await c.db.execute("UPDATE agent_runs SET cancel_requested_at=now() WHERE id=$1::uuid", c.run.run_id)
    denied = await c.client.post("/inference/ask", json=await followup(c, first))
    assert denied.status_code == 409, denied.text
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 1


@pytest.mark.parametrize("change", [
    {"model": MODELS[1]}, {"provider": "anthropic"}, {"enable_web_search": True},
    {"reasoning_effort": "high"}, {"attachments": [str(uuid4())]},
    {"native_history": False}, {"enable_tools": False},
])
async def test_options_cannot_be_silently_changed(continuation_db, change):
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    denied = await c.client.post("/inference/ask", json=await followup(c, first, **change))
    assert denied.status_code in {409, 422}, denied.text
    assert len(c.calls) == 1


async def test_two_keys_contend_for_exact_history_revision(continuation_db):
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    command = await followup(c, first)
    command.pop("idempotency_key")
    async with c.db.transaction() as tx:
        await tx.fetchrow("SELECT id FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        tasks = [asyncio.create_task(c.replay.begin(user_id=c.user, idempotency_key="race:" + str(i),
            command=command, allow_native_history=True)) for i in range(2)]
        await asyncio.sleep(0.05)
        assert all(not task.done() for task in tasks)
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 8)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, ContractError) for item in outcomes) == 1
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs") == 1
    claim = next(item for item in outcomes if not isinstance(item, Exception))
    assert await c.replay.abandon_before_dispatch(claim)
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs") == 0
    assert len(c.calls) == 1


@pytest.mark.parametrize("flag", ["OPENVEGAS_NATIVE_GENERATION_SCOPE", "OPENVEGAS_NATIVE_GENERATION_HISTORY"])
async def test_disabled_server_rejects_before_first_paid_generation(continuation_db, monkeypatch, flag):
    monkeypatch.setenv(flag, "0")
    response = await post(continuation_db)
    assert response.status_code == 409, response.text
    assert not continuation_db.calls


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_final_and_truncated_turns_are_not_automatically_continued(continuation_db, finish):
    c = continuation_db
    c.provider_body = {"id": "gen-final", "model": c.command["model"],
        "choices": [{"message": {"role": "assistant", "content": "Final"}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00002}}
    first = payload(await post(c))
    assert first["native_generation"]["continuation_supported"] is False
    response = await c.client.post("/inference/ask", json=await followup(c, first))
    assert response.status_code == 409, response.text
    assert len(c.calls) == 1


async def test_multiple_native_revisions_preserve_ancestors_without_restarting_prompt(continuation_db):
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    second = payload(await c.client.post("/inference/ask", json=await followup(c, first)))
    assert second["native_generation"]["history_revision"] == 1
    await complete_tools(c, second)
    c.emit_calls = False
    third = payload(await c.client.post("/inference/ask", json=await followup(c, second)))
    assert third["native_generation"]["history_revision"] == 2
    assert c.calls[2]["messages"][:len(c.calls[1]["messages"])] == c.calls[1]["messages"]
    assert len([m for m in c.calls[2]["messages"] if m["role"] == "tool"]) == 4
    assert await c.db.fetchval("SELECT count(*) FROM native_generation_envelopes") == 3


async def test_uncertain_followup_is_not_retried_under_same_or_different_key(continuation_db):
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    command = await followup(c, first)
    c.fail_provider = True
    response = await c.client.post("/inference/ask", json=command)
    assert response.status_code == 503, response.text
    assert len(c.calls) == 2
    assert (await c.client.post("/inference/ask", json=command)).status_code == 409
    assert (await c.client.post("/inference/ask", json={**command, "idempotency_key": "another"})).status_code == 409
    assert len(c.calls) == 2
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs") == 1


async def test_sql_revision_shape_rejects_null_bypass_and_missing_previous(continuation_db):
    import asyncpg
    c = continuation_db
    first = payload(await post(c))
    with pytest.raises(asyncpg.CheckViolationError):
        async with c.db.transaction() as tx:
            await tx.execute("UPDATE inference_route_commands SET native_history_revision=NULL, previous_native_request_id=$1::uuid",
                             first["native_generation"]["inference_request_id"])
    with pytest.raises(asyncpg.CheckViolationError):
        async with c.db.transaction() as tx:
            await tx.execute("UPDATE inference_route_commands SET native_history_revision=1, previous_native_request_id=NULL")
    assert await c.db.fetchval("SELECT native_history_revision FROM inference_route_commands") == 0


async def test_lost_completion_ack_replays_exact_response_without_another_dispatch(continuation_db):
    from tests.integration.test_native_generation_ownership_postgres import LostAck
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    command = await followup(c, first)
    c.emit_calls = False
    c.replay = InferenceReplayService(LostAck(c.db, skip=1))
    with pytest.raises(ConnectionError, match="lost commit"):
        await c.client.post("/inference/ask", json=command)
    c.replay = InferenceReplayService(c.db)
    replay = payload(await c.client.post("/inference/ask", json=command))
    assert replay["native_generation"]["history_revision"] == 1
    assert len(c.calls) == 2


async def setup_media(c, monkeypatch, *, web, attachment):
    import base64
    import hashlib

    from tests.test_models import test_openrouter_attachments as files
    from tests.test_models import test_openrouter_web_gateway as search
    config, review, file_review = search.catalog_row(), search.review(), files.review()
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
        choice = c.provider_body["choices"][0]
        choice["finish_reason"] = "tool_calls"
        choice["message"]["tool_calls"] = [{"id": "media-read", "type": "function", "function": {
            "name": "call_local_tool", "arguments": json.dumps({"tool_name": "Read",
                "arguments": {"path": "notes.txt"}, "shell_mode": "read_only", "timeout_sec": 30})}}]
        choice["message"]["reasoning_details"] = [{"type": "reasoning.encrypted", "data": "opaque-private-fixture"}]
    if attachment:
        uploads = FileUploadService(c.db)
        content = b"Complete owned synthetic content, not a preview."
        initialized = await uploads.upload_init(user_id=c.user, filename="owned.txt", size_bytes=len(content),
            mime_type="text/plain", sha256_hex=hashlib.sha256(content).hexdigest())
        uploaded = await uploads.upload_complete(user_id=c.user, upload_id=initialized["upload_id"],
            content_base64=base64.b64encode(content).decode())
        c.command["attachments"] = [uploaded["file_id"]]


@pytest.mark.parametrize("web,attachment", [(False, True), (True, False), (True, True)])
async def test_reviewed_media_and_web_retained_during_native_continuation(continuation_db, monkeypatch, web, attachment):
    c = continuation_db
    await setup_media(c, monkeypatch, web=web, attachment=attachment)
    first = payload(await post(c))
    await complete_tools(c, first)
    command = await followup(c, first)
    c.emit_calls = False
    if web:
        c.provider_body["choices"][0]["finish_reason"] = "stop"
        del c.provider_body["choices"][0]["message"]["tool_calls"]
    second = payload(await c.client.post("/inference/ask", json=command))
    assert second["native_generation"]["history_revision"] == 1
    assert {k: v for k, v in c.calls[1].items() if k != "messages"} == {k: v for k, v in c.calls[0].items() if k != "messages"}
    assert c.calls[1]["messages"][:len(c.calls[0]["messages"])] == c.calls[0]["messages"]
    if attachment:
        assert "Complete owned synthetic content" in json.dumps(c.calls[1]["messages"])
    if web:
        assert first["web_search_used"] and second["web_search_used"]


async def test_expired_owned_attachment_blocks_continuation_but_not_exact_replay(continuation_db, monkeypatch):
    c = continuation_db
    await setup_media(c, monkeypatch, web=False, attachment=True)
    first = payload(await post(c))
    await complete_tools(c, first)
    command = await followup(c, first)
    await c.db.execute("UPDATE chat_file_uploads SET expires_at=now()-interval '1 second'")
    denied = await c.client.post("/inference/ask", json=command)
    assert denied.status_code == 410, denied.text
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs") == 0
    assert payload(await post(c)) == first


@pytest.mark.parametrize("change", ["previous_id", "revision", "session", "owner", "run"])
async def test_continuation_reference_never_rebinds_generation_or_private_state(continuation_db, change):
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    command = await followup(c, first)
    if change == "previous_id":
        command["native_continuation"]["previous_inference_request_id"] = str(uuid4())
    elif change == "revision":
        command["native_continuation"]["expected_history_revision"] = 3
    elif change == "session":
        command["native_scope"]["runtime_session_id"] = str(uuid4())
    elif change == "owner":
        c.auth_user = str(uuid4())
    elif change == "run":
        other = await new_run(c.service, c.user)
        command["native_scope"] = {"run_id": other.run_id, "runtime_session_id": other.runtime_session_id,
                                    **await projection(c.service, other)}
    response = await c.client.post("/inference/ask", json=command)
    assert response.status_code == 409, response.text
    assert len(c.calls) == 1
    assert "opaque-private-fixture" not in response.text


async def test_tampered_private_reasoning_never_reaches_followup_supplier(continuation_db):
    c = continuation_db
    first = payload(await post(c))
    await complete_tools(c, first)
    await c.db.execute("UPDATE native_generation_envelopes SET assistant_message_json=replace(assistant_message_json,'opaque-private-fixture','wrong-private-state')")
    response = await c.client.post("/inference/ask", json=await followup(c, first))
    assert response.status_code == 409, response.text
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs") == 0


async def test_retained_file_digest_change_blocks_followup_without_advancing_revision(continuation_db, monkeypatch):
    import hashlib
    c = continuation_db
    await setup_media(c, monkeypatch, web=False, attachment=True)
    first = payload(await post(c))
    await complete_tools(c, first)
    # Use the real upload replacement invariants rather than trusting its ID.
    changed = b"Same id, different owned synthetic content."
    row = await c.db.fetchrow("SELECT * FROM chat_file_uploads LIMIT 1")
    assert row is not None
    columns = set(row.keys())
    assert {"content_bytes", "sha256", "size_bytes"} <= columns
    await c.db.execute("UPDATE chat_file_uploads SET content_bytes=$1,sha256=$2,size_bytes=$3",
                       changed, hashlib.sha256(changed).hexdigest(), len(changed))
    response = await c.client.post("/inference/ask", json=await followup(c, first))
    assert response.status_code == 400, response.text
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs") == 0
