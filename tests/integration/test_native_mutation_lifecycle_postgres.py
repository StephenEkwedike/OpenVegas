"""Actual native mutation orchestration, callbacks and continuation on local PG.

Synthetic provider only; no proposed-tool, approval, or accepted-result seed rows.
Real filesystem writes are limited to pytest's temporary registered workspace.
This proves authenticated observation consistency, not dishonest-client disk truth.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from openvegas.agent.native_mutation_service import load_preparation_tx
from openvegas.agent.runtime_write import capture_source, execute_native_mutation
from openvegas.contracts.errors import ContractError
from tests.integration.test_native_continuation_postgres import followup, payload, post
from tests.integration.test_native_history_postgres import new_run, projection
from tests.integration.test_native_mutation_preparation_postgres import (
    PRIVATE,
    continuation_db,
    mutation_db,
    require_owned_database,
)

# Explicit fixture imports preserve the real through-48 disposable DB setup.
__all__ = ["continuation_db", "mutation_db", "require_owned_database"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def lifecycle(mutation_db, tmp_path):
    c = mutation_db
    c.root = tmp_path.resolve()
    c.target = c.root / "notes.txt"
    c.target.write_bytes(b"before\n")
    await c.service.register_workspace(**c.run.scope(), workspace_root=str(c.root),
                                      workspace_fingerprint="sha256:" + "a" * 64)
    c.command["native_scope"] = {"run_id": c.run.run_id, "runtime_session_id": c.run.runtime_session_id,
                                **await projection(c.service, c.run)}
    yield c


def call(name="Write", **arguments):
    if not arguments:
        arguments = {"filepath": "notes.txt", "content": "after\n", "write_mode": "replace"}
    return {"tool_name": name, "arguments": arguments, "shell_mode": "mutating", "timeout_sec": 30}


async def emit(c, calls=None):
    calls = [call()] if calls is None else calls
    message = {"role": "assistant", "content": None,
               "reasoning_details": [{"type": "reasoning.encrypted", "data": PRIVATE}],
               "tool_calls": [{"id": f"original-{i}", "type": "function", "function": {
                   "name": "call_local_tool", "arguments": json.dumps(item)}} for i, item in enumerate(calls)]}
    c.provider_body = {"id": "gen-native-local", "model": c.command["model"],
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00002}}
    first = payload(await post(c))
    assert len(c.calls) == 1
    c.first = first
    return first


async def prepare(c, index=0):
    return await c.mutations.prepare(**c.run.scope(), **await projection(c.service, c.run),
        native_inference_request_id=c.first["native_generation"]["inference_request_id"],
        native_provider_call_id=f"original-{index}", idempotency_key=f"prepare-{index}",
        observed_source=capture_source(str(c.root), "notes.txt"))


async def propose(c, p, index=0, **changes):
    args = {**c.run.scope(), **await projection(c.service, c.run), "actor_role": "user",
        "idempotency_key": f"propose-{index}", "plan_mode": False,
        **{key: p[key] for key in ("tool_name", "arguments", "shell_mode", "timeout_sec")},
        "native_inference_request_id": c.first["native_generation"]["inference_request_id"],
        "native_provider_call_id": f"original-{index}", **changes}
    result = await c.service.propose_tool_call(**args)
    assert result.status_code == 200, result.payload
    tool = result.payload["tool_request"]
    assert tool["arguments"] == p["arguments"] and tool["requires_approval"] is True
    assert await c.db.fetchval("SELECT commit_state FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"]) == "pending_commit"
    return tool


async def approve(c, p, tool):
    return await c.mutations.approve(**c.run.scope(), **await projection(c.service, c.run),
        preparation_id=p["preparation_id"], tool_call_id=tool["tool_call_id"],
        contract_sha256=p["contract_sha256"], idempotency_key="approval-" + p["preparation_id"])


async def consume(c, tool, approved):
    result = await c.service.consume_approval(user_id=c.user, actor_role="user", run_id=c.run.run_id,
        tool_call_id=tool["tool_call_id"], approval_id=approved["approval_id"],
        idempotency_key="consume-" + approved["approval_id"],
        expected_run_version=approved["run_version"], expected_valid_actions_signature=approved["valid_actions_signature"])
    assert result.status_code == 200, result.payload
    assert result.payload["run_version"] == approved["run_version"] + 1
    return result


async def start(c, tool):
    return await c.service.start_tool_call(**c.run.scope(), **await projection(c.service, c.run),
        actor_role="user", tool_call_id=tool["tool_call_id"], execution_token=tool["execution_token"],
        idempotency_key="start-" + tool["tool_call_id"])


async def ready(c, index=0):
    p = await prepare(c, index)
    tool = await propose(c, p, index)
    await consume(c, tool, await approve(c, p, tool))
    result = await start(c, tool)
    assert result.status_code == 200, result.payload
    assert await c.db.fetchval("SELECT status FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"]) == "started"
    return p, tool


async def write(c, p):
    async with c.db.transaction() as tx:
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        _, plan = await load_preparation_tx(tx, run=run, preparation_id=p["preparation_id"])
    proof = execute_native_mutation(str(c.root), plan.document())
    assert proof["outcome"] in {"applied", "no_change"}, proof
    assert c.target.read_bytes() == plan.content_utf8.encode("utf-8")
    assert not list(c.root.glob(".openvegas-native-*.tmp"))
    return proof


async def result(c, tool, proof, *, status="succeeded", **changes):
    args = {**c.run.scope(), "actor_role": "user", "tool_call_id": tool["tool_call_id"],
            "execution_token": tool["execution_token"], "result_status": status,
            "result_payload": {"native_mutation_proof": proof}, "stdout": "", "stderr": "", **changes}
    return await c.service.result_tool_call(**args)


async def denied(awaitable):
    try:
        response = await awaitable
    except ContractError:
        return
    assert response.status_code == 409, response.payload


async def continue_native(c):
    command = await followup(c, c.first)
    c.provider_body = None
    c.emit_calls = False
    return await c.client.post("/inference/ask", json=command)


@pytest.mark.parametrize("operation,expected", [
    (call(), b"after\n"),
    (call("InsertAtEnd", filepath="notes.txt", content="tail"), b"before\ntail"),
    (call("FindAndReplace", filepath="notes.txt", old_string="before", new_string="after"), b"after\n"),
    (call("Write", filepath="notes.txt", content="before\n", write_mode="replace"), b"before\n"),
])
async def test_real_writer_approved_proof_reaches_native_continuation(lifecycle, operation, expected):
    c = lifecycle
    await emit(c, [operation])
    p, tool = await ready(c)
    proof = await write(c, p)
    finished = await result(c, tool, proof)
    assert finished.status_code == 200, finished.payload
    row = await c.db.fetchrow("SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"])
    assert row["status"] == "succeeded" and row["commit_state"] == "committed"
    assert c.target.read_bytes() == expected
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 1
    response = await continue_native(c)
    final = payload(response)
    assert final["native_generation"]["history_revision"] == 1
    assert len(c.calls) == 2
    messages = c.calls[-1]["messages"]
    assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "original-0"
    assert json.loads(messages[-1]["content"])["payload"] == {"native_mutation_proof": proof}
    assert PRIVATE in json.dumps(messages[-2]) and PRIVATE not in response.text


@pytest.mark.parametrize("approval_stage", ["missing", "unconsumed"])
async def test_start_requires_consumed_exact_approval(lifecycle, approval_stage):
    c = lifecycle
    await emit(c)
    p = await prepare(c)
    tool = await propose(c, p)
    if approval_stage == "unconsumed":
        await approve(c, p, tool)
    await denied(start(c, tool))
    row = await c.db.fetchrow("SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"])
    assert row["status"] == "proposed" and row["claimed_at"] is None
    assert c.target.read_bytes() == b"before\n"


@pytest.mark.parametrize("boundary", ["approve", "start"])
async def test_configured_secret_policy_rechecked_before_new_grant(lifecycle, monkeypatch, boundary):
    c = lifecycle
    await emit(c)
    p = await prepare(c)
    tool = await propose(c, p)
    if boundary == "start":
        await consume(c, tool, await approve(c, p, tool))
    original = await c.db.fetchrow("SELECT * FROM native_mutation_preparations")
    events = await c.db.fetchval("SELECT count(*) FROM agent_run_events")
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "^mutating$")
    await denied(approve(c, p, tool) if boundary == "approve" else start(c, tool))
    assert await c.db.fetchrow("SELECT * FROM native_mutation_preparations") == original
    row = await c.db.fetchrow("SELECT * FROM agent_run_tool_calls")
    assert row["status"] == "proposed" and row["claimed_at"] is None
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_events") == events
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 0
    if boundary == "approve":
        assert await c.db.fetchval("SELECT count(*) FROM agent_tool_approvals") == 0
    assert len(c.calls) == 1 and c.target.read_bytes() == b"before\n"


async def test_mutation_marker_cannot_enter_unowned_legacy_tool_path(lifecycle):
    c = lifecycle
    await emit(c)
    p = await prepare(c)
    other = await new_run(c.service, c.user)
    await denied(c.service.propose_tool_call(**other.scope(), **await projection(c.service, other),
        actor_role="user", idempotency_key="unowned-marker", plan_mode=False,
        **{key: p[key] for key in ("tool_name", "arguments", "shell_mode", "timeout_sec")}))
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 0


@pytest.mark.parametrize("change", ["contract", "path", "before", "after", "kind", "extra", "payload", "stdout", "status", "outcome"])
async def test_mismatched_proof_or_result_cannot_terminalize(lifecycle, change):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    proof = await write(c, p)
    bad = deepcopy(proof)
    options = {}
    if change == "contract":
        bad["contract_sha256"] = "0" * 64
    elif change == "path":
        bad["relative_path"] = "other.txt"
    elif change in {"before", "after"}:
        bad[f"observed_{change}"]["sha256"] = "0" * 64
    elif change == "kind":
        bad["kind"] = "trusted_disk"
    elif change == "extra":
        bad["patch"] = "untrusted patch"
    elif change == "payload":
        options["result_payload"] = {"ok": True}
    elif change == "stdout":
        options["stdout"] = "source bytes must not enter receipts"
    elif change == "status":
        options["status"] = "failed"
    else:
        bad["outcome"] = "unknown"
        bad["reason"] = "native_runtime_io"
    await denied(result(c, tool, bad, **options))
    assert await c.db.fetchval("SELECT status FROM agent_run_tool_calls") == "started"
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 0
    accepted = await result(c, tool, proof)
    assert accepted.status_code == 200


async def test_observation_insert_failure_rolls_back_terminalization_and_event(lifecycle):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    proof = await write(c, p)
    events = await c.db.fetchval("SELECT count(*) FROM agent_run_events")
    await c.db.execute("ALTER TABLE native_mutation_observations ADD CONSTRAINT fixture_insert_failure CHECK(false)")
    await denied(result(c, tool, proof))
    row = await c.db.fetchrow("SELECT * FROM agent_run_tool_calls")
    assert row["status"] == "started" and row["commit_state"] == "pending_commit"
    assert row["result_submission_hash"] is None
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_events") == events
    await c.db.execute("ALTER TABLE native_mutation_observations DROP CONSTRAINT fixture_insert_failure")
    assert (await result(c, tool, proof)).status_code == 200


async def test_exact_callback_replay_is_a_receipt_not_new_execution(lifecycle):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    proof = await write(c, p)
    first = await result(c, tool, proof)
    events = await c.db.fetchval("SELECT count(*) FROM agent_run_events")
    second = await result(c, tool, proof)
    assert second.status_code == first.status_code == 200 and second.payload == first.payload
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 1
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_events") == events
    changed = deepcopy(proof)
    changed["observed_after"]["bytes"] += 1
    await denied(result(c, tool, changed))
    await denied(start(c, tool))
    assert c.target.read_bytes() == b"after\n"
    assert (await continue_native(c)).status_code == 200
    ancestor = await result(c, tool, proof)
    assert ancestor.status_code == first.status_code and ancestor.payload == first.payload
    assert len(c.calls) == 2


async def test_expired_preparation_after_start_can_record_original_result(lifecycle):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    proof = await write(c, p)
    await c.db.execute("UPDATE native_mutation_preparations SET created_at=now()-interval '11 minutes', expires_at=now()-interval '1 minute'")
    assert (await result(c, tool, proof)).status_code == 200
    assert (await continue_native(c)).status_code == 200


@pytest.mark.parametrize("cause", ["cancel", "stale_heartbeat"])
async def test_unobserved_mutation_fences_task_without_fabricating_runtime_evidence(lifecycle, cause):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    before_version = await c.db.fetchval("SELECT version FROM agent_runs WHERE id=$1::uuid", c.run.run_id)
    cancel_args = {**c.run.scope(), "actor_role": "user", "tool_call_id": tool["tool_call_id"],
                   "execution_token": tool["execution_token"]}
    if cause == "cancel":
        response = await c.service.cancel_tool_call(**cancel_args)
        assert response.status_code == 200
    else:
        await c.db.execute("UPDATE agent_run_tool_calls SET last_heartbeat_at=NULL, started_at=now()-interval '2 minutes'")
        assert await c.service.reconcile_stale_started_tools(timeout_seconds=5) == 1
    row = await c.db.fetchrow("SELECT * FROM agent_run_tool_calls")
    run = await c.db.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid", c.run.run_id)
    assert row["status"] == ("cancelled" if cause == "cancel" else "timed_out")
    assert row["commit_state"] == "commit_unknown" and row["recovery_policy"] == "manual_intervention_required"
    assert run["state"] == "interrupted" and run["state_reason_code"] == "mutation_uncertain"
    assert run["is_resumable"] is False and run["version"] == before_version + 1
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 0
    events = await c.db.fetch("SELECT event_type,payload FROM agent_run_events")
    assert all("runtime_callback" not in str(event["payload"]) for event in events)
    if cause == "cancel":
        assert response.payload["run_version"] == run["version"]
        assert response.payload["current_state"] == "interrupted"
        assert (await c.service.cancel_tool_call(**cancel_args)).status_code == 200
    else:
        saved = json.loads(row["terminal_response_body_text"])
        assert saved["run_version"] == run["version"] and saved["current_state"] == "interrupted"
        assert await c.service.reconcile_stale_started_tools(timeout_seconds=5) == 0
        # A synthetic timeout receipt is not a genuine runtime callback to replay.
        await denied(result(c, tool, None, status="timed_out", result_payload=json.loads(row["result_payload"])))
    proof = {"kind": "runtime_observed_file_v1", "contract_sha256": p["contract_sha256"], "relative_path": "notes.txt",
             "observed_before": None, "observed_after": None, "outcome": "unknown", "reason": "native_runtime_interrupted"}
    await denied(result(c, tool, proof, status="failed"))
    await denied(start(c, tool))
    assert (await continue_native(c)).status_code == 409
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 0
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_events") == len(events)
    assert await c.db.fetchval("SELECT version FROM agent_runs WHERE id=$1::uuid", c.run.run_id) == run["version"]
    assert len(c.calls) == 1 and c.target.read_bytes() == b"before\n"


@pytest.mark.parametrize("outcome,state", [("not_applied", "commit_failed"), ("unknown", "commit_unknown")])
async def test_noncommitted_mutations_are_recorded_but_never_forwarded(lifecycle, outcome, state):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    before = {"exists": p["before_exists"], "sha256": p["before_sha256"], "bytes": p["before_bytes"]}
    proof = {"kind": "runtime_observed_file_v1", "contract_sha256": p["contract_sha256"], "relative_path": "notes.txt",
             "observed_before": before, "observed_after": before if outcome == "not_applied" else None,
             "outcome": outcome, "reason": "native_runtime_io"}
    response = await result(c, tool, proof, status="failed")
    assert response.status_code == 409
    row = await c.db.fetchrow("SELECT * FROM agent_run_tool_calls")
    assert row["status"] == "failed" and row["commit_state"] == state
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 1
    if outcome == "unknown":
        run = await c.db.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid", c.run.run_id)
        assert run["state"] not in {"created", "running"} and run["is_resumable"] is False
        assert run["state_reason_code"] == "mutation_uncertain"
        assert response.payload["current_state"] == run["state"]
        assert response.payload["run_version"] == run["version"]
        events = await c.db.fetchval("SELECT count(*) FROM agent_run_events")
        replay = await result(c, tool, proof, status="failed")
        assert replay.status_code == response.status_code and replay.payload == response.payload
        assert await c.db.fetchval("SELECT count(*) FROM agent_run_events") == events
        assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 1
        assert await c.db.fetchval("SELECT version FROM agent_runs WHERE id=$1::uuid", c.run.run_id) == run["version"]
        await denied(start(c, tool))
        await denied(prepare(c))
        await denied(approve(c, p, tool))
    assert (await continue_native(c)).status_code == 409
    assert len(c.calls) == 1 and c.target.read_bytes() == b"before\n"


@pytest.mark.parametrize("change", ["strict", "latest", "unexpired", "reason", "resumable", "cancel",
                                   "expired_run", "owner", "runtime", "workspace", "source"])
async def test_historical_loader_does_not_relax_other_gates(lifecycle, change):
    c = lifecycle
    await emit(c)
    p = await prepare(c)
    await c.db.execute("UPDATE agent_runs SET state='interrupted', state_reason_code='mutation_uncertain', is_resumable=FALSE")
    async with c.db.transaction() as tx:
        run = dict(await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id))
        kwargs = {"require_latest": False, "require_unexpired": False}
        _, plan = await load_preparation_tx(tx, run=run, preparation_id=p["preparation_id"], **kwargs)
        assert plan.contract_sha256 == p["contract_sha256"]
        if change in {"strict", "latest", "unexpired"}:
            kwargs = {"require_latest": change != "unexpired", "require_unexpired": change != "latest"}
        elif change == "reason":
            run["state_reason_code"] = "cancelled"
        elif change == "resumable":
            run["is_resumable"] = True
        elif change == "cancel":
            run["cancel_requested_at"] = datetime.now(UTC)
        elif change == "expired_run":
            run["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "owner":
            run["user_id"] = uuid4()
        elif change == "runtime":
            kwargs["runtime_session_id"] = str(uuid4())
        elif change == "workspace":
            run["workspace_fingerprint"] = "sha256:" + "b" * 64
        elif change == "source":
            kwargs["native_inference_request_id"] = str(uuid4())
        with pytest.raises(ContractError):
            await load_preparation_tx(tx, run=run, preparation_id=p["preparation_id"], **kwargs)
    assert len(c.calls) == 1 and c.target.read_bytes() == b"before\n"


async def test_same_file_batch_prepares_each_baseline_after_previous_accepted_write(lifecycle):
    c = lifecycle
    await emit(c, [call(), call("InsertAtEnd", filepath="notes.txt", content="tail")])
    first, first_tool = await ready(c, 0)
    assert (await result(c, first_tool, await write(c, first))).status_code == 200
    # A missing second callback must not dispatch a paid continuation.
    incomplete = await continue_native(c)
    assert incomplete.status_code == 409 and len(c.calls) == 1
    second, second_tool = await ready(c, 1)
    assert second["before_sha256"] == first["after_sha256"]
    assert (await result(c, second_tool, await write(c, second))).status_code == 200
    command = await followup(c, c.first)
    command["idempotency_key"] = "after-complete-batch"
    final = await c.client.post("/inference/ask", json=command)
    assert payload(final)["native_generation"]["history_revision"] == 1
    assert c.target.read_bytes() == b"after\ntail"
    assert [m["tool_call_id"] for m in c.calls[-1]["messages"][-2:]] == ["original-0", "original-1"]
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 2


@pytest.mark.parametrize("change", ["missing_observation", "proof", "commit_state"])
async def test_continuation_revalidates_stored_proof_and_commit_state(lifecycle, change):
    c = lifecycle
    await emit(c)
    p, tool = await ready(c)
    assert (await result(c, tool, await write(c, p))).status_code == 200
    if change == "missing_observation":
        await c.db.execute("DELETE FROM native_mutation_observations")
    elif change == "proof":
        await c.db.execute("UPDATE native_mutation_observations SET proof_json='{}'")
    else:
        await c.db.execute("UPDATE agent_run_tool_calls SET commit_state='commit_unknown'")
    assert (await continue_native(c)).status_code == 409
    assert len(c.calls) == 1


async def test_concurrent_start_grants_only_one_execution_claim(lifecycle):
    c = lifecycle
    await emit(c)
    p = await prepare(c)
    tool = await propose(c, p)
    await consume(c, tool, await approve(c, p, tool))
    responses = await asyncio.gather(start(c, tool), start(c, tool), return_exceptions=True)
    assert sum(getattr(item, "status_code", None) == 200 for item in responses) == 1
    assert all(isinstance(item, ContractError) or getattr(item, "status_code", None) in {200, 409}
               for item in responses)
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_events WHERE event_type='tool_claimed_started'") == 1
    assert (await result(c, tool, await write(c, p))).status_code == 200


async def test_expired_preparation_cannot_authorize_new_start(lifecycle):
    c = lifecycle
    await emit(c)
    p = await prepare(c)
    tool = await propose(c, p)
    await consume(c, tool, await approve(c, p, tool))
    await c.db.execute("UPDATE native_mutation_preparations SET created_at=now()-interval '11 minutes', expires_at=now()-interval '1 minute'")
    await denied(start(c, tool))
    assert await c.db.fetchval("SELECT status FROM agent_run_tool_calls") == "proposed"
    assert c.target.read_bytes() == b"before\n"
