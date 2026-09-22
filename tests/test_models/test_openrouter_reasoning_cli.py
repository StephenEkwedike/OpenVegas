"""Execute real CLI reasoning/switch branches and stream consumer, without auth."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_cli_stream_completion as streaming

from openvegas.tui.model_picker import ModelSelectionError, plan_switch, reviewed_capabilities, validate_selection

SOURCE = Path(__file__).resolve().parents[2] / "openvegas/cli.py"


def reasoning_shell(overrides=None):
    tree = ast.parse(SOURCE.read_text())
    helpers = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in {"_reasoning_status", "_reasoning_for_model", "_use_model_capabilities", "_chat_capability", "_refresh_model_capabilities"}]
    branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "cmd"
                and ((isinstance(node.test.comparators[0], ast.Constant)
                      and node.test.comparators[0].value == "/reasoning")
                     or (isinstance(node.test.comparators[0], ast.Set)
                         and {v.value for v in node.test.comparators[0].elts}
                         == {"/models", "/provider", "/model"}))]
    source = """async def run(commands, client):
    current_provider, current_model, current_thread_id = 'openrouter', 'fixture/one', None
    current_reasoning_effort, current_reasoning_efforts = None, ()
    current_model_capabilities, web_search_requested = None, True
    client.list_models = AsyncMock(side_effect=list_models)
    client._request = AsyncMock(side_effect=request)
    allow_model_switch, startup_bootstrap_task = True, None
    pending_attachments, chat_transcript = [], []
"""
    source += "\n".join("    " + line for node in helpers for line in ast.unparse(node).splitlines())
    source += "\n    for parts in commands:\n        cmd = parts[0]\n"
    source += "\n".join("        " + line for node in branches for line in ast.unparse(node).splitlines())
    source += "\n    return current_provider, current_model, current_reasoning_effort, current_reasoning_efforts, web_search_requested, current_model_capabilities\n"
    rows = {
        "fixture/one": ["low", "high", "xhigh"],
        "fixture/two": ["high"],
        "fixture/three": ["low"],
    }
    output = []
    def descriptor(provider, model):
        efforts = rows.get(model, []) if provider == "openrouter" else []
        return {"provider": provider, "model_id": model, "enabled": True, "available": True,
                "capabilities": {"reviewed": True, "reasoning_controls": bool(efforts), "reasoning_efforts": efforts,
                                 "web_search": model == "fixture/one", **(overrides or {}).get(model, {})}}
    async def list_models(provider):
        return {"models": [descriptor(provider, model) for model in rows]}
    async def request(method, path, *, json):
        assert (method, path) == ("POST", "/models/validate")
        return {"selection_valid": True, "state_changed": False,
                "model": descriptor(json["provider"], json["model"])}
    model_resolver = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_model_capability")
    source = "from __future__ import annotations\n" + ast.unparse(model_resolver) + "\n" + source
    namespace = {
        "asyncio": asyncio, "reviewed_capabilities": reviewed_capabilities,
        "AsyncMock": AsyncMock, "list_models": list_models, "request": request,
        "console": SimpleNamespace(print=lambda *args, **_: output.append(" ".join(map(str, args)))),
        "APIError": RuntimeError, "ModelSelectionError": ModelSelectionError,
        "validate_selection": validate_selection, "plan_switch": plan_switch,
        "model_switch_local_tools": SimpleNamespace(_BACKGROUND_JOBS={}),
        "_env_flag": lambda name, default: default == "1",
        "Confirm": SimpleNamespace(ask=lambda *a, **k: True),
        "_chat_modal": AsyncMock(side_effect=lambda callback: callback()),
    }
    exec(compile(source, str(SOURCE), "exec"), namespace)  # noqa: S102 - Trusted source AST only.
    return namespace["run"], output


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_slash_reasoning_uses_reviewed_options_and_works_in_canonical(canonical):
    run, output = reasoning_shell()
    client = SimpleNamespace(_canonical_chat={"revision": "revision"} if canonical else None)
    result = await run([["/reasoning"], ["/reasoning", "high"], ["/reasoning", "medium"]], client)
    assert result[2] == "high"
    assert any("low, high, xhigh" in line for line in output)
    assert any("not reviewed" in line for line in output)
    result = await run([["/reasoning", "high"], ["/reasoning", "default"]], client)
    assert result[2] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model,expected", [("fixture/two", "high"), ("fixture/three", None)])
async def test_model_change_carries_only_supported_effort_and_announces_reset(model, expected):
    run, output = reasoning_shell()
    result = await run([["/reasoning", "high"], ["/model", model]], SimpleNamespace())
    assert result[1:3] == (model, expected)
    assert any("reset to provider default" in line for line in output) is (expected is None)


@pytest.mark.asyncio
async def test_provider_change_explicitly_resets_effort():
    run, output = reasoning_shell()
    result = await run([["/reasoning", "high"], ["/provider", "openai", "fixture/one"]], SimpleNamespace())
    assert result[0] == "openai" and result[2] is None
    assert any("reset to provider default" in line for line in output)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_mode", ["native", "disabled", "unavailable"])
async def test_real_stream_consumer_propagates_effort_without_emitting_reasoning(stream_mode):
    consumer = streaming.consumer.__wrapped__()
    sent = []
    async def stream(*args, **kwargs):
        sent.append(kwargs)
        if stream_mode == "unavailable":
            raise streaming.APIError(404, "unavailable")
        yield streaming.event("reasoning.delta", {"text": "private-not-for-rendering"})
        yield streaming.event("response.completed", {"status": "ok", "text": "answer"})
    consumer.client.ask_stream = stream
    if stream_mode == "disabled":
        consumer.namespace["_env_flag"] = lambda *_: False
    result = await consumer.namespace["_ask_with_optional_stream"](
        "hello", idempotency_key="same-key", enable_tools=False,
        enable_web_search=False, attachments=[], reasoning_effort="high",
    )
    assert "private-not-for-rendering" not in str(result)
    if sent:
        assert sent[0]["reasoning_effort"] == "high"
    if stream_mode != "native":
        assert consumer.client.ask.call_args.kwargs["reasoning_effort"] == "high"


def test_every_normal_tool_loop_request_passes_effort():
    tree = ast.parse(SOURCE.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_ask_with_optional_stream"]
    assert len(calls) >= 4
    assert all(any(kw.arg == "reasoning_effort" for kw in call.keywords) for call in calls)


@pytest.mark.asyncio
async def test_actual_switch_uses_remote_web_default_and_exact_model_snapshot():
    run, _ = reasoning_shell()
    client = SimpleNamespace()
    result = await run([["/model", "fixture/one"]], client)
    assert result[4] is True
    assert result[5].supports("openrouter", "fixture/one", "web_search")
    result = await run([["/model", "fixture/one"], ["/model", "fixture/two"]], client)
    assert result[4] is False
    assert result[5].model_id == "fixture/two"
    assert not result[5].supports("openrouter", "fixture/one", "web_search")


@pytest.mark.asyncio
async def test_failed_switch_keeps_previous_remote_model_and_effort():
    run, output = reasoning_shell({"fixture/two": {"reviewed": False}})
    result = await run([["/model", "fixture/one"], ["/reasoning", "high"], ["/model", "fixture/two"]], SimpleNamespace())
    assert result[1:3] == ("fixture/one", "high")
    assert result[5].model_id == "fixture/one"
    assert any("Model unchanged" in line for line in output)
