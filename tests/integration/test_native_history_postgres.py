"""Settled native-call receipts against disposable PostgreSQL, not mocked CAS.

Uses database_factory's empty, loopback-only ov_test_* database and all 43
migrations. Inference/settlement rows and callback outputs are synthetic; no
provider, local tool, API server, wallet charge, or payment service is invoked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.runtime_contracts import result_submission_hash
from openvegas.contracts.errors import APIErrorCode, ContractError

pytestmark = pytest.mark.asyncio

PROVIDER = "openrouter"
MODEL = "openai/gpt-4.1-nano"
BINDING_KIND = "openvegas.native-tool-binding.v1"


@dataclass(frozen=True)
class Run:
    user_id: str
    run_id: str
    runtime_session_id: str

    def scope(self) -> dict[str, str]:
        return vars(self).copy()


@dataclass(frozen=True)
class Source:
    request_id: str
    provider_request_id: str
    call: dict[str, Any]
    body: dict[str, Any]


@pytest_asyncio.fixture
async def native_database(database_factory, monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "")
    monkeypatch.setenv("OPENVEGAS_TOOL_STDOUT_MAX_BYTES", "131072")
    monkeypatch.setenv("OPENVEGAS_TOOL_STDERR_MAX_BYTES", "131072")
    monkeypatch.setenv("OPENVEGAS_TOOL_RESULT_PAYLOAD_MAX_BYTES", "65536")
    monkeypatch.setenv("OPENVEGAS_TOOL_RESPONSE_MAX_BYTES", "131072")
    async with database_factory(through=43, max_size=4) as sandbox:
        assert await sandbox.db.fetchval("SELECT count(*) FROM schema_migrations") == 43
        yield sandbox


async def seed_user(db) -> str:
    user_id = str(uuid.uuid4())
    await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user_id)
    await db.execute(
        "INSERT INTO wallet_accounts(account_id,balance) VALUES ($1,0)", "user:" + user_id
    )
    return user_id


async def seed_source(db, user_id, *, tool_name="Read", arguments=None) -> Source:
    request_id = str(uuid.uuid4())
    provider_request_id = "gen-synthetic-" + uuid.uuid4().hex
    call = {
        "tool_name": tool_name,
        "arguments": {"path": "README.md"} if arguments is None else arguments,
        "shell_mode": "read_only",
        "timeout_sec": 30,
        "provider_call_id": "call-synthetic-" + uuid.uuid4().hex,
    }
    body = {
        "content": "Synthetic assistant text is not a replayable conversation.",
        "tool_calls": [call],
        "provider_request_id": provider_request_id,
        "completion_status": "complete",
        "input_tokens": 12,
        "output_tokens": 8,
    }
    await db.execute(
        """
        INSERT INTO inference_requests (
            id,user_id,idempotency_key,payload_hash,status,inference_source,
            wallet_funding_source,final_charge_v,final_provider_cost_usd,
            response_status,response_body_text,provider_request_id
        ) VALUES ($1::uuid,$2::uuid,$3,$4,'succeeded','wrapper','external',0,0,200,$5,$6)
        """,
        request_id, user_id, "synthetic:" + request_id,
        hashlib.sha256(request_id.encode()).hexdigest(), json.dumps(body), provider_request_id,
    )
    # request_id is TEXT in migration 009, unlike inference_requests.id (UUID).
    await db.execute(
        """
        INSERT INTO inference_preauthorizations (
            account_id,user_id,request_id,provider,model_id,reserved_v,settled_v,status
        ) VALUES ($1,$2::uuid,$3,$4,$5,0,0,'settled')
        """,
        "user:" + user_id, user_id, request_id, PROVIDER, MODEL,
    )
    return Source(request_id, provider_request_id, call, body)


async def new_run(service, user_id, *, state="running") -> Run:
    created = await service.create_run(user_id=user_id, state=state)
    run = Run(user_id, created["run_id"], str(uuid.uuid4()))
    await service.register_workspace(
        **run.scope(), workspace_root="/synthetic/native-workspace",
        workspace_fingerprint="sha256:" + "a" * 64,
    )
    return run


async def scenario(sandbox, *, state="running", **source_options):
    user_id = await seed_user(sandbox.db)
    source = await seed_source(sandbox.db, user_id, **source_options)
    service = AgentOrchestrationService(sandbox.db)
    # Deliberately create the run after inference settlement: source issuance
    # need not have happened inside this run or a persisted full conversation.
    run = await new_run(service, user_id, state=state)
    return service, run, source


async def projection(service, run) -> dict:
    current = await service.get_run(user_id=run.user_id, run_id=run.run_id)
    return {
        "expected_run_version": current["run_version"],
        "expected_valid_actions_signature": current["valid_actions_signature"],
    }


async def propose(service, run, source, **overrides):
    arguments = {
        **run.scope(), **await projection(service, run), "actor_role": "user",
        "idempotency_key": str(uuid.uuid4()),
        "tool_name": {"Read": "fs_read", "List": "fs_list"}.get(
            source.call["tool_name"], "fs_apply_patch"
        ),
        "arguments": dict(source.call["arguments"]),
        "shell_mode": "read_only", "timeout_sec": 30, "plan_mode": False,
        "native_inference_request_id": source.request_id,
        "native_provider_call_id": source.call["provider_call_id"],
    }
    arguments.update(overrides)
    return await service.propose_tool_call(**arguments)


async def propose_and_start(service, run, source, **overrides) -> dict:
    proposed = await propose(service, run, source, **overrides)
    assert proposed.status_code == 200, proposed.payload
    tool = proposed.payload["tool_request"]
    await start(service, run, tool)
    return tool


async def start(service, run, tool):
    started = await service.start_tool_call(
        **run.scope(), **await projection(service, run), actor_role="user",
        idempotency_key=str(uuid.uuid4()), tool_call_id=tool["tool_call_id"],
        execution_token=tool["execution_token"],
    )
    assert started.status_code == 200, started.payload


async def callback(service, run, tool, *, status="succeeded", payload=None,
                   stdout="fixture contents\n", stderr=""):
    return await service.result_tool_call(
        **run.scope(), actor_role="user", tool_call_id=tool["tool_call_id"],
        execution_token=tool["execution_token"], result_status=status,
        result_payload={"ok": True} if payload is None else payload,
        stdout=stdout, stderr=stderr,
    )


async def receipts(service, run, **overrides):
    arguments = {**run.scope(), "provider": PROVIDER, "model": MODEL}
    arguments.update(overrides)
    return await service.native_tool_receipts(**arguments)


async def row_counts(db) -> dict:
    return dict(await db.fetchrow(
        """
        SELECT (SELECT count(*) FROM agent_run_tool_calls) AS tools,
               (SELECT count(*) FROM agent_chat_turns) AS turns,
               (SELECT count(*) FROM agent_run_events) AS events,
               (SELECT count(*) FROM agent_mutation_replays) AS replays,
               (SELECT count(*) FROM agent_run_mutation_leases) AS leases,
               (SELECT count(*) FROM agent_tool_approvals) AS approvals
        """
    ))


async def assert_denied(awaitable, *, code=APIErrorCode.INVALID_TRANSITION):
    with pytest.raises(ContractError) as error:
        await awaitable
    assert error.value.code == code


@pytest.mark.parametrize("state", ["created", "running"])
@pytest.mark.parametrize("native_name,native_args,normalized", [
    ("Read", {"filepath": "README.md", "max_bytes": 4096},
     {"filepath": "README.md", "path": "README.md", "max_bytes": 4096}),
    ("List", {}, {"path": "."}),
])
async def test_binding_callback_reconnect_exact_receipt_without_execution_authority(
    native_database, state, native_name, native_args, normalized,
):
    sandbox = native_database
    service, run, source = await scenario(
        sandbox, state=state, tool_name=native_name, arguments=native_args
    )
    assert await receipts(service, run) == {
        "scope": "settled_native_call_receipts_v1", "receipts": [],
        "conversation_replay_supported": False,
        "original_turn_scope_verified": False, "execution_attested": False,
    }
    proposed = await propose(service, run, source)
    assert proposed.status_code == 200, proposed.payload
    tool = proposed.payload["tool_request"]
    assert await sandbox.db.fetchval(
        "SELECT status FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"]
    ) == "proposed"
    assert tool["arguments"] == normalized
    assert tool["timeout_sec"] == 5 and source.call["timeout_sec"] == 30
    binding_row = await sandbox.db.fetchrow(
        "SELECT * FROM agent_chat_turns WHERE run_id=$1::uuid", run.run_id
    )
    binding = json.loads(binding_row["content_json"])
    assert binding["kind"] == BINDING_KIND
    assert binding["native_call"] == source.call
    assert binding["runtime_timeout_sec"] == 5
    assert binding["runtime_session_id"] == run.runtime_session_id
    assert str(binding_row["tool_call_id"]) == tool["tool_call_id"]
    assert "execution_token" not in json.dumps(binding)
    await assert_denied(receipts(service, run))
    await start(service, run, tool)
    await assert_denied(receipts(service, run))

    output = "first line\nsecond line\n"
    result = {"ok": True, "entries": ["README.md"], "count": 1}
    accepted = await callback(service, run, tool, payload=result, stdout=output)
    assert accepted.status_code == 200
    expected_hash = result_submission_hash(
        "succeeded", result, hashlib.sha256(output.encode()).hexdigest(),
        hashlib.sha256(b"").hexdigest(),
    )
    stored = await sandbox.db.fetchrow(
        "SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"]
    )
    assert stored["stdout"] == output and stored["stderr"] == ""
    assert stored["result_submission_hash"] == expected_hash
    event = await sandbox.db.fetchrow(
        "SELECT payload FROM agent_run_events WHERE run_id=$1::uuid "
        "AND event_type='tool_finished_succeeded'", run.run_id,
    )
    event_payload = json.loads(event["payload"])
    assert event_payload == {
        "tool_call_id": tool["tool_call_id"], "status": "succeeded",
        "source": "runtime_callback", "redaction_checked": True, "redaction_required": False,
    }
    # Non-binding chat turns and legacy output columns must not become history.
    await sandbox.db.execute(
        "INSERT INTO agent_chat_turns(run_id,turn_no,role,content_json) "
        "VALUES ($1::uuid,2,'user',$2::jsonb)", run.run_id,
        json.dumps({"content": "Private conversation not included in receipts."}),
    )
    await sandbox.db.execute(
        "UPDATE agent_run_tool_calls SET stdout_redacted='legacy wrong output',"
        "stderr_redacted='legacy wrong error' WHERE id=$1::uuid", tool["tool_call_id"],
    )
    expected = {
        "scope": "settled_native_call_receipts_v1", "conversation_replay_supported": False,
        "original_turn_scope_verified": False, "execution_attested": False,
        "receipts": [{
            "provider": PROVIDER, "model": MODEL, "inference_request_id": source.request_id,
            "provider_request_id": source.provider_request_id,
            "provider_call_id": source.call["provider_call_id"], "call_ordinal": 0,
            "call": source.call, "runtime_tool_call_id": tool["tool_call_id"],
            "result": {"status": "succeeded", "payload": result,
                       "stdout": output, "stderr": ""},
            "result_submission_hash": expected_hash,
        }],
    }
    assert await receipts(service, run) == expected
    old_pool = sandbox.pool
    db = await sandbox.reconnect()
    assert sandbox.pool is not old_pool
    service = AgentOrchestrationService(db)
    restored = await receipts(service, run)
    assert restored == expected
    assert "execution_token" not in json.dumps(restored)
    assert tool["execution_token"] not in json.dumps(restored)
    before_replay = await row_counts(db)
    replayed = await callback(service, run, tool, payload=result, stdout=output)
    assert replayed.status_code == accepted.status_code and replayed.payload == accepted.payload
    assert await row_counts(db) == before_replay
    assert await receipts(service, run) == expected
    assert await db.fetchval("SELECT balance FROM wallet_accounts WHERE account_id=$1",
                             "user:" + run.user_id) == 0
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 0
    assert await db.fetchval("SELECT count(*) FROM inference_usage") == 0


@pytest.mark.parametrize("bad_reference", [
    "forged_request", "malformed_request", "forged_call", "malformed_call",
    "missing_call", "other_user_run", "other_user_source", "other_session",
])
async def test_proposal_reference_denial_rolls_back_every_row(native_database, bad_reference):
    service, run, source = await scenario(native_database)
    changes = {}
    if bad_reference == "forged_request":
        changes["native_inference_request_id"] = str(uuid.uuid4())
    elif bad_reference == "malformed_request":
        changes["native_inference_request_id"] = "not-a-canonical-uuid"
    elif bad_reference == "forged_call":
        changes["native_provider_call_id"] = "call-not-issued"
    elif bad_reference == "malformed_call":
        changes["native_provider_call_id"] = "call with spaces"
    elif bad_reference == "missing_call":
        changes["native_provider_call_id"] = None
    elif bad_reference == "other_user_run":
        changes["user_id"] = await seed_user(native_database.db)
    elif bad_reference == "other_user_source":
        run = await new_run(service, await seed_user(native_database.db))
    else:
        changes["runtime_session_id"] = str(uuid.uuid4())
    before = await row_counts(native_database.db)
    await assert_denied(propose(service, run, source, **changes))
    assert await row_counts(native_database.db) == before


@pytest.mark.parametrize("wrong_scope", ["user", "run", "session", "model", "provider"])
async def test_reconnect_receipts_reject_wrong_scope(native_database, wrong_scope):
    service, run, source = await scenario(native_database)
    tool = await propose_and_start(service, run, source)
    assert (await callback(service, run, tool)).status_code == 200
    expected = await receipts(service, run)
    other_user = await seed_user(native_database.db)
    other_run = await new_run(service, other_user)
    changes = {
        "user": {"user_id": other_user}, "run": {"run_id": other_run.run_id},
        "session": {"runtime_session_id": str(uuid.uuid4())},
        "model": {"model": "openai/gpt-4.1-mini"}, "provider": {"provider": "openai"},
    }[wrong_scope]
    service = AgentOrchestrationService(await native_database.reconnect())
    before = await row_counts(native_database.db)
    await assert_denied(receipts(service, run, **changes))
    assert await row_counts(native_database.db) == before
    assert await receipts(service, run) == expected


async def test_same_source_call_concurrent_two_runs_one_binding_no_orphan(native_database):
    sandbox = native_database
    service, first_run, source = await scenario(sandbox)
    second_run = await new_run(service, first_run.user_id)
    runs = [first_run, second_run]
    tasks = []
    try:
        # Hold the source row until both independent proposal transactions are
        # actually waiting on PostgreSQL locks. This is not a scheduling guess.
        async with sandbox.db.transaction() as blocker:
            await blocker.fetchrow(
                "SELECT id FROM inference_requests WHERE id=$1::uuid FOR UPDATE", source.request_id
            )
            tasks = [asyncio.create_task(propose(AgentOrchestrationService(sandbox.db), run, source))
                     for run in runs]
            async with asyncio.timeout(4):
                while True:
                    await blocker.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await blocker.fetchval(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND wait_event_type='Lock' AND query LIKE '%FROM inference_requests%FOR UPDATE%'"
                    )
                    if waiting == 2:
                        break
                    assert not any(task.done() for task in tasks), "Proposal exited before source lock"
                    await asyncio.sleep(0.01)
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    winners = [index for index, result in enumerate(outcomes)
               if not isinstance(result, BaseException) and result.status_code == 200]
    assert len(winners) == 1, outcomes
    winner = winners[0]
    loser = outcomes[1 - winner]
    assert isinstance(loser, ContractError), outcomes
    assert loser.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert await row_counts(sandbox.db) == {
        "tools": 1, "turns": 1, "events": 0, "replays": 0, "leases": 0, "approvals": 0,
    }
    bound = await sandbox.db.fetchrow(
        "SELECT c.run_id,c.tool_call_id,t.run_id AS tool_run_id "
        "FROM agent_chat_turns c LEFT JOIN agent_run_tool_calls t ON t.id=c.tool_call_id"
    )
    assert str(bound["run_id"]) == str(bound["tool_run_id"]) == runs[winner].run_id
    assert str(bound["tool_call_id"]) == outcomes[winner].payload["tool_request"]["tool_call_id"]
    assert (await receipts(service, runs[1 - winner]))["receipts"] == []


@pytest.mark.parametrize("change", ["path", "timeout", "write_transform", "shell_mode"])
async def test_preprocessing_changes_rollback_insert_and_binding(native_database, change):
    source_options = {}
    if change == "write_transform":
        source_options = {"tool_name": "Write", "arguments": {
            "filepath": "fixture.txt", "content": "new line\n", "write_mode": "replace",
        }}
    service, run, source = await scenario(native_database, **source_options)
    changes = {
        "path": {"arguments": {"path": "different.txt"}},
        "timeout": {"timeout_sec": 1},
        "write_transform": {"tool_name": "fs_apply_patch", "shell_mode": "mutating",
                            "arguments": {"patch": "*** Begin Patch\n*** End Patch\n"}},
        "shell_mode": {"shell_mode": "mutating"},
    }[change]
    before = await row_counts(native_database.db)
    await assert_denied(propose(service, run, source, **changes))
    assert await row_counts(native_database.db) == before
    assert (await receipts(service, run))["receipts"] == []
    if change != "write_transform":
        # Rejection cannot consume the source call or leave a hidden binding.
        assert (await propose(service, run, source)).status_code == 200
        counts = await row_counts(native_database.db)
        assert counts["tools"] == counts["turns"] == 1


@pytest.mark.parametrize("source_state", [
    "processing", "failed", "http_error", "reserved", "refunded", "voided",
    "missing_preauth", "wrong_provider", "wrong_model",
])
async def test_unsettled_or_invalid_inference_source_cannot_bind(native_database, source_state):
    service, run, source = await scenario(native_database)
    db = native_database.db
    if source_state in {"processing", "failed"}:
        await db.execute("UPDATE inference_requests SET status=$2 WHERE id=$1::uuid",
                         source.request_id, source_state)
    elif source_state == "http_error":
        await db.execute("UPDATE inference_requests SET response_status=409 WHERE id=$1::uuid",
                         source.request_id)
    elif source_state == "missing_preauth":
        await db.execute("DELETE FROM inference_preauthorizations WHERE request_id=$1",
                         source.request_id)
    elif source_state == "wrong_provider":
        await db.execute("UPDATE inference_preauthorizations SET provider='openai' WHERE request_id=$1",
                         source.request_id)
    elif source_state == "wrong_model":
        await db.execute("UPDATE inference_preauthorizations SET model_id='openrouter/auto' "
                         "WHERE request_id=$1", source.request_id)
    else:
        await db.execute("UPDATE inference_preauthorizations SET status=$2 WHERE request_id=$1",
                         source.request_id, source_state)
    before = await row_counts(db)
    await assert_denied(propose(service, run, source))
    assert await row_counts(db) == before


@pytest.mark.parametrize("state,reason,cancelling,expired", [
    ("awaiting_approval", None, False, False),
    ("completed", "completed_success", False, False),
    ("canceled", "canceled_user", False, False),
    ("expired", "expired_timeout", False, False),
    ("interrupted", "interrupted_worker_lost", False, False),
    ("running", None, True, False),
    ("running", None, False, True),
])
async def test_binding_rejects_inactive_cancelling_and_expired_runs(
    native_database, state, reason, cancelling, expired,
):
    service, run, source = await scenario(native_database)
    await native_database.db.execute(
        "UPDATE agent_runs SET state=$2,state_reason_code=$3,"
        "cancel_requested_at=CASE WHEN $4 THEN now() ELSE NULL END,"
        "expires_at=CASE WHEN $5 THEN now()-interval '1 minute' ELSE NULL END WHERE id=$1::uuid",
        run.run_id, state, reason, cancelling, expired,
    )
    before = await row_counts(native_database.db)
    await assert_denied(propose(service, run, source))
    assert await row_counts(native_database.db) == before


@pytest.mark.parametrize("status,error_code", [
    ("failed", APIErrorCode.TOOL_EXECUTION_FAILED),
    ("timed_out", APIErrorCode.TOOL_TIMEOUT),
    ("blocked", APIErrorCode.INVALID_TRANSITION),
])
async def test_callback_409_is_accepted_receipt_with_typed_failure(native_database, status, error_code):
    service, run, source = await scenario(native_database)
    tool = await propose_and_start(service, run, source)
    payload = {"ok": False, "reason_code": error_code.value, "detail": "Synthetic failure."}
    accepted = await callback(service, run, tool, status=status, payload=payload,
                              stdout="", stderr="Synthetic failure.\n")
    assert accepted.status_code == 409 and accepted.payload["error"] == error_code.value
    service = AgentOrchestrationService(await native_database.reconnect())
    restored = (await receipts(service, run))["receipts"]
    assert len(restored) == 1
    assert restored[0]["result"] == {
        "status": status, "payload": payload, "stdout": "", "stderr": "Synthetic failure.\n",
    }
    assert restored[0]["result"]["status"] != "succeeded"
    before = await row_counts(native_database.db)
    replay = await callback(service, run, tool, status=status, payload=payload,
                            stdout="", stderr="Synthetic failure.\n")
    assert replay.status_code == 409 and replay.payload == accepted.payload
    assert await row_counts(native_database.db) == before


@pytest.mark.parametrize("pending_state", ["proposed", "started", "cancelled", "reconciled"])
async def test_unaccepted_result_blocks_entire_receipt_set(native_database, pending_state):
    sandbox = native_database
    service, run, source = await scenario(sandbox)
    accepted_tool = await propose_and_start(service, run, source)
    assert (await callback(service, run, accepted_tool)).status_code == 200
    assert len((await receipts(service, run))["receipts"]) == 1
    second_source = await seed_source(sandbox.db, run.user_id, tool_name="List", arguments={})
    if pending_state == "proposed":
        assert (await propose(service, run, second_source)).status_code == 200
    else:
        tool = await propose_and_start(service, run, second_source)
        if pending_state == "cancelled":
            cancelled = await service.cancel_tool_call(
                **run.scope(), actor_role="user", tool_call_id=tool["tool_call_id"],
                execution_token=tool["execution_token"],
            )
            assert cancelled.status_code == 200
        elif pending_state == "reconciled":
            await sandbox.db.execute(
                "UPDATE agent_run_tool_calls SET started_at=now()-interval '5 minutes',"
                "last_heartbeat_at=NULL WHERE id=$1::uuid", tool["tool_call_id"],
            )
            assert await service.reconcile_stale_started_tools(timeout_seconds=5) == 1
            event = await sandbox.db.fetchrow(
                "SELECT payload FROM agent_run_events WHERE run_id=$1::uuid "
                "AND event_type='tool_finished_timed_out'", run.run_id,
            )
            assert json.loads(event["payload"]) == {
                "tool_call_id": tool["tool_call_id"], "status": "timed_out", "source": "reconciler",
            }
            stored = await sandbox.db.fetchrow(
                "SELECT status,result_submission_hash,finished_at FROM agent_run_tool_calls "
                "WHERE id=$1::uuid", tool["tool_call_id"],
            )
            assert stored["status"] == "timed_out" and stored["result_submission_hash"]
            assert stored["finished_at"] is not None
            # A late callback cannot relabel a reconciler receipt as accepted.
            await assert_denied(callback(service, run, tool), code=APIErrorCode.IDEMPOTENCY_CONFLICT)
    service = AgentOrchestrationService(await sandbox.reconnect())
    before = await row_counts(sandbox.db)
    await assert_denied(receipts(service, run))
    assert await row_counts(sandbox.db) == before


@pytest.mark.parametrize("field", ["stdout", "stderr"])
@pytest.mark.parametrize("damage", ["redacted", "truncated"])
async def test_lossy_callback_output_is_not_accepted_history(native_database, monkeypatch, field, damage):
    service, run, source = await scenario(native_database)
    tool = await propose_and_start(service, run, source)
    if damage == "redacted":
        monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "synthetic-private-value")
        original = "before synthetic-private-value after\n"
    else:
        monkeypatch.setenv(f"OPENVEGAS_TOOL_{field.upper()}_MAX_BYTES", "1024")
        original = "x" * 2048
    output = {"stdout": "", "stderr": "", field: original}
    assert (await callback(service, run, tool, **output)).status_code == 200
    stored = await native_database.db.fetchrow(
        "SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"],
    )
    assert stored[field] != original
    if damage == "truncated":
        assert stored[field + "_truncated"] is True
    else:
        assert "[REDACTED]" in stored[field]
        # A later configuration change must not turn a lossy result into history.
        monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "")
    service = AgentOrchestrationService(await native_database.reconnect())
    await assert_denied(receipts(service, run))


@pytest.mark.parametrize("pattern_at_callback", [True, False], ids=["existing-policy", "new-policy"])
@pytest.mark.parametrize("payload", [
    {"ok": True, "detail": "synthetic-private-value"},
    {"ok": True, "nested": [{"values": ["public", "synthetic-private-value"]}]},
    {"ok": True, "synthetic-private-value": "public"},
], ids=["scalar", "nested-list-object", "object-key"])
async def test_configured_redaction_in_result_payload_blocks_receipts(
    native_database, monkeypatch, payload, pattern_at_callback,
):
    service, run, source = await scenario(native_database)
    tool = await propose_and_start(service, run, source)
    if pattern_at_callback:
        monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "synthetic-private-value")
    accepted = await callback(service, run, tool, payload=payload, stdout="", stderr="")
    assert accepted.status_code == 200
    stored = await native_database.db.fetchrow(
        "SELECT result_submission_hash,stdout,stderr FROM agent_run_tool_calls WHERE id=$1::uuid",
        tool["tool_call_id"],
    )
    assert stored["result_submission_hash"]
    assert stored["stdout"] == stored["stderr"] == ""
    event = await native_database.db.fetchval(
        "SELECT payload FROM agent_run_events WHERE run_id=$1::uuid "
        "AND event_type='tool_finished_succeeded'", run.run_id,
    )
    metadata = json.loads(event)
    assert metadata["redaction_checked"] is True
    assert metadata["redaction_required"] is pattern_at_callback
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "synthetic-private-value")
    service = AgentOrchestrationService(await native_database.reconnect())
    await assert_denied(receipts(service, run))
    if pattern_at_callback:
        monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "")
        service = AgentOrchestrationService(await native_database.reconnect())
        await assert_denied(receipts(service, run))


@pytest.mark.parametrize("damage", [
    "stdout_hash", "result_payload", "callback_source", "redaction_metadata", "timeout",
])
async def test_tampered_persisted_receipt_fails_closed(native_database, damage):
    service, run, source = await scenario(native_database)
    tool = await propose_and_start(service, run, source)
    assert (await callback(service, run, tool)).status_code == 200
    db = native_database.db
    if damage == "stdout_hash":
        await db.execute("UPDATE agent_run_tool_calls SET stdout='changed' WHERE id=$1::uuid",
                         tool["tool_call_id"])
    elif damage == "result_payload":
        await db.execute("UPDATE agent_run_tool_calls SET result_payload=$2::jsonb WHERE id=$1::uuid",
                         tool["tool_call_id"], json.dumps({"ok": False}))
    elif damage == "callback_source":
        await db.execute("UPDATE agent_run_events SET payload=payload-'source' "
                         "WHERE run_id=$1::uuid AND event_type='tool_finished_succeeded'", run.run_id)
    elif damage == "redaction_metadata":
        await db.execute("UPDATE agent_run_events SET payload=payload-'redaction_checked' "
                         "WHERE run_id=$1::uuid AND event_type='tool_finished_succeeded'", run.run_id)
    else:
        await db.execute("UPDATE agent_run_tool_calls SET request_payload_json="
                         "jsonb_set(request_payload_json,'{timeout_sec}','1'::jsonb) "
                         "WHERE id=$1::uuid", tool["tool_call_id"])
    service = AgentOrchestrationService(await native_database.reconnect())
    await assert_denied(receipts(service, run))
