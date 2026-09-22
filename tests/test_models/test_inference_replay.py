"""Offline route replay contract, including transaction rollback/concurrency.

The DB boundary is simulated, not a PostgreSQL certification. No provider, real
credential, wallet, migration, or remote database is used. HTTP cases exercise
the real endpoints and replay helper, with a transactional DB/provider boundary.
"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult
from server.services.inference_replay import (
    GATEWAY_KEY_PREFIX,
    MAX_COMMAND_BYTES,
    MAX_ENVELOPE_BYTES,
    MAX_RESPONSE_BYTES,
    InferenceReplayService,
    command_fingerprint,
)

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
THREAD = "33333333-3333-4333-8333-333333333333"
FILE = "44444444-4444-4444-8444-444444444444"


def command(**changes):
    return {
        "prompt": "Describe my private image.",
        "provider": "openrouter",
        "model": "fixture/attachment-model",
        "thread_id": THREAD,
        "conversation_mode": "persistent",
        "persist_context": True,
        "enable_tools": False,
        "enable_web_search": False,
        "attachments": [FILE],
        "reasoning_effort": None,
        **changes,
    }


def response(**changes):
    return {
        "text": "An amber square.",
        "v_cost": "0.000123",
        "thread_id": THREAD,
        "input_tokens": 5001,
        "output_tokens": 4,
        "run_id": "original-run",
        "attachments_used": True,
        "warnings": [],
        "tool_calls": [],
        "diagnostics": {"history_used": 2},
        **changes,
    }


class MemoryTx:
    def __init__(self, db):
        self.db = db

    async def fetchrow(self, query, *args):
        self.db.queries.append((query, args))
        if "INSERT INTO inference_route_commands" in query:
            rid, user, key, digest, body = args
            scope = (user, key)
            if scope in self.db.rows:
                return None
            self.db.rows[scope] = {
                "id": rid,
                "user_id": user,
                "idempotency_key": key,
                "payload_hash": digest,
                "status": "processing",
                "response_status": None,
                "response_body_text": body,
            }
            return {"id": rid}
        if "FOR UPDATE" in query:
            assert "FROM inference_route_commands" in query
            user, key, bound = args
            assert "octet_length(response_body_text)" in query
            row = copy.deepcopy(self.db.rows.get((user, key)))
            if (
                row
                and isinstance(row["response_body_text"], str)
                and len(row["response_body_text"].encode()) > bound
            ):
                row["response_body_text"] = None
            return row
        if "FROM inference_requests" in query and "ANY($2::text[])" in query:
            user, keys = args
            return next(
                (
                    copy.deepcopy(row)
                    for row in self.db.gateway_rows.values()
                    if row["user_id"] == user and row["idempotency_key"] in keys
                ),
                None,
            )
        if query.lstrip().startswith("SELECT"):
            assert "FROM inference_requests" in query
            rid, user = args
            return next(
                (
                    copy.deepcopy(row)
                    for row in self.db.gateway_rows.values()
                    if row["id"] == rid and row["user_id"] == user
                ),
                None,
            )
        if "UPDATE inference_route_commands" in query:
            rid, user, body = args
            if self.db.fail_update:
                raise RuntimeError("Synthetic response write failure")
            for row in self.db.rows.values():
                if row["id"] == rid and row["user_id"] == user and row["status"] == "processing":
                    row.update(status="succeeded", response_status=200, response_body_text=body)
                    return {"id": rid}
            return None
        if "DELETE FROM inference_route_commands" in query:
            rid, user = args
            for key, row in self.db.rows.items():
                if row["id"] == rid and row["user_id"] == user and row["status"] == "processing":
                    del self.db.rows[key]
                    return {"id": rid}
            return None
        raise AssertionError(f"Unexpected SQL: {query}")

    async def execute(self, query, *args):
        if query == "INSERT TEST ROUTE HISTORY":
            self.db.history.extend(copy.deepcopy(args))
            return "INSERT 0 2"
        assert query == "INSERT TEST HISTORY"
        self.db.history.append(copy.deepcopy(args))
        return "INSERT 0 1"


class MemoryDB:
    """Serializable transaction fake with rollback and commit-ack loss injection."""

    def __init__(self):
        self.rows, self.gateway_rows, self.history, self.queries = {}, {}, [], []
        self.lock = asyncio.Lock()
        self.fail_update = False
        self.lose_commit_ack = False

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            before = copy.deepcopy((self.rows, self.gateway_rows, self.history))
            try:
                yield MemoryTx(self)
            except BaseException:
                self.rows, self.gateway_rows, self.history = before
                raise
            if self.lose_commit_ack:
                self.lose_commit_ack = False
                raise ConnectionError("Synthetic lost commit acknowledgement")


async def begin(service, *, user=OWNER, key="client-key", incoming=None):
    return await service.begin(
        user_id=user, idempotency_key=key, command=command() if incoming is None else incoming
    )


def settled_gateway(db, claim, **changes):
    rid = str(uuid4())
    result = InferenceResult("An amber square.", 5001, 4, v_cost=Decimal("0.000123"))
    db.gateway_rows[(claim.user_id, claim.gateway_idempotency_key)] = {
        "id": rid,
        "user_id": claim.user_id,
        "idempotency_key": claim.gateway_idempotency_key,
        "payload_hash": "f" * 64,
        "status": "succeeded",
        "response_status": 200,
        "response_body_text": AIGateway._serialize_success_body(result, reward_v=Decimal(0)),
        "inference_source": "wrapper",
        "final_charge_v": Decimal("0.000123"),
        "final_provider_cost_usd": Decimal("0.00001"),
        **changes,
    }
    return rid


async def append(tx):
    await tx.execute("INSERT TEST HISTORY", "user", command()["prompt"], FILE)
    await tx.execute("INSERT TEST HISTORY", "assistant", response()["text"])


@pytest.fixture
def setup():
    db = MemoryDB()
    return db, InferenceReplayService(db)


def test_fingerprint_normalizes_defaults_and_object_order_only():
    minimal = {"prompt": "x", "provider": "openrouter", "model": "fixture/model"}
    full = command(**minimal, attachments=[], thread_id=None, conversation_mode=None)
    assert command_fingerprint(minimal) == command_fingerprint(full)
    assert command_fingerprint(dict(reversed(list(full.items())))) == command_fingerprint(full)
    assert command_fingerprint(command(prompt="x")) != command_fingerprint(command(prompt="x "))
    assert command_fingerprint(command(prompt="\u00e9")) != command_fingerprint(
        command(prompt="e\u0301")
    )
    assert command_fingerprint(command(attachments=[FILE, OTHER])) != command_fingerprint(
        command(attachments=[OTHER, FILE])
    )


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "Different question"},
        {"provider": "anthropic"},
        {"model": "fixture/other"},
        {"thread_id": OTHER},
        {"conversation_mode": "ephemeral"},
        {"persist_context": False},
        {"enable_tools": True},
        {"enable_web_search": True},
        {"attachments": [OTHER]},
        {"reasoning_effort": "high"},
    ],
)
@pytest.mark.asyncio
async def test_each_incoming_field_changes_identity_and_conflicts(setup, change):
    db, service = setup
    first = await begin(service)
    with pytest.raises(ContractError) as error:
        await begin(service, incoming=command(**change))
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert len(db.rows) == 1 and first.response is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"prompt": "missing fields"},
        command(account_id=OTHER),
        command(idempotency_key="injected"),
        command(_managed_attachment_context={}),
        command(prompt=42),
        command(provider=""),
        command(model="m" * 301),
        command(enable_tools=1),
        command(persist_context="yes"),
        command(enable_web_search=None),
        command(thread_id={}),
        command(reasoning_effort=["high"]),
        command(attachments="url"),
        command(attachments=["https://example.test/private.png"]),
        command(attachments=[FILE] * 9),
        command(prompt="a" * MAX_COMMAND_BYTES),
        command(prompt="\ud800"),
    ],
)
def test_invalid_or_unbounded_command_rejected(bad):
    with pytest.raises(ContractError) as error:
        command_fingerprint(bad)
    assert error.value.code == APIErrorCode.INVALID_TRANSITION


@pytest.mark.parametrize(
    "bad_key",
    [None, "", "   ", "a" * 201, "\u4f60" * 67, "x\n", "x\x7f", "\ud800", GATEWAY_KEY_PREFIX + "x"],
)
@pytest.mark.asyncio
async def test_key_validation_before_database(setup, bad_key):
    db, service = setup
    with pytest.raises(ContractError):
        await begin(service, key=bad_key)
    assert not db.queries


@pytest.mark.parametrize(
    "user", [None, "agent:bot", "other", "0" * 36, "00000000-0000-0000-0000-000000000000"]
)
@pytest.mark.asyncio
async def test_authenticated_canonical_uuid_required(setup, user):
    db, service = setup
    with pytest.raises(ContractError):
        await begin(service, user=user)
    assert not db.queries


@pytest.mark.asyncio
async def test_claim_is_scoped_and_stores_no_prompt_or_file_bytes(setup):
    db, service = setup
    first = await begin(service)
    second = await begin(service, user=OTHER)
    third = await begin(service, key="another-key")
    assert len({item.gateway_idempotency_key for item in (first, second, third)}) == 3
    assert first.gateway_idempotency_key.startswith(GATEWAY_KEY_PREFIX)
    assert len(first.gateway_idempotency_key) <= 200
    assert first.gateway_idempotency_key != first.idempotency_key
    row = db.rows[(OWNER, "client-key")]
    assert row["id"] == first.request_id and row["payload_hash"] == command_fingerprint(command())
    assert command()["prompt"] not in row["response_body_text"]
    assert FILE not in row["response_body_text"]
    assert "final_charge_v" not in row and "final_provider_cost_usd" not in row
    assert not db.gateway_rows
    assert "owner_token=" not in repr(first) and "idempotency_key='client-key'" not in repr(first)


@pytest.mark.asyncio
async def test_pending_never_reclaimed_even_if_old_or_new_service_instance(setup):
    db, service = setup
    first = await begin(service)
    db.rows[(OWNER, "client-key")]["updated_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ContractError) as error:
        await begin(InferenceReplayService(db))
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert db.rows[(OWNER, "client-key")]["id"] == first.request_id
    assert not db.history and len(db.rows) == 1


@pytest.mark.asyncio
async def test_concurrent_begin_grants_exactly_one_dispatch_claim(setup):
    db, service = setup
    outcomes = await asyncio.gather(*(begin(service) for _ in range(8)), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert all(
        not isinstance(item, Exception)
        or isinstance(item, ContractError)
        and item.code == APIErrorCode.HOLD_CONFLICT
        for item in outcomes
    )
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_complete_and_replay_preserve_exact_original_metadata_and_append_once(setup):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    original_gateway = copy.deepcopy(db.gateway_rows[(OWNER, claim.gateway_idempotency_key)])
    first = await service.complete(
        claim, gateway_request_id=rid, response=response(), append=append
    )
    first["diagnostics"]["history_used"] = 999
    replay = await begin(InferenceReplayService(db))
    assert replay.response == response() and len(db.history) == 2
    replay.response["warnings"].append("mutated")
    assert replay.response == response()
    repeat = await service.complete(
        claim, gateway_request_id=rid, response=response(run_id="must-not-replace"), append=append
    )
    assert repeat == response() and len(db.history) == 2
    assert db.gateway_rows[(OWNER, claim.gateway_idempotency_key)] == original_gateway
    assert "final_charge_v" not in db.rows[(OWNER, "client-key")]


@pytest.mark.asyncio
async def test_concurrent_completions_append_once(setup):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    results = await asyncio.gather(
        *(
            service.complete(
                claim,
                gateway_request_id=rid,
                response=response(run_id=f"run-{index}"),
                append=append,
            )
            for index in range(8)
        )
    )
    assert len(db.history) == 2
    assert all(result == results[0] for result in results)


@pytest.mark.parametrize("failure", ["callback", "write", "cancel"])
@pytest.mark.asyncio
async def test_history_and_response_rollback_together(setup, failure):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)

    async def broken(tx):
        await append(tx)
        if failure == "callback":
            raise RuntimeError("Synthetic append failure")
        if failure == "cancel":
            raise asyncio.CancelledError

    db.fail_update = failure == "write"
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await service.complete(claim, gateway_request_id=rid, response=response(), append=broken)
    assert not db.history
    assert db.rows[(OWNER, "client-key")]["status"] == "processing"
    with pytest.raises(ContractError):
        await begin(service)
    db.fail_update = False
    assert await service.complete(claim, gateway_request_id=rid, response=response(), append=append)
    assert len(db.history) == 2


@pytest.mark.asyncio
async def test_lost_commit_ack_replays_without_second_append(setup):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    db.lose_commit_ack = True
    with pytest.raises(ConnectionError):
        await service.complete(claim, gateway_request_id=rid, response=response(), append=append)
    assert (await begin(InferenceReplayService(db))).response == response()
    assert len(db.history) == 2


@pytest.mark.asyncio
async def test_begin_commit_ack_loss_remains_blocked_not_second_dispatch(setup):
    db, service = setup
    db.lose_commit_ack = True
    with pytest.raises(ConnectionError):
        await begin(service)
    with pytest.raises(ContractError) as error:
        await begin(service)
    assert error.value.code == APIErrorCode.HOLD_CONFLICT and len(db.rows) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": OTHER},
        {"idempotency_key": "other-key"},
        {"status": "processing"},
        {"status": "failed"},
        {"response_status": 500},
        {"inference_source": "byok"},
    ],
)
@pytest.mark.asyncio
async def test_completion_requires_matching_owned_successful_gateway_request(setup, changes):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim, **changes)
    with pytest.raises(ContractError) as error:
        await service.complete(claim, gateway_request_id=rid, response=response(), append=append)
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert not db.history


@pytest.mark.asyncio
async def test_no_gateway_outcome_no_history_append(setup):
    db, service = setup
    claim = await begin(service)
    with pytest.raises(ContractError):
        await service.complete(
            claim, gateway_request_id=str(uuid4()), response=response(), append=append
        )
    assert not db.history


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_token": OTHER},
        {"request_id": OTHER},
        {"user_id": OTHER},
        {"gateway_idempotency_key": "spoof"},
        {"command_hash": "0" * 64},
    ],
)
@pytest.mark.asyncio
async def test_tampered_claim_cannot_complete(setup, changes):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    with pytest.raises(ContractError):
        await service.complete(
            replace(claim, **changes), gateway_request_id=rid, response=response(), append=append
        )
    assert not db.history


@pytest.mark.parametrize(
    "body",
    [
        None,
        "{}",
        "[]",
        "null",
        "broken",
        "x" * (MAX_ENVELOPE_BYTES + 1),
        '{"text":"legacy gateway response"}',
    ],
)
@pytest.mark.asyncio
async def test_legacy_and_malformed_stored_rows_never_reopened(setup, body):
    db, service = setup
    await begin(service)
    row = db.rows[(OWNER, "client-key")]
    row.update(status="succeeded", response_status=200, response_body_text=body)
    before = copy.deepcopy(row)
    with pytest.raises(ContractError) as error:
        await begin(service)
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert db.rows[(OWNER, "client-key")] == before


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "other"},
        {"owner_token": "bad"},
        {"gateway_key": "wrong"},
        {"state": "failed"},
        {"response": {}},
        {"extra": "ignored?"},
    ],
)
@pytest.mark.asyncio
async def test_envelope_tampering_fails_closed(setup, changes):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    row = db.rows[(OWNER, "client-key")]
    row["response_body_text"] = json.dumps({**json.loads(row["response_body_text"]), **changes})
    with pytest.raises(ContractError):
        await service.complete(claim, gateway_request_id=rid, response=response(), append=append)
    assert not db.history


def invalid_responses():
    cycle = {}
    cycle["loop"] = cycle
    deep = {}
    for _ in range(20):
        deep = {"more": deep}
    return [
        None,
        [],
        {"data": b"bytes"},
        {"n": float("nan")},
        {"n": float("inf")},
        {"n": 2**80},
        {1: "nonstring key"},
        {"s": "\ud800"},
        {"s": "\u4f60" * MAX_RESPONSE_BYTES},
        {"s": "x" * MAX_RESPONSE_BYTES},
        {"items": [0] * 32769},
        cycle,
        deep,
    ]


@pytest.mark.parametrize("bad_response", invalid_responses())
@pytest.mark.asyncio
async def test_oversized_or_invalid_response_fails_before_append(setup, bad_response):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    before = len(db.queries)
    with pytest.raises(ContractError) as error:
        await service.complete(claim, gateway_request_id=rid, response=bad_response, append=append)
    assert error.value.code == APIErrorCode.INVALID_TRANSITION
    assert len(db.queries) == before and not db.history


@pytest.mark.asyncio
async def test_response_is_snapshotted_before_callback_mutates_caller_dict(setup):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    outgoing = response()

    async def mutate(tx):
        outgoing["diagnostics"]["history_used"] = 999
        await append(tx)

    actual = await service.complete(claim, gateway_request_id=rid, response=outgoing, append=mutate)
    assert actual == response() and (await begin(service)).response == response()


@pytest.mark.asyncio
async def test_parent_route_pattern_replays_before_history_rematerialization(setup):
    db, service = setup
    calls = {"prepare": 0, "provider": 0}

    async def route(incoming):
        claim = await begin(service, incoming=incoming)
        if claim.response is not None:
            return claim.response
        calls["prepare"] += 1
        # A second preparation would include the just-appended exchange and files.
        calls["provider"] += 1
        rid = settled_gateway(db, claim)
        return await service.complete(
            claim, gateway_request_id=rid, response=response(), append=append
        )

    first = await route(command())
    assert db.history
    second = await route(command())
    assert second == first
    assert calls == {"prepare": 1, "provider": 1} and len(db.history) == 2
    with pytest.raises(ContractError) as error:
        await route(command(prompt="Actually a new question"))
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert calls == {"prepare": 1, "provider": 1}


@pytest.mark.asyncio
async def test_gateway_full_materialized_payload_identity_is_still_strict(setup):
    db, service = setup
    claim = await begin(service)
    original = InferenceRequest(
        f"user:{OWNER}",
        "openrouter",
        "fixture/model",
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe it"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
            },
        ],
        idempotency_key=claim.gateway_idempotency_key,
    )
    changed = copy.deepcopy(original)
    changed.messages += [
        {"role": "assistant", "content": "Previous answer"},
        copy.deepcopy(original.messages[0]),
    ]
    assert AIGateway._payload_hash(original) != AIGateway._payload_hash(changed)
    changed_bytes = copy.deepcopy(original)
    changed_bytes.messages[0]["content"][1]["image_url"]["url"] = "data:image/png;base64,REVG"
    assert AIGateway._payload_hash(original) != AIGateway._payload_hash(changed_bytes)

    class GatewayTx:
        async def fetchrow(self, query, *args):
            if "INSERT" in query:
                return None
            return {
                "id": str(uuid4()),
                "payload_hash": AIGateway._payload_hash(original),
                "status": "succeeded",
                "response_status": 200,
                "response_body_text": "{}",
            }

    gateway = AIGateway(db, None, None)
    with pytest.raises(ContractError) as error:
        await gateway._begin_inference_request(
            user_id=OWNER,
            idempotency_key=claim.gateway_idempotency_key,
            payload_hash=AIGateway._payload_hash(changed),
            tx=GatewayTx(),
        )
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_abandon_only_preflight_claim_permits_fresh_claim_with_new_owner_token(setup):
    db, service = setup
    first = await begin(service)
    assert await service.abandon_before_dispatch(first) is True
    assert await service.abandon_before_dispatch(first) is False
    assert not db.rows and not db.gateway_rows and not db.history
    second = await begin(service)
    assert first.request_id != second.request_id and first.owner_token != second.owner_token
    assert first.gateway_idempotency_key == second.gateway_idempotency_key
    with pytest.raises(ContractError):
        await service.abandon_before_dispatch(first)
    assert db.rows[(OWNER, "client-key")]["id"] == second.request_id


@pytest.mark.parametrize("status", ["processing", "failed", "succeeded"])
@pytest.mark.parametrize("key_kind", ["original", "derived"])
@pytest.mark.asyncio
async def test_abandon_cannot_remove_claim_when_any_gateway_attempt_exists(setup, status, key_kind):
    db, service = setup
    claim = await begin(service)
    settled_gateway(
        db,
        claim,
        status=status,
        idempotency_key=(
            claim.idempotency_key if key_kind == "original" else claim.gateway_idempotency_key
        ),
    )
    before = copy.deepcopy((db.rows, db.gateway_rows))
    with pytest.raises(ContractError) as error:
        await service.abandon_before_dispatch(claim)
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert (db.rows, db.gateway_rows) == before


@pytest.mark.parametrize(
    "change",
    [
        {"owner_token": OTHER},
        {"request_id": OTHER},
        {"command_hash": "0" * 64},
        {"gateway_idempotency_key": "bad"},
    ],
)
@pytest.mark.asyncio
async def test_abandon_rejects_tampered_claim(setup, change):
    db, service = setup
    claim = await begin(service)
    with pytest.raises(ContractError):
        await service.abandon_before_dispatch(replace(claim, **change))
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_abandon_cannot_delete_completed_envelope_even_without_gateway_row(setup):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    await service.complete(claim, gateway_request_id=rid, response=response(), append=append)
    db.gateway_rows.clear()
    with pytest.raises(ContractError):
        await service.abandon_before_dispatch(claim)
    assert (await begin(service)).response == response() and len(db.history) == 2


@pytest.mark.asyncio
async def test_abandon_lost_commit_ack_is_idempotent(setup):
    db, service = setup
    claim = await begin(service)
    db.lose_commit_ack = True
    with pytest.raises(ConnectionError):
        await service.abandon_before_dispatch(claim)
    assert await service.abandon_before_dispatch(claim) is False
    assert not db.rows


@pytest.mark.parametrize("key_kind", ["original", "derived"])
@pytest.mark.asyncio
async def test_missing_envelope_does_not_reopen_legacy_or_orphaned_gateway_request(setup, key_kind):
    db, service = setup
    claim = await begin(service)
    settled_gateway(
        db,
        claim,
        idempotency_key=(
            claim.idempotency_key if key_kind == "original" else claim.gateway_idempotency_key
        ),
    )
    db.rows.clear()
    with pytest.raises(ContractError) as error:
        await begin(service)
    assert error.value.code == APIErrorCode.HOLD_CONFLICT
    assert not db.rows and len(db.gateway_rows) == 1


@pytest.mark.asyncio
async def test_dedicated_table_writes_never_mutate_gateway_requests(setup):
    db, service = setup
    claim = await begin(service)
    rid = settled_gateway(db, claim)
    await service.complete(claim, gateway_request_id=rid, response=response(), append=append)
    other = await begin(service, key="preflight-only")
    await service.abandon_before_dispatch(other)
    writes = [
        query
        for query, _ in db.queries
        if query.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes and all("inference_route_commands" in query for query in writes)
    assert all("inference_requests" not in query for query in writes)


@pytest.fixture
def http_setup(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import test_openrouter as transport_fixtures
    import test_openrouter_attachments as file_fixtures
    import test_openrouter_route_preflight as route_fixtures

    from server.routes import inference as routes

    setup = route_fixtures.setup_route.__wrapped__(monkeypatch)
    setup.state["row"] = file_fixtures.config()
    transport_fixtures.install_review(monkeypatch, file_fixtures.review())
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    setup.thread.prepare_thread.side_effect = None
    setup.thread.prepare_thread.return_value = SimpleNamespace(
        thread_id=THREAD,
        thread_status="active",
        conversation_mode="persistent",
    )
    setup.uploads = SimpleNamespace(
        resolve_uploaded_for_inference=AsyncMock(return_value=[file_fixtures.row()])
    )
    monkeypatch.setattr(routes, "get_file_upload_service", lambda: setup.uploads)
    setup.incoming = {
        "model": file_fixtures.MODEL,
        "thread_id": THREAD,
        "idempotency_key": "http-file-replay",
        "attachments": [file_fixtures.IDS[0]],
    }

    async def post(endpoint, **changes):
        return await route_fixtures.post(setup, endpoint, **{**setup.incoming, **changes})

    setup.post = post
    return setup


def http_payload(http_response, endpoint):
    if endpoint == "ask":
        return http_response.json()
    events = [
        json.loads(line.removeprefix("data: "))
        for line in http_response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert not any(item["type"] == "response.error" for item in events), http_response.text
    completed = [item["payload"] for item in events if item["type"] == "response.completed"]
    assert len(completed) == 1 and completed[0]["status"] == "ok"
    return completed[0]


@pytest.mark.parametrize("first_endpoint", ["ask", "stream"])
@pytest.mark.parametrize("retry_endpoint", ["ask", "stream"])
@pytest.mark.parametrize("media", ["text", "image"])
@pytest.mark.asyncio
async def test_actual_http_retry_replays_before_changed_history_review_and_upload_resolution(
    http_setup,
    monkeypatch,
    first_endpoint,
    retry_endpoint,
    media,
):
    import test_openrouter_attachments as file_fixtures

    from openvegas.gateway.openrouter import build_payload, input_token_bound
    from openvegas.gateway.providers import model_capabilities
    from server.services.file_uploads import FileUploadError

    setup = http_setup
    stream_calls = []
    if media == "image":
        setup.uploads.resolve_uploaded_for_inference.return_value = [
            file_fixtures.row(file_fixtures.picture(), "image/png", "image.png")
        ]
    if first_endpoint == "stream":

        async def stream(req):
            stream_calls.append(req)
            result = await setup.gateway.infer(req)
            yield {"type": "text_delta", "text": result.text}
            yield {"type": "completed", "result": result}

        setup.gateway.stream_infer = stream
    first = await setup.post(first_endpoint)
    assert first.status_code == 200, first.text
    initial = http_payload(first, first_endpoint)
    req = setup.gateway.infer.call_args.args[0]
    payload = build_payload(req, setup.state["row"], model_capabilities("openrouter", req.model))
    part = payload["messages"][-1]["content"][1]
    assert part["type"] == ("image_url" if media == "image" else "text")
    if media == "image":
        assert part["image_url"]["url"].startswith("data:image/png;base64,")
        assert input_token_bound(req) >= 5000
    assert len(setup.db.history) == 2 and "attachment_refs" in setup.db.history[0]
    assert req.idempotency_key.startswith(GATEWAY_KEY_PREFIX)
    assert req.idempotency_key != setup.incoming["idempotency_key"]
    assert len(setup.db.rows) == len(setup.db.gateway_rows) == 1
    cached = json.loads(
        setup.db.rows[(OWNER, setup.incoming["idempotency_key"])]["response_body_text"]
    )["response"]
    original_hash = AIGateway._payload_hash(req)
    setup.uploads.resolve_uploaded_for_inference.side_effect = FileUploadError(
        status_code=404, code="not_found", detail="expired"
    )
    setup.thread.prepare_thread.side_effect = AssertionError("Retry created/prepared a thread")
    setup.thread.get_recent_messages_with_stats.side_effect = AssertionError(
        "Retry reloaded history"
    )
    setup.mode.resolve_for_user.side_effect = AssertionError(
        "Retry performed fresh mode preparation"
    )
    setup.state["row"] = None
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    repeated = await setup.post(retry_endpoint)
    assert repeated.status_code == 200, repeated.text
    replay = http_payload(repeated, retry_endpoint)
    for key in ("text", "v_cost", "thread_id", "input_tokens", "output_tokens"):
        assert replay[key] == initial[key] == cached[key]
    if retry_endpoint == "ask":
        assert replay == cached
    setup.gateway.infer.assert_awaited_once()
    assert stream_calls == []  # OpenRouter HTTP commits via infer before buffered delivery.
    setup.thread.append_exchange.assert_awaited_once()
    setup.uploads.resolve_uploaded_for_inference.assert_awaited_once()
    assert setup.fraud.check_inference.await_count == 2
    assert len(setup.db.history) == 2
    assert AIGateway._payload_hash(req) == original_hash
    assert isinstance(setup.thread.append_exchange.call_args.kwargs["tx"], MemoryTx)


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "different"},
        {"attachments": [OTHER]},
        {"thread_id": OTHER},
        {"enable_tools": True},
    ],
)
@pytest.mark.asyncio
async def test_actual_http_changed_command_conflicts_without_preparation_or_append(
    http_setup,
    endpoint,
    change,
):
    setup = http_setup
    first = await setup.post("ask")
    assert first.status_code == 200
    repeated = await setup.post(endpoint, **change)
    if endpoint == "ask":
        assert repeated.status_code == 409
        assert repeated.json()["error"] == APIErrorCode.IDEMPOTENCY_CONFLICT.value
    else:
        assert "event: response.error" in repeated.text
        assert APIErrorCode.IDEMPOTENCY_CONFLICT.value in repeated.text
    setup.gateway.infer.assert_awaited_once()
    setup.thread.prepare_thread.assert_awaited_once()
    setup.thread.append_exchange.assert_awaited_once()
    assert len(setup.db.history) == 2


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.asyncio
async def test_actual_http_preflight_rejection_abandons_unexecuted_claim(http_setup, endpoint):
    setup = http_setup
    original = setup.state["row"]
    setup.state["row"] = None
    failed = await setup.post(endpoint)
    if endpoint == "ask":
        assert failed.status_code == 503
    else:
        assert "event: response.error" in failed.text
    assert not setup.db.rows and not setup.db.gateway_rows and not setup.db.history
    setup.gateway.infer.assert_not_awaited()
    setup.state["row"] = original
    success = await setup.post(endpoint)
    assert (
        success.status_code == 200 and http_payload(success, endpoint)["text"] == "Fixture answer"
    )
    setup.gateway.infer.assert_awaited_once()
    assert len(setup.db.history) == 2


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.asyncio
async def test_actual_http_unknown_dispatch_outcome_stays_blocked(http_setup, endpoint):
    setup = http_setup
    setup.gateway.infer.side_effect = ContractError(
        APIErrorCode.PROVIDER_UNAVAILABLE, "Synthetic uncertain provider outcome"
    )
    first = await setup.post(endpoint)
    assert "provider_unavailable" in first.text
    retry = await setup.post(endpoint)
    assert APIErrorCode.HOLD_CONFLICT.value in retry.text
    setup.gateway.infer.assert_awaited_once()
    setup.thread.append_exchange.assert_not_awaited()
    assert len(setup.db.rows) == 1 and not setup.db.history


@pytest.mark.parametrize("key", ["", " ", GATEWAY_KEY_PREFIX + "spoof"])
@pytest.mark.asyncio
async def test_actual_http_explicit_invalid_key_does_not_silently_use_legacy_path(http_setup, key):
    setup = http_setup
    failed = await setup.post("ask", idempotency_key=key)
    assert failed.status_code == 409
    assert failed.json()["error"] == APIErrorCode.INVALID_TRANSITION.value
    setup.gateway.infer.assert_not_awaited()
    setup.thread.prepare_thread.assert_not_awaited()
    assert not setup.db.rows


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.asyncio
async def test_actual_http_first_answer_bytes_follow_committed_history_and_replay(
    http_setup, endpoint
):
    setup = http_setup
    application = setup.app
    answer_sends = []

    async def observe_send(scope, receive, send):
        async def checked_send(message):
            if message["type"] == "http.response.body" and b"Fixture answer" in message.get(
                "body", b""
            ):
                row = setup.db.rows[(OWNER, setup.incoming["idempotency_key"])]
                assert row["status"] == "succeeded"
                assert json.loads(row["response_body_text"])["response"]["text"] == "Fixture answer"
                assert len(setup.db.history) == 2
                assert not setup.db.lock.locked(), "Answer exposed before transaction commit"
                assert all(row["status"] == "succeeded" for row in setup.db.gateway_rows.values())
                answer_sends.append(message["body"])
            await send(message)

        await application(scope, receive, checked_send)

    # Observe the actual ASGI send boundary, not httpx's buffered response.text.
    setup.app = observe_send
    reply = await setup.post(endpoint)
    assert reply.status_code == 200 and answer_sends
    assert http_payload(reply, endpoint)["text"] == "Fixture answer"


@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.asyncio
async def test_actual_http_replay_commit_failure_never_delivers_answer(
    http_setup, monkeypatch, endpoint
):
    setup = http_setup
    original = MemoryTx.fetchrow

    async def fail_commit(tx, query, *args):
        if "UPDATE inference_route_commands" in query:
            raise ContractError(APIErrorCode.HOLD_CONFLICT, "Synthetic replay commit failure")
        return await original(tx, query, *args)

    monkeypatch.setattr(MemoryTx, "fetchrow", fail_commit)
    failed = await setup.post(endpoint)
    assert "Fixture answer" not in failed.text
    assert APIErrorCode.HOLD_CONFLICT.value in failed.text
    assert not setup.db.history
    assert setup.db.rows[(OWNER, setup.incoming["idempotency_key"])]["status"] == "processing"
    assert len(setup.db.gateway_rows) == 1
    assert next(iter(setup.db.gateway_rows.values()))["status"] == "succeeded"
    retried = await setup.post(endpoint)
    assert "Fixture answer" not in retried.text
    assert APIErrorCode.HOLD_CONFLICT.value in retried.text
    setup.gateway.infer.assert_awaited_once()


@pytest.mark.asyncio
async def test_buffered_gateway_commits_before_first_delta_and_close_is_delivery_only():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    gateway = AIGateway(None, None, None)
    context = SimpleNamespace(provider_api_key="synthetic-never-sent")
    result = InferenceResult("Fixture answer", 10, 4)
    gateway._prepare_inference_execution = AsyncMock(return_value=(context, None))
    gateway._route_to_provider = AsyncMock(return_value=result)
    gateway._finalize_inference_execution = AsyncMock(return_value=result)
    gateway._cleanup_inference_after_failure = AsyncMock()
    request = InferenceRequest(f"user:{OWNER}", "openrouter", "fixture/model", [])
    stream = gateway.stream_infer(request)
    try:
        first = await anext(stream)
        assert first == {"type": "text_delta", "text": "Fixture answer"}
        gateway._finalize_inference_execution.assert_awaited_once_with(context, request, result)
    finally:
        await stream.aclose()
    gateway._cleanup_inference_after_failure.assert_not_awaited()
    gateway._route_to_provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_buffered_gateway_settlement_failure_yields_no_answer():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    gateway = AIGateway(None, None, None)
    context = SimpleNamespace(provider_api_key="synthetic-never-sent")
    gateway._prepare_inference_execution = AsyncMock(return_value=(context, None))
    gateway._route_to_provider = AsyncMock(return_value=InferenceResult("Fixture answer", 10, 4))
    gateway._finalize_inference_execution = AsyncMock(
        side_effect=ContractError(APIErrorCode.HOLD_CONFLICT, "Synthetic settlement failure")
    )
    gateway._cleanup_inference_after_failure = AsyncMock()
    events = []
    with pytest.raises(ContractError):
        async for event in gateway.stream_infer(
            InferenceRequest(f"user:{OWNER}", "openrouter", "fixture/model", [])
        ):
            events.append(event)
    assert events == []
    gateway._cleanup_inference_after_failure.assert_awaited_once_with(context)
