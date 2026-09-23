"""Real database proposal recovery, without executing a tool or calling a provider."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio

from openvegas.agent.orchestration_service import AgentOrchestrationService
from openvegas.contracts.errors import ContractError
from tests.integration.test_native_history_postgres import (
    callback,
    new_run,
    projection,
    propose,
    row_counts,
    scenario,
    seed_user,
    start,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def replay_database(database_factory):
    async with database_factory(through=44, max_size=4) as sandbox:
        yield sandbox


async def request_scope(service, run):
    return {**await projection(service, run), "idempotency_key": str(uuid.uuid4())}


async def test_lost_response_recovered_after_reconnect_without_second_proposal(replay_database):
    service, run, source = await scenario(replay_database)
    scope = await request_scope(service, run)
    first = await propose(service, run, source, **scope)
    assert first.status_code == 200
    before = await row_counts(replay_database.db)
    service = AgentOrchestrationService(await replay_database.reconnect())
    recovered = await propose(service, run, source, **scope)
    assert (recovered.status_code, recovered.payload) == (first.status_code, first.payload)
    assert await row_counts(replay_database.db) == before
    tool = first.payload["tool_request"]
    binding = await replay_database.db.fetchval(
        "SELECT content_json FROM agent_chat_turns WHERE tool_call_id=$1::uuid", tool["tool_call_id"]
    )
    serialized = binding if isinstance(binding, str) else json.dumps(binding)
    assert "execution_token" not in serialized
    assert tool["execution_token"] not in serialized
    assert await replay_database.db.fetchval(
        "SELECT status FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"]
    ) == "proposed"


async def test_concurrent_identical_proposals_recover_one_identity(replay_database):
    service, run, source = await scenario(replay_database)
    scope = await request_scope(service, run)
    tasks = []
    try:
        async with replay_database.db.transaction() as blocker:
            await blocker.fetchrow("SELECT id FROM agent_runs WHERE id=$1::uuid FOR UPDATE", run.run_id)
            tasks = [asyncio.create_task(propose(
                AgentOrchestrationService(replay_database.db), run, source, **scope
            )) for _ in range(2)]
            async with asyncio.timeout(5):
                while True:
                    await blocker.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await blocker.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
                        "AND query LIKE '%FROM agent_runs%FOR UPDATE%'"
                    )
                    if waiting == 2:
                        break
                    assert not any(t.done() for t in tasks)
                    await asyncio.sleep(0.01)
        first, second = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert first.status_code == second.status_code == 200
    assert first.payload == second.payload
    counts = await row_counts(replay_database.db)
    assert counts["tools"] == counts["turns"] == 1
    assert counts["events"] == 0
    await start(service, run, first.payload["tool_request"])
    claimed = await row_counts(replay_database.db)
    assert claimed["events"] == 1
    # Even a freshly loaded projection must not grant the second consumer a start.
    with pytest.raises(ContractError, match="already started; do not execute"):
        await start(service, run, second.payload["tool_request"])
    assert await row_counts(replay_database.db) == claimed


async def test_legacy_unbound_start_keeps_existing_idempotent_behavior(replay_database):
    service, run, source = await scenario(replay_database)
    proposal = await propose(
        service, run, source, native_inference_request_id=None, native_provider_call_id=None
    )
    tool = proposal.payload["tool_request"]
    await start(service, run, tool)
    before = await row_counts(replay_database.db)
    await start(service, run, tool)
    assert await row_counts(replay_database.db) == before


@pytest.mark.parametrize("change", [
    {"idempotency_key": "another-key"}, {"arguments": {"path": "different.txt"}},
    {"plan_mode": True}, {"timeout_sec": 1}, {"expected_run_version": 999},
    {"expected_valid_actions_signature": "another-signature"},
    {"native_provider_call_id": "another-native-call"},
])
async def test_changed_request_cannot_recover_original(replay_database, change):
    service, run, source = await scenario(replay_database)
    scope = await request_scope(service, run)
    first = await propose(service, run, source, **scope)
    assert first.status_code == 200
    before = await row_counts(replay_database.db)
    with pytest.raises(ContractError):
        await propose(service, run, source, **{**scope, **change})
    assert await row_counts(replay_database.db) == before


@pytest.mark.parametrize("state", ["started", "succeeded", "failed"])
async def test_replay_never_reissues_execution_authority_after_start(replay_database, state):
    service, run, source = await scenario(replay_database)
    scope = await request_scope(service, run)
    first = await propose(service, run, source, **scope)
    tool = first.payload["tool_request"]
    await start(service, run, tool)
    if state != "started":
        await callback(service, run, tool, status=state)
    before = await row_counts(replay_database.db)
    with pytest.raises(ContractError):
        await propose(service, run, source, **scope)
    assert await row_counts(replay_database.db) == before
    assert await replay_database.db.fetchval(
        "SELECT status FROM agent_run_tool_calls WHERE id=$1::uuid", tool["tool_call_id"]
    ) == state


@pytest.mark.parametrize("wrong_scope", ["user", "run", "session"])
async def test_replay_cannot_cross_owner_run_or_runtime(replay_database, wrong_scope):
    service, run, source = await scenario(replay_database)
    scope = await request_scope(service, run)
    first = await propose(service, run, source, **scope)
    assert first.status_code == 200
    other_user = await seed_user(replay_database.db)
    other_run = await new_run(service, run.user_id)
    overrides = {
        "user": {"user_id": other_user}, "run": {"run_id": other_run.run_id},
        "session": {"runtime_session_id": str(uuid.uuid4())},
    }[wrong_scope]
    before = await row_counts(replay_database.db)
    with pytest.raises(ContractError):
        await propose(service, run, source, **{**scope, **overrides})
    assert await row_counts(replay_database.db) == before


@pytest.mark.parametrize("condition", ["cancel", "expired", "completed"])
async def test_replay_denied_for_inactive_run(replay_database, condition):
    service, run, source = await scenario(replay_database)
    scope = await request_scope(service, run)
    assert (await propose(service, run, source, **scope)).status_code == 200
    if condition == "cancel":
        await replay_database.db.execute(
            "UPDATE agent_runs SET cancel_requested_at=now() WHERE id=$1::uuid", run.run_id
        )
    elif condition == "expired":
        await replay_database.db.execute(
            "UPDATE agent_runs SET expires_at=now()-interval '1 second' WHERE id=$1::uuid", run.run_id
        )
    else:
        await replay_database.db.execute(
            "UPDATE agent_runs SET state='completed',state_reason_code='completed_noop' "
            "WHERE id=$1::uuid", run.run_id
        )
    before = await row_counts(replay_database.db)
    with pytest.raises(ContractError):
        await propose(service, run, source, **scope)
    assert await row_counts(replay_database.db) == before
