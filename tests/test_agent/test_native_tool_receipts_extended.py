"""Lossless native result materialization; no fake completion from pending work."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest

from openvegas.agent.native_history import (
    accepted_native_receipts_tx,
    bind_native_call_tx,
    expected_runtime_call,
    load_native_tool_results_tx,
)
from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.runtime_contracts import result_submission_hash, tool_payload_hash
from openvegas.contracts.errors import ContractError
from tests.test_agent.test_native_history_contract import REQUEST, RUN_ROW, Tx

NORMALIZE = AgentOrchestrationService._normalize_tool_arguments
MODEL = "google/gemini-2.5-flash"
RUN = dict(RUN_ROW, workspace_root="/synthetic/workspace", workspace_fingerprint="sha256:" + "a" * 64,
           git_root=None)


class ReceiptTx(Tx):
    def __init__(self, model):
        super().__init__()
        self.preauth["model_id"] = model
        self.rows = []
        self.tools = {}
        self.source.update(id=REQUEST, user_id=RUN["user_id"])

    async def fetch(self, query, *args):
        assert "agent_chat_turns" in query
        return self.rows

    async def fetchrow(self, query, *args):
        if "agent_run_tool_calls" in query:
            return self.tools.get(args[0])
        return await super().fetchrow(query, *args)

    async def execute(self, query, *args):
        outcome = await super().execute(query, *args)
        self.rows.append({"content_json": copy.deepcopy(self.binding), "tool_call_id": args[2]})
        return outcome


def rehash(tool):
    tool["stdout_sha256"] = hashlib.sha256(tool["stdout"].encode()).hexdigest()
    tool["stderr_sha256"] = hashlib.sha256(tool["stderr"].encode()).hexdigest()
    tool["result_submission_hash"] = result_submission_hash(
        result_status=tool["status"], result_payload=tool["result_payload"],
        stdout_sha256=tool["stdout_sha256"], stderr_sha256=tool["stderr_sha256"],
    )


async def scenario(*, model=MODEL, shell_mode="read_only"):
    tx = ReceiptTx(model)
    calls = [
        {"tool_name": "Search", "arguments": {"pattern": "error"}, "shell_mode": "read_only",
         "timeout_sec": 30, "provider_call_id": "call-search"},
        {"tool_name": "Bash", "arguments": {"command": "printf 'complete\\n'"}, "shell_mode": shell_mode,
         "timeout_sec": 30, "provider_call_id": "call-shell"},
    ]
    tx.body["tool_calls"] = copy.deepcopy(calls)
    original = {"role": "assistant", "content": None, "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
                "tool_calls": []}
    for call in calls:
        if model.startswith("google/"):
            raw_args = dict(call["arguments"], timeout_sec=call["timeout_sec"])
            if call["tool_name"] == "Bash":
                raw_args["shell_mode"] = call["shell_mode"]
            function = {"name": call["tool_name"], "arguments": json.dumps(raw_args)}
        else:
            function = {"name": "call_local_tool", "arguments": json.dumps({
                key: value for key, value in call.items() if key != "provider_call_id"})}
        original["tool_calls"].append({"id": call["provider_call_id"], "type": "function", "function": function})
        tool_id = str(uuid.uuid4())
        name, args, mode = expected_runtime_call(call, NORMALIZE)
        timeout = 30 if name == "shell_run" else 5
        await bind_native_call_tx(
            tx, run=RUN, inference_request_id=REQUEST, provider_call_id=call["provider_call_id"],
            tool_call_id=tool_id, tool_name=name, arguments=args, shell_mode=mode, timeout_sec=timeout,
            normalize=NORMALIZE,
        )
        payload = ({"ok": True, "matches": [{"path": "notes.txt", "line": 17, "text": "error detail"}],
                    "truncated": False} if name == "fs_search" else {
                        "ok": True, "requested_command": args["command"], "effective_command": args["command"],
                        "execution_cwd": RUN["workspace_root"], "exit_code": 0,
                    })
        tool = {"payload_hash": tool_payload_hash(name, args, mode), "status": "succeeded",
                "finished_at": datetime.now(UTC), "commit_state": "not_applicable",
                "request_payload_json": {"tool_name": name, "arguments": args, "shell_mode": mode, "timeout_sec": timeout},
                "result_payload": payload, "stdout": "exact output\nwith tabs\tand unicode \u00e9\n", "stderr": "warning\n",
                "stdout_truncated": False, "stderr_truncated": False, "execution_token": "private-never-export"}
        rehash(tool)
        tx.tools[tool_id] = tool
    return tx, original


async def load(tx, original):
    return await load_native_tool_results_tx(tx, run=RUN, source=tx.source,
                                           request_id=REQUEST, assistant_message=original)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [MODEL, "openai/gpt-4.1-nano"])
@pytest.mark.parametrize("shell_mode", ["read_only", "mutating"])
async def test_all_original_calls_materialized_in_provider_order_losslessly(model, shell_mode):
    tx, original = await scenario(model=model, shell_mode=shell_mode)
    before = copy.deepcopy((tx.rows, tx.tools, original, tx.body))
    tx.rows.reverse()  # Runtime completion order is not provider function order.
    messages = await load(tx, original)
    assert [m["tool_call_id"] for m in messages] == ["call-search", "call-shell"]
    for message, tool in zip(messages, tx.tools.values(), strict=True):
        assert message["role"] == "tool"
        assert json.loads(message["content"]) == {"status": "succeeded", "payload": tool["result_payload"],
                                                "stdout": tool["stdout"], "stderr": tool["stderr"]}
    assert "private-never-export" not in json.dumps(messages)
    assert (list(reversed(tx.rows)), tx.tools, original, tx.body) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_binding", "extra_binding", "duplicate_id", "order", "raw_argument",
                                   "duplicate_json_key", "ordinal", "source_argument", "source_owner", "source_id",
                                   "source_status", "workspace", "session", "payload", "timeout", "mode", "digest",
                                   "cancelled", "proposed", "started", "uncertain", "stdout", "truncated", "no_callback"])
async def test_incomplete_or_changed_generation_never_returns_partial_messages(change):
    tx, original = await scenario()
    tool = next(iter(tx.tools.values()))
    binding = tx.rows[0]["content_json"]
    if change == "missing_binding":
        tx.rows.pop()
    elif change == "extra_binding":
        tx.rows.append(copy.deepcopy(tx.rows[0]))
    elif change == "duplicate_id":
        original["tool_calls"][1]["id"] = original["tool_calls"][0]["id"]
    elif change == "order":
        original["tool_calls"].reverse()
    elif change in {"raw_argument", "duplicate_json_key"}:
        original["tool_calls"][0]["function"]["arguments"] = (
            '{"pattern":"changed"}' if change == "raw_argument" else '{"pattern":"wrong","pattern":"error"}')
    elif change == "ordinal":
        binding["call_ordinal"] = 1
    elif change == "source_argument":
        tx.body["tool_calls"][0]["arguments"]["pattern"] = "changed"
    elif change == "source_owner":
        tx.source["user_id"] = str(uuid.uuid4())
    elif change == "source_id":
        tx.source["id"] = str(uuid.uuid4())
    elif change == "source_status":
        tx.source["status"] = "failed"
    elif change == "workspace":
        binding["workspace"]["workspace_root"] = "/other"
    elif change == "session":
        binding["runtime_session_id"] = str(uuid.uuid4())
    elif change == "payload":
        tool["request_payload_json"]["arguments"]["pattern"] = "changed"
    elif change == "timeout":
        tool["request_payload_json"]["timeout_sec"] = 1
    elif change == "mode":
        tool["request_payload_json"]["shell_mode"] = "mutating"
    elif change == "digest":
        tool["result_submission_hash"] = "a" * 64
    elif change in {"cancelled", "proposed", "started"}:
        tool["status"] = change
    elif change == "uncertain":
        tool["commit_state"] = "commit_unknown"
    elif change == "stdout":
        tool["stdout"] += "changed"
    elif change == "truncated":
        tool["stdout_truncated"] = True
    elif change == "no_callback":
        tx.callback = False
    with pytest.raises(ContractError):
        await load(tx, original)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime_timeout", "server_timeout", "background", "missing_exit",
                                    "wrong_cwd", "rewritten_command", "wrong_requested", "wrong_exit"])
async def test_timeout_or_uncertain_shell_completion_is_not_a_native_result(failure):
    tx, original = await scenario()
    tool = list(tx.tools.values())[1]
    result = tool["result_payload"]
    if failure in {"runtime_timeout", "server_timeout"}:
        tool["status"] = "timed_out"
        result.update(ok=False, reason_code="tool_timeout")
        if failure == "server_timeout":
            tx.callback = False
    elif failure == "background":
        result["status"] = "running_in_background"
    elif failure == "missing_exit":
        result.pop("exit_code")
    elif failure == "wrong_cwd":
        result["execution_cwd"] = "/other"
    elif failure == "rewritten_command":
        result["effective_command"] = "different-command"
    elif failure == "wrong_requested":
        result["requested_command"] = "different-command"
    elif failure == "wrong_exit":
        result["exit_code"] = 1
    rehash(tool)
    with pytest.raises(ContractError):
        await load(tx, original)
    if failure == "runtime_timeout":
        diagnostic = await accepted_native_receipts_tx(tx, run=RUN, provider="openrouter", model=MODEL)
        assert diagnostic[1]["result"]["status"] == "timed_out"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["stdout", "stderr", "payload", "payload_key"])
async def test_secrets_in_accepted_callback_are_rejected_not_stripped(location):
    tx, original = await scenario()
    tool = next(iter(tx.tools.values()))
    secret = "sk-or-v1-" + "a" * 64
    if location == "payload":
        tool["result_payload"]["matches"][0]["text"] = secret
    elif location == "payload_key":
        tool["result_payload"][secret] = "value"
    else:
        tool[location] = secret
    rehash(tool)
    before = copy.deepcopy(tool)
    with pytest.raises(ContractError, match="Sensitive"):
        await load(tx, original)
    assert tool == before


@pytest.mark.asyncio
async def test_configured_redaction_policy_rejects_without_altering_output(monkeypatch):
    tx, original = await scenario()
    monkeypatch.setenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "exact output")
    with pytest.raises(ContractError, match="Sensitive"):
        await load(tx, original)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "blocked"])
async def test_actual_accepted_failures_remain_lossless_typed_errors(status):
    tx, original = await scenario()
    tool = next(iter(tx.tools.values()))
    tool["status"] = status
    tool["result_payload"] = {"ok": False, "reason_code": "runtime_policy_denied", "detail": "Denied as requested."}
    rehash(tool)
    messages = await load(tx, original)
    assert json.loads(messages[0]["content"]) == {
        "status": status, "payload": tool["result_payload"], "stdout": tool["stdout"], "stderr": tool["stderr"],
    }


@pytest.mark.asyncio
async def test_stored_timeout_cap_survives_environment_change(monkeypatch):
    tx, original = await scenario()
    monkeypatch.setenv("OPENVEGAS_TOOL_SHELL_TIMEOUT_SEC", "3")
    assert len(await load(tx, original)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["runtime_timeout_sec", "runtime_timeout_cap_sec"])
async def test_stored_timeout_bools_cannot_alias_integer_one(field):
    tx, original = await scenario()
    tx.rows[0]["content_json"][field] = True
    with pytest.raises(ContractError):
        await load(tx, original)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["workspace", "runtime_timeout_cap_sec"])
async def test_new_tools_cannot_downgrade_to_legacy_receipt_without_snapshot(field):
    tx, original = await scenario()
    tx.rows[0]["content_json"].pop(field)
    with pytest.raises(ContractError):
        await load(tx, original)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"state": "completed"}, {"state": "failed"},
                                   {"cancel_requested_at": datetime.now(UTC)}])
async def test_inactive_run_cannot_materialize_a_continuation(change):
    tx, original = await scenario()
    with pytest.raises(ContractError):
        await load_native_tool_results_tx(tx, run=dict(RUN, **change), source=tx.source,
                                         request_id=REQUEST, assistant_message=original)
