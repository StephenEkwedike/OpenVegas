from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.tui.model_picker import ModelSelectionError


def command_branch():
    tree = ast.parse((Path(__file__).parents[2] / "openvegas/cli.py").read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "cmd"
        and isinstance(node.test.comparators[0], ast.Set)
        and {value.value for value in node.test.comparators[0].elts}
        == {"/models", "/provider", "/model"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["transfer", "cancel", "stale", "pending"])
async def test_cli_canonical_switch_commits_only_after_confirmation(scenario):
    block = ast.unparse(command_branch())
    source = """async def run(client):
    current_provider, current_model, current_thread_id = 'openai', 'old', 'source'
    allow_model_switch, startup_bootstrap_task = True, None
    pending_attachments, chat_transcript = [], [{'role': 'user'}]
    native_generation_session = SimpleNamespace(history_active=False)
    model_switch_local_tools = SimpleNamespace(_BACKGROUND_JOBS=jobs)
"""
    tree = ast.parse((Path(__file__).parents[2] / "openvegas/cli.py").read_text())
    helpers = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name in {"_reasoning_status", "_reasoning_for_model", "_use_model_capabilities", "_chat_capability"}]
    source += "    current_reasoning_effort, current_reasoning_efforts, current_model_capabilities = None, (), None\n"
    source += "\n".join("    " + line for helper in helpers for line in ast.unparse(helper).splitlines())
    source += "\n    for cmd, parts in [('/provider', ['/provider', 'mistral', 'reviewed-test'])]:\n"
    source += "\n".join("        " + line for line in block.splitlines())
    source += "\n    return current_provider, current_model, current_thread_id\n"
    client = SimpleNamespace(_canonical_chat={"revision": "before"}, _request=AsyncMock())
    client._request.side_effect = [
        {"status": "ready", "revision": "reviewed"},
        RuntimeError("stale revision")
        if scenario == "stale"
        else {"context_transferred": True, "thread_id": "fork", "revision": "reviewed"},
    ]
    namespace = {
        "asyncio": asyncio,
        "SimpleNamespace": SimpleNamespace,
        "jobs": {"running": SimpleNamespace(process=SimpleNamespace(returncode=None))}
        if scenario == "pending"
        else {},
        "console": SimpleNamespace(print=lambda *a, **k: None),
        "APIError": RuntimeError,
        "ModelSelectionError": ModelSelectionError,
        "validate_selection": AsyncMock(
            return_value={"provider": "mistral", "model_id": "reviewed-test"}
        ),
        "Confirm": SimpleNamespace(ask=lambda *a, **k: scenario != "cancel"),
        "_chat_modal": AsyncMock(side_effect=lambda callback: callback()),
    }
    exec(compile(source, "trusted_cli_continuity_branch", "exec"), namespace)  # noqa: S102 - Trusted repository AST tests the exact CLI branch.
    result = await namespace["run"](client)
    if scenario == "transfer":
        assert result == ("mistral", "reviewed-test", "fork")
        assert client._request.await_count == 2
        assert client._request.call_args.kwargs["json"]["expected_revision"] == "reviewed"
    else:
        assert result == ("openai", "old", "source")
        assert client._canonical_chat == {"revision": "before"}
    assert all("/ask" not in args.args[1] for args in client._request.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['text', 'attachment', 'tool', 'blocked', 'error'])
async def test_cli_canonical_turn_bypasses_tool_loop_and_has_no_paid_fallback(scenario):
    tree = ast.parse((Path(__file__).parents[2] / 'openvegas/cli.py').read_text())
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == '_run_tool_loop')
    index = next(i for i, node in enumerate(function.body) if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'canonical_chat' for t in node.targets))
    fragment = '\n'.join(ast.unparse(node) for node in function.body[index:index + 2])
    source = 'async def run(client, pending_attachments, user_message):\n' + '\n'.join('    ' + line for line in fragment.splitlines())
    client = SimpleNamespace(_canonical_chat={'revision': None if scenario == 'blocked' else 'before'}, _request=AsyncMock(return_value={'text': 'answer', 'revision': 'after', 'v_cost': '1'}))
    if scenario == 'error':
        client._request.side_effect = RuntimeError('uncertain request')
    finish = []
    namespace = {
        'APIError': RuntimeError, 'current_provider': 'openai', 'current_model': 'reviewed-test',
        'current_reasoning_effort': None,
        'current_thread_id': 'source', '_has_workspace_tooling_intent': lambda _: scenario == 'tool',
        'uuid': __import__('uuid'), 'console': SimpleNamespace(print=lambda *a, **k: None),
        'render_assistant': lambda *a: None, '_render_usage_summary': lambda *_: None,
        'emote_turn': 'logical-turn', 'emote_bridge': SimpleNamespace(finish=lambda **k: finish.append(k)),
    }
    validator = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == '_validate_openrouter_request')
    exec(compile(ast.Module(body=[validator], type_ignores=[]), 'trusted_cli_validator', 'exec'), namespace)  # noqa: S102 - Actual CLI validator.
    exec(compile(source, 'trusted_cli_canonical_turn', 'exec'), namespace)  # noqa: S102 - Exact trusted repository AST, not caller-supplied code.
    if scenario == 'text':
        assert await namespace['run'](client, [], 'plain question')
        client._request.assert_awaited_once()
        assert client._request.call_args.args == ('POST', '/models/conversations/ask')
        assert client._request.call_args.kwargs['json']['prompt'] == 'plain question'
        assert client._canonical_chat['revision'] == 'after'
        assert finish == [{'success': True, 'turn': 'logical-turn'}]
    else:
        with pytest.raises(RuntimeError):
            await namespace['run'](client, ['file'] if scenario == 'attachment' else [], 'question')
        assert client._request.await_count == (1 if scenario == 'error' else 0)
        assert not finish
