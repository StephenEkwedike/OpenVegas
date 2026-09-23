"""Real PostgreSQL run/tool lock races; no provider or local tool is executed."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.agent.runtime_contracts import tool_payload_hash

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def reconciliation_database(database_factory):
    async with database_factory(through=44, max_size=4) as sandbox:
        yield sandbox.db


class PausedDatabase:
    """Pause after actual SQL, retaining the real transaction and its row locks."""

    def __init__(self, db, *, after):
        self.db = db
        self.after = after
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def transaction(self):
        async with self.db.transaction() as tx:
            yield PausedTransaction(tx, self)


class PausedTransaction:
    def __init__(self, tx, pause):
        self.tx = tx
        self.pause = pause

    def __getattr__(self, name):
        return getattr(self.tx, name)

    async def _after(self, query, result):
        sql = " ".join(query.split())
        matches = (
            self.pause.after == "run_lock"
            and "FROM agent_runs" in sql and "FOR UPDATE" in sql
        ) or (
            self.pause.after == "candidates"
            and "FROM agent_run_tool_calls" in sql and "last_heartbeat_at" in sql
        )
        if result and matches and not self.pause.reached.is_set():
            self.pause.reached.set()
            await self.pause.release.wait()
        return result

    async def fetchrow(self, query, *args):
        return await self._after(query, await self.tx.fetchrow(query, *args))

    async def fetch(self, query, *args):
        return await self._after(query, await self.tx.fetch(query, *args))


async def seed_started_tool(db, *, null_heartbeat=False):
    user_id, run_id, session_id, tool_id = (str(uuid.uuid4()) for _ in range(4))
    token = uuid.uuid4().hex
    request = {"tool_name": "fs_read", "arguments": {"path": "fixture.txt"},
               "shell_mode": "read_only", "timeout_sec": 5}
    await db.execute("INSERT INTO auth.users(id) VALUES ($1::uuid)", user_id)
    await db.execute(
        "INSERT INTO agent_runs(id,user_id,state,runtime_session_id) "
        "VALUES ($1::uuid,$2::uuid,'running',$3::uuid)", run_id, user_id, session_id,
    )
    await db.execute(
        """INSERT INTO agent_run_tool_calls
           (id,run_id,run_version,tool_name,tool_class,payload_hash,request_payload_json,
            execution_token,status,claimed_at,started_at,last_heartbeat_at)
           VALUES ($1::uuid,$2::uuid,0,'fs_read','read_only',$3,$4::jsonb,$5,'started',
                   now()-interval '10 minutes',now()-interval '10 minutes',
                   CASE WHEN $6::boolean THEN NULL ELSE now()-interval '10 minutes' END)""",
        tool_id, run_id, tool_payload_hash("fs_read", request["arguments"], "read_only"),
        json.dumps(request), token, null_heartbeat,
    )
    return {"user_id": user_id, "run_id": run_id, "runtime_session_id": session_id,
            "tool_call_id": tool_id, "execution_token": token}


async def complete_tool(service, scope):
    return await service.result_tool_call(
        **scope, actor_role="user", result_status="succeeded", result_payload={"ok": True},
        stdout="synthetic result", stderr="",
    )


async def status(db, scope):
    return await db.fetchval(
        "SELECT status FROM agent_run_tool_calls WHERE id=$1::uuid", scope["tool_call_id"]
    )


async def timeout_events(db):
    return await db.fetch(
        "SELECT run_id,event_seq,payload FROM agent_run_events "
        "WHERE event_type='tool_finished_timed_out' ORDER BY run_id,event_seq"
    )


async def cleanup(task, pause):
    pause.release.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("null_heartbeat", [False, True])
async def test_callback_run_is_skipped_without_tool_lock_and_other_run_progresses(
    reconciliation_database, null_heartbeat,
):
    db = reconciliation_database
    busy = await seed_started_tool(db, null_heartbeat=null_heartbeat)
    available = await seed_started_tool(db, null_heartbeat=null_heartbeat)
    pause = PausedDatabase(db, after="run_lock")
    callback = asyncio.create_task(complete_tool(AgentOrchestrationService(pause), busy))
    try:
        await asyncio.wait_for(pause.reached.wait(), 2)
        # The real callback owns the run but has not attempted its tool lock yet.
        # The old reconciler locks that tool, then waits for this run indefinitely.
        reconciler = AgentOrchestrationService(db)
        assert await asyncio.wait_for(
            reconciler.reconcile_stale_started_tools(timeout_seconds=5), 2
        ) == 1
        assert await status(db, busy) == "started"
        assert await status(db, available) == "timed_out"
        async with db.transaction() as observer:
            assert await observer.fetchval(
                "SELECT id FROM agent_run_tool_calls WHERE id=$1::uuid FOR UPDATE NOWAIT",
                busy["tool_call_id"],
            )
        pause.release.set()
        assert (await asyncio.wait_for(callback, 2)).status_code == 200
        assert await status(db, busy) == "succeeded"
        events = await timeout_events(db)
        assert len(events) == 1 and str(events[0]["run_id"]) == available["run_id"]
        assert await reconciler.reconcile_stale_started_tools(timeout_seconds=5) == 0
    finally:
        await cleanup(callback, pause)


@pytest.mark.parametrize("change", ["heartbeat", "result"])
async def test_candidate_snapshot_is_rechecked_after_concurrent_callback(
    reconciliation_database, change,
):
    db = reconciliation_database
    scope = await seed_started_tool(db, null_heartbeat=True)
    pause = PausedDatabase(db, after="candidates")
    task = asyncio.create_task(
        AgentOrchestrationService(pause).reconcile_stale_started_tools(timeout_seconds=5)
    )
    try:
        await asyncio.wait_for(pause.reached.wait(), 2)
        service = AgentOrchestrationService(db)
        if change == "heartbeat":
            result = await asyncio.wait_for(service.heartbeat_tool_call(**scope), 2)
            assert result["active"] is True
        else:
            assert (await asyncio.wait_for(complete_tool(service, scope), 2)).status_code == 200
        pause.release.set()
        assert await asyncio.wait_for(task, 2) == 0
        assert await status(db, scope) == ("started" if change == "heartbeat" else "succeeded")
        assert await timeout_events(db) == []
    finally:
        await cleanup(task, pause)


async def test_overlapping_reconcilers_terminalize_each_run_once(reconciliation_database):
    db = reconciliation_database
    scopes = [await seed_started_tool(db), await seed_started_tool(db, null_heartbeat=True)]
    pause = PausedDatabase(db, after="run_lock")
    first = asyncio.create_task(
        AgentOrchestrationService(pause).reconcile_stale_started_tools(timeout_seconds=5)
    )
    try:
        await asyncio.wait_for(pause.reached.wait(), 2)
        second = AgentOrchestrationService(db)
        assert await asyncio.wait_for(second.reconcile_stale_started_tools(timeout_seconds=5), 2) == 1
        pause.release.set()
        assert await asyncio.wait_for(first, 2) == 1
        assert [await status(db, scope) for scope in scopes] == ["timed_out", "timed_out"]
        events = await timeout_events(db)
        assert {str(event["run_id"]) for event in events} == {scope["run_id"] for scope in scopes}
        assert len(events) == 2 and all(event["event_seq"] == 1 for event in events)
        assert await second.reconcile_stale_started_tools(timeout_seconds=5) == 0
        assert await timeout_events(db) == events
    finally:
        await cleanup(first, pause)
