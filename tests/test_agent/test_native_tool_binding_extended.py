"""Exact flat tool mappings, without provider calls or real shell execution."""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest

from openvegas.agent.native_history import (
    bind_native_call_tx,
    expected_runtime_call,
    lock_native_source_tx,
)
from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.contracts.errors import ContractError
from openvegas.gateway.openrouter import _flat_tool
from tests.test_agent.test_native_history_contract import REQUEST, RUN_ROW, TOOL, Tx
from tests.test_agent.test_native_proposal_replay import Database, propose

NORMALIZE = AgentOrchestrationService._normalize_tool_arguments


def native(name, arguments, *, mode="read_only", timeout=30):
    return {"tool_name": name, "arguments": arguments, "shell_mode": mode,
            "timeout_sec": timeout, "provider_call_id": "call-1"}


@pytest.mark.parametrize("call,tool,args,mode", [
    (native("Read", {"path": "a.txt"}), "fs_read", {"path": "a.txt"}, "read_only"),
    (native("Read", {"filepath": "a.txt"}), "fs_read", {"filepath": "a.txt", "path": "a.txt"}, "read_only"),
    (native("List", {}), "fs_list", {"path": "."}, "read_only"),
    (native("List", {"path": "src", "recursive": True, "max_entries": 9}), "fs_list",
     {"path": "src", "recursive": True, "max_entries": 9}, "read_only"),
    (native("Search", {"pattern": r"error\d+"}), "fs_search",
     {"pattern": r"error\d+", "path": ".", "max_files": 250, "max_matches": 120}, "read_only"),
    (native("Search", {"pattern": "error", "path": "src", "max_files": 1000, "max_matches": 201}),
     "fs_search", {"pattern": "error", "path": "src", "max_files": 500, "max_matches": 200}, "read_only"),
    (native("Bash", {"command": "printf 'hello\\n'"}), "shell_run", {"command": "printf 'hello\\n'"}, "read_only"),
    (native("Bash", {"command": "printf 'hello\\n' > a.txt"}, mode="mutating"), "shell_run",
     {"command": "printf 'hello\\n' > a.txt"}, "mutating"),
])
def test_exact_mapping_preserves_original_and_only_adds_reviewed_defaults(call, tool, args, mode):
    original = copy.deepcopy(call)
    assert expected_runtime_call(call, NORMALIZE) == (tool, args, mode)
    assert call == original


@pytest.mark.parametrize("call", [
    native("Read", {}), native("Read", {"path": "a", "filepath": "b"}),
    native("Read", {"path": {"path": "a"}}), native("Read", {"path": " a"}),
    native("Read", {"path": "a", "max_bytes": True}), native("List", {"recursive": 1}),
    native("List", {"path": "a"}, mode="mutating"),
    native("Search", {}), native("Search", {"query": "error"}),
    native("Search", {"pattern": " error "}), native("Search", {"pattern": "error", "max_files": "9"}),
    native("Search", {"pattern": "error", "max_matches": 0}),
    native("Search", {"pattern": "error"}, mode="mutating"),
    native("Bash", {}), native("Bash", {"command": "pwd", "cwd": "/different"}),
    native("Bash", {"command": "pwd", "foreground_job_id": "job-1"}),
    native("Bash", {"command": "pwd", "background": True}),
    native("Bash", {"command": " pwd"}), native("Bash", {"cmd": "pwd"}),
    native("Bash", {"command": "pwd"}, mode="unrestricted"),
    native("Bash", {"command": "printf 'sk-or-v1-" + "a" * 64 + "'"}),
    native("Search", {"pattern": "\x1b[2J"}),
])
def test_inferred_fields_coercion_mode_and_secret_changes_are_rejected(call):
    with pytest.raises(ContractError):
        expected_runtime_call(call, NORMALIZE)


@pytest.mark.parametrize("name,args", [
    ("Write", {"filepath": "a.txt", "content": "hello", "write_mode": "replace"}),
    ("FindAndReplace", {"filepath": "a.txt", "old_string": "old", "new_string": "new"}),
    ("InsertAtEnd", {"filepath": "a.txt", "content": "tail"}),
])
def test_advertised_flat_mutations_fail_closed_without_verified_patch_proof(name, args):
    import json

    call = _flat_tool({"name": name, "arguments": json.dumps(args)}, "google/gemini-2.5-flash")
    with pytest.raises(ContractError, match="source-to-patch"):
        expected_runtime_call(call, NORMALIZE)
    with pytest.raises(ContractError, match="source-to-patch"):
        expected_runtime_call(native(name, dict(args, patch="client-inferred-patch")), NORMALIZE)


def test_unreviewed_normalizer_changes_are_rejected():
    def rewrite(**kwargs):
        return dict(kwargs["arguments"], command="python3 --version")

    with pytest.raises(ContractError, match="meaning"):
        expected_runtime_call(native("Bash", {"command": "python --version"}), rewrite)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,mode", [
    ("Search", {"pattern": "error"}, "read_only"),
    ("Bash", {"command": "pwd"}, "read_only"),
    ("Bash", {"command": "printf done > out.txt"}, "mutating"),
])
async def test_real_proposal_and_replay_keep_approval_and_exact_original(name, args, mode):
    db = Database()
    call = native(name, args, mode=mode)
    db.source["response_body_text"]["tool_calls"] = [call]
    tool, arguments, _ = expected_runtime_call(call, NORMALIZE)
    options = {"tool_name": tool, "arguments": arguments, "shell_mode": mode}
    first = await propose(db, **options)
    before = db.snapshot()
    second = await propose(db, **options)
    assert first.payload == second.payload
    assert second.payload["tool_request"]["requires_approval"] is (mode == "mutating")
    assert db.turns[0]["content_json"]["native_call"] == call
    assert db.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["command", "mode", "timeout", "root", "fingerprint", "git_root"])
async def test_shell_replay_rejects_changed_execution_identity(change):
    db = Database()
    call = native("Bash", {"command": "pwd"}, mode="mutating")
    db.source["response_body_text"]["tool_calls"] = [call]
    options = {"tool_name": "shell_run", "arguments": {"command": "pwd"}, "shell_mode": "mutating"}
    await propose(db, **options)
    if change == "command":
        options["arguments"] = {"command": "ls"}
    elif change == "mode":
        options["shell_mode"] = "read_only"
    elif change == "timeout":
        options["timeout_sec"] = 1
    else:
        key = {"root": "workspace_root", "fingerprint": "workspace_fingerprint", "git_root": "git_root"}[change]
        next(iter(db.runs.values()))[key] = "changed"
    with pytest.raises(ContractError):
        await propose(db, **options)


@pytest.mark.asyncio
async def test_new_binding_from_ancestor_revision_is_rejected_before_write():
    tx = Tx()
    tx.source["native_route_command_id"] = "44444444-4444-4444-8444-444444444444"
    run = dict(RUN_ROW, native_generation_claim_id="55555555-5555-4555-8555-555555555555")
    with pytest.raises(ContractError, match="revision advanced"):
        await bind_native_call_tx(
            tx, run=run, inference_request_id=REQUEST, provider_call_id="call-1", tool_call_id=TOOL,
            tool_name="fs_read", arguments={"path": "a.txt"}, shell_mode="read_only", timeout_sec=5,
            normalize=NORMALIZE, locked_source=(tx.source, {}),
        )
    assert tx.binding is None and tx.queries == []


@pytest.mark.asyncio
async def test_source_route_lock_targets_original_gateway_before_gateway_lock(monkeypatch):
    import openvegas.agent.native_history as history

    tx = AsyncMock()
    source = {"status": "succeeded", "response_status": 200}
    tx.fetchrow.side_effect = [{"id": "ancestor-route"}, source]
    monkeypatch.setattr(history, "verify_source_scope_tx", AsyncMock(return_value={"owned": True}))
    run = dict(RUN_ROW, native_generation_claim_id="latest-route")
    assert await lock_native_source_tx(tx, run=run, request_id=REQUEST) == (source, {"owned": True})
    route, gateway = tx.fetchrow.call_args_list
    assert "gateway_request_id=$1::uuid" in route.args[0] and "FOR UPDATE" in route.args[0]
    assert route.args[1:] == (REQUEST, run["id"], run["user_id"])
    assert "inference_requests" in gateway.args[0] and "FOR UPDATE" in gateway.args[0]
