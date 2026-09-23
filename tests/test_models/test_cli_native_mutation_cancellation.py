"""Real CLI loop plus delayed real local writes; synthetic result API only.

Cancellation cannot kill a writer thread. An unknown observation must be sent
once; its eventual local success must never replace that authoritative outcome.
All files live in pytest's temporary workspace; the reused fixture blocks network
and subprocess calls before importing the production CLI.
"""
from __future__ import annotations

import asyncio
import gc
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.agent import native_mutation_client as mutation_client
from openvegas.client import APIError
from tests.test_models.test_cli_native_loop import (
    loop_driver as loop_driver,  # noqa: PLC0414 - pytest fixture export
)
from tests.test_models.test_cli_native_mutation_loop import edit, enable


async def _until(event: threading.Event, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        assert loop.time() < deadline, "fixture worker did not reach its bounded checkpoint"
        await asyncio.sleep(0.002)


@pytest.fixture
def delayed_writer(loop_driver, monkeypatch):
    def setup(*, late_error=False, reply="ok"):
        driver = loop_driver(batches=[[edit()], []])
        target = driver.root / "notes.txt"
        target.write_bytes(b"before\n")
        prepared, decisions, consumed = enable(driver, monkeypatch)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        executions, local_results, callbacks = [], [], []
        real_write = mutation_client.execute_native_mutation

        def delayed(*args):
            executions.append(1)
            entered.set()
            try:
                assert release.wait(8), "test must always release its local writer"
                if late_error:
                    raise OSError("fixture private worker error must not become unobserved")
                proof = real_write(*args)
                local_results.append(deepcopy(proof))
                return proof
            finally:
                finished.set()

        async def result(**request):
            callbacks.append(deepcopy(request))
            if reply == "accepted_409":
                # The real result endpoint persists failed/unknown and returns 409.
                raise APIError(409, "Tool execution failed.", data={"error":"tool_execution_failed"})
            if reply == "lost_ack":
                raise APIError(503, "fixture response lost")
            if reply == "hang":
                await asyncio.Event().wait()
            return {"run_version": 3, "current_state": "interrupted", "is_resumable": False}

        monkeypatch.setattr(mutation_client, "execute_native_mutation", delayed)
        driver.client.agent_tool_result = result
        driver.client.agent_tool_heartbeat = AsyncMock(return_value={"active":True,"status":"started"})
        return SimpleNamespace(driver=driver,target=target,prepared=prepared,decisions=decisions,
            consumed=consumed,entered=entered,release=release,finished=finished,executions=executions,
            local_results=local_results,callbacks=callbacks)
    return setup


def _assert_unknown_once(case):
    assert len(case.callbacks) == 1, "cancellation must report one unknown result, with no callback retry"
    request = case.callbacks[0]
    assert request["result_status"] == "failed"
    assert request["stdout"] == request["stderr"] == ""
    assert set(request["result_payload"]) == {"native_mutation_proof"}
    proof = request["result_payload"]["native_mutation_proof"]
    assert set(proof) == {"kind","contract_sha256","relative_path","observed_before","observed_after","outcome","reason"}
    assert proof["kind"] == "runtime_observed_file_v1"
    assert proof["outcome"] == "unknown" and proof["reason"] == "native_runtime_interrupted"
    assert proof["observed_before"] is None, "cancellation has no independently returned source observation"
    assert proof["observed_after"] is None, "no post-write observation exists while the worker is blocked"
    marker = case.driver.proposals[0]["arguments"]["native_mutation"]
    assert proof["contract_sha256"] == marker["contract_sha256"]
    assert proof["relative_path"] == marker["relative_path"]
    start = case.driver.starts[0]
    for key in ("run_id","runtime_session_id","tool_call_id","execution_token"):
        assert request[key] == start[key]
    assert len(case.driver.requests) == 1, "uncertain writes must not request provider continuation"
    assert not case.driver.executions, "native cancellation must not invoke the legacy executor"
    assert not case.driver.rendered
    assert len(case.executions) == len(case.prepared) == len(case.decisions) == len(case.consumed) == 1
    assert len(case.driver.starts) == 1


async def _release(case):
    case.release.set()
    await _until(case.finished)
    # Let the supervisor retrieve the worker's late result/exception.
    await asyncio.sleep(0.03)
    gc.collect()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["ok","accepted_409","lost_ack"])
async def test_caller_cancel_records_unknown_once_then_ignores_late_success(delayed_writer,reply):
    case = delayed_writer(reply=reply)
    task = asyncio.create_task(case.driver.loop(case.driver.client,"Replace notes.txt"))
    try:
        await _until(case.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,timeout=6)
        _assert_unknown_once(case)
        assert case.target.read_bytes() == b"before\n"
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    assert case.target.read_bytes() == b"after\r\nlast", "fixture proves the worker was not killed"
    assert case.local_results[0]["outcome"] == "applied"
    _assert_unknown_once(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_status", ["cancelled","timed_out"])
async def test_inactive_heartbeat_records_unknown_before_late_write(delayed_writer,remote_status):
    case = delayed_writer()
    case.driver.client.agent_tool_heartbeat.return_value = {"active":False,"status":remote_status}
    task = asyncio.create_task(case.driver.loop(case.driver.client,"Replace notes.txt"))
    try:
        await _until(case.entered)
        with pytest.raises(APIError):
            await asyncio.wait_for(task,timeout=6)
        assert case.driver.client.agent_tool_heartbeat.await_count == 1
        _assert_unknown_once(case)
        assert case.target.read_bytes() == b"before\n"
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    assert case.target.read_bytes() == b"after\r\nlast"
    _assert_unknown_once(case)


@pytest.mark.asyncio
async def test_late_worker_error_retrieved_not_reported_as_second_result(delayed_writer):
    case = delayed_writer(late_error=True)
    loop = asyncio.get_running_loop()
    errors=[]
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop,context:errors.append(context))
    task = asyncio.create_task(case.driver.loop(case.driver.client,"Replace notes.txt"))
    try:
        await _until(case.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,timeout=6)
        _assert_unknown_once(case)
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        loop.set_exception_handler(previous)
    assert not errors, "late writer exception escaped task supervision"
    assert case.target.read_bytes() == b"before\n"
    _assert_unknown_once(case)


@pytest.mark.asyncio
async def test_unknown_callback_hang_is_bounded_and_not_retried(delayed_writer):
    case = delayed_writer(reply="hang")
    task = asyncio.create_task(case.driver.loop(case.driver.client,"Replace notes.txt"))
    try:
        await _until(case.entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,timeout=6)
        _assert_unknown_once(case)
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    _assert_unknown_once(case)


@pytest.mark.asyncio
async def test_cancel_while_heartbeat_transport_waits_still_reports_unknown(delayed_writer):
    case = delayed_writer()
    heartbeat_entered = threading.Event()
    async def heartbeat(**_request):
        heartbeat_entered.set()
        await asyncio.Event().wait()
    case.driver.client.agent_tool_heartbeat = heartbeat
    task = asyncio.create_task(case.driver.loop(case.driver.client,"Replace notes.txt"))
    try:
        await _until(case.entered)
        await _until(heartbeat_entered,timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,timeout=6)
        _assert_unknown_once(case)
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    _assert_unknown_once(case)


@pytest.mark.asyncio
async def test_worker_exception_during_inactive_report_is_retrieved(delayed_writer):
    case = delayed_writer(late_error=True)
    case.driver.client.agent_tool_heartbeat.return_value = {"active":False,"status":"cancelled"}
    async def result(**request):
        case.callbacks.append(deepcopy(request))
        # The worker completes WITH AN ERROR while the reporting request awaits.
        case.release.set()
        await _until(case.finished)
        await asyncio.sleep(0.03)
        return {"current_state":"interrupted","is_resumable":False}
    case.driver.client.agent_tool_result = result
    loop = asyncio.get_running_loop()
    errors=[]
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop,context:errors.append(context))
    task = asyncio.create_task(case.driver.loop(case.driver.client,"Replace notes.txt"))
    try:
        with pytest.raises(APIError):
            await asyncio.wait_for(task,timeout=6)
        _assert_unknown_once(case)
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        loop.set_exception_handler(previous)
    assert not errors, "already-finished writer task exception was not consumed"
    assert case.target.read_bytes() == b"before\n"
    _assert_unknown_once(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat_hangs", [False, True])
async def test_local_deadline_bounds_writer_even_with_active_or_hung_heartbeat(delayed_writer, heartbeat_hangs):
    case = delayed_writer()
    if heartbeat_hangs:
        async def heartbeat(**_request):
            await asyncio.Event().wait()
        case.driver.client.agent_tool_heartbeat = heartbeat
    task = asyncio.create_task(case.driver.loop(case.driver.client, "Replace notes.txt"))
    try:
        await _until(case.entered)
        with pytest.raises(APIError):
            await asyncio.wait_for(task, timeout=7)
        _assert_unknown_once(case)
        assert case.target.read_bytes() == b"before\n"
    finally:
        await _release(case)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert case.target.read_bytes() == b"after\r\nlast", "deadline is not a claim that the OS write was killed"
    _assert_unknown_once(case)
