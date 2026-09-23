"""Exercise the actual CLI scope/transport branch without auth or paid calls."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import test_cli_stream_completion as streaming

from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.tui.model_picker import ReviewedModelCapabilities

consumer = streaming.consumer
APIError, event = streaming.APIError, streaming.event


@pytest.fixture
def native_consumer(consumer):
    consumer.namespace.update({
        "current_provider": "openrouter", "current_run_id": str(uuid4()),
        "runtime_session_id": str(uuid4()), "current_run_version": 1,
        "current_signature": "sha256:" + "a" * 64,
        "native_generation_session": NativeGenerationSession(),
        "current_model_capabilities": ReviewedModelCapabilities(
            "openrouter", "offline-test-model", frozenset({"stream_events", "tools"}), (), "buffered",
        ),
        "_ensure_runtime_run": AsyncMock(return_value=True),
        "_validate_openrouter_request": AsyncMock(),
        "_env_flag": lambda name, default: name == "OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE" or default == "1",
    })
    consumer.client.ask.return_value = native_payload(consumer, text="non-stream")
    return consumer


def native_payload(consumer, *, text="answer", status="complete", **extra):
    return {"status": "ok", "text": text, "completion_status": status,
            "provider_request_id": "fixture-supplier-id", "native_generation": {
                "scope_version": 1, "run_id": consumer.namespace["current_run_id"],
                "runtime_session_id": consumer.namespace["runtime_session_id"],
                "original_turn_scope_verified": True, "continuation_supported": False,
                "inference_request_id": str(uuid4()),
            }, **extra}


def invoke(consumer, *, key="same-key", enable_tools=True):
    return asyncio.run(consumer.namespace["_ask_with_optional_stream"](
        "prompt", idempotency_key=key, enable_tools=enable_tools,
        enable_web_search=False, attachments=[],
    ))


@pytest.mark.parametrize("stream", [True, False])
def test_registered_scope_flows_through_actual_send(native_consumer, stream):
    c = native_consumer
    c.namespace["_env_flag"] = lambda name, default: (
        True if name == "OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE" else stream
    )
    result = c.run([event("response.completed", native_payload(c))])
    kwargs = c.requests[0][1] if stream else c.client.ask.call_args.kwargs
    assert kwargs["native_scope"] == {
        "run_id": c.namespace["current_run_id"], "runtime_session_id": c.namespace["runtime_session_id"],
        "expected_run_version": 1, "expected_valid_actions_signature": "sha256:" + "a" * 64,
    }
    assert kwargs["thread_id"] is None and kwargs["persist_context"] is False
    assert kwargs["conversation_mode"] == "ephemeral"
    assert result["completion_status"] == "complete"
    assert result["provider_request_id"] == "fixture-supplier-id"
    assert result["native_generation"]["run_id"] == c.namespace["current_run_id"]
    c.namespace["_ensure_runtime_run"].assert_awaited_once_with(wait=True)


@pytest.mark.parametrize("status", [404, 405, 501])
def test_stream_fallback_preserves_original_native_scope(native_consumer, status):
    c = native_consumer
    assert c.run([APIError(status, "unavailable")]) == c.client.ask.return_value
    args, kwargs = c.requests[0]
    c.client.ask.assert_awaited_once_with(*args, **kwargs)
    assert kwargs["native_scope"]["run_id"] == c.namespace["current_run_id"]


def test_second_generation_does_not_reset_run_or_send_unowned(native_consumer):
    c = native_consumer
    c.run([event("response.completed", native_payload(c))])
    with pytest.raises(APIError, match="No further inference"):
        invoke(c, key="different")
    c.namespace["current_run_id"] = str(uuid4())
    with pytest.raises(APIError, match="No further inference"):
        invoke(c, key="third")
    assert len(c.requests) == 1
    c.client.ask.assert_not_awaited()


def test_unregistered_run_fails_before_sending(native_consumer):
    c = native_consumer
    c.namespace["_ensure_runtime_run"].return_value = False
    with pytest.raises(APIError, match="registered workspace"):
        c.run([])
    assert c.requests == []
    c.client.ask.assert_not_awaited()


def test_legacy_text_finalizer_cannot_escape_scope_guard(native_consumer):
    c = native_consumer
    with pytest.raises(APIError, match="Native continuation"):
        invoke(c, enable_tools=False)
    assert c.requests == []
    c.client.ask.assert_not_awaited()


def test_transport_failure_does_not_create_a_new_paid_attempt(native_consumer):
    c = native_consumer
    with pytest.raises(OSError, match="lost"):
        c.run([OSError("lost")])
    with pytest.raises(APIError, match="No further inference"):
        invoke(c, key="replacement")
    assert len(c.requests) == 1
    c.client.ask.assert_not_awaited()


@pytest.mark.parametrize("status", ["incomplete", "truncated", "unknown", None])
def test_incomplete_text_is_not_a_final_answer(native_consumer, status):
    c = native_consumer
    with pytest.raises(APIError, match="incomplete") as exc:
        c.run([event("response.completed", native_payload(c, status=status, v_cost="0.01"))])
    assert exc.value.data["v_cost"] == "0.01"
    assert exc.value.data["text"] == "answer"
    c.client.ask.assert_not_awaited()


def test_tool_completion_retains_original_refs_without_becoming_a_final_answer(native_consumer):
    c = native_consumer
    payload = native_payload(c, text="", status="incomplete")
    payload["tool_calls"] = [{"tool_name": "Read", "arguments": {"path": "fixture.txt"},
                              "provider_call_id": "call-1", "native_inference_request_id":
                              payload["native_generation"]["inference_request_id"]}]
    result = c.run([event("response.completed", payload)])
    assert result["completion_status"] == "incomplete" and result["tool_calls"] == payload["tool_calls"]
    with pytest.raises(APIError, match="No further inference"):
        invoke(c, key="unsafe-follow-up")


def test_missing_native_receipt_is_not_accepted_as_success(native_consumer):
    with pytest.raises(APIError, match="receipt is missing"):
        native_consumer.run([event("response.completed", {"status": "ok", "text": "legacy"})])


def test_scoped_flag_forwards_tool_refs_without_separate_history_flag():
    source = Path(__file__).resolve().parents[2] / "openvegas" / "cli.py"
    tree = ast.parse(source.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_native_tool_proposal_metadata"]
    assert len(calls) == 1
    condition = next(item.value for item in calls[0].keywords if item.arg == "enabled")
    for scope, history, expected in [(True, False, True), (False, True, True), (False, False, False)]:
        namespace = {"_env_flag": lambda name, default, _scope=scope, _history=history: {
            "OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE": _scope, "OPENVEGAS_NATIVE_TOOL_HISTORY": _history,
        }[name]}
        # Only the trusted repository expression is evaluated, never user input.
        assert eval(compile(ast.Expression(condition), str(source), "eval"), namespace) is expected


def test_actual_registration_updates_current_projection():
    source = Path(__file__).resolve().parents[2] / "openvegas" / "cli.py"
    nodes = [node for node in ast.walk(ast.parse(source.read_text()))
             if isinstance(node, ast.AsyncFunctionDef) and node.name == "_create_and_register_runtime_run"]
    assert len(nodes) == 1
    wrapper = "async def run():\n    current_run_id, current_run_version, current_signature = None, 0, 'sha256:'\n"
    wrapper += "\n".join("    " + line for line in ast.unparse(nodes[0]).splitlines())
    wrapper += "\n    success = await _create_and_register_runtime_run()\n    return success, current_run_id, current_run_version, current_signature\n"
    run_id = str(uuid4())
    client = SimpleNamespace(
        agent_run_create=AsyncMock(return_value={"run_id": run_id, "run_version": 1, "valid_actions_signature": "old"}),
        agent_register_workspace=AsyncMock(return_value={"run_version": 2, "valid_actions_signature": "new"}),
    )
    namespace = {"client": client, "runtime_session_id": str(uuid4()), "workspace_root": "/fixture",
                 "workspace_fp": "fixture", "workspace_git_root": "/fixture"}
    exec(compile(wrapper, str(source), "exec"), namespace)  # noqa: S102 - Trusted repository function, not user input.
    assert asyncio.run(namespace["run"]()) == (True, run_id, 2, "new")
