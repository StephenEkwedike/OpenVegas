"""Private handoff continuation through real SQL/gateway/wallet, synthetic HTTP.

Routes are reserved internally because public handoff replay remains disabled.
No production guard, provenance loader, wallet or gateway reservation is mocked.
Run only with the repository's disposable, loopback-only PostgreSQL harness.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from openvegas.agent.native_continuation import reserve_history_tx
from openvegas.agent.native_envelope import NativeHistoryInputs
from openvegas.agent.native_generation import NativeGenerationClaim, scope_document
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from openvegas.gateway.inference import InferenceRequest
from server.services import native_handoff_continuation as continuation
from server.services.file_uploads import FileUploadService
from server.services.inference_replay import (
    _KIND,
    InferenceReplayService,
    ReplayClaim,
    _gateway_key,
    command_fingerprint,
)
from server.services.native_handoff_provenance import verify_consumed_handoff_tx
from server.services.native_handoff_service import HandoffSelection
from tests.integration import test_native_handoff_dispatch_postgres as initial
from tests.integration.test_native_continuation_postgres import (
    complete_tools,
    payload,
    post,
    setup_media,
)
from tests.integration.test_native_generation_ownership_postgres import LostAck
from tests.integration.test_native_handoff_dispatch_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414 - pytest fixture registration
)
from tests.integration.test_native_handoff_dispatch_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_dispatch_postgres import (
    handoff_db as handoff_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_provenance_postgres import settled
from tests.integration.test_native_history_postgres import Run, projection

pytestmark = pytest.mark.asyncio
OPAQUE = "opaque-private-fixture"


async def tool_first(c, monkeypatch, *, accepted=2, **changes):
    original = initial.supplier

    def supplier(context, destination, **kwargs):
        handle = original(context, destination, **kwargs)

        async def respond(request):
            # Retain the first-dispatch helper's committed proof/hold assertions.
            response = await handle(request)
            body = response.json()
            choice = body["choices"][0]
            choice["finish_reason"] = "tool_calls"
            choice["message"]["reasoning_details"] = [{
                "type": "reasoning.encrypted", "data": OPAQUE,
                "format": "google-gemini-v1", "index": 0,
            }]
            choice["message"]["tool_calls"] = [{
                "id": "handoff-read-" + str(index), "type": "function", "function": {
                    "name": "call_local_tool", "arguments": json.dumps({
                        "tool_name": "Read", "arguments": {"path": "notes.txt"},
                        "shell_mode": "read_only", "timeout_sec": 30,
                    }),
                },
            } for index in range(2)]
            return httpx.Response(200, json=body)

        return respond

    monkeypatch.setattr(initial, "supplier", supplier)
    d, result = await settled(c, **changes)
    assert len(result.tool_calls) == 2
    run = Run(c.user, d.scope.run_id, d.scope.runtime_session_id)
    await complete_tools(SimpleNamespace(service=c.service, run=run), {
        "tool_calls": result.tool_calls, "provider_request_id": result.provider_request_id,
        "native_generation": {"inference_request_id": result.inference_request_id},
    }, count=accepted)
    d.run, d.first_result = run, result
    d.proof = await proof(c, d)
    return d


async def proof(c, d):
    return dict(await c.db.fetchrow(
        "SELECT first_dispatch_json,first_request_id,first_route_command_id,consumed_at "
        "FROM native_task_handoffs WHERE id=$1::uuid", d.preview.handoff_id,
    ))


async def reserve(c, d):
    """Only fixture route minting is private; history/provenance are production."""
    scope = NativeInferenceScope(run_id=d.run.run_id, runtime_session_id=d.run.runtime_session_id,
                                **await projection(c.service, d.run))
    inputs = json.loads(await c.db.fetchval(
        "SELECT history_inputs_json FROM native_generation_envelopes WHERE request_id=$1::uuid",
        d.first_result.inference_request_id,
    ))
    settings = inputs["settings"]
    command = {name: settings[name] for name in (
        "provider", "model", "enable_tools", "enable_web_search", "reasoning_effort", "attachments",
    )}
    command.update(prompt="Untrusted continuation prose must not enter history.",
                   native_scope=scope.model_dump(), native_history=True, persist_context=False,
                   conversation_mode="ephemeral", thread_id=None, native_continuation={
                       "previous_inference_request_id": d.first_result.inference_request_id,
                       "expected_history_revision": 0,
                   })
    route_id, owner, key = str(uuid4()), str(uuid4()), "continuation:" + str(uuid4())
    gateway_key, digest = _gateway_key(c.user, key), command_fingerprint(command)
    async with c.db.transaction() as tx:
        # All ancestry runs/routes precede gateway/history reads.
        await verify_consumed_handoff_tx(tx, user_id=c.user, scope=scope)
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid", scope.run_id)
        revision, previous, wire, raw_inputs = await reserve_history_tx(
            tx, run=run, scope=scope, command=command,
        )
        scope_json = scope_document(scope, run)
        await tx.execute("""INSERT INTO inference_route_commands
            (id,user_id,idempotency_key,payload_hash,status,response_body_text,native_run_id,
             native_scope,native_history_revision,previous_native_request_id)
            VALUES($1::uuid,$2::uuid,$3,$4,'processing',$5,$6::uuid,$7::jsonb,$8,$9::uuid)""",
            route_id, c.user, key, digest, json.dumps({"kind": _KIND, "state": "processing",
                "owner_token": owner, "gateway_key": gateway_key}), scope.run_id, scope_json,
            revision, previous)
        await tx.execute("UPDATE agent_runs SET native_generation_claim_id=$2::uuid,"
                         "native_history_revision=$3 WHERE id=$1::uuid", scope.run_id, route_id, revision)
    claim = NativeGenerationClaim(c.user, route_id, scope, scope_json, owner, digest, gateway_key,
                                  revision, previous, wire, raw_inputs)
    request = InferenceRequest("user:" + c.user, settings["provider"], settings["model"],
        json.loads(wire)["messages"], max_tokens=settings["max_tokens"],
        enable_tools=settings["enable_tools"], enable_web_search=settings["enable_web_search"],
        reasoning_effort=settings["reasoning_effort"], idempotency_key=gateway_key)
    request._native_generation_claim = claim
    request._native_history_inputs = NativeHistoryInputs(raw_inputs)
    prepared = await continuation.prepare_continuation(c.db, request=request)
    return SimpleNamespace(request=prepared.request, binding=prepared.binding, claim=claim,
                           key=key, calls=[], appended=[])


async def infer_continuation(c, d, next_turn, *, fail=False):
    async def supplier(request):
        route = await c.db.fetchrow("SELECT * FROM inference_route_commands WHERE id=$1::uuid",
                                    next_turn.claim.route_command_id)
        assert route["gateway_request_id"] is not None and route["native_history_revision"] == 1
        gateway = await c.db.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid",
                                      route["gateway_request_id"])
        assert gateway["native_route_command_id"] == route["id"]
        assert gateway["user_id"] == route["user_id"]
        assert await c.db.fetchval("SELECT status FROM inference_preauthorizations WHERE request_id=$1",
                                  str(route["gateway_request_id"])) == "reserved"
        assert await proof(c, d) == d.proof
        assert request.content == next_turn.binding.payload_json.encode("utf-8")
        next_turn.calls.append(json.loads(request.content))
        if fail:
            raise httpx.ReadTimeout("Synthetic continuation outcome unknown")
        return httpx.Response(200, json={
            "id": "gen-private-continuation", "model": next_turn.request.model,
            "choices": [{"message": {"role": "assistant", "content": "Retained tools checked."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108, "cost": 0.0001},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(supplier)) as http:
        c.gateway.http_client = http
        return await c.gateway.infer(next_turn.request)


async def finish(c, next_turn, result, *, db=None):
    native = next_turn.claim
    claim = ReplayClaim(c.user, native.route_command_id, next_turn.key, native.command_hash,
                        native.gateway_key, native.owner_token, native_claim=native)

    async def append(_tx):
        next_turn.appended.append(result.inference_request_id)

    return await InferenceReplayService(db if db is not None else c.db).complete(
        claim, gateway_request_id=result.inference_request_id,
        response={"text": result.text, "native_generation": {"inference_request_id": result.inference_request_id}},
        append=append,
    )


async def assert_success(c, d, next_turn, *, lost_ack=False):
    result = await infer_continuation(c, d, next_turn)
    if lost_ack:
        with pytest.raises(ConnectionError, match="lost commit"):
            await finish(c, next_turn, result, db=LostAck(c.db))
    receipt = await finish(c, next_turn, result)
    assert receipt["native_generation"]["history_revision"] == 1
    assert receipt["native_generation"]["continuation_supported"] is False
    assert await finish(c, next_turn, result) == receipt
    assert next_turn.appended == [result.inference_request_id]
    first = await c.db.fetchrow("SELECT * FROM native_generation_envelopes WHERE request_id=$1::uuid",
                                d.first_result.inference_request_id)
    current = await c.db.fetchrow("SELECT * FROM native_generation_envelopes WHERE request_id=$1::uuid",
                                  result.inference_request_id)
    assert current["history_inputs_json"] == first["history_inputs_json"]
    sent = next_turn.calls[0]
    prior = json.loads(first["request_payload_json"])
    assert sent["messages"][:len(prior["messages"])] == prior["messages"]
    assert sent["messages"][len(prior["messages"])] == json.loads(first["assistant_message_json"])
    tools = sent["messages"][len(prior["messages"]) + 1:]
    assert [tool["tool_call_id"] for tool in tools] == ["handoff-read-0", "handoff-read-1"]
    assert all("exact result " + tool["tool_call_id"] in tool["content"] for tool in tools)
    assert OPAQUE in current["request_payload_json"] and OPAQUE not in json.dumps(receipt)
    assert "Untrusted continuation prose" not in current["request_payload_json"]
    assert await proof(c, d) == d.proof
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 3
    assert await c.db.fetchval("SELECT count(*) FROM inference_preauthorizations WHERE status='settled'") == 3
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage WHERE request_id=$1",
                              result.inference_request_id) == 1
    charges = await c.db.fetch("""SELECT p.request_id,p.settled_v,count(l.id) AS charge_count,
        sum(l.amount) AS charged_v FROM inference_preauthorizations p
        JOIN ledger_entries l ON l.reference_id='infer-preauth:' || p.id::text
          AND l.entry_type='reserve_settle'
        GROUP BY p.request_id,p.settled_v""")
    assert len(charges) == 3
    assert all(row["charge_count"] == 1 and row["charged_v"] == row["settled_v"] > 0 for row in charges)
    ledger_count = await c.db.fetchval("SELECT count(*) FROM ledger_entries")
    with pytest.raises(ContractError):
        await infer_continuation(c, d, next_turn)
    assert len(d.calls) == len(next_turn.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 3
    assert await c.db.fetchval("SELECT count(*) FROM ledger_entries") == ledger_count


async def test_real_gateway_continuation_and_lost_completion_ack(handoff_db, monkeypatch):
    c = handoff_db
    d = await tool_first(c, monkeypatch)
    next_turn = await reserve(c, d)
    assert next_turn.request._native_handoff_binding is None
    assert next_turn.request._native_handoff_continuation_binding is next_turn.binding
    await assert_success(c, d, next_turn, lost_ack=True)


async def test_inherited_and_current_owned_files_survive_actual_continuation(continuation_db, monkeypatch):
    c = continuation_db
    await c.sandbox.migrate(through=49)
    await setup_media(c, monkeypatch, web=False, attachment=True)
    c.emit_calls = False
    source = payload(await post(c, native_user_text="Remember the inherited owned file."))
    c.source_scope = NativeInferenceScope(run_id=c.run.run_id, runtime_session_id=c.run.runtime_session_id,
                                         **await projection(c.service, c.run))
    c.source_ref = NativeContinuationRef(
        previous_inference_request_id=source["native_generation"]["inference_request_id"],
        expected_history_revision=0)
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    content = b"Distinct current owned attachment, retained exactly."
    uploads = FileUploadService(c.db)
    upload = await uploads.upload_init(user_id=c.user, filename="current.txt", size_bytes=len(content),
        mime_type="text/plain", sha256_hex=hashlib.sha256(content).hexdigest())
    current = await uploads.upload_complete(user_id=c.user, upload_id=upload["upload_id"],
        content_base64=base64.b64encode(content).decode())
    d = await tool_first(c, monkeypatch, selection=HandoffSelection(c.command["model"], max_tokens=100),
                         current_files=(current["file_id"],))
    next_turn = await reserve(c, d)
    assert next_turn.binding.file_ids == (c.command["attachments"][0], current["file_id"])
    inputs = next_turn.request._native_history_inputs.values()
    assert inputs["attachment_refs"] == d.request._native_history_inputs.values()["attachment_refs"]
    assert inputs["incoming_handoff"] == d.request._native_handoff_binding.provenance()
    await assert_success(c, d, next_turn)


@pytest.mark.parametrize("change", ["expiry", "lost_binding"])
async def test_post_wallet_guard_failure_rolls_back_real_reservation(handoff_db, monkeypatch, change):
    c = handoff_db
    d = await tool_first(c, monkeypatch)
    next_turn = await reserve(c, d)
    ledger_count = await c.db.fetchval("SELECT count(*) FROM ledger_entries")
    snapshot, reserve_wallet = c.gateway._snapshot_handoff_request, c.gateway.wallet.reserve
    active, reservations = [], []

    def track(request):
        copied = snapshot(request)
        active.append(copied)
        return copied

    async def after_reservation(*args, **kwargs):
        result = await reserve_wallet(*args, **kwargs)
        reservations.append(True)
        if change == "lost_binding":
            active[-1]._native_handoff_continuation_binding = None
        else:
            original_datetime = continuation.datetime

            class Clock:
                fromisoformat = staticmethod(original_datetime.fromisoformat)

                @staticmethod
                def now(_tz):
                    return next_turn.binding.expires_at + timedelta(seconds=1)

            monkeypatch.setattr(continuation, "datetime", Clock)
        return result

    monkeypatch.setattr(c.gateway, "_snapshot_handoff_request", track)
    monkeypatch.setattr(c.gateway.wallet, "reserve", after_reservation)
    with pytest.raises(ContractError):
        await infer_continuation(c, d, next_turn)
    assert reservations == [True] and not next_turn.calls
    assert await proof(c, d) == d.proof
    for table in ("inference_requests", "inference_preauthorizations", "inference_usage"):
        assert await c.db.fetchval("SELECT count(*) FROM " + table) == 2
    assert await c.db.fetchval("SELECT count(*) FROM ledger_entries") == ledger_count
    assert await c.db.fetchval("SELECT gateway_request_id FROM inference_route_commands WHERE id=$1::uuid",
                              next_turn.claim.route_command_id) is None


async def test_uncertain_continuation_never_reopens_or_recharges(handoff_db, monkeypatch):
    c = handoff_db
    d = await tool_first(c, monkeypatch)
    next_turn = await reserve(c, d)
    with pytest.raises(ContractError):
        await infer_continuation(c, d, next_turn, fail=True)
    with pytest.raises(ContractError):
        await infer_continuation(c, d, next_turn)
    assert len(next_turn.calls) == 1 and await proof(c, d) == d.proof
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 3
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 2
    assert await c.db.fetchval("SELECT count(*) FROM native_generation_envelopes") == 2
    assert await c.db.fetchval("SELECT native_history_revision FROM agent_runs WHERE id=$1::uuid",
                              d.scope.run_id) == 1


async def test_incomplete_original_tool_receipts_cannot_reserve(handoff_db, monkeypatch):
    c = handoff_db
    d = await tool_first(c, monkeypatch, accepted=1)
    with pytest.raises(ContractError):
        await reserve(c, d)
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 2
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 2
    assert await proof(c, d) == d.proof
