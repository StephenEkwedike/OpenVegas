"""Direct Search/Bash bindings through real CAS and private receipts, not execution."""
from __future__ import annotations

import json
import uuid

import pytest

from openvegas.agent.native_history import expected_runtime_call, load_native_tool_results_tx
from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.contracts.errors import ContractError
from tests.integration import test_native_history_postgres as native_fixtures
from tests.integration.test_native_history_postgres import (
    callback,
    projection,
    scenario,
    start,
)

pytestmark = pytest.mark.asyncio
native_database = native_fixtures.native_database


async def create_bound(sandbox, *, name="Search", mode="read_only"):
    args = {"pattern": "error"} if name == "Search" else {"command": "printf 'complete\\n'"}
    service, run, source = await scenario(sandbox, tool_name=name, arguments=args)
    source.call["shell_mode"] = mode
    source.body["tool_calls"] = [source.call]
    await sandbox.db.execute("UPDATE inference_requests SET response_body_text=$2 WHERE id=$1::uuid",
                             source.request_id, json.dumps(source.body))
    tool_name, arguments, shell_mode = expected_runtime_call(source.call, service._normalize_tool_arguments)
    proposal = {**run.scope(), **await projection(service, run), "actor_role": "user",
                "idempotency_key": str(uuid.uuid4()), "tool_name": tool_name, "arguments": arguments,
                "shell_mode": shell_mode, "timeout_sec": 30, "plan_mode": False,
                "native_inference_request_id": source.request_id,
                "native_provider_call_id": source.call["provider_call_id"]}
    first = await service.propose_tool_call(**proposal)
    second = await service.propose_tool_call(**proposal)
    assert first.payload == second.payload and first.status_code == 200
    tool = first.payload["tool_request"]
    assert tool["requires_approval"] is (mode == "mutating")
    await start(service, run, tool)
    original = {"role": "assistant", "content": None, "tool_calls": [{
        "id": source.call["provider_call_id"], "type": "function", "function": {
            "name": "call_local_tool", "arguments": json.dumps({
                k: v for k, v in source.call.items() if k != "provider_call_id"}),
        },
    }]}
    return service, run, source, tool, original


async def load(sandbox, run, source, original):
    async with sandbox.db.transaction() as tx:
        locked_run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", run.run_id)
        locked_source = await tx.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid FOR UPDATE", source.request_id)
        return await load_native_tool_results_tx(tx, run=locked_run, source=locked_source,
                                                request_id=source.request_id, assistant_message=original)


@pytest.mark.parametrize("name,mode", [("Search", "read_only"), ("Bash", "read_only"), ("Bash", "mutating")])
async def test_direct_native_proposal_callback_restart_and_lossless_continuation(native_database, name, mode):
    sandbox = native_database
    service, run, source, tool, original = await create_bound(sandbox, name=name, mode=mode)
    payload = ({"ok": True, "matches": [{"path": "a.txt", "line": 4, "text": "error"}]} if name == "Search" else {
        "ok": True, "requested_command": source.call["arguments"]["command"],
        "effective_command": source.call["arguments"]["command"],
        "execution_cwd": "/synthetic/native-workspace", "exit_code": 0,
    })
    await callback(service, run, tool, payload=payload, stdout="complete\n", stderr="warning\n")
    await sandbox.reconnect()
    result = await load(sandbox, run, source, original)
    assert result == [{"role": "tool", "tool_call_id": source.call["provider_call_id"],
                       "content": json.dumps({"status": "succeeded", "payload": payload,
                                              "stdout": "complete\n", "stderr": "warning\n"},
                                             sort_keys=True, separators=(",", ":"), ensure_ascii=False)}]
    assert await sandbox.db.fetchval("SELECT count(*) FROM agent_chat_turns WHERE run_id=$1::uuid", run.run_id) == 1


@pytest.mark.parametrize("condition", ["pending", "timeout", "secret", "changed_workspace", "changed_original", "unproposed"])
async def test_incomplete_or_changed_actual_database_receipts_block_continuation(native_database, condition):
    sandbox = native_database
    service, run, source, tool, original = await create_bound(sandbox)
    if condition != "pending":
        await callback(service, run, tool, status="timed_out" if condition == "timeout" else "succeeded",
                       payload={"ok": condition != "timeout"},
                       stdout="sk-or-v1-" + "a" * 64 if condition == "secret" else "complete\n")
    if condition == "changed_workspace":
        await sandbox.db.execute("UPDATE agent_runs SET workspace_root='/other' WHERE id=$1::uuid", run.run_id)
    elif condition == "changed_original":
        original["tool_calls"][0]["function"]["arguments"] = json.dumps({
            "tool_name": "Search", "arguments": {"pattern": "different"}})
    elif condition == "unproposed":
        await sandbox.db.execute("DELETE FROM agent_chat_turns WHERE run_id=$1::uuid", run.run_id)
    await sandbox.reconnect()
    with pytest.raises(ContractError):
        await load(sandbox, run, source, original)


async def test_mutating_native_shell_is_not_allowed_in_plan_mode(native_database):
    sandbox = native_database
    service, run, source = await scenario(sandbox, tool_name="Bash", arguments={"command": "printf done > a.txt"})
    source.call["shell_mode"] = "mutating"
    source.body["tool_calls"] = [source.call]
    await sandbox.db.execute("UPDATE inference_requests SET response_body_text=$2 WHERE id=$1::uuid",
                             source.request_id, json.dumps(source.body))
    with pytest.raises(ContractError):
        await AgentOrchestrationService(sandbox.db).propose_tool_call(
            **run.scope(), **await projection(service, run), actor_role="user", idempotency_key=str(uuid.uuid4()),
            tool_name="shell_run", arguments=source.call["arguments"], shell_mode="mutating", timeout_sec=30,
            plan_mode=True, native_inference_request_id=source.request_id,
            native_provider_call_id=source.call["provider_call_id"],
        )
    assert await sandbox.db.fetchval("SELECT count(*) FROM agent_run_tool_calls WHERE run_id=$1::uuid", run.run_id) == 0
