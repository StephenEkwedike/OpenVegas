"""Execute trusted CLI AST closures and command branches, not copied switch logic.

Only UI/authenticated transport are fakes. No CLI import, native app, HTTP,
provider, filesystem mutation or database is required.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.client import APIError
from openvegas.contracts.native_handoff import NativeHandoffResponse
from openvegas.tui.model_picker import (
    ModelSelectionError,
    ReviewedModelCapabilities,
    format_options,
    model_options,
    plan_switch,
    reviewed_capabilities,
    validate_selection,
)

SOURCE = Path(__file__).resolve().parents[2] / "openvegas/cli.py"
OLD, NEW = "fixture/source-v1", "fixture/destination-v1"


def descriptor(model=NEW):
    return {"provider": "openrouter", "model_id": model, "max_tokens": 512,
            "enabled": True, "available": True, "capabilities": {
                "reviewed": True, "text": True, "tools": True, "file_upload": True,
                "image_input": True, "web_search": False, "stream_events": True,
                "streaming_mode": "buffered", "reasoning_controls": False, "reasoning_efforts": [],
            }}


def result(scope):
    return {"status": "ok", "text": "Finished", "completion_status": "complete", "tool_calls": [],
        "native_generation": {"scope_version": 1, "run_id": scope["run_id"],
            "runtime_session_id": scope["runtime_session_id"], "original_turn_scope_verified": True,
            "inference_request_id": str(uuid.uuid4()), "continuation_supported": False, "history_revision": 0}}


class Shell:
    def __init__(self):
        self.scope = {"run_id": str(uuid.uuid4()), "runtime_session_id": str(uuid.uuid4()),
                      "expected_run_version": 2, "expected_valid_actions_signature": "sha256:" + "a" * 64}
        self.destination_id = str(uuid.uuid4())
        self.target = descriptor()
        self.source_session = NativeGenerationSession()
        self.source_session.prepare(key="source", scope=self.scope, options={
            "provider": "openrouter", "model": OLD, "enable_tools": True,
            "enable_web_search": False, "attachments": [], "reasoning_effort": None,
        }, history=True, user_text="Original source task")
        self.source_session.validate_result(result(self.scope))
        self.notes, self.events, self.prompts = [], [], []
        self.prepare_bodies, self.confirm_bodies, self.inference_bodies = [], [], []
        self.decision, self.fail_registration, self.lose_confirm = True, False, False
        self.on_confirm = None
        self.stream_status = None
        self.response = None
        self.preview = None
        self.client = SimpleNamespace(
            list_models=AsyncMock(side_effect=lambda _provider: {"models": [deepcopy(self.target)]}),
            _request=AsyncMock(side_effect=self.validate_model), agent_run_get=AsyncMock(return_value={
                "run_version": 2, "valid_actions_signature": self.scope["expected_valid_actions_signature"]}),
            agent_run_create=AsyncMock(side_effect=self.create_run),
            agent_register_workspace=AsyncMock(side_effect=self.register),
            native_handoff_prepare=AsyncMock(side_effect=self.prepare),
            native_handoff_confirm=AsyncMock(side_effect=self.confirm),
            ask=AsyncMock(side_effect=self.ask), ask_stream=self.stream,
        )
        self._compile()

    async def validate_model(self, method, path, *, json):
        assert (method, path) == ("POST", "/models/validate")
        assert json["model"] == NEW
        return {"selection_valid": True, "state_changed": False, "model": deepcopy(self.target)}

    def assert_old(self):
        current = self.state()
        assert current["current_model"] == OLD
        assert current["current_run_id"] == self.scope["run_id"]
        assert current["native_generation_session"] is self.source_session

    async def create_run(self, **kwargs):
        self.assert_old()
        assert kwargs == {"state": "running", "is_resumable": True}
        self.events.append("create")
        return {"run_id": self.destination_id}

    async def register(self, **kwargs):
        self.assert_old()
        assert kwargs == {"run_id": self.destination_id, "runtime_session_id": self.scope["runtime_session_id"],
            "workspace_root": "/synthetic/workspace", "workspace_fingerprint": "sha256:" + "c" * 64,
            "git_root": "/synthetic/workspace"}
        self.events.append("register")
        if self.fail_registration:
            raise APIError(503, "synthetic registration unavailable")
        return {"run_id": self.destination_id, "runtime_session_id": self.scope["runtime_session_id"],
                "run_version": 3, "valid_actions_signature": "sha256:" + "d" * 64}

    async def prepare(self, request):
        self.assert_old()
        self.events.append("prepare")
        self.prepare_bodies.append(request.model_dump())
        if self.preview is None:
            self.preview = {"handoff_id": str(uuid.uuid4()), "handoff_sha256": "b" * 64,
                "selection": request.selection.model_dump(), "expires_at": "2099-01-01T00:00:00+00:00",
                "task_count": 1, "file_count": 1, "unique_file_count": 1, "observation_count": 2,
                "destination_scope": None}
        return NativeHandoffResponse.model_validate(self.preview)

    async def confirm(self, request):
        self.assert_old()
        self.events.append("confirm")
        self.confirm_bodies.append(request.model_dump())
        response = NativeHandoffResponse.model_validate({**self.preview,
            "destination_scope": request.destination_scope.model_dump()})
        if self.on_confirm:
            self.on_confirm()
        if self.lose_confirm:
            self.lose_confirm = False
            raise APIError(503, "Synthetic lost confirmation ACK")
        return response

    def choose(self, question, *, default):
        self.prompts.append(question)
        self.events.append("user-confirm")
        assert default is False
        return self.decision

    async def ask(self, prompt, provider, model, **kwargs):
        self.inference_bodies.append(deepcopy({"prompt": prompt, "provider": provider, "model": model, **kwargs}))
        if self.response is not None:
            return deepcopy(self.response)
        return result(kwargs["native_scope"])

    async def stream(self, prompt, provider, model, **kwargs):
        if self.stream_status is not None:
            self.inference_bodies.append(deepcopy(kwargs))
            raise APIError(self.stream_status, "Synthetic stream unavailable")
        response = await self.ask(prompt, provider, model, **kwargs)
        yield {"event": "response.completed", "data": {"payload": response}}

    def _compile(self):
        tree = ast.parse(SOURCE.read_text())
        names = {"_stage_handoff_destination", "_switch_native_model", "_use_model_capabilities",
                 "_refresh_model_capabilities", "_chat_capability", "_validate_openrouter_request",
                 "_ask_with_optional_stream", "_env_flag", "_model_capability", "_reasoning_status",
                 "_reasoning_for_model", "_ensure_runtime_run", "_create_and_register_runtime_run", "_reset_native_task"}
        names.update({"_should_enable_web_search_for_turn", "_has_workspace_tooling_intent",
                      "_is_local_attachment_analysis_request", "_has_web_request_signal", "_has_patch_intent",
                      "_has_explicit_file_target", "_is_noncode_asset_reference", "_has_code_filename_reference",
                      "_has_local_path_syntax", "_has_workspace_action_verb"})
        nodes = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names}
        assert names == nodes.keys()
        loop = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_tool_loop")
        stop = next(i for i, node in enumerate(loop.body) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "canonical_chat" for t in node.targets))
        entry = deepcopy(loop)
        entry.name = "_enter_native_task"
        entry.body = entry.body[:stop] + [ast.Return(value=ast.Name(id="native_history_mode", ctx=ast.Load()))]
        # Extract the contiguous production web-selection block, including its fail-closed guard.
        web_parent = next(node for node in ast.walk(loop) if isinstance(node, ast.If)
            and any(isinstance(item, ast.Assign) and any(isinstance(t, ast.Name)
                and t.id == "web_search_requested_turn" for t in item.targets) for item in node.body))
        web_start = next(i for i, node in enumerate(web_parent.body) if isinstance(node, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == "web_search_requested_turn" for t in node.targets))
        web_end = next(i for i, node in enumerate(web_parent.body) if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "web_search_activity_turn" for t in node.targets))
        web = ast.parse("def turn_web(user_message, attachment_file_ids_for_turn):\n"
                        "    return web_search_requested_turn, web_search_effective_turn\n").body[0]
        web.body[:0] = deepcopy(web_parent.body[web_start:web_end])

        def command_branch(value):
            return next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "cmd" and (
                    isinstance(node.test.comparators[0], ast.Constant) and node.test.comparators[0].value == value
                    or isinstance(node.test.comparators[0], ast.Set)
                    and {item.value for item in node.test.comparators[0].elts} == value))

        pending_guard = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
            and isinstance(node.test, ast.BoolOp) and any(isinstance(item, ast.Name) and item.id == "message"
                for item in ast.walk(node.test))
            and any(isinstance(item, ast.Name) and item.id == "pending_native_handoff" for item in ast.walk(node.test)))
        initial = {
            "current_provider": "openrouter", "current_model": OLD, "current_thread_id": None,
            "current_run_id": self.scope["run_id"], "runtime_session_id": self.scope["runtime_session_id"],
            "current_run_version": 2, "current_signature": self.scope["expected_valid_actions_signature"],
            "current_reasoning_effort": None, "current_reasoning_efforts": (),
            "current_model_capabilities": reviewed_capabilities(descriptor(OLD), "openrouter", OLD),
            "native_generation_session": self.source_session, "pending_native_handoff": None,
            "pending_handoff_capabilities": None, "last_successful_tool": None,
            "context_warning_emitted": False, "web_search_requested": False,
            "last_web_search_effective": False, "last_web_search_used": False,
            "last_web_search_retry_without_tool": False, "last_assistant_text_for_turn": "",
            "runtime_run_task": None, "startup_bootstrap_task": None,
        }
        wrapper = ast.parse("def factory():\n" + "".join(f"    {key} = initial[{key!r}]\n" for key in initial)
            + "    def state():\n        return {" + ",".join(f"{key!r}: {key}" for key in initial) + "}\n"
            + "    def set_state(**values):\n        nonlocal " + ",".join(initial) + "\n"
            + "".join(f"        if {key!r} in values: {key} = values[{key!r}]\n" for key in initial)
            + "    async def command(messages):\n        nonlocal " + ",".join(initial)
            + "\n        for message in messages:\n            parts = message.split()\n            cmd = parts[0].lower()\n"
            + "            pass\n"
            + "    return SimpleNamespace(state=state, set_state=set_state, command=command, switch=_switch_native_model, "
              "stage=_stage_handoff_destination, enter=_enter_native_task, ask=_ask_with_optional_stream, "
              "web=turn_web, heuristic=_should_enable_web_search_for_turn, reset=_reset_native_task)\n")
        factory = wrapper.body[0]
        commands = next(node for node in factory.body if isinstance(node, ast.AsyncFunctionDef))
        commands.body[1].body[-1:] = [pending_guard, command_branch("/handoff"),
                                     command_branch({"/models", "/provider", "/model"}), command_branch("/reasoning")]
        factory.body[-1:-1] = [*nodes.values(), entry, web]
        ast.fix_missing_locations(wrapper)
        namespace = {"initial": initial, "SimpleNamespace": SimpleNamespace, "Any": Any,
            "asyncio": asyncio, "os": os, "re": re, "uuid": uuid, "APIError": APIError,
            "NativeGenerationSession": NativeGenerationSession,
            "ReviewedModelCapabilities": ReviewedModelCapabilities, "reviewed_capabilities": reviewed_capabilities,
            "validate_selection": validate_selection, "ModelSelectionError": ModelSelectionError,
            "plan_switch": plan_switch, "model_options": model_options, "format_options": format_options,
            "client": self.client, "Confirm": SimpleNamespace(ask=self.choose),
            "_chat_modal": AsyncMock(side_effect=lambda callback: callback()),
            "console": SimpleNamespace(print=lambda *args, **_kwargs: self.notes.append(str(args))),
            "model_switch_local_tools": SimpleNamespace(_BACKGROUND_JOBS={}),
            "workspace_root": "/synthetic/workspace", "workspace_git_root": "/synthetic/workspace",
            "workspace_fp": "sha256:" + "c" * 64, "pending_attachments": [], "chat_transcript": [],
            "allow_model_switch": True, "conversation_mode": "persistent", "show_stream_status": False,
            "native_history_mode": True, "user_message": "Exact new task", "emote_bridge": SimpleNamespace(current_turn="turn"),
        }
        exec(compile(wrapper, str(SOURCE), "exec"), namespace)  # noqa: S102 - Unmodified trusted CLI nodes.
        self.helpers = namespace["factory"]()
        self.state = self.helpers.state
        self.namespace = namespace

    async def bound_session(self, phase):
        assert await self.helpers.switch("openrouter", NEW, self.target)
        session = self.state()["native_generation_session"]
        if phase == "pending_first":
            return session
        if phase == "native_history":
            session = NativeGenerationSession()
            self.helpers.set_state(native_generation_session=session)
        scope = self.confirm_bodies[0]["destination_scope"]
        session.prepare(key="already-dispatched", scope=scope, options={
            "provider": "openrouter", "model": NEW, "enable_tools": True,
            "enable_web_search": False, "attachments": [], "reasoning_effort": None,
            **({"max_tokens": session.max_tokens} if session.max_tokens is not None else {}),
        }, history=True, user_text="Exact new task")
        response = result(scope)
        response["completion_status"] = "incomplete"
        response["native_generation"]["continuation_supported"] = True
        response["tool_calls"] = [{"native_inference_request_id": response["native_generation"]["inference_request_id"],
                                   "provider_call_id": "accepted-original-tool"}]
        session.validate_result(response)
        return session


@pytest.fixture
def shell(monkeypatch):
    for flag in ("OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY"):
        monkeypatch.setenv(flag, "1")
    monkeypatch.delenv("OPENVEGAS_CHAT_NATIVE_TASK_HANDOFF", raising=False)
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", "0")
    return Shell()


@pytest.mark.asyncio
async def test_happy_path_commits_before_adoption_with_same_runtime_registration(shell):
    assert await shell.helpers.switch("openrouter", NEW, shell.target)
    assert shell.events == ["prepare", "user-confirm", "create", "register", "confirm"]
    state = shell.state()
    assert state["current_model"] == NEW and state["current_run_id"] == shell.destination_id
    assert state["runtime_session_id"] == shell.scope["runtime_session_id"]
    assert state["native_generation_session"].max_tokens == 512
    assert state["pending_native_handoff"] is None and shell.source_session.finalized
    shell.client.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_registration_retains_source_then_explicit_cancel(shell):
    shell.fail_registration = True
    assert not await shell.helpers.switch("openrouter", NEW, shell.target)
    shell.assert_old()
    assert shell.state()["pending_native_handoff"].state == "prepared"
    shell.client.native_handoff_confirm.assert_not_awaited()
    await shell.helpers.command(["/handoff cancel"])
    shell.assert_old()
    assert shell.state()["pending_native_handoff"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["/handoff retry", "/model " + NEW])
async def test_lost_ack_retains_exact_confirm_and_blocks_other_model_and_prompt(shell, retry):
    shell.lose_confirm = True
    assert not await shell.helpers.switch("openrouter", NEW, shell.target)
    shell.assert_old()
    pending = shell.state()["pending_native_handoff"]
    assert pending.state == "confirm_uncertain"
    await shell.helpers.command(["Do another task", "/model fixture/different", "/handoff cancel"])
    assert shell.state()["pending_native_handoff"] is pending
    assert len(shell.confirm_bodies) == 1
    with pytest.raises(APIError, match="pending model switch"):
        await shell.helpers.enter(shell.client, "Do another task")
    await shell.helpers.command([retry])
    assert shell.state()["current_model"] == NEW
    assert shell.confirm_bodies[0] == shell.confirm_bodies[1]
    shell.client.agent_run_create.assert_awaited_once()
    shell.client.agent_register_workspace.assert_awaited_once()
    assert len(shell.prompts) == 1


@pytest.mark.asyncio
async def test_decline_does_not_stage_confirm_or_change_selection(shell):
    shell.decision = False
    assert not await shell.helpers.switch("openrouter", NEW, shell.target)
    shell.assert_old()
    assert shell.state()["pending_native_handoff"] is None
    shell.client.agent_run_create.assert_not_awaited()
    shell.client.native_handoff_confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_model_command_respects_default_off_handoff_gate(shell, monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_TASK_HANDOFF", "1")
    await shell.helpers.command(["/model " + NEW])
    if enabled:
        assert shell.state()["current_model"] == NEW
    else:
        shell.assert_old()
        shell.client.native_handoff_prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_pending_first_dispatch_is_not_reset_and_injects_exact_context(shell, monkeypatch, stream):
    assert await shell.helpers.switch("openrouter", NEW, shell.target)
    session = shell.state()["native_generation_session"]
    assert session.awaiting_first_dispatch and not session.history_active
    assert await shell.helpers.enter(shell.client, "Exact new task")
    assert shell.state()["native_generation_session"] is session
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", "1" if stream else "0")
    await shell.helpers.ask("Runtime prompt", idempotency_key="destination-first", enable_tools=True,
                            enable_web_search=False, attachments=[])
    sent = shell.inference_bodies[0]
    assert sent["native_scope"] == shell.confirm_bodies[0]["destination_scope"]
    assert sent["native_handoff"] == session.handoff_ref.model_dump()
    assert sent["max_tokens"] == 512 and sent["native_user_text"] == "Exact new task"
    assert sent["native_history"] is True and sent["persist_context"] is False
    assert sent["conversation_mode"] == "ephemeral" and sent["thread_id"] is None
    assert "native_continuation" not in sent
    shell.client.agent_run_create.assert_awaited_once()
    assert session.finalized


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405, 501])
async def test_bound_stream_has_no_legacy_ask_fallback(shell, monkeypatch, status):
    assert await shell.helpers.switch("openrouter", NEW, shell.target)
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", "1")
    shell.stream_status = status
    with pytest.raises(APIError) as error:
        await shell.helpers.ask("Runtime prompt", idempotency_key="destination-first", enable_tools=True,
                                enable_web_search=False, attachments=[])
    assert error.value.status == status and len(shell.inference_bodies) == 1
    shell.client.ask.assert_not_awaited()
    assert shell.state()["native_generation_session"].awaiting_first_dispatch


@pytest.mark.asyncio
async def test_late_capability_change_cannot_leave_partial_local_adoption(shell):
    shell.on_confirm = lambda: shell.target["capabilities"].update(reviewed=False)
    success = await shell.helpers.switch("openrouter", NEW, shell.target)
    # A frozen, previously checked capability snapshot can complete adoption;
    # a rejected snapshot must leave all active state pointing to the source.
    if success:
        assert shell.state()["current_model"] == NEW
        assert shell.state()["pending_native_handoff"] is None
    else:
        shell.assert_old()
        assert shell.state()["pending_native_handoff"].state == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,with_attachment", [
    ("Edit ./src/main.py to fix the local workspace bug", False),
    ("Summarize this PDF attachment", True),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_confirmed_web_true_survives_local_turn_heuristic(shell, monkeypatch, prompt, with_attachment, stream):
    shell.target["capabilities"]["web_search"] = True
    shell.helpers.set_state(web_search_requested=True)
    assert await shell.helpers.switch("openrouter", NEW, shell.target)
    files = [str(uuid.uuid4())] if with_attachment else []
    assert shell.helpers.heuristic(prompt, has_uploaded_attachments=bool(files)) is False
    assert shell.helpers.web(prompt, files) == (True, True)
    shell.namespace["user_message"] = prompt
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", str(int(stream)))
    await shell.helpers.ask("Runtime prompt", idempotency_key="web-first", enable_tools=True,
                            enable_web_search=shell.helpers.web(prompt, files)[1], attachments=files)
    assert len(shell.inference_bodies) == 1
    sent = shell.inference_bodies[0]
    assert sent["enable_web_search"] is True and sent["attachments"] == files
    assert sent["native_user_text"] == prompt
    assert sent["native_handoff"] == shell.state()["native_generation_session"].handoff_ref.model_dump()


@pytest.mark.asyncio
async def test_missing_confirmed_web_capability_rejects_without_freezing_session(shell):
    shell.target["capabilities"]["web_search"] = True
    shell.helpers.set_state(web_search_requested=True)
    assert await shell.helpers.switch("openrouter", NEW, shell.target)
    session = shell.state()["native_generation_session"]
    before = deepcopy(vars(session))
    shell.target["capabilities"]["web_search"] = False
    shell.helpers.set_state(current_model_capabilities=reviewed_capabilities(shell.target, "openrouter", NEW))
    with pytest.raises(APIError, match="confirmed web capability"):
        shell.helpers.web("Edit ./src/main.py", [])
    with pytest.raises(APIError, match="web_search"):
        await shell.helpers.ask("Runtime prompt", idempotency_key="web-first", enable_tools=True,
                                enable_web_search=True, attachments=[])
    assert shell.state()["native_generation_session"] is session and vars(session) == before
    assert not shell.inference_bodies


NATIVE_FLAGS = ["OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY"]
BOUND_PHASES = ["pending_first", "handoff_history", "native_history"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", BOUND_PHASES)
@pytest.mark.parametrize("flag", NATIVE_FLAGS)
async def test_dropped_native_flag_at_task_entry_retains_session(shell, monkeypatch, phase, flag):
    session = await shell.bound_session(phase)
    before = deepcopy(vars(session))
    monkeypatch.setenv(flag, "0")
    with pytest.raises(APIError, match="Native history cannot switch"):
        await shell.helpers.enter(shell.client, "Exact new task")
    assert shell.state()["native_generation_session"] is session and vars(session) == before
    assert not shell.inference_bodies
    shell.client.agent_run_create.assert_awaited_once()
    assert len(shell.prompts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", BOUND_PHASES)
@pytest.mark.parametrize("flag", NATIVE_FLAGS)
@pytest.mark.parametrize("timing", ["before", "preflight", "ensure"])
async def test_dropped_native_flag_before_or_across_await_never_sends_or_freezes(
    shell, monkeypatch, phase, flag, timing,
):
    session = await shell.bound_session(phase)
    before = deepcopy(vars(session))
    reached = []
    if timing == "before":
        monkeypatch.setenv(flag, "0")
    elif timing == "preflight":
        async def validate_then_drop(*args, **kwargs):
            response = await shell.validate_model(*args, **kwargs)
            await asyncio.sleep(0)
            reached.append("preflight")
            monkeypatch.setenv(flag, "0")
            return response
        shell.client._request.side_effect = validate_then_drop
    else:
        # Use the real _ensure_runtime_run await on its existing bootstrap task.
        async def finish_registration():
            await asyncio.sleep(0)
            reached.append("ensure")
            monkeypatch.setenv(flag, "0")
            shell.helpers.set_state(current_run_id=shell.destination_id)
            return True
        task = asyncio.create_task(finish_registration())
        shell.helpers.set_state(current_run_id=None, runtime_run_task=task)
    with pytest.raises(APIError, match="cannot fall back to an unowned request"):
        await shell.helpers.ask("Runtime prompt", idempotency_key="never-reserved", enable_tools=True,
                                enable_web_search=False, attachments=[])
    assert reached == ([] if timing == "before" else [timing])
    assert shell.state()["native_generation_session"] is session and vars(session) == before
    assert not shell.inference_bodies
    shell.client.ask.assert_not_awaited()
    shell.client.agent_run_create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", BOUND_PHASES)
async def test_failed_tool_preflight_leaves_bound_session_unmodified(shell, phase):
    session = await shell.bound_session(phase)
    before = deepcopy(vars(session))
    shell.target["capabilities"]["tools"] = False
    with pytest.raises(APIError, match="tools"):
        await shell.helpers.ask("Runtime prompt", idempotency_key="never-reserved", enable_tools=True,
                                enable_web_search=False, attachments=[])
    assert shell.state()["native_generation_session"] is session and vars(session) == before
    assert not shell.inference_bodies


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["high", "default"])
async def test_pending_first_dispatch_blocks_reasoning_mutation(shell, argument):
    shell.target["capabilities"].update(reasoning_controls=True, reasoning_efforts=["medium", "high"])
    shell.helpers.set_state(current_reasoning_effort="medium")
    session = await shell.bound_session("pending_first")
    before = deepcopy(vars(session))
    assert session.confirmed_selection.reasoning_effort == "medium"
    shell.client._request.reset_mock()
    await shell.helpers.command(["/reasoning " + argument])
    assert shell.state()["current_reasoning_effort"] == "medium"
    assert any("Reasoning is fixed" in note for note in shell.notes)
    assert shell.state()["native_generation_session"] is session and vars(session) == before
    shell.client._request.assert_not_awaited()
    assert not shell.inference_bodies


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "api_error", "runtime_error", "success"])
async def test_native_reset_adopts_only_after_registered_scope_ack(shell, outcome):
    before = shell.state()
    source_state = deepcopy(vars(shell.source_session))
    entered, release = asyncio.Event(), asyncio.Event()
    registered_scope = {
        "run_id": shell.destination_id, "runtime_session_id": shell.scope["runtime_session_id"],
        "run_version": 3, "valid_actions_signature": "sha256:" + "d" * 64,
    }

    async def blocked_registration(**kwargs):
        assert kwargs == {
            "run_id": shell.destination_id, "runtime_session_id": shell.scope["runtime_session_id"],
            "workspace_root": "/synthetic/workspace", "workspace_fingerprint": "sha256:" + "c" * 64,
            "git_root": "/synthetic/workspace",
        }
        entered.set()
        await release.wait()
        if outcome == "api_error":
            raise APIError(503, "Synthetic registration unavailable")
        if outcome == "runtime_error":
            raise RuntimeError("Synthetic registration failure")
        return registered_scope

    def assert_retained():
        assert shell.state() == before
        assert shell.state()["native_generation_session"] is shell.source_session
        assert vars(shell.source_session) == source_state
        assert not shell.inference_bodies

    shell.client.agent_register_workspace.side_effect = blocked_registration
    task = asyncio.create_task(shell.helpers.reset())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not task.done()
        assert_retained()
        shell.client.agent_run_create.assert_awaited_once_with(state="running", is_resumable=True)
        shell.client.agent_register_workspace.assert_awaited_once()
        if outcome == "cancel":
            task.cancel("cancel-native-reset")
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ("cancel-native-reset",)
            assert task.cancelled()
            assert_retained()
        elif outcome in {"api_error", "runtime_error"}:
            release.set()
            with pytest.raises(APIError) as error:
                await task
            assert error.value.status == 409
            assert_retained()
        else:
            release.set()
            await task
            after = shell.state()
            session = after["native_generation_session"]
            assert session is not shell.source_session
            assert type(session) is NativeGenerationSession
            assert vars(session) == vars(NativeGenerationSession())
            assert after == {
                **before, "current_run_id": registered_scope["run_id"],
                "current_run_version": registered_scope["run_version"],
                "current_signature": registered_scope["valid_actions_signature"],
                "native_generation_session": session,
            }
            assert after["runtime_session_id"] == shell.scope["runtime_session_id"]
            assert vars(shell.source_session) == source_state
        assert not shell.inference_bodies
        shell.client.ask.assert_not_awaited()
        shell.client._request.assert_not_awaited()
        shell.client.native_handoff_prepare.assert_not_awaited()
        shell.client.native_handoff_confirm.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("phase", [*BOUND_PHASES, "scope_only"])
async def test_malformed_native_200_never_reflects_private_body_or_finalizes(shell, monkeypatch, stream, phase):
    if phase == "scope_only":
        session = NativeGenerationSession()
        shell.helpers.set_state(current_model=NEW, native_generation_session=session)
        shell.namespace["native_history_mode"] = False
        monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY", "0")
    else:
        session = await shell.bound_session(phase)
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", str(int(stream)))
    canary = "PRIVATECANARY-native-malformed-200"
    # status=ok reaches native receipt validation in both transports. The body
    # has no receipt; text also survives the stream helper's payload projection.
    shell.response = {"status": "ok", "completion_status": "complete", "tool_calls": [],
                      "text": canary, "detail": canary, "error": canary,
                      "private_state": {"history": canary}}
    scope_before = {name: shell.state()[name] for name in
                    ("current_run_id", "current_run_version", "current_signature", "runtime_session_id")}
    handoff_before = session.handoff_ref
    with pytest.raises(APIError) as failure:
        await shell.helpers.ask("Runtime prompt", idempotency_key="malformed-native-200", enable_tools=True,
                                enable_web_search=False, attachments=[])
    error = failure.value
    assert error.status == 502
    assert error.data == {}
    assert canary not in str(error)
    assert canary not in repr(error)
    assert canary not in repr(vars(error))
    assert canary not in repr(error.__cause__)
    assert canary not in repr(shell.notes)
    assert shell.state()["native_generation_session"] is session
    assert session.handoff_ref == handoff_before
    assert not session.finalized
    assert session._receipt is None and session._result_unverified
    assert all(shell.state()[name] == value for name, value in scope_before.items())
    assert len(shell.inference_bodies) == 1  # No fallback, retry, or finalizer send.
    assert shell.client.ask.await_count == (0 if stream else 1)
    assert "native_scope" in shell.inference_bodies[0]
    if phase == "pending_first":
        assert session.awaiting_first_dispatch
    with pytest.raises(ValueError, match="verified final native task"):
        session.handoff_source(shell.inference_bodies[0]["native_scope"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_legacy_unbound_200_keeps_existing_response_behavior(shell, monkeypatch, stream):
    session = NativeGenerationSession()
    shell.helpers.set_state(current_model=NEW, native_generation_session=session)
    for flag in NATIVE_FLAGS:
        monkeypatch.setenv(flag, "0")
    monkeypatch.setenv("OPENVEGAS_CHAT_STREAM_EVENTS", str(int(stream)))
    shell.namespace["native_history_mode"] = False
    before = deepcopy(vars(session))
    shell.response = {"status": "ok", "text": "Legacy response text", "detail": "Legacy detail",
                      "error": "legacy-extra-field", "completion_status": "complete", "tool_calls": []}
    response = await shell.helpers.ask("Runtime prompt", idempotency_key="legacy-200", enable_tools=True,
                                       enable_web_search=False, attachments=[])
    assert response["text"] == shell.response["text"]
    if not stream:
        assert response == shell.response
    assert len(shell.inference_bodies) == 1 and "native_scope" not in shell.inference_bodies[0]
    assert shell.client.ask.await_count == (0 if stream else 1)
    assert shell.state()["native_generation_session"] is session and vars(session) == before
