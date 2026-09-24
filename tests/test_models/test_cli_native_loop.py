"""Run the real nested CLI tool loop; mock only transport, execution and UI.

No provider, database, shell command, microphone, or external network is used.
The AST wrapper preserves nonlocal cells and does not rewrite the loop body.
Source overrides are for frozen, trusted checkout snapshots in negative controls.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _forbid_io(*_args, **_kwargs):
    raise AssertionError("Native loop regression attempted real network/process execution")


def _session_type():
    source = os.getenv("CLI_NATIVE_SESSION_SOURCE")
    if not source:
        from openvegas.agent.native_scope_client import NativeGenerationSession

        return NativeGenerationSession
    name = "_openvegas_frozen_native_scope_review"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.NativeGenerationSession


def tool(name, arguments, *, mode="read_only"):
    return {"tool_name": name, "arguments": arguments, "shell_mode": mode, "timeout_sec": 30}


READ_LIST = [tool("Read", {"path": "a.txt"}), tool("List", {"path": "."})]


class LoopDriver:
    def __init__(self, cli, *, tmp_path, batches, conflict_call=None, deny_approval=False,
                 fresh_decision=False, start_conflict_call=None, callback_failure_call=None,
                 reuse_provider_call_id=False):
        self.cli, self.root = cli, tmp_path
        self.batches = list(batches)
        self.requests, self.proposals, self.starts, self.callbacks = [], [], [], []
        self.executions, self.approvals, self.rendered, self.notes, self.events = [], [], [], [], []
        self.conflict_call, self.conflicts_remaining = conflict_call, 4
        self.start_conflict_call, self.start_conflicts_remaining = start_conflict_call, 4
        self.callback_failure_call = callback_failure_call
        self.start_attempts, self.callback_attempts, self.confirmations, self.created_runs = [], [], [], []
        self.deny_approval = deny_approval
        self.fresh_decision = fresh_decision
        self.reuse_provider_call_id = reuse_provider_call_id
        self.runtime_keys, self.receipts = {}, []
        self.pending, self.accepted = {}, set()
        self.run_id, self.runtime_id = str(uuid4()), str(uuid4())
        self.client = SimpleNamespace(
            ask=self.ask, ide_get_context=AsyncMock(return_value=None),
            agent_tool_propose=self.propose, agent_tool_start=self.start,
            agent_tool_result=self.callback, agent_tool_cancel=AsyncMock(return_value={}),
            agent_run_get=AsyncMock(return_value={
                "current_state": "running", "valid_actions": [{"action": "handoff"}],
            }),
            agent_run_create=self.create_run, agent_register_workspace=AsyncMock(return_value={
                "run_version": 1, "valid_actions_signature": "sha256:" + "a" * 64,
            }),
        )
        self._compile()

    async def create_run(self, **_kwargs):
        self.run_id = str(uuid4())
        self.created_runs.append(self.run_id)
        return {"run_id": self.run_id, "run_version": 1, "valid_actions_signature": "sha256:" + "a" * 64}

    async def ask(self, prompt, provider, model, **kwargs):
        request = deepcopy(dict(kwargs, prompt=prompt, provider=provider, model=model))
        self.requests.append(request)
        self.events.append(("ask", len(self.requests)))
        if kwargs.get("native_continuation"):
            assert not self.pending, "Continuation sent before every original callback was accepted"
        assert self.batches, "Unexpected extra inference/finalizer request"
        batch = self.batches.pop(0)
        request_id = str(uuid4())
        scope = kwargs["native_scope"]
        previous = kwargs.get("native_continuation")
        revision = previous["expected_history_revision"] + 1 if previous else 0
        calls = [dict(deepcopy(call), provider_call_id=("reused-call" if self.reuse_provider_call_id
                                                     else f"call-{len(self.requests)}-{index}"),
                      native_inference_request_id=request_id) for index, call in enumerate(batch)]
        for call in calls:
            provider_id = call["provider_call_id"]
            runtime_key = f"{request_id}:{provider_id}" if self.reuse_provider_call_id else provider_id
            self.runtime_keys[request_id, provider_id] = runtime_key
            self.pending[runtime_key] = call
        result = {
            "status": "ok", "text": "Tool preamble must not be rendered as final." if calls else "FINAL ANSWER",
            "completion_status": "incomplete" if calls else "complete", "v_cost": "0.001",
            "tool_calls": calls, "native_generation": {
                "scope_version": 1, "run_id": scope["run_id"],
                "runtime_session_id": scope["runtime_session_id"], "original_turn_scope_verified": True,
                "inference_request_id": request_id, "continuation_supported": bool(calls),
                "history_revision": revision,
            },
        }
        self.receipts.append(deepcopy(result["native_generation"]))
        return result

    async def propose(self, **kwargs):
        from openvegas.client import APIError

        self.proposals.append(deepcopy(kwargs))
        generation = kwargs["native_inference_request_id"]
        identity = self.runtime_keys[generation, kwargs["native_provider_call_id"]]
        self.events.append(("propose", identity))
        assert identity not in self.accepted, "Already accepted native call was proposed again"
        assert identity in self.pending, "Proposal lost its original provider call identity"
        if identity == self.conflict_call and self.conflicts_remaining:
            self.conflicts_remaining -= 1
            raise APIError(409, "fixture busy", data={"error": "active_mutation_in_progress"})
        request = {key: deepcopy(kwargs[key]) for key in ("tool_name", "arguments", "shell_mode", "timeout_sec")}
        token = generation.replace("-", "") if self.reuse_provider_call_id else "a" * 32
        request.update(tool_call_id=identity, execution_token=token)
        return {"tool_request": request}

    async def start(self, **kwargs):
        from openvegas.client import APIError

        identity = kwargs["tool_call_id"]
        self.start_attempts.append(deepcopy(kwargs))
        if identity == self.start_conflict_call and self.start_conflicts_remaining:
            self.start_conflicts_remaining -= 1
            raise APIError(409, "fixture busy", data={"error": "active_mutation_in_progress"})
        assert identity not in {item["tool_call_id"] for item in self.starts}, "Native tool started twice"
        self.starts.append(deepcopy(kwargs))
        self.events.append(("start", identity))
        return {}

    def execute(self, *, tool_name, **kwargs):
        self.executions.append(dict(kwargs, tool_name=tool_name))
        self.events.append(("execute", tool_name))
        return self.cli.ToolExecutionResult("succeeded", {"ok": True}, "fixture output", "")

    async def execute_shell(self, **kwargs):
        return self.execute(tool_name="shell_run", **kwargs)

    async def callback(self, **kwargs):
        from openvegas.client import APIError

        identity = kwargs["tool_call_id"]
        self.callback_attempts.append(deepcopy(kwargs))
        if identity == self.callback_failure_call:
            raise APIError(409, "fixture callback unconfirmed", data={"error": "active_mutation_in_progress"})
        assert identity in self.pending, "Duplicate or unowned result callback"
        assert kwargs["result_status"] == "succeeded"
        self.callbacks.append(deepcopy(kwargs))
        self.events.append(("callback", identity))
        self.pending.pop(identity)
        self.accepted.add(identity)
        return {}

    def choose_approval(self, **kwargs):
        self.approvals.append(deepcopy(kwargs))
        self.events.append(("approval", kwargs["tool_name"]))
        return (self.cli.ApprovalDecision.DENY_AND_REPLAN if self.deny_approval
                else self.cli.ApprovalDecision.ALLOW_ONCE)

    def confirm(self, question, *, default, **_kwargs):
        self.confirmations.append({"question": question, "default": default})
        return self.fresh_decision

    def _compile(self):
        from openvegas.client import APIError
        from openvegas.tui.approval_menu import SessionApprovalState

        source = Path(os.getenv("CLI_NATIVE_LOOP_SOURCE", str(ROOT / "openvegas/cli.py")))
        tree = ast.parse(source.read_text())
        names = {"_run_tool_loop", "_update_fence", "_create_and_register_runtime_run", "_ensure_runtime_run"}
        optional = {"_reset_native_task", "_stage_handoff_destination"}
        nodes = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names | optional}
        assert names <= nodes.keys()
        initial = {
            "current_provider": "openrouter", "current_model": "google/fixture-model",
            "current_thread_id": None, "current_run_id": self.run_id, "runtime_session_id": self.runtime_id,
            "current_run_version": 1, "current_signature": "sha256:" + "a" * 64,
            "current_reasoning_effort": None, "last_successful_tool": None, "context_warning_emitted": False,
            "web_search_requested": False, "last_web_search_effective": False, "last_web_search_used": False,
            "last_web_search_retry_without_tool": False, "last_assistant_text_for_turn": "",
            "runtime_run_task": None, "native_generation_session": _session_type()(),
            "pending_native_handoff": None,
            "APIError": APIError,
        }
        # Nested helpers declare cells in their enclosing function, not chat().
        # Inspect each extracted function's own scope without descending into
        # nested function definitions; the compiled production AST is unchanged.
        cells = set()

        class OuterCells(ast.NodeVisitor):
            def visit_Nonlocal(self, node):
                cells.update(node.names)

            def visit_FunctionDef(self, node):
                return

            visit_AsyncFunctionDef = visit_FunctionDef

        for node in nodes.values():
            for statement in node.body:
                OuterCells().visit(statement)
        assert cells <= initial.keys(), "Add real outer state to the harness, not a production-path stub"
        # Read-only outer values are closure cells too (provider, model, APIError).
        wrapper = ast.parse("def factory():\n" + "".join(f"    {name} = initial[{name!r}]\n" for name in sorted(initial))
                            + "    return _run_tool_loop\n")
        # Keep original AST and source line numbers, including every nested branch.
        factory = wrapper.body[0]
        factory.body[-1:-1] = list(nodes.values())
        ast.fix_missing_locations(wrapper)
        no_op = lambda *_a, **_k: None
        self.namespace = {
            **vars(self.cli), **initial, "initial": initial, "client": self.client,
            "NativeGenerationSession": _session_type(), "Confirm": SimpleNamespace(ask=self.confirm),
            "workspace_root": str(self.root), "workspace_git_root": str(self.root), "workspace_fp": "fixture",
            "approval_mode": "ask", "plan_mode": False, "conversation_mode": "ephemeral",
            "pending_attachments": [], "attachment_file_ids_for_turn": [], "attachment_context_for_turn": "",
            "voice_transcript_context_for_turn": "", "verbose_tool_events": False, "show_stream_status": False,
            "show_web_diagnostics": False, "show_token_usage": False, "session_approval": SessionApprovalState(),
            "console": SimpleNamespace(print=lambda *args, **_kw: self.notes.append(str(args))),
            "_validate_openrouter_request": AsyncMock(), "_env_flag": lambda name, default: os.getenv(name, default) == "1",
            "_chat_capability": lambda _name: True, "_status_actor": lambda: "openvegas",
            "_tool_protocol_prompt": lambda message, *_a, **_kw: message,
            "_tool_debug": no_op, "emit_metric": no_op, "_render_usage_summary": no_op,
            "render_assistant": lambda _console, text: self.rendered.append(text),
            "render_status_bar": no_op, "render_tool_event": no_op, "render_tool_result": no_op,
            "execute_tool_request": self.execute, "execute_shell_run_streaming": self.execute_shell,
            "_mutation_retry_backoff_sec": lambda *_a: 0, "_chat_drain_stdin": no_op,
            "choose_approval": self.choose_approval, "_chat_modal": AsyncMock(side_effect=lambda callback: callback()),
            "dealer_panel": SimpleNamespace(render=no_op),
            "emote_bridge": SimpleNamespace(current_turn="fixture-turn", finish=no_op, pause=no_op, resume=no_op),
        }
        # Only trusted repository AST is compiled; the production loop remains intact.
        exec(compile(wrapper, str(source), "exec"), self.namespace)  # noqa: S102
        self.loop = self.namespace["factory"]()

    async def run(self, message="Read a.txt and list files", **loop_options):
        return await asyncio.wait_for(self.loop(self.client, message, **loop_options), timeout=3)

    def outer_cell(self, name):
        return dict(zip(self.loop.__code__.co_freevars, self.loop.__closure__))[name]


@pytest.fixture
def loop_driver(monkeypatch, tmp_path):
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, _forbid_io)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, _forbid_io)
    monkeypatch.setattr(subprocess, "Popen", _forbid_io)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbid_io)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbid_io)
    empty = tmp_path / "empty.env"
    empty.touch()
    monkeypatch.setenv("OPENVEGAS_ENV_FILE", str(empty))
    monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "1")
    monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY", "1")
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", "0")
    monkeypatch.setenv("OPENVEGAS_CHAT_MAX_TOOL_STEPS", "4")
    from openvegas import cli

    return lambda batches=None, **kwargs: LoopDriver(
        cli, tmp_path=tmp_path, batches=[READ_LIST, []] if batches is None else batches, **kwargs,
    )


@pytest.mark.asyncio
async def test_two_original_calls_two_callbacks_and_one_final_answer(loop_driver):
    driver = loop_driver()
    assert await driver.run() is True
    assert [call["tool_name"] for call in driver.executions] == ["fs_read", "fs_list"]
    assert [call["tool_call_id"] for call in driver.starts] == ["call-1-0", "call-1-1"]
    assert [call["tool_call_id"] for call in driver.callbacks] == ["call-1-0", "call-1-1"]
    assert len(driver.requests) == 2 and driver.requests[1]["native_continuation"]["expected_history_revision"] == 0
    assert driver.events.index(("callback", "call-1-1")) < driver.events.index(("ask", 2))
    assert driver.rendered == ["FINAL ANSWER"] and not driver.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["read_only", "mutating"])
async def test_arbitrary_native_bash_requires_approval_regardless_of_declared_mode(loop_driver, mode):
    driver = loop_driver(batches=[[tool("Bash", {"command": "printf x > review-marker.txt"}, mode=mode)], []])
    assert await driver.run() is True
    assert len(driver.approvals) == 1
    assert driver.events.index(("approval", "shell_run")) < driver.events.index(("start", "call-1-0"))
    assert [item["tool_name"] for item in driver.executions] == ["shell_run"]
    assert driver.proposals[0]["shell_mode"] == mode
    assert not (driver.root / "review-marker.txt").exists()


@pytest.mark.asyncio
async def test_denied_native_bash_never_proposes_executes_or_sends_replacement(loop_driver):
    from openvegas.client import APIError

    driver = loop_driver(batches=[[tool("Bash", {"command": "printf x > review-marker.txt"})], []], deny_approval=True)
    with pytest.raises(APIError, match="declined"):
        await driver.run()
    assert len(driver.approvals) == 1 and len(driver.requests) == 1
    assert not driver.proposals and not driver.executions and not driver.callbacks and not driver.rendered


@pytest.mark.asyncio
async def test_new_user_task_after_final_does_not_inherit_completed_generation(loop_driver):
    driver = loop_driver(batches=[READ_LIST, [], []])
    assert await driver.run() is True
    assert await driver.run("Read b.txt instead") is True
    first, fresh = driver.requests[0], driver.requests[-1]
    assert "native_continuation" not in fresh
    assert fresh["native_scope"]["run_id"] != first["native_scope"]["run_id"]
    assert fresh["prompt"] == "Read b.txt instead"
    assert driver.created_runs == [fresh["native_scope"]["run_id"]]
    driver.client.agent_register_workspace.assert_awaited_once()
    assert not driver.confirmations
    assert driver.receipts[-1]["history_revision"] == 0
    assert any("previous tool state is not transferred" in note for note in driver.notes)
    assert driver.rendered == ["FINAL ANSWER", "FINAL ANSWER"]


@pytest.mark.asyncio
async def test_new_prompt_after_iteration_cap_never_resumes_the_previous_task(loop_driver):
    from openvegas.client import APIError

    driver = loop_driver(batches=[READ_LIST] * 4 + [[]])
    assert await driver.run() is False
    assert len(driver.requests) == 4 and len(driver.callbacks) == 8
    assert not driver.rendered
    assert any("No finalizer or automatic retry" in note for note in driver.notes)
    with pytest.raises(APIError, match="retained"):
        await driver.run("Read b.txt instead")
    assert len(driver.requests) == 4 and not driver.created_runs


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_call", ["call-1-0", "call-1-1"])
async def test_native_conflict_preserves_original_batch_without_replaying_accepted_calls(loop_driver, conflict_call):
    driver = loop_driver(conflict_call=conflict_call)
    assert await driver.run() is True
    assert len(driver.requests) == 2 and driver.rendered == ["FINAL ANSWER"]
    assert [item["tool_name"] for item in driver.executions] == ["fs_read", "fs_list"]
    assert [item["tool_call_id"] for item in driver.callbacks] == ["call-1-0", "call-1-1"]
    assert [item["tool_call_id"] for item in driver.starts] == ["call-1-0", "call-1-1"]
    assert not driver.pending and driver.conflicts_remaining == 0
    attempts = [item for item in driver.proposals if item["native_provider_call_id"] == conflict_call]
    assert len(attempts) == 5 and len({item["idempotency_key"] for item in attempts}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [False, True])
async def test_unfinished_task_requires_explicit_fresh_decision_and_never_implicit_resume(loop_driver, decision):
    from openvegas.client import APIError

    driver = loop_driver(batches=[READ_LIST] * 4 + [[]], fresh_decision=decision)
    assert await driver.run() is False
    assert len(driver.requests) == 4 and len(driver.callbacks) == 8 and not driver.rendered
    assert any("paused" in note.lower() or "stopped" in note.lower() for note in driver.notes)
    if decision:
        assert await driver.run("Read b.txt instead") is True
        fresh = driver.requests[4]
        assert "native_continuation" not in fresh
        assert fresh["native_scope"]["run_id"] != driver.requests[0]["native_scope"]["run_id"]
        assert driver.created_runs == [fresh["native_scope"]["run_id"]]
        driver.client.agent_register_workspace.assert_awaited_once()
        assert driver.receipts[-1]["history_revision"] == 0
        assert driver.rendered == ["FINAL ANSWER"]
    else:
        with pytest.raises(APIError, match="retained|No new request"):
            await driver.run("Read b.txt instead")
        assert len(driver.requests) == 4 and not driver.created_runs
        driver.client.agent_register_workspace.assert_not_awaited()
    assert len(driver.confirmations) == 1 and driver.confirmations[0]["default"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["call-1-0", "call-1-1"])
async def test_start_conflict_keeps_proposal_identity_and_remaining_batch(loop_driver, identity):
    driver = loop_driver(start_conflict_call=identity)
    assert await driver.run() is True
    assert [item["native_provider_call_id"] for item in driver.proposals] == ["call-1-0", "call-1-1"]
    assert [item["tool_call_id"] for item in driver.callbacks] == ["call-1-0", "call-1-1"]
    assert [item["tool_name"] for item in driver.executions] == ["fs_read", "fs_list"]
    retries = [item for item in driver.start_attempts if item["tool_call_id"] == identity]
    assert len(retries) == 5 and len({item["idempotency_key"] for item in retries}) == 1
    assert len({item["execution_token"] for item in retries}) == 1
    assert driver.rendered == ["FINAL ANSWER"] and len(driver.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["call-1-0", "call-1-1"])
async def test_unconfirmed_result_callback_never_reexecutes_or_requests_continuation(loop_driver, identity):
    from openvegas.client import APIError

    driver = loop_driver(callback_failure_call=identity)
    with pytest.raises(APIError):
        await driver.run()
    expected = ["fs_read"] if identity == "call-1-0" else ["fs_read", "fs_list"]
    assert [item["tool_name"] for item in driver.executions] == expected
    assert len(driver.requests) == 1 and not driver.rendered
    attempts = [item for item in driver.callback_attempts if item["tool_call_id"] == identity]
    assert len(attempts) == 4
    assert all(item == attempts[0] for item in attempts)


@pytest.mark.asyncio
async def test_reused_provider_call_id_in_new_generation_gets_new_proposal_and_token(loop_driver):
    driver = loop_driver(batches=[[READ_LIST[0]], [READ_LIST[0]], []], reuse_provider_call_id=True)
    assert await driver.run() is True
    assert [item["native_provider_call_id"] for item in driver.proposals] == ["reused-call"] * 2
    assert len({item["native_inference_request_id"] for item in driver.proposals}) == 2
    assert len({item["idempotency_key"] for item in driver.proposals}) == 2
    assert len({item["execution_token"] for item in driver.starts}) == 2
    assert len({item["idempotency_key"] for item in driver.starts}) == 2
    assert [item["tool_name"] for item in driver.executions] == ["fs_read", "fs_read"]
    assert len(driver.callbacks) == 2 and not driver.pending
    assert [receipt["history_revision"] for receipt in driver.receipts] == [0, 1, 2]
    assert driver.rendered == ["FINAL ANSWER"] and len(driver.requests) == 3


@pytest.mark.asyncio
async def test_plain_prompt_uses_owned_native_tool_path_not_legacy_one_shot(loop_driver):
    driver = loop_driver(batches=[[]])
    assert await driver.run("Hello") is True
    assert len(driver.requests) == 1 and driver.rendered == ["FINAL ANSWER"]
    request = driver.requests[0]
    assert request["native_history"] is True and request["enable_tools"] is True
    assert request["native_scope"]["run_id"] == driver.run_id
    assert "native_continuation" not in request
    assert driver.outer_cell("native_generation_session").cell_contents.finalized


@pytest.mark.asyncio
async def test_native_bash_excluded_by_policy_fails_closed_without_replacement(loop_driver):
    from openvegas.client import APIError

    driver = loop_driver(batches=[[tool("Bash", {"command": "printf x > review-marker.txt"})], []])
    driver.namespace["approval_mode"] = "exclude"
    with pytest.raises(APIError, match="excluded"):
        await driver.run()
    assert len(driver.requests) == 1
    assert not driver.approvals and not driver.proposals and not driver.executions and not driver.rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["provider", "canonical", "history_flag"])
async def test_active_native_history_cannot_silently_downgrade(loop_driver, monkeypatch, change):
    from openvegas.client import APIError

    driver = loop_driver(batches=[[], []])
    assert await driver.run() is True
    if change == "provider":
        driver.outer_cell("current_provider").cell_contents = "openai"
    elif change == "canonical":
        driver.client._canonical_chat = {"revision": 1}
    else:
        monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY", "0")
    with pytest.raises(APIError, match="cannot switch"):
        await driver.run("Read b.txt instead")
    assert len(driver.requests) == 1 and not driver.created_runs
    assert driver.rendered == ["FINAL ANSWER"]


@pytest.mark.asyncio
async def test_failed_fresh_registration_restores_previous_session_and_fence(loop_driver):
    from openvegas.client import APIError

    driver = loop_driver(batches=[[], []])
    assert await driver.run() is True
    names = ("native_generation_session", "current_run_id", "current_run_version", "current_signature")
    previous = {name: driver.outer_cell(name).cell_contents for name in names}
    driver.client.agent_register_workspace.side_effect = APIError(503, "fixture registration failure")
    with pytest.raises(APIError, match="Could not register"):
        await driver.run("Read b.txt instead")
    assert {name: driver.outer_cell(name).cell_contents for name in names} == previous
    assert driver.outer_cell("native_generation_session").cell_contents is previous["native_generation_session"]
    assert len(driver.requests) == 1 and driver.rendered == ["FINAL ANSWER"]
    assert len(driver.created_runs) == 1
    driver.client.agent_register_workspace.assert_awaited_once()
