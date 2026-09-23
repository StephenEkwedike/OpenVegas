"""Real private-envelope settlement; disposable loopback PG, synthetic supplier."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from openvegas.agent.native_envelope import load_native_envelope_tx
from openvegas.contracts.errors import ContractError
from tests.integration.test_native_generation_ownership_postgres import owned as _owned_fixture
from tests.integration.test_native_generation_ownership_postgres import payload, post

owned = _owned_fixture

pytestmark = pytest.mark.asyncio
PRIVATE = "private-vendor-signature-not-public"


@pytest_asyncio.fixture
async def native_envelope_case(owned, monkeypatch):
    c = owned
    # 046 stores envelopes; 047 supplies the explicit route history opt-in claim.
    await c.sandbox.migrate(through=47)
    monkeypatch.setenv("OPENVEGAS_NATIVE_GENERATION_HISTORY", "1")
    c.command["native_history"] = True
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-" + str(i), "type": "function", "function": {
            "name": "call_local_tool", "arguments": '{ "tool_name":"Read", "arguments":{"path":"notes.txt"} }'},
         "extra_content": {"google": {"thought_signature": PRIVATE + str(i)}}}
        for i in range(3)
    ], "reasoning_details": [{"type": "reasoning.encrypted", "data": PRIVATE, "index": 2},
                             {"type": "reasoning.text", "text": "private explanation", "signature": PRIVATE, "index": 0}]}
    c.provider_body = {"id": "gen-native-local", "model": c.command["model"],
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00002}}
    yield c


async def load(c, **changes):
    request_id = str(await c.db.fetchval("SELECT request_id FROM native_generation_envelopes"))
    args = {"user_id": c.user, "run_id": c.run.run_id, "runtime_session_id": c.run.runtime_session_id,
            "request_id": request_id, "provider": "openrouter", "model": c.command["model"], **changes}
    async with c.db.transaction() as tx:
        return await load_native_envelope_tx(tx, **args)


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
async def test_native_envelope_settles_with_gateway_and_stays_out_of_public_results(native_envelope_case, endpoint):
    c = native_envelope_case
    response = await post(c, endpoint)
    public = payload(response, endpoint)
    assert PRIVATE not in response.text and "private explanation" not in response.text
    assert public["native_generation"]["history_revision"] == 0
    assert public["native_generation"]["continuation_supported"] is True
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1
    assert await c.db.fetchval("SELECT count(*) FROM native_generation_envelopes") == 1
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 0  # None proposed, ALL retained.
    envelope = await load(c)
    assert envelope.assistant_message() == c.provider_body["choices"][0]["message"]
    assert envelope.request_payload() == c.calls[0]
    assert envelope.history_inputs()["settings"]["prompt"] == c.command["prompt"]
    assert envelope.history_inputs()["attachment_refs"] == []
    for sql in ("SELECT response_body_text FROM inference_requests", "SELECT response_body_text FROM inference_route_commands",
                "SELECT metadata_json::text FROM wallet_history_projection LIMIT 1"):
        assert PRIVATE not in str(await c.db.fetchval(sql))
    again = await post(c, endpoint)
    assert payload(again, endpoint) == public
    assert len(c.calls) == 1
    c.db = await c.sandbox.reconnect()
    assert (await load(c)).assistant_message_json == envelope.assistant_message_json


@pytest.mark.parametrize("field", ["user_id", "run_id", "runtime_session_id", "request_id", "provider", "model"])
async def test_native_envelope_retrieval_checks_original_owner_session_and_model(native_envelope_case, field):
    c = native_envelope_case
    payload(await post(c))
    bad = "openai" if field == "provider" else "other/model" if field == "model" else str(uuid4())
    with pytest.raises(ContractError):
        await load(c, **{field: bad})


@pytest.mark.parametrize("tamper", ["source", "preauth", "usage", "account", "model", "digest", "public_binding"])
async def test_native_envelope_retrieval_revalidates_settlement_and_integrity(native_envelope_case, tamper):
    c = native_envelope_case
    payload(await post(c))
    sql = {
        "source": "UPDATE inference_requests SET status='failed'",
        "preauth": "UPDATE inference_preauthorizations SET status='voided'",
        "usage": "UPDATE inference_usage SET v_cost=v_cost+1",
        "account": "UPDATE inference_usage SET account_id='house:float'",
        "model": "UPDATE inference_usage SET model_id='other/model'",
        "digest": "UPDATE native_generation_envelopes SET assistant_sha256=repeat('0',64)",
        "public_binding": "UPDATE native_generation_envelopes SET public_binding=repeat('0',64)",
    }[tamper]
    await c.db.execute(sql)
    with pytest.raises(ContractError):
        await load(c)


async def test_native_envelope_private_insert_failure_rolls_back_settlement(native_envelope_case):
    c = native_envelope_case
    await c.db.execute("ALTER TABLE native_generation_envelopes ADD CONSTRAINT synthetic_storage_failure CHECK(false)")
    response = await post(c)
    assert PRIVATE not in response.text
    assert response.status_code != 200 or "error" in response.json()
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM native_generation_envelopes") == 0
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 0
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests WHERE status='succeeded'") == 0
    assert await c.db.fetchval("SELECT status FROM inference_preauthorizations") == "voided"


async def test_native_envelope_grant_covered_zero_charge_is_still_settled(native_envelope_case):
    c = native_envelope_case
    order = str(uuid4())
    await c.db.execute("INSERT INTO store_orders(id,user_id,item_id,cost_v,status,idempotency_key,idempotency_payload_hash) "
                       "VALUES($1::uuid,$2::uuid,'native-envelope-grant',0,'fulfilled',$3,$4)", order, c.user, order, "a" * 64)
    await c.db.execute("INSERT INTO inference_token_grants(user_id,source_order_id,provider,model_id,tokens_total,tokens_remaining) "
                       "VALUES($1::uuid,$2::uuid,'openrouter',$3,100000,100000)", c.user, order, c.command["model"])
    public = payload(await post(c))
    assert Decimal(public["v_cost"]) == 0
    assert await c.db.fetchval("SELECT status FROM inference_preauthorizations") == "refunded"
    assert (await load(c)).assistant_message() == c.provider_body["choices"][0]["message"]


async def test_native_envelope_not_written_for_legacy_scope_with_global_flag_on(native_envelope_case):
    c = native_envelope_case
    del c.command["native_history"]
    payload(await post(c))
    assert await c.db.fetchval("SELECT count(*) FROM native_generation_envelopes") == 0
    assert len(c.calls) == 1


async def test_native_envelope_046_has_no_customer_grants_or_rls_policy(database_factory):
    async with database_factory(through=46) as sandbox:
        db = sandbox.db
        assert await db.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid='native_generation_envelopes'::regclass")
        assert await db.fetchval("SELECT count(*) FROM pg_policy WHERE polrelid='native_generation_envelopes'::regclass") == 0
        # Make the toy schema visible so denial proves TABLE privacy, not lookup failure.
        await db.execute("GRANT USAGE ON SCHEMA public TO authenticated")
        for role in ("anon", "authenticated"):
            for permission in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await db.fetchval("SELECT has_table_privilege($1,'native_generation_envelopes',$2)", role, permission)
        with pytest.raises(Exception) as error:
            async with db.transaction() as tx:
                await tx.execute("SET LOCAL ROLE authenticated")
                await tx.fetchval("SELECT assistant_message_json FROM public.native_generation_envelopes")
        assert getattr(error.value, "sqlstate", None) == "42501"
