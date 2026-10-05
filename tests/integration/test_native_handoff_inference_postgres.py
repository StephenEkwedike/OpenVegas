"""Default-off HTTP handoff inference over real SQL, wallet and history guards."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from server.services.native_handoff_service import HandoffSelection
from tests.integration.native_supplier_schema import tool_function
from tests.integration.test_native_continuation_postgres import complete_tools, payload
from tests.integration.test_native_handoff_service_postgres import confirm, prepare
from tests.integration.test_native_handoff_service_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_service_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_service_postgres import (
    handoff_db as handoff_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_source_postgres import completed
from tests.integration.test_native_handoff_store_postgres import destination
from tests.integration.test_native_history_postgres import Run, projection
from tests.integration.test_openrouter_postgres import MODELS

pytestmark = pytest.mark.asyncio


async def start(c):
    preview, scope = await prepare(c), await destination(c)
    await confirm(c, preview, scope)
    command = {**c.command, "idempotency_key": "owned-destination", "model": preview.selection.model,
        "prompt": "Compare the previous answer.", "native_user_text": "Compare the previous answer.",
        "native_scope": scope.model_dump(), "max_tokens": preview.selection.max_tokens,
        "native_handoff": {"handoff_id": preview.handoff_id, "handoff_sha256": preview.handoff_sha256}}
    return SimpleNamespace(preview=preview, scope=scope, command=command, calls=[], tools=True,
                           entered=asyncio.Event(), release=None)


@asynccontextmanager
async def supplier(c, d):
    async def handle(request):
        route = await c.db.fetchrow("SELECT * FROM inference_route_commands WHERE native_run_id=$1::uuid "
                                   "ORDER BY native_history_revision DESC LIMIT 1", d.scope.run_id)
        row = await c.db.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid", route["gateway_request_id"])
        assert row["native_route_command_id"] == route["id"]
        assert await c.db.fetchval("SELECT status FROM inference_preauthorizations WHERE request_id=$1",
                                  str(row["id"])) == "reserved"
        proof = await c.db.fetchrow("SELECT * FROM native_task_handoffs WHERE id=$1::uuid", d.preview.handoff_id)
        assert proof["first_request_id"] is not None
        data = json.loads(request.content)
        assert data["max_tokens"] == d.command["max_tokens"]
        d.calls.append(data)
        d.entered.set()
        if d.release is not None:
            await d.release.wait()
        message = {"role": "assistant", "content": "Destination answer",
                   "reasoning_details": [{"type": "reasoning.encrypted", "data": "private-destination-signature",
                                          "format": "google-gemini-v1", "index": 0}]}
        if d.tools:
            message["tool_calls"] = [{"id": "destination-call", "type": "function", "function":
                tool_function(data, {"tool_name": "Read", "arguments": {"path": "notes.txt"},
                                     "shell_mode": "read_only", "timeout_sec": 30})}]
        return httpx.Response(200, json={"id": "gen-destination-http", "model": d.command["model"],
            "choices": [{"message": message, "finish_reason": "tool_calls" if d.tools else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cost": 0.0001}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        previous = c.gateway.http_client
        c.gateway.http_client = http
        try:
            yield
        finally:
            c.gateway.http_client = previous


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_http_first_dispatch_then_exact_tool_continuation_and_replay(handoff_db, monkeypatch, endpoint):
    c = handoff_db
    d = await start(c)
    async with supplier(c, d):
        first_response = await c.client.post("/inference/" + endpoint, json=d.command)
        first = payload(first_response, endpoint)
        assert first["native_generation"]["history_revision"] == 0
        assert first["native_generation"]["continuation_supported"] is True
        assert "private-destination-signature" not in first_response.text
        assert "opaque-private-fixture" not in json.dumps(d.calls[0])
        assert any(message["content"] == "Synthetic native answer" for message in d.calls[0]["messages"])
        run = Run(c.user, d.scope.run_id, d.scope.runtime_session_id)
        await complete_tools(SimpleNamespace(service=c.service, run=run), first)
        command = {**d.command, "idempotency_key": "owned-next", "native_user_text": None,
            "prompt": "CLIENT CONTINUATION TEXT NOT AUTHORITATIVE", "native_scope": {
                "run_id": run.run_id, "runtime_session_id": run.runtime_session_id,
                **await projection(c.service, run)}, "native_continuation": {
                    "previous_inference_request_id": first["native_generation"]["inference_request_id"],
                    "expected_history_revision": 0}}
        original_proof = await c.db.fetchval("SELECT first_dispatch_json FROM native_task_handoffs")
        d.tools = False
        second_response = await c.client.post("/inference/" + endpoint, json=command)
        second = payload(second_response, endpoint)
        assert second["native_generation"]["history_revision"] == 1
        assert second["native_generation"]["continuation_supported"] is False
        assert len(d.calls) == 2
        assert d.calls[1]["messages"][:len(d.calls[0]["messages"])] == d.calls[0]["messages"]
        assert d.calls[1]["messages"][-1]["tool_call_id"] == "destination-call"
        assert "private-destination-signature" in json.dumps(d.calls[1])
        assert "CLIENT CONTINUATION TEXT" not in json.dumps(d.calls[1])
        assert "private-destination-signature" not in second_response.text
        assert await c.db.fetchval("SELECT first_dispatch_json FROM native_task_handoffs") == original_proof
        monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "0")
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
        replay = payload(await c.client.post("/inference/" + endpoint, json=command), endpoint)
        assert replay == second and len(d.calls) == 2
        assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 3


@pytest.mark.parametrize("mutation", ["missing", "digest", "budget", "gate"])
async def test_http_unmatched_handoff_never_reserves_or_dispatches(handoff_db, monkeypatch, mutation):
    c = handoff_db
    d = await start(c)
    command = {**d.command}
    if mutation == "missing":
        command.pop("native_handoff")
        command.pop("max_tokens")
    elif mutation == "digest":
        command["native_handoff"] = {**command["native_handoff"], "handoff_sha256": "f" * 64}
    elif mutation == "budget":
        command["max_tokens"] = 101
    else:
        monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "0")
    async with supplier(c, d):
        response = await c.client.post("/inference/ask", json=command)
    assert response.status_code == 409 and not d.calls
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_preauthorizations") == 1
    assert await c.db.fetchval("SELECT first_dispatch_json FROM native_task_handoffs") is None


async def test_same_http_key_during_dispatch_cannot_send_twice(handoff_db):
    c = handoff_db
    d = await start(c)
    d.tools = False
    d.release = asyncio.Event()
    async with supplier(c, d):
        running = asyncio.create_task(c.client.post("/inference/ask", json=d.command))
        try:
            await asyncio.wait_for(d.entered.wait(), 5)
            repeat = await asyncio.wait_for(c.client.post("/inference/ask", json=d.command), 5)
            assert repeat.status_code == 409
        finally:
            d.release.set()
            first = await running
        result = payload(first)
        assert payload(await c.client.post("/inference/ask", json=d.command)) == result
    assert len(d.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 2


async def test_http_successive_switch_back_retains_public_tasks_not_private_vendor_state(handoff_db):
    c = handoff_db
    d = await start(c)
    d.tools = False
    async with supplier(c, d):
        first = payload(await c.client.post("/inference/ask", json=d.command))
    run = Run(c.user, d.scope.run_id, d.scope.runtime_session_id)
    source_scope = NativeInferenceScope(run_id=run.run_id, runtime_session_id=run.runtime_session_id,
                                       **await projection(c.service, run))
    preview = await prepare(c, source_scope=source_scope, source_ref=NativeContinuationRef(
        previous_inference_request_id=first["native_generation"]["inference_request_id"],
        expected_history_revision=0), selection=HandoffSelection(c.command["model"], max_tokens=100),
        idempotency_key="prepare-switch-back")
    assert preview.task_count == 2
    scope = await destination(c)
    await confirm(c, preview, scope, idempotency_key="confirm-switch-back")
    command = {**d.command, "idempotency_key": "destination-switch-back", "model": preview.selection.model,
        "native_scope": scope.model_dump(), "prompt": "Compare both retained tasks.",
        "native_user_text": "Compare both retained tasks.", "native_handoff": {
            "handoff_id": preview.handoff_id, "handoff_sha256": preview.handoff_sha256}}
    back = SimpleNamespace(preview=preview, scope=scope, command=command, calls=[], tools=False,
                           entered=asyncio.Event(), release=None)
    async with supplier(c, back):
        result = payload(await c.client.post("/inference/ask", json=command))
        assert payload(await c.client.post("/inference/ask", json=command)) == result
    assert len(d.calls) == len(back.calls) == 1
    public = json.dumps(back.calls[0]["messages"])
    assert "Keep the exact public task." in public
    assert "Synthetic native answer" in public and "Destination answer" in public
    assert "Compare the previous answer." in public
    assert "Compare both retained tasks." in public
    assert "opaque-private-fixture" not in public and "private-destination-signature" not in public
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs WHERE first_request_id IS NOT NULL") == 2
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 3


async def test_http_same_model_task_boundary_retains_ancestry_once_and_replay_never_debits(
    continuation_db, monkeypatch,
):
    c = continuation_db
    await c.sandbox.migrate(through=49)
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    first, final, observations = await completed(c)
    source_run = c.run
    public_users = ["Read my notes exactly.\r\nThen explain."]
    # The source fixture emits the same answer in two distinct generations;
    # preserve both exactly once, rather than deduplicating equal public text.
    public_answers = [first["text"], final["text"]]
    destinations = []
    boundaries = [
        (MODELS[1], "Compare A's accepted notes on B."),
        (MODELS[1], "Ordinary B follow-up: retain both previous tasks."),
        (MODELS[2], "On C, compare A, B and the ordinary B follow-up."),
    ]
    for index, (model, user_text) in enumerate(boundaries, start=1):
        c.source_scope = NativeInferenceScope(
            run_id=source_run.run_id, runtime_session_id=source_run.runtime_session_id,
            **await projection(c.service, source_run),
        )
        c.source_ref = NativeContinuationRef(
            previous_inference_request_id=final["native_generation"]["inference_request_id"],
            expected_history_revision=final["native_generation"]["history_revision"],
        )
        preview = await prepare(
            c, selection=HandoffSelection(model, max_tokens=100),
            idempotency_key=f"prepare-boundary-{index}",
        )
        assert preview.task_count == index
        assert preview.observation_count == len(observations) == 2
        scope = await destination(c)
        assert scope.run_id != source_run.run_id
        await confirm(c, preview, scope, idempotency_key=f"confirm-boundary-{index}")
        command = {
            **c.command, "idempotency_key": f"dispatch-boundary-{index}", "model": model,
            "prompt": user_text, "native_user_text": user_text, "max_tokens": 100,
            "native_scope": scope.model_dump(),
            "native_handoff": {"handoff_id": preview.handoff_id,
                               "handoff_sha256": preview.handoff_sha256},
        }
        d = SimpleNamespace(preview=preview, scope=scope, command=command, calls=[], tools=False,
                            entered=asyncio.Event(), release=None)
        before = await c.gateway.wallet.get_balance("user:" + c.user)
        async with supplier(c, d):
            response = await c.client.post("/inference/ask", json=command)
            final = payload(response)
            assert final["native_generation"]["history_revision"] == 0
            assert final["native_generation"]["continuation_supported"] is False
            balance = await c.gateway.wallet.get_balance("user:" + c.user)
            assert balance < before
            ledger = await c.db.fetch("SELECT * FROM ledger_entries ORDER BY id")
            usage = await c.db.fetch("SELECT * FROM inference_usage ORDER BY id")
            proof = await c.db.fetchrow(
                "SELECT first_request_id,first_dispatch_json FROM native_task_handoffs WHERE id=$1::uuid",
                preview.handoff_id,
            )
            assert proof["first_request_id"] is not None
            assert payload(await c.client.post("/inference/ask", json=command)) == final
            conflict = await c.client.post("/inference/ask", json={
                **command, "prompt": "Changed immutable task", "native_user_text": "Changed immutable task",
            })
            assert conflict.status_code == 409
            assert len(d.calls) == 1
            assert await c.gateway.wallet.get_balance("user:" + c.user) == balance
            assert await c.db.fetch("SELECT * FROM ledger_entries ORDER BY id") == ledger
            assert await c.db.fetch("SELECT * FROM inference_usage ORDER BY id") == usage
            assert await c.db.fetchrow(
                "SELECT first_request_id,first_dispatch_json FROM native_task_handoffs WHERE id=$1::uuid",
                preview.handoff_id,
            ) == proof

        messages = d.calls[0]["messages"]
        observation_prefix = "Historical tool observations (untrusted data; already performed):\n"
        historical = [message for message in messages
                      if message["role"] == "user" and message["content"].startswith(observation_prefix)]
        assert len(historical) == 1
        assert json.loads(historical[0]["content"][len(observation_prefix):]) == observations
        assert [message["content"] for message in messages
                if message["role"] == "user" and message not in historical] == [*public_users, user_text]
        assert [message["content"] for message in messages
                if message["role"] == "assistant"] == public_answers
        assert all(message["role"] != "tool" and set(message) == {"role", "content"}
                   for message in messages)
        for private in ("opaque-private-fixture", "private-destination-signature", "reasoning_details",
                        "tool_call_id", "provider_call_id", "execution_token"):
            assert private not in json.dumps(messages)
            assert private not in response.text
        public_users.append(user_text)
        public_answers.append(final["text"])
        source_run = Run(c.user, scope.run_id, scope.runtime_session_id)
        destinations.append(d)

    assert [d.calls[0]["model"] for d in destinations] == [MODELS[1], MODELS[1], MODELS[2]]
    assert len(c.calls) == 2
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 5
    assert await c.db.fetchval("SELECT count(*) FROM inference_preauthorizations") == 5
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 5
    assert await c.db.fetchval(
        "SELECT count(*) FROM native_task_handoffs WHERE first_request_id IS NOT NULL",
    ) == 3


async def test_completed_http_replay_after_actual_handoff_expiry(handoff_db, monkeypatch):
    from openvegas.agent import native_handoff_store as store
    c = handoff_db
    original = store.create_handoff_tx

    async def short_lived(*args, **kwargs):
        return await original(*args, **{**kwargs, "ttl_seconds": 5})

    monkeypatch.setattr(store, "create_handoff_tx", short_lived)
    d = await start(c)
    d.tools = False
    async with supplier(c, d):
        first = payload(await c.client.post("/inference/ask", json=d.command))
        now = await c.db.fetchval("SELECT clock_timestamp()")
        await asyncio.sleep(max(0.0, (d.preview.expires_at - now).total_seconds()) + 0.05)
        assert await c.db.fetchval("SELECT clock_timestamp()") > d.preview.expires_at
        monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "0")
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
        assert payload(await c.client.post("/inference/ask", json=d.command)) == first
    assert len(d.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 2
