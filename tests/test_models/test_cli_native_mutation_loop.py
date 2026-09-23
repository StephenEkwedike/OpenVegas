"""Actual CLI loop and POSIX file writes; synthetic API, no provider or network."""
from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from openvegas.agent.native_mutation import build_mutation_plan
from openvegas.agent.native_mutation_service import _public
from openvegas.client import APIError
from tests.test_models.test_cli_native_loop import (
    loop_driver as loop_driver,  # noqa: PLC0414 - pytest fixture export
)
from tests.test_models.test_cli_native_loop import tool


def edit(name="Write", **args):
    return {"type": "tool_call", **tool(name, args or {
        "filepath": "notes.txt", "content": "after\r\nlast", "write_mode": "replace"}, mode="mutating")}


def enable(driver, monkeypatch, *, approve=True, corrupt=False, changed=False):
    monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS", "1")
    prepared, decisions, consumed = [], [], []

    async def prepare(**request):
        prepared.append(deepcopy(request))
        call = deepcopy(driver.pending[request["native_provider_call_id"]])
        call.pop("native_inference_request_id")
        plan = build_mutation_plan(call, request["observed_source"])
        return _public({"id": str(uuid4()), "expires_at": datetime.now(UTC) + timedelta(minutes=9)}, plan)

    async def decision(**request):
        decisions.append(deepcopy(request))
        return {"approval_id": str(uuid4())}

    async def consume(**request):
        consumed.append(deepcopy(request))
        return {"run_version": request["expected_run_version"] + 1,
                "valid_actions_signature": "sha256:" + "c" * 64}

    def confirm(*_a, **_kw):
        if changed:
            (driver.root / "notes.txt").write_text("user edit")
        return approve

    driver.namespace["click"] = SimpleNamespace(confirm=confirm)
    driver.client.agent_native_mutation_prepare = prepare
    driver.client.agent_native_mutation_approve = decision
    driver.client.agent_approval_consume = consume
    if changed:
        async def failed_callback(**kwargs):
            assert kwargs["result_status"] == "failed"
            assert kwargs["result_payload"]["native_mutation_proof"]["outcome"] == "not_applied"
            driver.callbacks.append(deepcopy(kwargs))
            return {}

        driver.client.agent_tool_result = failed_callback
    if corrupt:
        original = driver.client.agent_tool_propose

        async def bad_proposal(**kwargs):
            response = await original(**kwargs)
            response["tool_request"]["arguments"]["patch"] += "corrupted"
            return response

        driver.client.agent_tool_propose = bad_proposal
    return prepared, decisions, consumed


@pytest.mark.asyncio
async def test_real_cli_prepares_approves_writes_and_continues_once(loop_driver, monkeypatch):
    driver = loop_driver(batches=[[edit()], []])
    path = driver.root / "notes.txt"
    path.write_bytes(b"before\n")
    prepared, decisions, consumed = enable(driver, monkeypatch)
    assert await driver.run("Replace notes.txt exactly") is True
    assert path.read_bytes() == b"after\r\nlast"
    assert len(prepared) == len(decisions) == len(consumed) == len(driver.starts) == len(driver.callbacks) == 1
    assert driver.starts[0]["expected_run_version"] == 2
    assert not driver.executions, "Native write reached the legacy tool executor"
    assert driver.callbacks[0]["result_payload"]["native_mutation_proof"]["outcome"] == "applied"
    assert len(driver.requests) == 2 and driver.rendered == ["FINAL ANSWER"]


@pytest.mark.asyncio
async def test_same_file_batch_captures_after_preceding_write_not_before(loop_driver, monkeypatch):
    driver = loop_driver(batches=[[edit(content="one", filepath="notes.txt", write_mode="replace"),
                                  edit("InsertAtEnd", filepath="notes.txt", content="two")], []])
    (driver.root / "notes.txt").write_text("original")
    prepared, decisions, consumed = enable(driver, monkeypatch)
    assert await driver.run("Write one then append two") is True
    assert [p["observed_source"]["content_utf8"] for p in prepared] == ["original", "one"]
    assert (driver.root / "notes.txt").read_bytes() == b"onetwo"
    assert len(decisions) == len(consumed) == 2
    assert len(driver.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["denied", "changed", "corrupt", "ack_lost", "disabled", "plan", "excluded"])
async def test_native_mutation_never_falls_back_or_reexecutes(loop_driver, monkeypatch, failure):
    driver = loop_driver(batches=[[edit()], []],
                         callback_failure_call="call-1-0" if failure == "ack_lost" else None)
    path = driver.root / "notes.txt"
    path.write_bytes(b"before\n")
    prepared, decisions, consumed = enable(driver, monkeypatch,
        approve=failure != "denied", corrupt=failure == "corrupt", changed=failure == "changed")
    if failure == "disabled":
        monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS", "0")
    if failure == "plan":
        driver.namespace["plan_mode"] = True
    if failure == "excluded":
        driver.namespace["approval_mode"] = "exclude"
    with pytest.raises(APIError):
        await driver.run("Replace notes.txt")
    assert not driver.executions and len(driver.requests) == 1 and not driver.rendered
    if failure == "ack_lost":
        assert path.read_bytes() == b"after\r\nlast" and len(driver.starts) == 1
    elif failure == "changed":
        assert path.read_bytes() == b"user edit"
        assert driver.callbacks[0]["result_payload"]["native_mutation_proof"]["outcome"] == "not_applied"
    else:
        assert path.read_bytes() == b"before\n" and not driver.starts
        assert not decisions and not consumed
    if failure in {"disabled", "plan", "excluded"}:
        assert not prepared


@pytest.mark.asyncio
async def test_start_conflict_does_not_recapture_reapprove_or_consume_twice(loop_driver, monkeypatch):
    driver = loop_driver(batches=[[edit()], []], start_conflict_call="call-1-0")
    (driver.root / "notes.txt").write_text("before")
    prepared, decisions, consumed = enable(driver, monkeypatch)
    assert await driver.run("Replace notes.txt") is True
    assert len(prepared) == len(decisions) == len(consumed) == len(driver.starts) == len(driver.callbacks) == 1
    assert len(driver.start_attempts) == 5 and not driver.executions


@pytest.mark.asyncio
async def test_unsafe_source_gives_bounded_chat_error_not_uncaught_exception(loop_driver, monkeypatch):
    driver = loop_driver(batches=[[edit()], []])
    os.mkfifo(driver.root / "notes.txt")
    prepared, _, _ = enable(driver, monkeypatch)
    with pytest.raises(APIError, match="preparation validation failed"):
        await driver.run("Replace notes.txt")
    assert not prepared and not driver.starts and len(driver.requests) == 1


@pytest.mark.asyncio
async def test_unknown_worker_error_stops_without_fallback(loop_driver, monkeypatch):
    from openvegas.agent import native_mutation_client
    from openvegas.contracts.errors import APIErrorCode, ContractError

    driver = loop_driver(batches=[[edit()], []])
    (driver.root / "notes.txt").write_text("before")
    enable(driver, monkeypatch)

    async def unknown(*_args):
        raise ContractError(APIErrorCode.MUTATION_UNCERTAIN, "Native edit result unavailable.")

    monkeypatch.setattr(native_mutation_client, "execute_prepared_mutation", unknown)
    async def unknown_callback(**kwargs):
        assert kwargs["result_status"] == "failed"
        assert kwargs["result_payload"]["native_mutation_proof"]["outcome"] == "unknown"
        driver.callbacks.append(deepcopy(kwargs))
        return {}

    driver.client.agent_tool_result = unknown_callback
    with pytest.raises(APIError, match="unavailable") as error:
        await driver.run("Replace notes.txt")
    assert error.value.data["error"] == "mutation_uncertain"
    assert len(driver.starts) == 1 and len(driver.callbacks) == 1 and not driver.executions
    assert len(driver.requests) == 1


@pytest.mark.asyncio
async def test_cancelled_worker_is_explicitly_uncertain_and_never_continues(loop_driver, monkeypatch):
    from openvegas.agent import native_mutation_client

    driver = loop_driver(batches=[[edit()], []])
    (driver.root / "notes.txt").write_text("before")
    enable(driver, monkeypatch)
    entered = asyncio.Event()

    async def wait_forever(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(native_mutation_client, "execute_prepared_mutation", wait_forever)
    async def unknown_callback(**kwargs):
        assert kwargs["result_status"] == "failed"
        assert kwargs["result_payload"]["native_mutation_proof"]["outcome"] == "unknown"
        driver.callbacks.append(deepcopy(kwargs))
        return {}

    driver.client.agent_tool_result = unknown_callback
    task = asyncio.create_task(driver.loop(driver.client, "Replace notes.txt"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any("uncertain" in note for note in driver.notes)
    assert len(driver.requests) == 1 and len(driver.callbacks) == 1 and not driver.executions
