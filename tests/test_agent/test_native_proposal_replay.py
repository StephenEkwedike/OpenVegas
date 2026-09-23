"""Offline proposal recovery contracts; real PostgreSQL is a separate gate."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from openvegas.agent.native_history import MAX_BYTES, object_value
from openvegas.agent.orchestration_contracts import canonical_json, valid_actions_signature
from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.tool_cas import claim_started_tx
from openvegas.contracts.errors import APIErrorCode, ContractError

USER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
REQUEST = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"
CALL = {"tool_name": "Read", "arguments": {"path": "notes.txt"},
        "shell_mode": "read_only", "timeout_sec": 30, "provider_call_id": "call-1"}


class Database:
    def __init__(self):
        self.runs = {RUN: {"id": RUN, "user_id": USER, "runtime_session_id": SESSION,
                           "state": "running", "version": 7, "is_resumable": False,
                           "expires_at": None, "cancel_requested_at": None}}
        self.source = {"status": "succeeded", "response_status": 200,
                       "provider_request_id": "gen-1", "response_body_text": {
                           "provider_request_id": "gen-1", "tool_calls": [copy.deepcopy(CALL)]}}
        self.preauth = {"provider": "openrouter", "model_id": "openai/gpt-4.1-nano", "status": "settled"}
        self.tools = {}
        self.turns = []
        self.events = []
        self.queries = []
        self.run_locks = defaultdict(asyncio.Lock)
        self.source_lock = asyncio.Lock()
        self.projection = 9
        self.fail_binding_insert = False

    def transaction(self):
        return Transaction(self)

    def snapshot(self):
        return copy.deepcopy((self.tools, self.turns, self.events))


class Transaction:
    def __init__(self, db):
        self.db = db
        self.run_id = None
        self.locks = []
        self.inserted_tools = []
        self.inserted_turns = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, _exc, _tb):
        if kind:
            for tool_id in self.inserted_tools:
                self.db.tools.pop(tool_id, None)
            for row in self.inserted_turns:
                self.db.turns.remove(row)
        for lock in reversed(self.locks):
            lock.release()

    async def record(self, query, args):
        self.db.queries.append((query, args))
        # Force interleaving; a missing run lock must not serialize accidentally.
        await asyncio.sleep(0)
        return " ".join(query.split())

    async def fetchrow(self, query, *args):
        sql = await self.record(query, args)
        if "FROM agent_runs" in sql:
            assert "FOR UPDATE" in sql and "user_id" in sql
            lock = self.db.run_locks[args[0]]
            await lock.acquire()
            self.locks.append(lock)
            self.run_id = args[0]
            run = self.db.runs.get(args[0])
            return copy.deepcopy(run) if run and run["user_id"] == args[1] else None
        assert self.run_id is not None
        if "FROM inference_requests" in sql:
            assert "FOR UPDATE" in sql and "user_id=$2::uuid" in sql
            await self.db.source_lock.acquire()
            self.locks.append(self.db.source_lock)
            return copy.deepcopy(self.db.source) if args == (REQUEST, USER) else None
        if "FROM inference_preauthorizations" in sql:
            assert args == (REQUEST, USER)
            return copy.deepcopy(self.db.preauth)
        if "FROM run_status_projection" in sql:
            return {"projection_version": self.db.projection}
        if "FROM agent_run_tool_calls" in sql:
            if sql.startswith("SELECT 1"):
                return next(({} for t in self.db.tools.values()
                             if t["run_id"] == args[0] and t["status"] == "started"), None)
            tool = self.db.tools.get(args[0])
            return copy.deepcopy(tool) if tool and tool["run_id"] == args[1] else None
        raise AssertionError(sql)

    async def fetch(self, query, *args):
        sql = await self.record(query, args)
        assert self.run_id is not None
        assert self.db.run_locks[self.run_id].locked()
        if "FROM agent_tool_approvals" in sql:
            return []
        assert "FROM agent_chat_turns" in sql and "ORDER BY turn_no LIMIT 2" in sql
        run_id, key, request_id, call_id = args
        assert run_id == self.run_id
        result = []
        for row in self.db.turns:
            content = row["content_json"]
            stored_key = content.get("proposal_replay", {}).get("request", {}).get("idempotency_key")
            same_reference = request_id is not None and call_id is not None and (
                content.get("inference_request_id"), content.get("provider_call_id")) == (request_id, call_id)
            if row["run_id"] == run_id and (stored_key == key or same_reference):
                result.append(copy.deepcopy(row))
        return result[:2]

    async def fetchval(self, query, *args):
        sql = await self.record(query, args)
        assert self.run_id is not None
        if "count(*)" in sql:
            return sum(r["run_id"] == args[0] and r["content_json"].get("kind") == args[1]
                       for r in self.db.turns)
        assert self.db.source_lock in self.locks
        assert "inference_request_id" in sql and "provider_call_id" in sql
        return next((r["id"] for r in self.db.turns if (
            r["content_json"].get("kind"), r["content_json"].get("inference_request_id"),
            r["content_json"].get("provider_call_id")) == args), None)

    async def execute(self, query, *args):
        sql = await self.record(query, args)
        assert self.run_id is not None
        if sql.startswith("INSERT INTO agent_run_tool_calls"):
            fields = ("id", "run_id", "run_version", "tool_name", "tool_class", "payload_hash",
                      "request_payload_json", "execution_token", "status", "approval_required",
                      "state_reason_code", "started_at", "finished_at")
            tool = dict(zip(fields, args, strict=True), commit_state="not_applicable")
            tool["request_payload_json"] = json.loads(tool["request_payload_json"])
            self.db.tools[tool["id"]] = tool
            self.inserted_tools.append(tool["id"])
            return "INSERT 0 1"
        if sql.startswith("INSERT INTO agent_chat_turns"):
            assert self.db.source_lock in self.locks
            if self.db.fail_binding_insert:
                raise RuntimeError("synthetic binding write failure")
            row = {"id": f"binding-{len(self.db.turns)}", "run_id": args[0], "role": "assistant",
                   "content_json": json.loads(args[1]), "tool_call_id": args[2]}
            self.db.turns.append(row)
            self.inserted_turns.append(row)
            return "INSERT 0 1"
        if sql.startswith("UPDATE agent_run_tool_calls SET status='started'"):
            tool_id, run_id, token = args
            tool = self.db.tools.get(tool_id)
            if tool and (tool["run_id"], tool["execution_token"], tool["status"]) == (run_id, token, "proposed"):
                tool["status"] = "started"
                return "UPDATE 1"
            return "UPDATE 0"
        raise AssertionError(sql)


def request(**changes):
    values = {"user_id": USER, "actor_role": "user", "run_id": RUN, "runtime_session_id": SESSION,
              "expected_run_version": 7,
              "expected_valid_actions_signature": valid_actions_signature(7, [{"action": "cancel"}, {"action": "handoff"}]),
              "idempotency_key": "native-proposal-1", "tool_name": "fs_read", "arguments": {"path": "notes.txt"},
              "shell_mode": "read_only", "timeout_sec": 30, "plan_mode": False,
              "native_inference_request_id": REQUEST, "native_provider_call_id": "call-1"}
    return dict(values, **changes)


async def propose(db, **changes):
    return await AgentOrchestrationService(db).propose_tool_call(**request(**changes))


def seal(binding):
    # Used only to probe validation beyond the accidental-corruption checksum.
    binding["proposal_replay_sha256"] = hashlib.sha256(canonical_json({
        k: v for k, v in binding.items() if k != "proposal_replay_sha256"
    }).encode()).hexdigest()


@pytest.mark.asyncio
async def test_lost_response_recovered_by_new_service_without_new_identity_or_writes(monkeypatch):
    db = Database()
    first = await propose(db)
    original = canonical_json(first.payload)
    before = db.snapshot()
    stored = db.turns[0]["content_json"]["proposal_replay"]
    token = first.payload["tool_request"]["execution_token"]
    assert "execution_token" not in canonical_json(db.turns)
    assert token not in canonical_json(db.turns)
    assert stored["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert stored["response_sha256"] == hashlib.sha256(original.encode()).hexdigest()
    restored = json.loads(stored["response_body_text"])
    restored["tool_request"]["execution_token"] = token
    assert canonical_json(restored) == original
    assert stored["response_status"] == first.status_code == 200
    assert stored["request"]["tool_request"]["timeout_sec"] == 5
    # Mutating the delivered object or settled source cannot mutate the snapshot.
    first.payload["tool_request"]["arguments"]["path"] = "changed-client-copy"
    db.source = None
    db.preauth = None
    db.runs[RUN]["version"] = 8
    db.projection = 100
    def unexpected_uuid():
        raise AssertionError("retry generated a new identity or token")
    monkeypatch.setattr("openvegas.agent.orchestration_service.uuid.uuid4", unexpected_uuid)
    start = len(db.queries)
    replay = await propose(db, timeout_sec=5)
    assert canonical_json(replay.payload) == original
    assert db.snapshot() == before
    assert not any("inference_requests" in q or q.lstrip().startswith(("INSERT", "UPDATE"))
                   for q, _ in db.queries[start:])


@pytest.mark.asyncio
async def test_concurrent_exact_retries_are_serialized_by_run_lock():
    db = Database()
    results = await asyncio.gather(*(propose(db) for _ in range(16)))
    assert len({canonical_json(r.payload) for r in results}) == 1
    assert len(db.tools) == len(db.turns) == 1
    assert db.events == []
    assert sum("INSERT INTO agent_run_tool_calls" in q for q, _ in db.queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"idempotency_key": "different-key"}, {"arguments": {"path": "other.txt"}},
    {"timeout_sec": 1}, {"shell_mode": "mutating"}, {"plan_mode": True},
    {"expected_run_version": 8}, {"expected_valid_actions_signature": "sha256:" + "f" * 64},
    {"native_inference_request_id": OTHER}, {"native_provider_call_id": "call-other"},
    {"native_inference_request_id": None, "native_provider_call_id": None},
    {"tool_name": "fs_list"}, {"actor_role": "admin"},
])
async def test_changed_key_or_normalized_request_conflicts_without_side_effects(changes):
    db = Database()
    await propose(db)
    before = db.snapshot()
    with pytest.raises(ContractError) as error:
        await propose(db, **changes)
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert db.snapshot() == before


@pytest.mark.asyncio
async def test_concurrent_different_keys_bind_once_and_conflict():
    db = Database()
    results = await asyncio.gather(propose(db), propose(db, idempotency_key="other-key"), return_exceptions=True)
    assert sum(isinstance(r, ContractError) for r in results) == 1
    assert len(db.tools) == len(db.turns) == 1


@pytest.mark.asyncio
async def test_cross_run_global_binding_still_conflicts_and_rolls_back():
    db = Database()
    db.runs[OTHER] = dict(db.runs[RUN], id=OTHER)
    results = await asyncio.gather(propose(db), propose(db, run_id=OTHER), return_exceptions=True)
    assert sum(isinstance(r, ContractError) for r in results) == 1
    assert len(db.tools) == len(db.turns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"user_id": OTHER}, {"runtime_session_id": OTHER}])
async def test_current_owner_and_session_are_checked_before_replay(changes):
    db = Database()
    await propose(db)
    start = len(db.queries)
    with pytest.raises(ContractError):
        await propose(db, **changes)
    assert not any("agent_chat_turns" in q for q, _ in db.queries[start:])


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"state": state} for state in ("completed", "failed", "canceled", "expired", "interrupted", "awaiting_approval")
] + [{"cancel_requested_at": datetime.now(UTC)}, {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
     {"runtime_session_id": OTHER}])
async def test_current_run_lifecycle_and_session_rotation_fail_closed(changes):
    db = Database()
    await propose(db)
    db.runs[RUN].update(changes)
    before = db.snapshot()
    with pytest.raises(ContractError):
        await propose(db)
    assert db.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["started", "succeeded", "failed", "timed_out", "blocked", "cancelled"])
async def test_started_or_terminal_proposals_never_return_an_executable_request(status):
    db = Database()
    first = await propose(db)
    db.tools[first.payload["tool_request"]["tool_call_id"]]["status"] = status
    before = db.snapshot()
    with pytest.raises(ContractError, match="must not be executed again"):
        await propose(db)
    assert db.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending_commit", "committed", "commit_failed", "commit_unknown"])
async def test_proposed_but_uncertain_commit_cannot_recover(state):
    db = Database()
    await propose(db)
    next(iter(db.tools.values()))["commit_state"] = state
    with pytest.raises(ContractError, match="must not be executed again"):
        await propose(db)


@pytest.mark.asyncio
async def test_start_remains_separate_and_legacy_start_semantics_do_not_enable_proposal_replay():
    db = Database()
    proposal = (await propose(db)).payload["tool_request"]
    assert db.tools[proposal["tool_call_id"]]["status"] == "proposed"
    async with db.transaction() as tx:
        await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1 AND user_id=$2 FOR UPDATE", RUN, USER)
        with pytest.raises(ContractError):
            await claim_started_tx(tx, run_id=RUN, tool_call_id=proposal["tool_call_id"], execution_token="wrong")
        assert await claim_started_tx(tx, run_id=RUN, tool_call_id=proposal["tool_call_id"],
                                      execution_token=proposal["execution_token"]) == "claimed"
        assert await claim_started_tx(tx, run_id=RUN, tool_call_id=proposal["tool_call_id"],
                                      execution_token=proposal["execution_token"]) == "idempotent"
    with pytest.raises(ContractError, match="must not be executed again"):
        await propose(db)


@pytest.mark.asyncio
async def test_legacy_bindings_without_evidence_fail_closed():
    db = Database()
    await propose(db)
    binding = db.turns[0]["content_json"]
    del binding["proposal_replay"]
    del binding["proposal_replay_sha256"]
    before = db.snapshot()
    with pytest.raises(ContractError):
        await propose(db)
    assert db.snapshot() == before


@pytest.mark.asyncio
async def test_binding_write_failure_rolls_back_tool_and_retry_can_create_once():
    db = Database()
    db.fail_binding_insert = True
    with pytest.raises(RuntimeError, match="synthetic"):
        await propose(db)
    assert db.snapshot() == ({}, [], [])
    db.fail_binding_insert = False
    assert (await propose(db)).status_code == 200
    assert len(db.tools) == len(db.turns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"idempotency_key": ""}, {"idempotency_key": "x" * 201}, {"idempotency_key": "bad\nkey"},
    {"idempotency_key": "\ud800"}, {"native_inference_request_id": "x" * 1000},
    {"native_provider_call_id": "x" * 257}, {"native_provider_call_id": "sk-credential"},
    {"native_provider_call_id": None}, {"expected_run_version": True}, {"expected_run_version": 2**63},
    {"expected_valid_actions_signature": "x" * 10000}, {"plan_mode": 1},
    {"arguments": {"path": "x" * MAX_BYTES}}, {"arguments": {"path": "\ud800"}},
    {"arguments": {"path": "notes.txt", "bad": float("nan")}}, {"arguments": []},
    {"shell_mode": []}, {"timeout_sec": "30"},
])
async def test_native_inputs_are_bounded_before_database_mutation(changes):
    db = Database()
    with pytest.raises(ContractError):
        await propose(db, **changes)
    assert db.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("execution_token", "f" * 32), ("payload_hash", "f" * 64), ("tool_name", "fs_list"),
    ("tool_class", "mutating"), ("approval_required", True), ("run_version", 8),
    ("request_payload_json", {"tool_name": "fs_read", "arguments": {"path": "other"},
                              "shell_mode": "read_only", "timeout_sec": 5}),
])
async def test_corrupt_authoritative_tool_fields_fail_closed(field, value):
    db = Database()
    await propose(db)
    next(iter(db.tools.values()))[field] = value
    with pytest.raises(ContractError):
        await propose(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [
    "checksum", "body", "body-bound", "duplicate-json", "wrong-response-token", "bad-actions",
    "wrong-kind", "wrong-provider", "wrong-native-call", "normalizer-bool", "ordinal-bool",
    "response-version-bool", "wrong-role", "wrong-tool-id", "missing-tool", "duplicate-row",
    "token-hash", "response-hash", "missing-token-hash", "missing-response-hash",
])
async def test_corrupt_replay_evidence_never_returns_a_proposal(corruption):
    db = Database()
    await propose(db)
    row = db.turns[0]
    binding = row["content_json"]
    replay = binding["proposal_replay"]
    if corruption == "checksum":
        binding["proposal_replay_sha256"] = "f" * 64
    elif corruption in {"token-hash", "response-hash"}:
        replay[corruption.replace("-hash", "_sha256")] = "f" * 64
    elif corruption in {"missing-token-hash", "missing-response-hash"}:
        del replay[corruption.removeprefix("missing-").replace("-hash", "_sha256")]
    elif corruption == "body":
        replay["response_body_text"] = "not-json"
    elif corruption == "body-bound":
        replay["response_body_text"] = "x" * MAX_BYTES
    elif corruption == "duplicate-json":
        replay["response_body_text"] = '{"error":null,"error":null}'
    elif corruption in {"wrong-response-token", "bad-actions", "response-version-bool"}:
        body = json.loads(replay["response_body_text"])
        if corruption == "wrong-response-token":
            body["tool_request"]["execution_token"] = "a" * 32
        elif corruption == "bad-actions":
            body["valid_actions"] = [None]
        else:
            body["run_version"] = True
        replay["response_body_text"] = canonical_json(body)
    elif corruption == "wrong-kind":
        binding["kind"] = "wrong"
    elif corruption == "wrong-provider":
        binding["provider"] = "other"
    elif corruption == "wrong-native-call":
        binding["native_call"]["arguments"]["path"] = "other"
    elif corruption == "normalizer-bool":
        binding["normalizer_version"] = True
    elif corruption == "ordinal-bool":
        binding["call_ordinal"] = True
    elif corruption == "wrong-role":
        row["role"] = "user"
    elif corruption == "wrong-tool-id":
        row["tool_call_id"] = "bad-id"
    elif corruption == "missing-tool":
        db.tools.clear()
    elif corruption == "duplicate-row":
        db.turns.append(copy.deepcopy(row))
    if corruption != "checksum":
        seal(binding)
    before = db.snapshot()
    with pytest.raises(ContractError):
        await propose(db)
    assert db.snapshot() == before


@pytest.mark.parametrize("value", [{1: "key"}, {"x": [0] * 10001}, '{"x":1,"x":2}'])
def test_structural_json_corruption_is_rejected(value):
    with pytest.raises(ContractError):
        object_value(value)


@pytest.mark.asyncio
async def test_new_native_stale_projection_is_not_stored():
    db = Database()
    result = await propose(db, expected_run_version=6)
    assert result.status_code == 409
    assert result.payload["error"] == APIErrorCode.STALE_PROJECTION.value
    assert db.snapshot() == ({}, [], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tool_name", "shell_mode"])
@pytest.mark.parametrize("bad", [[], {}, None, True, 12])
async def test_container_or_nontext_native_fields_rejected_as_contract_error(field, bad):
    db = Database()
    db.source["response_body_text"]["tool_calls"][0][field] = bad
    with pytest.raises(ContractError):
        await propose(db)
    assert db.snapshot() == ({}, [], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tool_name", "shell_mode", "current_state"])
@pytest.mark.parametrize("bad", [[], {}, None, True, 12])
async def test_corrupt_replay_enums_fail_closed_without_typeerror(field, bad):
    db = Database()
    await propose(db)
    binding = db.turns[0]["content_json"]
    if field == "current_state":
        body = json.loads(binding["proposal_replay"]["response_body_text"])
        body[field] = bad
        binding["proposal_replay"]["response_body_text"] = canonical_json(body)
    else:
        binding["native_call"][field] = bad
    seal(binding)
    before = db.snapshot()
    with pytest.raises(ContractError):
        await propose(db)
    assert db.snapshot() == before
