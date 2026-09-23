"""Private preparation/approval foundations on guarded disposable PostgreSQL.

Uses real route/gateway settlement with a synthetic supplier and auth scaffold.
Does not certify router wiring, local disk truth, actual execution or continuation.
"""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from openvegas.agent.native_mutation_service import (
    NativeMutationService,
    approval_context_hash,
    load_preparation_tx,
)
from openvegas.agent.runtime_contracts import tool_payload_hash
from openvegas.contracts.errors import ContractError
from tests.integration.test_native_continuation_postgres import continuation_db as _continuation_db
from tests.integration.test_native_continuation_postgres import payload, post
from tests.integration.test_native_history_postgres import projection

continuation_db = _continuation_db
pytestmark = pytest.mark.asyncio
PRIVATE = "opaque-private-mutation-fixture"


@pytest.fixture(autouse=True)
def require_owned_database(integration_environment):
    # The shared guard requires loopback/ov_test_* and strips supplier secrets.
    # database_factory additionally requires an empty DB and acquires ownership.
    assert integration_environment


@pytest_asyncio.fixture
async def mutation_db(continuation_db, monkeypatch):
    c = continuation_db
    await c.sandbox.migrate(through=48)
    monkeypatch.setenv("OPENVEGAS_NATIVE_MUTATIONS", "1")
    c.mutations = NativeMutationService(c.db)
    c.seeded = False
    yield c


async def seed(c, *, name="Write", arguments=None):
    arguments = {"filepath": "notes.txt", "content": "after\n", "write_mode": "replace"} if arguments is None else arguments
    call = {"tool_name": name, "arguments": arguments, "shell_mode": "mutating", "timeout_sec": 30}
    c.provider_body = {"id": "gen-native-local", "model": c.command["model"],
        "choices": [{"message": {"role": "assistant", "content": None,
            "reasoning_details": [{"type": "reasoning.encrypted", "data": PRIVATE}],
            "tool_calls": [{"id": "native-write-1", "type": "function", "function": {
                "name": "call_local_tool", "arguments": json.dumps(call)}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00002}}
    result = payload(await post(c))
    c.seeded = True
    c.prepare_args = {**c.run.scope(), **await projection(c.service, c.run),
        "native_inference_request_id": result["native_generation"]["inference_request_id"],
        "native_provider_call_id": "native-write-1", "idempotency_key": "prepare-1",
        "observed_source": {"exists": True, "content_utf8": "before\n"}}
    return result


async def prepare(c, **changes):
    if not c.seeded:
        await seed(c)
    return await c.mutations.prepare(**{**c.prepare_args, **changes})


async def proposed(c, p):
    """Parent owns proposal integration; seed only its immutable persistence boundary."""
    tool_id = str(uuid4())
    request = {key: p[key] for key in ("tool_name", "arguments", "shell_mode", "timeout_sec")}
    digest = tool_payload_hash(p["tool_name"], p["arguments"], p["shell_mode"])
    await c.db.execute("""INSERT INTO agent_run_tool_calls(id,run_id,run_version,tool_name,tool_class,payload_hash,
        status,commit_state,approval_required,request_payload_json,execution_token)
        VALUES($1::uuid,$2::uuid,$3,'fs_apply_patch','mutating',$4,'proposed','pending_commit',TRUE,$5::jsonb,$6)""",
        tool_id, c.run.run_id, c.prepare_args["expected_run_version"], digest, json.dumps(request), "a" * 32)
    await c.db.execute("UPDATE native_mutation_preparations SET tool_call_id=$2::uuid WHERE id=$1::uuid", p["preparation_id"], tool_id)
    return tool_id, digest


async def approval_args(c, p):
    tool, digest = await proposed(c, p)
    return {**c.run.scope(), **await projection(c.service, c.run), "tool_call_id": tool,
            "preparation_id": p["preparation_id"], "contract_sha256": p["contract_sha256"], "idempotency_key": "approve-1"}, digest


@pytest.mark.parametrize("name,args,expected", [
    ("Write", {"filepath": "notes.txt", "content": "after\n", "write_mode": "replace"}, "after\n"),
    ("InsertAtEnd", {"filepath": "notes.txt", "content": "tail"}, "before\ntail"),
    ("FindAndReplace", {"filepath": "notes.txt", "old_string": "before", "new_string": "after"}, "after\n"),
])
async def test_original_private_source_prepares_all_three_transforms(mutation_db, name, args, expected):
    c = mutation_db
    await seed(c, name=name, arguments=args)
    p = await prepare(c)
    assert p["evidence_kind"] == "runtime_observed_file_v1"
    assert PRIVATE not in json.dumps(p) and "observed_source" not in p and "content_utf8" not in p
    async with c.db.transaction() as tx:
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        row, plan = await load_preparation_tx(tx, run=run, preparation_id=p["preparation_id"])
        assert plan.content_utf8 == expected
        assert row["original_ordinal"] == 0
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM agent_run_tool_calls") == 0


async def test_exact_prepare_replay_concurrent_and_after_restart(mutation_db):
    c = mutation_db
    await seed(c)
    first, second = await asyncio.gather(prepare(c), prepare(c))
    assert first == second
    c.mutations = NativeMutationService(await c.sandbox.reconnect())
    assert await prepare(c) == first
    assert await c.sandbox.db.fetchval("SELECT count(*) FROM native_mutation_preparations") == 1


@pytest.mark.parametrize("change", ["source", "key", "projection"])
async def test_prepare_replay_changes_reject(mutation_db, change):
    c = mutation_db
    await prepare(c)
    changes = {"source": {"observed_source": {"exists": True, "content_utf8": "different"}},
               "key": {"idempotency_key": "another-key"},
               "projection": {"expected_run_version": c.prepare_args["expected_run_version"] + 1}}[change]
    with pytest.raises(ContractError):
        await prepare(c, **changes)
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_preparations") == 1


@pytest.mark.parametrize("change", ["owner", "runtime", "projection", "workspace", "cancel", "expired_run", "unsettled", "usage", "envelope", "latest", "plan"])
async def test_ownership_settlement_and_projection_gates_before_storage(mutation_db, change):
    c = mutation_db
    await seed(c)
    args = {}
    if change in {"owner", "runtime"}:
        args["user_id" if change == "owner" else "runtime_session_id"] = str(uuid4())
    elif change == "projection":
        args["expected_valid_actions_signature"] = "sha256:" + "0" * 64
    elif change == "workspace":
        await c.db.execute("UPDATE agent_runs SET workspace_fingerprint=$1", "sha256:" + "b" * 64)
    elif change == "cancel":
        await c.db.execute("UPDATE agent_runs SET cancel_requested_at=now()")
    elif change == "expired_run":
        await c.db.execute("UPDATE agent_runs SET expires_at=now()-interval '1 second'")
    elif change == "unsettled":
        await c.db.execute("UPDATE inference_preauthorizations SET status='voided'")
    elif change == "usage":
        await c.db.execute("UPDATE inference_usage SET v_cost=v_cost+1")
    elif change == "envelope":
        await c.db.execute("UPDATE native_generation_envelopes SET assistant_sha256=repeat('0',64)")
    elif change == "latest":
        await c.db.execute("UPDATE agent_runs SET native_history_revision=native_history_revision+1")
    elif change == "plan":
        args["plan_mode"] = True
    with pytest.raises(ContractError):
        await prepare(c, **args)
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_preparations") == 0


@pytest.mark.parametrize("flag", ["OPENVEGAS_NATIVE_MUTATIONS", "OPENVEGAS_NATIVE_GENERATION_HISTORY", "OPENVEGAS_NATIVE_GENERATION_SCOPE"])
async def test_each_gate_off_refuses_before_storage(mutation_db, monkeypatch, flag):
    c = mutation_db
    await seed(c)
    monkeypatch.delenv(flag)
    with pytest.raises(ContractError, match="disabled"):
        await prepare(c)
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_preparations") == 0


@pytest.mark.parametrize("observed", [
    {"exists": True, "content_utf8": "x" * 32769},
    {"exists": True, "content_utf8": "before", "patch": "client authority"},
    {"exists": False, "content_utf8": ""},
    {"exists": True, "content_utf8": "\x00"},
])
async def test_invalid_observations_are_never_stored_or_echoed(mutation_db, observed):
    c = mutation_db
    await seed(c)
    with pytest.raises(ContractError) as exc:
        await prepare(c, observed_source=observed)
    assert len(str(exc.value)) < 180
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_preparations") == 0


@pytest.mark.parametrize("location", ["observed", "call", "call_metadata", "patch", "replacement_marker"])
async def test_configured_sensitive_pattern_rejects_without_private_insert(mutation_db, monkeypatch, location):
    c = mutation_db
    marker = "CUSTOM_FIXTURE_ONLY_7"
    content = marker if location == "call" else "[REDACTED]" if location == "replacement_marker" else "after\n"
    await seed(c, arguments={"filepath": "notes.txt", "content": content, "write_mode": "replace"})
    observed = {"exists": True, "content_utf8": marker if location == "observed" else "before\n"}
    patterns = {"call_metadata": "^mutating$", "patch": r"(?m)^\+after$", "replacement_marker": r"\[REDACTED\]"}
    original = await c.db.fetch("SELECT * FROM native_generation_envelopes ORDER BY request_id")
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", patterns.get(location, marker))
    with pytest.raises(ContractError) as exc:
        await prepare(c, observed_source=observed)
    assert exc.value.detail == "Native mutation transformation rejected."
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_preparations") == 0
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_approval_commands") == 0
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_observations") == 0
    assert await c.db.fetch("SELECT * FROM native_generation_envelopes ORDER BY request_id") == original
    assert observed["content_utf8"] == (marker if location == "observed" else "before\n")
    assert len(c.calls) == 1


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("pattern", ["before", "after", "^mutating$", r"(?m)^\+after$"])
async def test_load_rechecks_configured_pattern_without_rewriting_private_bytes(mutation_db, monkeypatch, historical, pattern):
    c = mutation_db
    p = await prepare(c)
    original = await c.db.fetch("SELECT * FROM native_generation_envelopes ORDER BY request_id")
    stored = await c.db.fetchrow("SELECT * FROM native_mutation_preparations")
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", pattern)
    async with c.db.transaction() as tx:
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        with pytest.raises(ContractError) as exc:
            await load_preparation_tx(tx, run=run, preparation_id=p["preparation_id"],
                                      require_latest=not historical, require_unexpired=not historical)
        assert exc.value.detail == "Native mutation transformation rejected."
    assert await c.db.fetchrow("SELECT * FROM native_mutation_preparations") == stored
    assert await c.db.fetch("SELECT * FROM native_generation_envelopes ORDER BY request_id") == original
    assert await c.db.fetchval("SELECT count(*) FROM agent_tool_approvals") == 0
    assert len(c.calls) == 1


@pytest.mark.parametrize("change", ["expired", "plan", "digest"])
async def test_loading_revalidates_expiry_and_committed_plan(mutation_db, change):
    c = mutation_db
    p = await prepare(c)
    if change == "expired":
        await c.db.execute("UPDATE native_mutation_preparations SET created_at=now()-interval '11 minutes', expires_at=now()-interval '1 minute'")
    elif change == "plan":
        await c.db.execute("UPDATE native_mutation_preparations SET plan_json=jsonb_set(plan_json::jsonb,'{after_bytes}','123')::text")
    else:
        await c.db.execute("UPDATE native_mutation_preparations SET contract_sha256=repeat('0',64)")
    async with c.db.transaction() as tx:
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        with pytest.raises(ContractError):
            await load_preparation_tx(tx, run=run, preparation_id=p["preparation_id"])


async def test_approval_exact_replay_and_existing_atomic_consumption(mutation_db):
    c = mutation_db
    p = await prepare(c)
    args, digest = await approval_args(c, p)
    approved = await c.mutations.approve(**args)
    assert await c.mutations.approve(**args) == approved
    row = await c.db.fetchrow("SELECT * FROM agent_tool_approvals")
    assert row["decision_state"] == "approved" and row["consumed_at"] is None
    assert row["approval_context_hash"] == approval_context_hash(p["preparation_id"], p["contract_sha256"], digest)
    for change in ({"idempotency_key": "new-key"}, {"expected_run_version": args["expected_run_version"] + 1}):
        with pytest.raises(ContractError):
            await c.mutations.approve(**{**args, **change})
    consumed = await c.service.consume_approval(user_id=c.user, actor_role="user", run_id=c.run.run_id,
        tool_call_id=args["tool_call_id"], approval_id=approved["approval_id"], idempotency_key="consume-1",
        expected_run_version=approved["run_version"], expected_valid_actions_signature=approved["valid_actions_signature"])
    assert consumed.status_code == 200
    assert await c.db.fetchval("SELECT decision_state FROM agent_tool_approvals") == "consumed"
    assert await c.db.fetchval("SELECT status FROM agent_run_tool_calls") == "proposed"
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_approval_commands") == 1


async def test_approval_storage_failure_rolls_back_all_three_writes(mutation_db):
    c = mutation_db
    p = await prepare(c)
    args, _ = await approval_args(c, p)
    await c.db.execute("ALTER TABLE native_mutation_approval_commands ADD CONSTRAINT fixture_fail CHECK(false)")
    with pytest.raises(ContractError, match="storage"):
        await c.mutations.approve(**args)
    assert await c.db.fetchval("SELECT count(*) FROM agent_tool_approvals") == 0
    assert await c.db.fetchval("SELECT approval_id FROM native_mutation_preparations") is None


@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_private_tables_deny_real_roles_not_missing_schema(mutation_db, role):
    c = mutation_db
    await prepare(c)
    await c.db.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    for table in ("native_mutation_preparations", "native_mutation_observations", "native_mutation_approval_commands"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as exc:
            async with c.db.transaction() as tx:
                await tx.execute(f"SET LOCAL ROLE {role}")
                await tx.fetch(f"SELECT * FROM public.{table}")
        assert exc.value.sqlstate == "42501"


async def test_observations_are_claims_not_independent_disk_truth(mutation_db):
    c = mutation_db
    p = await prepare(c, observed_source={"exists": True, "content_utf8": "a dishonest runtime could claim this"})
    assert p["evidence_kind"] == "runtime_observed_file_v1"
    assert p["before_bytes"] == len("a dishonest runtime could claim this")
    assert "trusted_file" not in json.dumps(p)


@pytest.mark.parametrize("change", ["contract", "tool_payload", "projection", "runtime", "started"])
async def test_approval_rejects_changed_execution_or_authority(mutation_db, change):
    c = mutation_db
    p = await prepare(c)
    args, _ = await approval_args(c, p)
    if change == "contract":
        args["contract_sha256"] = "0" * 64
    elif change == "tool_payload":
        await c.db.execute("UPDATE agent_run_tool_calls SET payload_hash=repeat('0',64)")
    elif change == "projection":
        args["expected_valid_actions_signature"] = "sha256:" + "0" * 64
    elif change == "runtime":
        args["runtime_session_id"] = str(uuid4())
    else:
        await c.db.execute("UPDATE agent_run_tool_calls SET status='started', started_at=now(), claimed_at=now()")
    with pytest.raises(ContractError):
        await c.mutations.approve(**args)
    assert await c.db.fetchval("SELECT count(*) FROM agent_tool_approvals") == 0
    assert await c.db.fetchval("SELECT count(*) FROM native_mutation_approval_commands") == 0
