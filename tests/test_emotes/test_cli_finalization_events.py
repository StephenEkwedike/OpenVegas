"""Production CLI branches plus real bridge; synthetic transport, no native UX claim."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openvegas.client import APIError
from openvegas.emotes.bridge import ChatEmoteBridge
from openvegas.emotes.compositor import TurnCancelled
from openvegas.emotes.events import Phase
from tests.test_models import test_cli_native_loop as native

loop_driver = native.loop_driver
READ_LIST = native.READ_LIST

SOURCE = Path(__file__).resolve().parents[2] / "openvegas" / "cli.py"
STATUS_VALUES = {"null": None, "unknown": "unknown", "failed": "failed",
                 "cancelled": "cancelled", "list": ["complete"], "object": {"status": "complete"},
                 "boolean": True}


def _bridge(driver):
    events = []

    def publish(event):
        events.append(event)
        return True

    bridge = ChatEmoteBridge(driver.runtime_id, publish=publish)
    driver.namespace["emote_bridge"] = bridge
    return bridge, events


def _outer_turn(driver, bridge, *, message="Read a.txt and list files"):
    """Keep production begin/supervision/APIError/finally logic unchanged."""
    tree = ast.parse(SOURCE.read_text())
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
               and node.body and isinstance(node.body[0], ast.Assign)
               and ast.unparse(node.body[0]) == "active_emote_turn = emote_bridge.begin()"]
    assert len(matches) == 1
    wrapper = ast.parse("async def run():\n    active_emote_turn = None\n    for _ in [0]:\n        pass\n")
    wrapper.body[0].body[1].body = matches
    namespace = {
        **driver.namespace, "emote_bridge": bridge, "owned_compositor": None,
        "_run_tool_loop": lambda *_: driver.run(message), "message": message,
        "last_assistant_text_for_turn": "", "chat_transcript": [],
        "_schedule_low_balance_hint": AsyncMock(), "TurnCancelled": TurnCancelled,
        "APIError": APIError, "active_emote_turn": None,
    }
    exec(compile(ast.fix_missing_locations(wrapper), str(SOURCE), "exec"), namespace)  # noqa: S102
    return namespace["run"]


def _terminal(events):
    return [event.phase for event in events if event.phase in {Phase.COMPLETE, Phase.ERROR, Phase.CANCEL}]


def _force_finalizer(driver, turn):
    names = {"_force_finalize", "_emote_response_is_final"}
    nodes = [n for n in ast.walk(ast.parse(SOURCE.read_text()))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {node.name for node in nodes} == names
    wrapper = ast.parse("def factory():\n    last_assistant_text_for_turn = ''\n    return _force_finalize\n")
    wrapper.body[0].body[1:1] = nodes
    namespace = {**driver.namespace, "emote_turn": turn}
    exec(compile(ast.fix_missing_locations(wrapper), str(SOURCE), "exec"), namespace)  # noqa: S102
    return namespace["factory"]()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [None, "call-1-1"])
async def test_native_tools_and_retries_do_not_complete_before_one_final_answer(loop_driver, conflict):
    driver = loop_driver(conflict_call=conflict)
    bridge, events = _bridge(driver)
    original_ask, original_callback = driver.client.ask, driver.client.agent_tool_result

    async def ask(*args, **kwargs):
        assert not _terminal(events)
        return await original_ask(*args, **kwargs)

    async def callback(**kwargs):
        result = await original_callback(**kwargs)
        assert not _terminal(events), "An accepted tool result is not final success"
        return result

    driver.client.ask, driver.client.agent_tool_result = ask, callback
    await _outer_turn(driver, bridge)()
    assert len(driver.callbacks) == 2 and len(driver.requests) == 2
    assert _terminal(events) == [Phase.COMPLETE]
    assert not bridge.finish(success=True, turn=bridge.current_turn)
    assert not bridge.cancel(turn=bridge.current_turn)
    assert _terminal(events) == [Phase.COMPLETE]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "cancel", "partial", "missing", "null", "unknown", "callback_failure", "tool_cap"])
async def test_native_unfinished_paths_never_complete(loop_driver, outcome):
    driver = loop_driver(batches=[READ_LIST] * 4 if outcome == "tool_cap" else [READ_LIST, []],
                         callback_failure_call="call-1-1" if outcome == "callback_failure" else None)
    bridge, events = _bridge(driver)
    original = driver.client.ask

    async def ask(*args, **kwargs):
        if driver.requests and outcome == "failure":
            raise APIError(503, "synthetic final failure")
        if driver.requests and outcome == "cancel":
            raise asyncio.CancelledError
        result = await original(*args, **kwargs)
        if not result["tool_calls"] and outcome == "partial":
            result["completion_status"] = "incomplete"
        elif not result["tool_calls"] and outcome == "missing":
            result.pop("completion_status")
        elif not result["tool_calls"] and outcome in {"null", "unknown"}:
            result["completion_status"] = None if outcome == "null" else "unknown"
        return result

    driver.client.ask = ask
    run = _outer_turn(driver, bridge)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run()
    else:
        await run()
    expected = Phase.CANCEL if outcome in {"cancel", "tool_cap"} else Phase.ERROR
    assert _terminal(events) == [expected]
    assert not driver.rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "legacy_tools", "canonical"])
@pytest.mark.parametrize("outcome", ["success", "empty", "partial", "missing", *STATUS_VALUES, "failure", "cancel"])
async def test_non_native_finalization_requires_confirmed_nonempty_success(loop_driver, kind, outcome):
    driver = loop_driver(batches=[])
    bridge, events = _bridge(driver)
    requests, usage = [], []
    driver.namespace["_render_usage_summary"] = usage.append
    driver.namespace["_env_flag"] = lambda name, default: False if name in {
        "OPENVEGAS_CHAT_NATIVE_GENERATION_SCOPE", "OPENVEGAS_CHAT_NATIVE_GENERATION_HISTORY",
        "OPENVEGAS_CHAT_STREAM_EVENTS",
    } else default == "1"

    async def answer(*args, **kwargs):
        requests.append(kwargs)
        assert not _terminal(events)
        if outcome == "failure":
            raise APIError(503, "synthetic final failure")
        if outcome == "cancel":
            raise asyncio.CancelledError
        result = {"text": "" if outcome == "empty" else "Synthetic final answer",
                "status": "ok", "completion_status": "incomplete" if outcome == "partial" else "complete",
                "tool_calls": [], "revision": "next", "v_cost": "0.25"}
        if outcome == "missing":
            result.pop("completion_status")
        elif outcome in STATUS_VALUES:
            result["completion_status"] = STATUS_VALUES[outcome]
        return result

    driver.client.ask = answer
    if kind == "canonical":
        driver.client._canonical_chat = {"revision": "before"}
        driver.client._request = answer
    if kind == "legacy_tools":
        # Select local-tool routing without inventing a file-reading completion
        # criterion: this exercises the production step-zero text-only return.
        driver.namespace["_has_workspace_tooling_intent"] = lambda _: True
    message = "Hello"
    run = _outer_turn(driver, bridge, message=message)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run()
    else:
        await run()
    assert len(requests) == 1, "Finalization must not add an inference or retry"
    if outcome not in {"empty", "failure", "cancel"}:
        assert driver.rendered == ["Synthetic final answer"]
        assert len(usage) == 1 and usage[0]["v_cost"] == "0.25"
        if kind == "canonical":
            assert driver.client._canonical_chat["revision"] == "next"
    if outcome in {"success", "missing", "null"}:
        assert _terminal(events) == [Phase.COMPLETE]
    else:
        assert Phase.COMPLETE not in _terminal(events)
        assert len(_terminal(events)) == 1
        if outcome == "partial" or outcome in STATUS_VALUES:
            assert _terminal(events) == [Phase.ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,success", [
    ("completed", True), ("spurious_mutation_block_ignored", True), ("duplicate_suppressed", True),
    ("completion_criteria_unmet_after_retries", False), ("max_iterations", False), ("unknown", False),
])
@pytest.mark.parametrize("text", ["Synthetic final answer", ""])
@pytest.mark.parametrize("status", ["complete", "incomplete", "missing", *STATUS_VALUES.values()])
async def test_force_finalizer_reason_and_empty_text_gates(loop_driver, reason, success, text, status):
    driver = loop_driver(batches=[])
    bridge, events = _bridge(driver)
    turn = bridge.begin()
    finalize = _force_finalizer(driver, turn)
    result = {"text": text, **({"completion_status": status} if status != "missing" else {})}
    await finalize(result, reason=reason)
    await finalize(result, reason=reason)
    bridge.cancel(turn=turn)
    accepted = status is None or (type(status) is str and status in {"complete", "missing"})
    assert _terminal(events) == [Phase.COMPLETE if success and text and accepted else Phase.ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "canonical", "forced"])
@pytest.mark.parametrize("status", ["missing", None, "complete"])
async def test_pending_tool_calls_never_signal_final_success(loop_driver, kind, status):
    driver = loop_driver(batches=[])
    bridge, events = _bridge(driver)
    usage = []
    driver.namespace["_render_usage_summary"] = usage.append
    driver.namespace["_env_flag"] = lambda *_: False
    result = {"text": "Tool preamble", "v_cost": "0.25", "revision": "next",
              "tool_calls": [native.tool("Read", {"path": "a.txt"})]}
    if status != "missing":
        result["completion_status"] = status
    answer = AsyncMock(return_value=result)
    driver.client.ask = answer
    if kind == "forced":
        turn = bridge.begin()
        await _force_finalizer(driver, turn)(result)
        bridge.cancel(turn=turn)
        answer.assert_not_awaited()
    else:
        if kind == "canonical":
            driver.client._canonical_chat = {"revision": "before"}
            driver.client._request = answer
        await _outer_turn(driver, bridge, message="Hello")()
        answer.assert_awaited_once()
    assert _terminal(events) == [Phase.ERROR]
    assert driver.rendered == ["Tool preamble"]
    assert usage == [result]
    assert not driver.executions and not driver.callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "legacy_tools"])
@pytest.mark.parametrize("first,last", [("complete", "incomplete"), ("incomplete", "complete")])
async def test_existing_web_retry_finalizes_from_latest_response_only(loop_driver, kind, first, last):
    driver = loop_driver(batches=[])
    bridge, events = _bridge(driver)
    driver.outer_cell("web_search_requested").cell_contents = True
    driver.namespace.update({
        "_env_flag": lambda *_: False,
        "_has_workspace_tooling_intent": lambda _: kind == "legacy_tools",
        "_should_enable_web_search_for_turn": lambda *a, **k: True,
        "_is_scrape_request": lambda _: True,
        "_is_scrape_refusal_text": lambda _: True,
        "_render_capability_status": lambda *a, **k: None,
    })
    driver.client.ask = AsyncMock(side_effect=[
        {"text": "refusal fixture", "completion_status": first, "tool_calls": []},
        {"text": "Final retry output", "completion_status": last, "tool_calls": []},
    ])
    await _outer_turn(driver, bridge, message="Hello")()
    assert driver.client.ask.await_count == 2
    assert driver.rendered == ["Final retry output"]
    assert _terminal(events) == [Phase.COMPLETE if last == "complete" else Phase.ERROR]


def test_finish_site_inventory_requires_explicit_coverage_updates():
    tree = ast.parse(SOURCE.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "finish"
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "emote_bridge"]
    # Canonical, forced finalizer, direct, native, legacy text, outer API error.
    assert len(calls) == 6, "New production finalization path needs regression coverage"
