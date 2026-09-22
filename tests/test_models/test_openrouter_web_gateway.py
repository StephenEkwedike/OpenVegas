"""Offline full gateway contracts. Synthetic reviews/credentials, no paid calls."""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.conversation import CanonicalConversation
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from openvegas.gateway.openrouter_web import reviewed_web_capability
from openvegas.gateway.reconciliation import inspect_turn, restore_turn

MODEL = "fixture/exact-model-20260901"
USER = "11111111-1111-4111-8111-111111111111"
KEY = "22222222-2222-4222-8222-222222222222"
TOKEN = "synthetic-web-transport-credential"


def catalog_row():
    return {
        "provider": "openrouter",
        "model_id": MODEL,
        "enabled": True,
        "max_tokens": 1024,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
    }


def review():
    now = datetime.now(UTC)
    expires = (now + timedelta(hours=1)).isoformat()
    return {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": expires,
        "account_access": True,
        "completion_chat": True,
        "context_window_tokens": 8192,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "supported_parameters": ["reasoning"],
        "capabilities": {"tools": True, "web_search": True, "reasoning_efforts": ["low", "high"]},
        "web_search": {
            "schema_version": 1,
            "limits": {"max_results": 3, "max_characters": 2000},
            "prices": {
                "review_id": "fixture-prices",
                "expires_at": expires,
                "supplier_input_usd_per_million": "1",
                "supplier_output_usd_per_million": "2",
                "supplier_search_usd_per_call": "0.007",
                "supplier_cap_usd": "0.05",
                "retail_input_v_per_million": "10",
                "retail_output_v_per_million": "20",
                "retail_search_v_per_call": "0.2",
                "retail_cap_v": "1",
            },
            "execution": {
                "review_id": "fixture-execution",
                "evidence_ref": "offline-fixture-only",
                "price_review_id": "fixture-prices",
                "expires_at": expires,
                "model": MODEL,
                "provider_slug": "fixture/endpoint",
                "context_window_tokens": 8192,
                "max_output_tokens": 1024,
                "account_plugins_reviewed": True,
                "combined_output_budget_reviewed": True,
                "aggregate_usage_reviewed": True,
                "token_prices_cover_all_fees": True,
            },
        },
    }


def request(**changes):
    return InferenceRequest(
        **(
            {
                "account_id": f"user:{USER}",
                "provider": "openrouter",
                "model": MODEL,
                "messages": [{"role": "user", "content": "Find bounded evidence."}],
                "max_tokens": 64,
                "idempotency_key": KEY,
                "enable_web_search": True,
                "strict_continuity": True,
            }
            | changes
        )
    )


def response(searches=1):
    # Both token counts deliberately exceed the old single-pass request bounds.
    return {
        "id": "gen-fixture-web-1",
        "model": MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Evidence found.",
                    "annotations": (
                        [
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    "url": "https://example.com/evidence",
                                    "title": "Evidence",
                                    "start_index": 0,
                                    "end_index": 8,
                                },
                            }
                        ]
                        if searches
                        else []
                    ),
                },
            }
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cost": "0.0082" if searches else "0.0012",
            "server_tool_use_details": {"web_search_requests": searches},
        },
    }


class MemoryDB:
    """Strict SQL simulation with rollback and serialized transaction boundaries."""

    def __init__(self, grants=0):
        self.data = {
            "requests": {},
            "holds": {},
            "usage": [],
            "charges": [],
            "ledger": [],
            "balance": Decimal(10),
            "escrow": {},
            "grants": grants,
            "grant_usage": [],
        }
        self.lock = asyncio.Lock()
        self.lose_ack = False

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            before = copy.deepcopy(self.data)
            try:
                yield self
            except BaseException:
                self.data = before
                raise
            if self.lose_ack and any(
                r["status"] == "succeeded" for r in self.data["requests"].values()
            ):
                self.lose_ack = False
                raise ConnectionError("synthetic commit acknowledgement lost")

    async def fetchrow(self, sql, *args):
        if "INSERT INTO inference_requests" in sql:
            rid, user, key, digest = args
            if any(
                r["user_id"] == user and r["idempotency_key"] == key
                for r in self.data["requests"].values()
            ):
                return None
            self.data["requests"][rid] = {
                "id": rid,
                "user_id": user,
                "idempotency_key": key,
                "payload_hash": digest,
                "status": "processing",
                "response_status": None,
                "response_body_text": None,
                "inference_source": "wrapper",
                "updated_at": datetime.now(UTC),
            }
            return {"id": rid}
        if "FROM inference_requests" in sql:
            rows = self.data["requests"].values()
            row = next(
                (
                    r
                    for r in rows
                    if (
                        r["user_id"] == args[0] and r["idempotency_key"] == args[1]
                        if len(args) == 2
                        else r["id"] == args[0]
                    )
                ),
                None,
            )
            return copy.deepcopy(row)
        if "FROM inference_preauthorizations" in sql:
            key = "request_id" if "WHERE request_id" in sql else "id"
            return copy.deepcopy(
                next((r for r in self.data["holds"].values() if r[key] == args[0]), None)
            )
        if "UPDATE inference_token_grants" in sql:
            if self.data["grants"] >= args[1]:
                self.data["grants"] -= args[1]
                return {"id": args[0]}
            return None
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "FROM inference_token_grants" in sql:
            return (
                [{"id": "fixture-grant", "tokens_remaining": self.data["grants"]}]
                if self.data["grants"]
                else []
            )
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "INSERT INTO inference_preauthorizations" in sql:
            keys = (
                "id",
                "account_id",
                "user_id",
                "request_id",
                "provider",
                "model_id",
                "reserved_v",
            )
            self.data["holds"][args[0]] = dict(zip(keys, args)) | {
                "status": "reserved",
                "settled_v": Decimal(0),
            }
        elif "UPDATE inference_preauthorizations" in sql:
            self.data["holds"][args[0]].update(
                {"settled_v": args[1], "status": args[2]}
                if len(args) == 3
                else {"settled_v": Decimal(0), "status": "voided"}
            )
        elif "INSERT INTO inference_usage" in sql:
            keys = (
                "id",
                "request_id",
                "user_id",
                "account_id",
                "actor_type",
                "provider",
                "model_id",
                "input_tokens",
                "output_tokens",
                "v_cost",
                "actual_cost_usd",
                "inference_source",
                "wallet_funding_source",
                "billed_v_input_per_1m",
                "billed_v_output_per_1m",
                "billed_cost_input_per_1m",
                "billed_cost_output_per_1m",
            )
            self.data["usage"].append(dict(zip(keys, args)))
        elif "INSERT INTO wallet_history_projection" in sql:
            self.data["charges"].append(
                dict(
                    zip(
                        (
                            "user_id",
                            "request_id",
                            "display_amount_v",
                            "display_status",
                            "metadata_json",
                        ),
                        args,
                    )
                )
                | {"event_id": str(uuid4())}
            )
        elif "UPDATE inference_requests" in sql:
            if "SET response_body_text=$2" in sql:
                self.data["requests"][args[0]]["response_body_text"] = args[1]
            elif "SET provider_request_id=$2" in sql:
                self.data["requests"][args[0]]["provider_request_id"] = args[1]
            elif "status = 'succeeded'" in sql:
                keys = (
                    "response_body_text",
                    "final_charge_v",
                    "final_provider_cost_usd",
                    "provider_request_id",
                )
                self.data["requests"][args[0]].update(
                    dict(zip(keys, args[1:])) | {"status": "succeeded", "response_status": 200}
                )
            elif "status = 'failed'" in sql:
                self.data["requests"][args[0]].update(
                    status="failed", response_status=500, response_body_text=args[1]
                )
            else:
                raise AssertionError("Web must not reset/retry a request")
        elif "INSERT INTO inference_grant_usages" in sql:
            self.data["grant_usage"].append(args)
        else:
            raise AssertionError(sql)
        return "OK"


class MemoryWallet:
    def __init__(self, db):
        self.db = db

    async def get_balance(self, account, tx):
        return tx.data["balance"]

    def ledger(self, tx, ref, kind, debit, credit, amount):
        if amount:
            tx.data["ledger"].append(
                {
                    "id": str(uuid4()),
                    "reference_id": ref,
                    "entry_type": kind,
                    "debit_account": debit,
                    "credit_account": credit,
                    "amount": amount,
                }
            )

    async def reserve(self, *, account_id, amount, reference_id, tx):
        tx.data["balance"] -= amount
        tx.data["escrow"][reference_id] = amount
        self.ledger(tx, reference_id, "reserve", account_id, "escrow:" + reference_id, amount)

    async def settle_reservation(self, *, account_id, reservation_ref, settle_amount, tx):
        reserved = tx.data["escrow"][reservation_ref]
        assert 0 <= settle_amount <= reserved
        tx.data["escrow"][reservation_ref] = Decimal(0)
        tx.data["balance"] += reserved - settle_amount
        self.ledger(
            tx,
            reservation_ref,
            "reserve_settle",
            "escrow:" + reservation_ref,
            "store",
            settle_amount,
        )
        self.ledger(
            tx,
            reservation_ref,
            "reserve_refund",
            "escrow:" + reservation_ref,
            account_id,
            reserved - settle_amount,
        )

    async def redeem(self, **kwargs):
        raise AssertionError("No unreserved web overage permitted")


@pytest_asyncio.fixture
async def setup(monkeypatch):
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: review()})
    )
    monkeypatch.setenv("WRAPPER_REWARDS_ENABLED", "false")
    clients = []

    def create(body=None, *, grants=0, handler=None, reserved="0.366400"):
        db = MemoryDB(grants)
        observed = []

        async def respond(req):
            observed.append(json.loads(req.content))
            assert req.headers["authorization"] == "Bearer " + TOKEN
            assert next(iter(db.data["holds"].values()))["status"] == "reserved"
            assert next(iter(db.data["holds"].values()))["reserved_v"] == Decimal(reserved)
            row = next(iter(db.data["requests"].values()))
            durable = json.loads(row["response_body_text"])["managed_web_request"]
            assert durable["request_hash"] == row["payload_hash"]
            assert Decimal(durable["reserved_v"]) == Decimal(reserved)
            assert "messages" not in durable
            if handler is not None:
                return await handler(req)
            return httpx.Response(200, json=response() if body is None else body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        catalog = SimpleNamespace(get_model=AsyncMock(return_value=catalog_row()))
        gateway = AIGateway(db, MemoryWallet(db), catalog, client)
        gateway._resolve_provider_api_key = AsyncMock(return_value=TOKEN)
        return gateway, db, observed

    yield create
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("searches", [0, 1])
async def test_gateway_reserves_two_passes_and_bills_aggregate_once(setup, searches):
    gateway, db, observed = setup(response(searches))
    result = await gateway.infer(request())
    fee = Decimal("0.2") * searches
    assert result.v_cost == Decimal("0.012") + fee
    assert result.actual_cost_usd == Decimal("0.0012") + Decimal("0.007") * searches
    assert result.web_search_cost_v == fee and result.web_search_requests == searches
    assert result.web_search_used is bool(searches)
    assert result.web_search_sources == (["https://example.com/evidence"] if searches else [])
    assert len(observed) == len(db.data["usage"]) == len(db.data["charges"]) == 1
    assert db.data["balance"] == Decimal(10) - result.v_cost
    payload = observed[0]
    assert payload["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "exa",
                "mode": "fast",
                "max_uses": 1,
                "max_results": 3,
                "max_total_results": 3,
                "max_characters": 2000,
            },
        }
    ]
    assert payload["stop_server_tools_when"] == [
        {"type": "step_count_is", "step_count": 1},
        {"type": "max_cost", "max_cost_in_dollars": 0.03468},
    ]
    assert payload["provider"]["only"] == ["fixture/endpoint"]
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["provider"]["max_price"]["request"] == 0
    assert all(p["enabled"] is False for p in payload["plugins"])


@pytest.mark.asyncio
async def test_full_token_grant_does_not_cover_search_fee_or_reduce_reservation(setup):
    gateway, db, _ = setup(grants=10000)
    result = await gateway.infer(request())
    assert result.v_cost == result.web_search_cost_v == Decimal("0.2")
    assert db.data["grants"] == 8900
    assert result._managed_web_accounting["grant_v"] == "0.012000"
    assert next(iter(db.data["holds"].values()))["reserved_v"] == Decimal("0.366400")


@pytest.mark.asyncio
async def test_replay_does_not_read_new_prices_resolve_credentials_or_dispatch(setup, monkeypatch):
    gateway, db, observed = setup()
    first = await gateway.infer(request())
    before = copy.deepcopy(db.data)
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    gateway.catalog.get_model = AsyncMock(side_effect=AssertionError("No fresh catalog on replay"))
    gateway._resolve_provider_api_key = AsyncMock(
        side_effect=AssertionError("No credentials on replay")
    )
    replay = await gateway.infer(request())
    assert replay.v_cost == first.v_cost and replay.web_search_cost_v == first.web_search_cost_v
    assert replay.inference_request_id == first.inference_request_id
    assert db.data == before and len(observed) == 1


@pytest.mark.asyncio
async def test_lost_settlement_ack_replays_without_another_charge(setup):
    gateway, db, observed = setup()
    db.lose_ack = True
    with pytest.raises(ConnectionError):
        await gateway.infer(request())
    result = await gateway.infer(request())
    assert result.v_cost == Decimal("0.212")
    assert len(observed) == len(db.data["usage"]) == len(db.data["charges"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_count",
        "excess_searches",
        "excess_tokens",
        "excess_cost",
        "bad_url",
        "tool_calls",
        "unknown_model",
        "float_cost",
    ],
)
async def test_invalid_receipt_refunds_hold_but_never_retries_uncertain_attempt(setup, case):
    body = response()
    if case == "missing_count":
        del body["usage"]["server_tool_use_details"]
    elif case == "excess_searches":
        body["usage"]["server_tool_use_details"]["web_search_requests"] = 2
    elif case == "excess_tokens":
        body["usage"]["completion_tokens"] = 129
    elif case == "excess_cost":
        body["usage"]["cost"] = "0.008201"
    elif case == "bad_url":
        body["choices"][0]["message"]["annotations"][0]["url_citation"]["url"] = "http://127.0.0.1/"
    elif case == "tool_calls":
        body["choices"][0]["message"]["tool_calls"] = [{"type": "function"}]
    elif case == "unknown_model":
        body["model"] = "fixture/unreviewed"
    else:
        body["usage"]["cost"] = 0.0082  # Valid JSON number must be decoded as Decimal.
    gateway, db, observed = setup(body)
    if case == "float_cost":
        assert (await gateway.infer(request())).actual_cost_usd == Decimal("0.0082")
        return
    with pytest.raises(ContractError):
        await gateway.infer(request())
    assert db.data["balance"] == 10 and db.data["usage"] == []
    row = next(iter(db.data["requests"].values()))
    failure = json.loads(row["response_body_text"])
    assert failure["managed_web_request"]["reserved_v"] == "0.366400"
    assert row["provider_request_id"] == "gen-fixture-web-1"
    row["updated_at"] = datetime.now(UTC) - timedelta(days=1)
    with pytest.raises(ContractError) as error:
        await gateway.infer(request())
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "tools",
        "attachments",
        "media",
        "missing_key",
        "agent",
        "missing_review",
        "unreviewed_plugins",
        "low_cap",
        "catalog_price",
        "forged_context",
    ],
)
async def test_preflight_fails_explicitly_before_credentials_reservation_or_network(
    setup, monkeypatch, case
):
    gateway, db, observed = setup()
    req = request()
    config = review()
    if case == "tools":
        req.enable_tools = True
        config["capabilities"]["tools"] = False
    elif case == "attachments":
        req._managed_attachment_context = object()
    elif case == "media":
        req.messages[0]["content"] = [{"type": "image_url"}]
    elif case == "missing_key":
        req.idempotency_key = None
    elif case == "agent":
        req.account_id = "agent:fixture"
    elif case == "missing_review" or case == "forged_context":
        config.pop("web_search")
        req._managed_web_context = object()
    elif case == "unreviewed_plugins":
        config["web_search"]["execution"]["account_plugins_reviewed"] = False
    elif case == "low_cap":
        config["web_search"]["prices"]["retail_cap_v"] = "0.01"
    elif case == "catalog_price":
        gateway.catalog.get_model.return_value["v_price_input_per_1m"] = "11"
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: config}))
    with pytest.raises(ContractError):
        await gateway.infer(req)
    gateway._resolve_provider_api_key.assert_not_called()
    assert not observed and not db.data["holds"] and not db.data["requests"]


@pytest.mark.asyncio
async def test_frozen_dispatch_prices_and_reasoning_survive_review_change(setup, monkeypatch):
    async def handler(req):
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
        gateway.catalog.get_model.return_value["v_price_output_per_1m"] = "10000"
        return httpx.Response(200, json=response())

    gateway, db, observed = setup(handler=handler)
    req = request(reasoning_effort="low")
    ctx, replay = await gateway._prepare_inference_execution(req)
    assert replay is None
    result = await gateway._route_to_provider(req, ctx.provider_api_key)
    settled = await gateway._finalize_inference_execution(ctx, req, result)
    again = await gateway._finalize_inference_execution(ctx, req, result)
    assert observed[0]["reasoning"] == {"effort": "low", "exclude": True}
    assert settled.v_cost == again.v_cost == Decimal("0.212")
    assert len(db.data["usage"]) == 1


@pytest.mark.asyncio
async def test_mutated_public_request_cannot_reuse_private_context(setup):
    gateway, db, observed = setup()
    req = request()
    ctx, _ = await gateway._prepare_inference_execution(req)
    req.max_tokens = 1024
    with pytest.raises(ContractError):
        await gateway._route_to_provider(req, ctx.provider_api_key)
    await gateway._cleanup_inference_after_failure(ctx)
    assert not observed and db.data["balance"] == 10


@pytest.mark.asyncio
async def test_stream_is_one_buffered_web_dispatch_and_one_settlement(setup):
    gateway, db, observed = setup()
    events = [event async for event in gateway.stream_infer(request())]
    assert [event["type"] for event in events] == ["text_delta", "completed"]
    assert events[-1]["result"].web_search_requests == 1
    assert len(observed) == len(db.data["usage"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["close", "cancel"])
@pytest.mark.parametrize("searches", [0, 1])
async def test_buffered_stream_settles_before_answer_and_replays_after_interruption(
    setup, monkeypatch, interruption, searches,
):
    gateway, db, observed = setup(response(searches))
    stream = gateway.stream_infer(request(enable_tools=True))
    delivered = asyncio.Event()
    received = []

    async def consume():
        try:
            async for event in stream:
                received.append(event)
                delivered.set()
                await asyncio.Event().wait()
        finally:
            await stream.aclose()

    consumer = asyncio.create_task(consume()) if interruption == "cancel" else None
    try:
        if consumer is not None:
            await asyncio.wait_for(delivered.wait(), timeout=2)
            first = received[0]
        else:
            first = await anext(stream)
        assert first == {"type": "text_delta", "text": "Evidence found."}
        # Check while suspended at the first answer, not after normal exhaustion.
        row = next(iter(db.data["requests"].values()))
        hold = next(iter(db.data["holds"].values()))
        charge = Decimal("0.012") + Decimal("0.2") * searches
        assert row["status"] == "succeeded" and row["response_status"] == 200
        assert hold["status"] == "settled" and hold["settled_v"] == charge
        assert len(db.data["usage"]) == len(db.data["charges"]) == len(observed) == 1
        assert db.data["usage"][0]["actual_cost_usd"] == Decimal(response(searches)["usage"]["cost"])
        assert db.data["balance"] == Decimal(10) - charge
        committed = copy.deepcopy(db.data)
    finally:
        if consumer is not None:
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        else:
            await stream.aclose()
    assert db.data == committed
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    replay = await gateway.infer(request(enable_tools=True))
    assert replay.text == first["text"] and replay.inference_request_id == row["id"]
    assert replay.v_cost == charge and replay.web_search_requests == searches
    assert replay.web_search_cost_v == Decimal("0.2") * searches
    assert db.data == committed and len(observed) == 1
    gateway._resolve_provider_api_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_true_openai_stream_keeps_incremental_deltas_before_final_settlement():
    gateway = AIGateway(None, None, None)
    req = request(provider="openai", model="gpt-5", enable_web_search=False)
    ctx = SimpleNamespace(provider_api_key=TOKEN)
    result = InferenceResult("First second", 10, 2)
    gateway._prepare_inference_execution = AsyncMock(return_value=(ctx, None))
    gateway._finalize_inference_execution = AsyncMock(return_value=result)
    gateway._route_to_provider = AsyncMock(side_effect=AssertionError("Must keep real streaming branch"))

    async def upstream(**kwargs):
        assert kwargs == {"req": req, "api_key": TOKEN}
        yield {"type": "text_delta", "text": "First"}
        yield {"type": "text_delta", "text": " second"}
        yield {"type": "completed", "result": result}

    gateway._stream_openai_responses = upstream
    stream = gateway.stream_infer(req)
    try:
        assert await anext(stream) == {"type": "text_delta", "text": "First"}
        gateway._finalize_inference_execution.assert_not_awaited()
        assert await anext(stream) == {"type": "text_delta", "text": " second"}
        gateway._finalize_inference_execution.assert_not_awaited()
        assert await anext(stream) == {"type": "completed", "result": result}
        gateway._finalize_inference_execution.assert_awaited_once_with(ctx, req, result)
        gateway._route_to_provider.assert_not_awaited()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_concurrent_same_key_has_one_dispatch(setup):
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(req):
        started.set()
        await release.wait()
        return httpx.Response(200, json=response())

    gateway, db, observed = setup(handler=handler)
    running = asyncio.create_task(gateway.infer(request()))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        with pytest.raises(ContractError):
            await gateway.infer(request())
    finally:
        release.set()
    await running
    assert len(observed) == len(db.data["usage"]) == 1


def test_private_context_is_not_a_public_constructor_field_and_capability_is_reviewed():
    with pytest.raises(TypeError):
        request(_managed_web_context={"approved": True})
    assert reviewed_web_capability(MODEL, review())
    assert not reviewed_web_capability(MODEL, {})


@pytest.mark.asyncio
async def test_exact_two_context_two_output_boundary_fits_full_reservation(setup):
    body = response()
    body["usage"].update(
        prompt_tokens=16384, completion_tokens=128, total_tokens=16512, cost="0.02364"
    )
    gateway, db, observed = setup(body)
    result = await gateway.infer(request())
    hold = next(iter(db.data["holds"].values()))
    assert result.v_cost == hold["reserved_v"] == hold["settled_v"] == Decimal("0.3664")
    assert result.actual_cost_usd == Decimal("0.02364")
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_cancellation_refunds_and_blocks_automatic_retry(setup):
    started = asyncio.Event()

    async def handler(req):
        started.set()
        await asyncio.Event().wait()

    gateway, db, observed = setup(handler=handler)
    running = asyncio.create_task(gateway.infer(request()))
    await asyncio.wait_for(started.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert db.data["balance"] == 10 and db.data["usage"] == []
    with pytest.raises(ContractError) as error:
        await gateway.infer(request())
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_same_key_changed_web_intent_is_conflict_not_second_paid_dispatch(setup):
    gateway, db, observed = setup()
    await gateway.infer(request())
    with pytest.raises(ContractError) as error:
        await gateway.infer(request(messages=[{"role": "user", "content": "Different intent"}]))
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert len(observed) == len(db.data["usage"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("searches", [0, 1])
@pytest.mark.parametrize("flat", [False, True])
async def test_local_tools_and_web_compose_preserve_ids_and_settle_once(
    setup, monkeypatch, searches, flat
):
    model = "google/fixture-model-20260901" if flat else MODEL
    body = response(searches)
    body["model"] = model
    body["choices"][0]["finish_reason"] = "tool_calls"
    function = (
        {"name": "Read", "arguments": json.dumps({"path": "README.md"})}
        if flat
        else {
            "name": "call_local_tool",
            "arguments": json.dumps({"tool_name": "Read", "arguments": {"path": "README.md"}}),
        }
    )
    body["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call-preserved-fixture-id",
            "type": "function",
            "function": function,
        }
    ]
    body["usage"]["server_tool_use_details"].update(
        tool_calls_requested=searches, tool_calls_executed=searches
    )
    policy = review()
    policy["web_search"]["execution"]["model"] = model
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + model: policy}))
    gateway, db, observed = setup(body)
    gateway.catalog.get_model.return_value["model_id"] = model
    result = await gateway.infer(request(model=model, enable_tools=True))
    assert result.tool_calls == [
        {
            "tool_name": "Read",
            "arguments": {"path": "README.md"},
            "shell_mode": "read_only",
            "timeout_sec": 30,
            "provider_call_id": "call-preserved-fixture-id",
        }
    ]
    assert result.completion_status == "incomplete"
    assert result.web_search_requests == searches
    assert result.v_cost == Decimal("0.012") + Decimal("0.2") * searches
    assert observed[0]["tools"][0]["type"] == "openrouter:web_search"
    functions = [t["function"]["name"] for t in observed[0]["tools"][1:]]
    assert ("Read" in functions) if flat else functions == ["call_local_tool"]
    replay = await gateway.infer(request(model=model, enable_tools=True))
    assert (
        replay.tool_calls == result.tool_calls
        and replay.web_search_cost_v == result.web_search_cost_v
    )
    assert len(observed) == len(db.data["usage"]) == 1


@pytest.mark.asyncio
async def test_local_only_call_can_have_null_text_and_explicit_zero_search_usage(setup):
    body = response(0)
    body["choices"][0] = {
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-only-local",
                    "type": "function",
                    "function": {
                        "name": "call_local_tool",
                        "arguments": '{"tool_name":"List","arguments":{}}',
                    },
                }
            ],
        },
    }
    gateway, _, observed = setup(body)
    result = await gateway.infer(request(enable_tools=True))
    assert result.text == "" and result.tool_calls[0]["provider_call_id"] == "call-only-local"
    assert result.web_search_requests == 0 and result.web_search_cost_v == 0
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_standard_tools_enabled_chat_can_return_final_web_answer(setup):
    gateway, db, observed = setup()
    result = await gateway.infer(request(enable_tools=True))
    assert result.completion_status == "complete" and result.web_search_requests == 1
    assert len(observed[0]["tools"]) == 2
    assert len(db.data["usage"]) == 1


async def attachment_web_request(monkeypatch, *, kind="image", mismatch=False):
    from server.services.file_uploads import FileUploadService
    from server.services.openrouter_attachment_request import prepare_attachment_request
    from tests.test_models.test_openrouter_attachment_request import (
        IMAGE_ID,
        UploadDB,
        model_review,
    )

    policy = review()
    attachment_policy = model_review()
    policy.update({k: v for k, v in catalog_row().items() if "_per_1m" in k})
    policy.update(
        max_tokens=1024,
        context_window_tokens=16384,
        attachments=attachment_policy["attachments"],
        observed_pricing=attachment_policy["observed_pricing"],
    )
    policy["web_search"]["execution"]["context_window_tokens"] = 16384
    policy["attachments"]["provider"] = (
        "fixture/different-endpoint" if mismatch else "fixture/endpoint"
    )
    policy["attachments"]["pdf_page_tokens"] = 1000
    uploads = UploadDB()
    file_id = IMAGE_ID
    if kind == "pdf":
        import io

        from pypdf import PdfWriter

        from server.services import openrouter_attachments as normalization

        stream = io.BytesIO()
        pdf = PdfWriter()
        pdf.add_blank_page(width=100, height=100)
        pdf.write(stream)
        file_id = str(uuid4())
        uploads.put(file_id, stream.getvalue(), "application/pdf", "fixture.pdf")
        # PDF sandbox resource limits are not available on this Mac. Composition
        # uses a known synthetic PDF; content/isolation checks have separate tests.
        monkeypatch.setattr(normalization, "_inspect_pdf", lambda _: (1, 100))
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps({"openrouter:" + MODEL: policy}))
    messages, context, _ = await prepare_attachment_request(
        history=[],
        prompt="Compare this upload with web evidence.",
        file_ids=[file_id],
        user_id=USER,
        model_id=MODEL,
        model_config=catalog_row(),
        upload_service=FileUploadService(uploads),
    )
    req = request(messages=messages, enable_tools=True)
    req._managed_attachment_context = context
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "pdf"])
async def test_compatible_owned_attachments_local_tools_and_web_compose(setup, monkeypatch, kind):
    from server.services import openrouter_attachments as normalization

    async def handler(outbound):
        class AfterReviewExpires(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=2)

        monkeypatch.setattr(normalization, "datetime", AfterReviewExpires)
        return httpx.Response(200, json=response())

    req = await attachment_web_request(monkeypatch, kind=kind)
    original = copy.deepcopy(req.messages)
    gateway, db, observed = setup(handler=handler, reserved="0.530240")
    result = await gateway.infer(req)
    assert observed[0]["messages"] == original and req.messages == original
    assert result.v_cost == Decimal("0.212") and result.web_search_requests == 1
    assert observed[0]["provider"]["only"] == ["fixture/endpoint"]
    assert observed[0]["provider"]["max_price"]["image"] == 0
    parser = next(p for p in observed[0]["plugins"] if p["id"] == "file-parser")
    assert parser == (
        {"id": "file-parser", "pdf": {"engine": "native"}}
        if kind == "pdf"
        else {"id": "file-parser", "enabled": False}
    )
    assert len(observed[0]["tools"]) == 2 and len(db.data["usage"]) == 1


@pytest.mark.asyncio
async def test_different_attachment_and_web_endpoint_is_explicit_preflight_block(
    setup, monkeypatch
):
    req = await attachment_web_request(monkeypatch, mismatch=True)
    gateway, db, observed = setup(reserved="0.530240")
    with pytest.raises(ContractError, match="web_attachment_review_mismatch"):
        await gateway.infer(req)
    gateway._resolve_provider_api_key.assert_not_called()
    assert not observed and not db.data["holds"]


@pytest.mark.asyncio
@pytest.mark.parametrize("searches", [0, 1])
@pytest.mark.parametrize("tools", [False, True])
async def test_completed_web_recovery_uses_stored_evidence_without_rebilling(
    setup, searches, monkeypatch, tools
):
    from tests.test_models import test_reconciliation as recovery_module
    from tests.test_models.test_reconciliation import MemoryDB as RecoveryDB

    gateway, db, observed = setup(response(searches))
    req = request(enable_tools=tools)
    result = await gateway.infer(req)
    thread = str(uuid4())
    operator = str(uuid4())
    for name, value in {
        "USER": USER,
        "THREAD": thread,
        "REQUEST": result.inference_request_id,
        "OPERATOR": operator,
    }.items():
        monkeypatch.setattr(recovery_module, name, value)
    history = CanonicalConversation.from_messages([])
    pending = json.loads(history.to_json()) | {"pending": KEY}
    row = copy.deepcopy(db.data["requests"][result.inference_request_id])
    recovery = RecoveryDB(
        {
            "thread": {
                "id": thread,
                "user_id": USER,
                "provider": "openrouter",
                "model_id": MODEL,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            },
            "records": [{"id": str(uuid4()), "role": "system", "content": json.dumps(pending)}],
            "request": row,
            "holds": list(db.data["holds"].values()),
            "usage": db.data["usage"],
            "charges": db.data["charges"],
            "ledger": db.data["ledger"],
            "escrow": {"balance": Decimal(0)},
            "receipts": [],
        }
    )
    scope = {
        "user_id": USER,
        "thread_id": thread,
        "request_id": result.inference_request_id,
        "prompt": req.messages[-1]["content"],
        "max_tokens": req.max_tokens,
    }
    before = copy.deepcopy(db.data)
    plan = await inspect_turn(recovery, **scope)
    assert plan["can_restore"] and plan["settlement_verified"]
    restored = await restore_turn(
        recovery, **scope, operator_id=operator, expected_plan=plan["plan_token"]
    )
    assert restored["status"] == "restored"
    replay = await inspect_turn(recovery, **scope)
    assert replay["status"] == "already_reconciled"
    assert db.data == before and len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("web_search_cost_v", "0.4"),
        ("web_search_requests", 0),
        ("v_cost", "0"),
        ("web_search_sources", ["http://127.0.0.1/"]),
    ],
)
async def test_tampered_stored_receipt_cannot_replay(setup, field, value):
    gateway, db, observed = setup()
    result = await gateway.infer(request())
    row = db.data["requests"][result.inference_request_id]
    body = json.loads(row["response_body_text"])
    body[field] = value
    row["response_body_text"] = json.dumps(body)
    with pytest.raises(ContractError):
        await gateway.infer(request())
    assert len(observed) == 1
