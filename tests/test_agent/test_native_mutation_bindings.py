"""Offline native mutation history integration; private service/DB boundaries mocked."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from openvegas.agent import native_history as history
from openvegas.agent import native_mutation_service as service
from openvegas.agent.native_mutation import build_mutation_plan
from openvegas.agent.orchestration_contracts import canonical_json, valid_actions_signature
from openvegas.agent.runtime_contracts import result_submission_hash, tool_payload_hash
from openvegas.contracts.errors import ContractError

USER, RUN, SESSION, REQUEST, TOOL, PREP = [f"{n:08d}-1111-4111-8111-111111111111" for n in range(1, 7)]
MODEL = "openai/gpt-4.1-nano"
RUN_ROW = {"id": RUN, "user_id": USER, "runtime_session_id": SESSION, "state": "running",
           "version": 3, "workspace_root": "/unit-workspace", "workspace_fingerprint": "sha256:" + "a" * 64,
           "git_root": None}
NORMALIZE = lambda *, tool_name, arguments: arguments


class Tx:
    def __init__(self, call):
        self.call = copy.deepcopy(call)
        self.plan = build_mutation_plan(call, {"exists": True, "content_utf8": "PRIVATE_BASELINE\nold"})
        self.prep = {"id": PREP, "tool_call_id": None, "approval_id": None, "original_ordinal": 0,
                     "native_inference_request_id": REQUEST, "native_provider_call_id": "call-write"}
        self.args = service.executable_arguments(PREP, self.plan)
        self.source = {"status": "succeeded", "response_status": 200, "provider_request_id": "gen-1",
                       "response_body_text": {"provider_request_id": "gen-1", "tool_calls": [copy.deepcopy(call)]}}
        self.preauth = {"provider": "openrouter", "model_id": MODEL, "status": "settled"}
        self.binding = None
        self.queries = []
        self.updated = "UPDATE 1"
        self.private_observation = True
        self.callback = True
        self.tool = None
        self.loader_calls = []
        self.lifecycle_calls = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "FROM inference_requests" in query:
            return self.source
        if "FROM inference_preauthorizations" in query:
            return self.preauth
        if "FROM agent_run_tool_calls" in query:
            return self.tool
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        if "count(*)" in query:
            return 0
        if "agent_run_events" in query:
            return "callback" if self.callback else None
        return None

    async def execute(self, query, *args):
        self.queries.append((query, args))
        if query.startswith("UPDATE native_mutation_preparations"):
            assert "tool_call_id IS NULL AND approval_id IS NULL" in query
            assert args == (PREP, TOOL, RUN, USER, SESSION, REQUEST, "call-write", self.plan.contract_sha256)
            if self.updated == "UPDATE 1":
                self.prep["tool_call_id"] = TOOL
            return self.updated
        assert query.startswith("INSERT INTO agent_chat_turns")
        self.binding = json.loads(args[1])
        return "INSERT 0 1"

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        if self.binding is None:
            return []
        return [{"run_id": RUN, "role": "assistant", "content_json": copy.deepcopy(self.binding), "tool_call_id": TOOL}]


@pytest.fixture
def boundary(monkeypatch):
    async def load(tx, **kwargs):
        tx.loader_calls.append(copy.deepcopy(kwargs))
        assert kwargs["run"]["id"] == RUN
        assert kwargs["runtime_session_id"] == SESSION
        if (kwargs["preparation_id"] != PREP or kwargs["native_inference_request_id"] != REQUEST
                or kwargs["native_provider_call_id"] != "call-write"
                or kwargs.get("contract_sha256") not in {None, tx.plan.contract_sha256}):
            history.fail("Private preparation ownership does not match.")
        return dict(tx.prep), tx.plan

    async def validate_observation(tx, *, run, tool, preparation, plan):
        tx.lifecycle_calls.append((run, tool, preparation, plan))
        if not tx.private_observation:
            history.fail("No private accepted observation.")
        proof = tool["result_payload"]["native_mutation_proof"]
        if proof != proof_for(plan):
            history.fail("Private observation disagrees with the accepted callback.")

    monkeypatch.setattr(service, "load_preparation_tx", load)
    # Parent owns the lifecycle module; do not require it to have landed yet.
    module = types.ModuleType("openvegas.agent.native_mutation_lifecycle")
    module.validate_observation_tx = validate_observation
    monkeypatch.setitem(sys.modules, "openvegas.agent.native_mutation_lifecycle", module)


def original(name="Write"):
    args = {"filepath": "notes.txt", "content": "literal new", "write_mode": "replace"}
    if name == "FindAndReplace":
        args = {"filepath": "notes.txt", "old_string": "old", "new_string": "new"}
    elif name == "InsertAtEnd":
        args = {"filepath": "notes.txt", "content": " tail"}
    return {"tool_name": name, "arguments": args, "provider_call_id": "call-write",
            "shell_mode": "mutating", "timeout_sec": 30}


def proof_for(plan):
    return {"kind": "runtime_observed_file_v1", "contract_sha256": plan.contract_sha256,
            "relative_path": plan.relative_path,
            "observed_before": {"exists": True, "sha256": plan.before_sha256, "bytes": plan.before_bytes},
            "observed_after": {"exists": True, "sha256": plan.after_sha256, "bytes": plan.after_bytes},
            "outcome": "no_change" if plan.no_change else "applied", "reason": None}


def request(tx):
    actions = [{"action": "cancel"}, {"action": "handoff"}]
    return {"user_id": USER, "run_id": RUN, "runtime_session_id": SESSION, "actor_role_class": "user",
            "idempotency_key": "mutation-1", "expected_run_version": 3,
            "expected_valid_actions_signature": valid_actions_signature(3, actions), "plan_mode": False,
            "native_inference_request_id": REQUEST, "native_provider_call_id": "call-write",
            "tool_request": {"tool_name": "fs_apply_patch", "arguments": copy.deepcopy(tx.args),
                             "shell_mode": "mutating", "timeout_sec": 5}}


def proposal(tx):
    req = request(tx)
    return {"run_id": RUN, "run_version": 3, "current_state": "running", "projection_version": 1,
            "valid_actions": [{"action": "cancel"}, {"action": "handoff"}],
            "valid_actions_signature": req["expected_valid_actions_signature"],
            "tool_request": {**req["tool_request"], "tool_call_id": TOOL, "execution_token": "c" * 32,
                             "payload_hash": tool_payload_hash("fs_apply_patch", tx.args, "mutating"),
                             "requires_approval": True}}


async def bind(tx, **overrides):
    kwargs = {"run": RUN_ROW, "inference_request_id": REQUEST, "provider_call_id": "call-write", "tool_call_id": TOOL,
              "tool_name": "fs_apply_patch", "arguments": copy.deepcopy(tx.args), "shell_mode": "mutating", "timeout_sec": 5,
              "normalize": NORMALIZE, "proposal_request": request(tx), "proposal_response": proposal(tx)}
    kwargs.update(overrides)
    await history.bind_native_call_tx(tx, **kwargs)


def terminal_tool(tx):
    tool = {"id": TOOL, "run_id": RUN, "tool_name": "fs_apply_patch", "tool_class": "mutating",
            "approval_required": True, "run_version": 3, "execution_token": "c" * 32,
            "status": "succeeded", "commit_state": "committed", "finished_at": datetime.now(UTC),
            "request_payload_json": request(tx)["tool_request"],
            "payload_hash": tool_payload_hash("fs_apply_patch", tx.args, "mutating"),
            "result_payload": {"native_mutation_proof": proof_for(tx.plan)}, "stdout": "", "stderr": "",
            "stdout_truncated": False, "stderr_truncated": False}
    rehash(tool)
    return tool


def rehash(tool):
    for key in ("stdout", "stderr"):
        tool[key + "_sha256"] = hashlib.sha256(tool[key].encode()).hexdigest()
    tool["result_submission_hash"] = result_submission_hash(
        result_status=tool["status"], result_payload=tool["result_payload"],
        stdout_sha256=tool["stdout_sha256"], stderr_sha256=tool["stderr_sha256"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Write", "FindAndReplace", "InsertAtEnd"])
async def test_bind_original_plan_then_private_observation_receipt(boundary, name):
    tx = Tx(original(name))
    await bind(tx)
    assert tx.binding["normalizer_version"] == 2
    assert tx.binding["native_mutation_preparation_id"] == PREP
    assert tx.binding["native_mutation_contract_sha256"] == tx.plan.contract_sha256
    assert tx.binding["native_call"] == original(name)
    assert tx.prep["tool_call_id"] == TOOL
    assert "PRIVATE_BASELINE" not in canonical_json(tx.binding["native_call"])
    tx.tool = terminal_tool(tx)
    receipts = await history.accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL, for_continuation=True)
    assert receipts[0]["call"] == original(name)
    assert receipts[0]["result"]["payload"] == tx.tool["result_payload"]
    assert "plan_json" not in canonical_json(receipts)
    assert "observed_source" not in canonical_json(receipts)
    assert len(tx.lifecycle_calls) == 1
    assert tx.loader_calls[-1]["require_unexpired"] is False
    assert tx.loader_calls[-1]["require_latest"] is False


@pytest.mark.asyncio
async def test_exact_proposal_replay_pending_commit_not_execution(boundary):
    tx = Tx(original())
    expected = proposal(tx)
    await bind(tx)
    tx.tool = terminal_tool(tx)
    tx.tool.update(status="proposed", commit_state="pending_commit")
    recovered = await history.replay_native_proposal_tx(tx, run=RUN_ROW, idempotency_key="mutation-1", request=request(tx), normalize=NORMALIZE)
    assert recovered == expected
    assert tx.loader_calls[-1]["require_unexpired"] is True
    assert tx.loader_calls[-1]["require_latest"] is True
    assert len([q for q, _ in tx.queries if q.startswith("UPDATE")]) == 1
    tx.tool["commit_state"] = "not_applicable"
    with pytest.raises(ContractError):
        await history.replay_native_proposal_tx(tx, run=RUN_ROW, idempotency_key="mutation-1", request=request(tx), normalize=NORMALIZE)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["marker", "patch", "mode", "timeout", "call", "guard", "ordinal"])
async def test_bad_executable_or_original_or_link_refuses_binding(boundary, bad):
    tx = Tx(original())
    kw = {}
    if bad == "marker":
        tx.args["native_mutation"]["preparation_id"] = USER
    elif bad == "patch":
        tx.args["patch"] += "unapproved"
    elif bad == "mode":
        kw["shell_mode"] = "read_only"
    elif bad == "timeout":
        kw["timeout_sec"] = 1
    elif bad == "call":
        tx.source["response_body_text"]["tool_calls"][0]["arguments"]["content"] = "altered"
    elif bad == "guard":
        tx.updated = "UPDATE 0"
    else:
        tx.prep["original_ordinal"] = 1
    with pytest.raises(ContractError):
        await bind(tx, **kw)
    assert tx.binding is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["pending_commit", "not_applicable", "commit_failed", "commit_unknown", "failed", "blocked", "timed_out", "private_missing", "proof", "extra", "stdout", "callback", "linked_tool", "normalizer", "digest"])
async def test_mutation_success_requires_committed_private_matching_observation(boundary, bad):
    tx = Tx(original())
    await bind(tx)
    tx.tool = terminal_tool(tx)
    if bad in {"pending_commit", "not_applicable", "commit_failed", "commit_unknown"}:
        tx.tool["commit_state"] = bad
    elif bad in {"failed", "blocked", "timed_out"}:
        tx.tool["status"] = bad
    elif bad == "private_missing":
        tx.private_observation = False
    elif bad == "proof":
        tx.tool["result_payload"]["native_mutation_proof"]["observed_after"]["sha256"] = "a" * 64
    elif bad == "extra":
        tx.tool["result_payload"]["source"] = "PRIVATE"
    elif bad == "stdout":
        tx.tool["stdout"] = "unreviewed text"
    elif bad == "callback":
        tx.callback = False
    elif bad == "linked_tool":
        tx.prep["tool_call_id"] = USER
    elif bad == "normalizer":
        tx.binding["normalizer_version"] = 1
    else:
        tx.binding["native_mutation_contract_sha256"] = "a" * 64
    # Probe beyond accidental corruption checks, not merely the binding checksum.
    tx.binding["proposal_replay_sha256"] = history._binding_digest(tx.binding)
    rehash(tx.tool)
    with pytest.raises(ContractError):
        await history.accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL, for_continuation=True)


@pytest.mark.asyncio
async def test_direct_wrapper_has_no_preparation_loader_or_queries(monkeypatch):
    loader = AsyncMock(side_effect=AssertionError("legacy queried mutation storage"))
    monkeypatch.setattr(service, "load_preparation_tx", loader)
    call = {"tool_name": "Read", "arguments": {"path": "notes.txt"}, "shell_mode": "read_only"}
    result = await history.expected_runtime_call_tx(None, run=RUN_ROW, call=call, normalize=NORMALIZE,
                                                   inference_request_id=REQUEST, provider_call_id="read")
    assert result == ("fs_read", {"path": "notes.txt"}, "read_only", None, None)
    loader.assert_not_awaited()
    call["arguments"]["native_mutation"] = {"preparation_id": PREP}
    with pytest.raises(ContractError):
        await history.expected_runtime_call_tx(None, run=RUN_ROW, call=call, normalize=NORMALIZE,
                                              inference_request_id=REQUEST, provider_call_id="read")
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_gate_blocks_before_storage(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_NATIVE_MUTATIONS", raising=False)
    class NoQueries:
        def __getattr__(self, name):
            raise AssertionError("default-off storage access")
    with pytest.raises(ContractError, match="disabled"):
        await history.expected_runtime_call_tx(NoQueries(), run=RUN_ROW, call=original(), normalize=NORMALIZE,
                                              inference_request_id=REQUEST, provider_call_id="call-write", preparation_id=PREP)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["marker", "patch", "linked", "version", "started"])
async def test_replay_rejects_substituted_preparation_and_execution(boundary, bad):
    tx = Tx(original())
    await bind(tx)
    tx.tool = terminal_tool(tx)
    tx.tool.update(status="proposed", commit_state="pending_commit")
    req = request(tx)
    if bad == "marker":
        req["tool_request"]["arguments"]["native_mutation"]["preparation_id"] = USER
    elif bad == "patch":
        req["tool_request"]["arguments"]["patch"] += "different"
    elif bad == "linked":
        tx.prep["tool_call_id"] = USER
    elif bad == "version":
        tx.binding["normalizer_version"] = 1
        tx.binding["proposal_replay_sha256"] = history._binding_digest(tx.binding)
    else:
        tx.tool["status"] = "started"
    with pytest.raises(ContractError):
        await history.replay_native_proposal_tx(tx, run=RUN_ROW, idempotency_key="mutation-1", request=req, normalize=NORMALIZE)


@pytest.mark.asyncio
async def test_direct_binding_and_receipt_never_query_mutation_storage(monkeypatch):
    call = {"tool_name": "Read", "arguments": {"path": "notes.txt"}, "shell_mode": "read_only", "timeout_sec": 30, "provider_call_id": "call-write"}
    tx = Tx(original())
    tx.source["response_body_text"]["tool_calls"] = [call]
    loader = AsyncMock(side_effect=AssertionError("direct tools must not load preparations"))
    monkeypatch.setattr(service, "load_preparation_tx", loader)
    await history.bind_native_call_tx(tx, run=RUN_ROW, inference_request_id=REQUEST, provider_call_id="call-write",
        tool_call_id=TOOL, tool_name="fs_read", arguments={"path": "notes.txt"}, shell_mode="read_only", timeout_sec=5, normalize=NORMALIZE)
    tx.tool = terminal_tool(tx)
    req = {"tool_name": "fs_read", "arguments": {"path": "notes.txt"}, "shell_mode": "read_only", "timeout_sec": 5}
    tx.tool.update(request_payload_json=req, payload_hash=tool_payload_hash("fs_read", req["arguments"], "read_only"),
                   result_payload={"content": "text"}, commit_state="not_applicable")
    rehash(tx.tool)
    rows = await history.accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL, for_continuation=True)
    assert rows[0]["call"] == call
    assert tx.binding["normalizer_version"] == 1
    loader.assert_not_awaited()
    assert not any("native_mutation" in query for query, _ in tx.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_real_parent_private_observation_verifier_is_invoked(boundary, monkeypatch, corrupt):
    # Restore the actual parent-owned module for this check (not the boundary stub).
    import importlib
    monkeypatch.delitem(sys.modules, "openvegas.agent.native_mutation_lifecycle")
    module = importlib.import_module("openvegas.agent.native_mutation_lifecycle")
    assert callable(module.validate_observation_tx)
    tx = Tx(original())
    await bind(tx)
    tx.tool = terminal_tool(tx)
    original_fetchrow = tx.fetchrow
    stored = {"preparation_id": PREP, "result_submission_sha256": tx.tool["result_submission_hash"],
              "proof_json": canonical_json(proof_for(tx.plan))}
    if corrupt:
        stored["result_submission_sha256"] = "0" * 64
    async def fetchrow(query, *args):
        if "native_mutation_observations" in query:
            assert args == (TOOL,)
            return stored
        return await original_fetchrow(query, *args)
    tx.fetchrow = fetchrow
    if corrupt:
        with pytest.raises(ContractError):
            await history.accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL, for_continuation=True)
    else:
        rows = await history.accepted_native_receipts_tx(tx, run=RUN_ROW, provider="openrouter", model=MODEL, for_continuation=True)
        assert rows[0]["result"]["status"] == "succeeded"
