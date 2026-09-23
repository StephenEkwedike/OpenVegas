"""Exercise the real nested CLI stream consumer without importing CLI side effects."""

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


class APIError(Exception):
    def __init__(self, status, detail, data=None):
        self.status = status
        self.detail = detail
        self.data = data or {}
        super().__init__(detail)


def event(name, payload, sequence=1):
    return {
        "event": name,
        "data": {
            "run_id": "run-1",
            "turn_id": "turn-1",
            "sequence_no": sequence,
            "payload": payload,
        },
    }


@pytest.fixture
def consumer():
    source = Path(os.environ.get(
        "CLI_STREAM_SOURCE",
        str(Path(__file__).resolve().parents[2] / "openvegas" / "cli.py"),
    ))
    functions = [
        node for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ask_with_optional_stream"
    ]
    assert len(functions) == 1
    functions.extend(node for node in ast.walk(ast.parse(source.read_text()))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in {"_model_capability", "_chat_capability", "_validate_openrouter_request"})
    requests = []
    client = SimpleNamespace(ask=AsyncMock(return_value={"text": "non-stream"}))
    namespace = {
        "Any": Any,
        "ReviewedModelCapabilities": __import__("openvegas.tui.model_picker", fromlist=["ReviewedModelCapabilities"]).ReviewedModelCapabilities,
        "current_model_capabilities": None,
        "APIError": APIError,
        "client": client,
        "_env_flag": lambda _name, default: default == "1",
        "resolve_capability": lambda *_: True,
        "current_provider": "openai",
        "current_model": "offline-test-model",
        "current_thread_id": "thread-1",
        "conversation_mode": "persistent",
        "show_stream_status": False,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)  # noqa: S102 - Trusted repository AST only; avoids CLI import side effects.

    def run(events):
        async def ask_stream(*args, **kwargs):
            requests.append((args, kwargs))
            for item in events:
                if isinstance(item, Exception):
                    raise item
                yield item

        client.ask_stream = ask_stream
        return asyncio.run(asyncio.wait_for(
            namespace["_ask_with_optional_stream"](
                "prompt", idempotency_key="same-key", enable_tools=True,
                enable_web_search=False, attachments=[],
            ), timeout=1,
        ))

    return SimpleNamespace(run=run, client=client, requests=requests, namespace=namespace)


@pytest.mark.parametrize("events", [
    [],
    [event("response.delta", {"text": "partial"})],
    [event("response.delta", {"text": "partial"}), event("stream_end", {"status": "ok"})],
    [event("stream_end", {"status": "ok"})],
    [event("tool.call", {"tool": {"name": "write_file"}})],
    [{"event": "response.completed", "data": None}],
])
def test_eof_without_authoritative_completion_fails_without_replay(consumer, events):
    with pytest.raises(APIError) as exc:
        consumer.run(events)
    assert exc.value.status == 502
    assert exc.value.data["error"] == "incomplete_stream"
    consumer.client.ask.assert_not_awaited()
    assert len(consumer.requests) == 1


@pytest.mark.parametrize("payload", [{}, {"status": "failed"}, {"status": None}, "ok"])
def test_invalid_completion_fails(consumer, payload):
    with pytest.raises(APIError) as exc:
        consumer.run([event("response.completed", payload)])
    assert exc.value.status == 502
    assert exc.value.data["error"] == "invalid_stream_completion"
    consumer.client.ask.assert_not_awaited()


@pytest.mark.parametrize("name", ["response.error", "error", "response.completed", "stream_end"])
def test_error_events_cannot_become_success(consumer, name):
    with pytest.raises(APIError) as exc:
        consumer.run([
            event("response.delta", {"text": "partial"}),
            event(name, {"status": "error", "error": "upstream_failed", "detail": "offline failure"}),
            event("response.completed", {"status": "ok"}, sequence=3),
        ])
    assert exc.value.status == 400
    assert exc.value.detail == "upstream_failed: offline failure"
    consumer.client.ask.assert_not_awaited()


def test_server_error_completion_without_error_code_fails(consumer):
    with pytest.raises(APIError, match="stream_error"):
        consumer.run([event("response.completed", {"status": "error", "v_cost": "0"})])
    consumer.client.ask.assert_not_awaited()


def test_normal_server_terminal_preserves_text_billing_and_metadata(consumer):
    delta = event("response.delta", {"text": "answer"})
    result = consumer.run([
        event("response.started", {}), delta, delta,
        event("stream_delta", {"chars": 6}),
        event("stream_end", {"status": "ok"}),
        event("response.completed", {
            "status": "ok", "text": "answer", "v_cost": "0.25", "thread_id": "thread-1",
            "input_tokens": 2, "output_tokens": 3, "total_tokens": 5,
            "warning": "notice", "warnings": ["notice"],
        }),
    ])
    assert result["text"] == "answer"
    assert result["v_cost"] == "0.25"
    assert result["thread_id"] == "thread-1"
    assert [result[k] for k in ("input_tokens", "output_tokens", "total_tokens")] == [2, 3, 5]
    assert result["warnings"] == ["notice"]
    consumer.client.ask.assert_not_awaited()


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "mistral"])
def test_completed_payload_without_deltas_is_valid_for_each_provider(consumer, provider):
    consumer.namespace["current_provider"] = provider
    result = consumer.run([event("response.completed", {"status": "ok", "text": "answer"})])
    assert result["text"] == "answer"


def test_tool_only_completion_is_valid_and_marker_cannot_replace_it(consumer):
    calls = [{"id": "call-1", "name": "read_file", "arguments": {"path": "test"}}]
    result = consumer.run([
        event("response.completed", {"status": "ok", "tool_calls": calls, "v_cost": "0.5"}),
        event("stream_end", {"status": "ok"}),
    ])
    assert result["text"] == ""
    assert result["tool_calls"] == calls
    assert result["v_cost"] == "0.5"


@pytest.mark.parametrize("status", [404, 405, 501])
def test_endpoint_unavailable_retains_same_key_compatibility_fallback(consumer, status):
    assert consumer.run([APIError(status, "unavailable")]) == {"text": "non-stream"}
    args, kwargs = consumer.requests[0]
    consumer.client.ask.assert_awaited_once_with(*args, **kwargs)
    assert kwargs["idempotency_key"] == "same-key"


def test_transport_error_after_delta_propagates_without_replay(consumer):
    with pytest.raises(OSError, match="connection lost"):
        consumer.run([event("response.delta", {"text": "partial"}), OSError("connection lost")])
    consumer.client.ask.assert_not_awaited()


def test_disabled_stream_compatibility(consumer):
    consumer.namespace["_env_flag"] = lambda *_: False
    assert consumer.run([]) == {"text": "non-stream"}
    assert consumer.requests == []
    consumer.client.ask.assert_awaited_once()
