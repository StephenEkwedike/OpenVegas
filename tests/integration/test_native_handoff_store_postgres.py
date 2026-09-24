"""Private storage only: real guarded PostgreSQL, synthetic settled source.

The coordinator must run SQL cases through the existing owned disposable DB
harness. No new HTTP route, provider dispatch, CLI, native UX or billing claim.
The small validation/gate tests do not use a DB and can run offline.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from tests.integration.test_native_continuation_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_continuation_postgres import payload, post
from tests.integration.test_native_handoff_source_postgres import completed
from tests.integration.test_native_history_postgres import new_run, projection, seed_user


def target(**changes):
    return store.HandoffTarget(**{
        "provider": "openrouter", "model": "openai/gpt-4.1-nano", "enable_tools": True,
        "enable_web_search": False, "reasoning_effort": None, "max_tokens": 1024,
        "tool_definitions_sha256": "a" * 64, "attachment_review_sha256": "e" * 64,
        "review_fingerprint": "b" * 64,
        "review_expires_at": datetime.now(UTC) + timedelta(minutes=30), **changes,
    })


@pytest.mark.parametrize("changes", [
    {"provider": "anthropic"}, {"provider": None}, {"model": "openrouter/auto"},
    {"model": "openai/latest"}, {"model": "private\ncanary"}, {"model": []},
    {"enable_tools": 1}, {"enable_tools": False}, {"enable_web_search": 0},
    {"reasoning_effort": []}, {"reasoning_effort": "unknown"},
    {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": 1_000_001},
    {"tool_definitions_sha256": "sha256:" + "a" * 64},
    {"attachment_review_sha256": None},
    {"review_fingerprint": "B" * 64}, {"review_expires_at": datetime(2026, 1, 1)},  # noqa: DTZ001 - reject naive dates
])
def test_offline_target_positive_validation_and_safe_diagnostics(changes):
    with pytest.raises(ContractError) as error:
        target(**changes)
    assert error.value.code == APIErrorCode.HANDOFF_BLOCKED
    assert "canary" not in str(error.value)
    assert len(error.value.detail) < 100


def test_offline_values_are_immutable_private_and_no_source_document_import():
    value = target()
    assert repr(value) == "<HandoffTarget private>"
    with pytest.raises(FrozenInstanceError):
        value.max_tokens = 2
    assert "document" not in inspect.signature(store.create_handoff_tx).parameters
    assert "source" not in inspect.signature(store.create_handoff_tx).parameters
    assert store._target(value.to_json()) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                                 "OPENVEGAS_NATIVE_GENERATION_HISTORY"])
@pytest.mark.parametrize("api", [store.create_handoff_tx, store.load_handoff_tx, store.lock_handoff_runs_tx,
                                store.bind_destination_tx, store.consume_first_generation_tx])
async def test_offline_every_api_is_default_off_before_any_db_access(monkeypatch, gate, api):
    for name in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                 "OPENVEGAS_NATIVE_GENERATION_HISTORY"):
        monkeypatch.setenv(name, "1")
    monkeypatch.delenv(gate)
    with pytest.raises(ContractError, match="disabled"):
        await api(object())


@pytest.mark.asyncio
async def test_offline_storage_diagnostics_do_not_echo_sql_or_private_values(monkeypatch):
    for name in ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
                 "OPENVEGAS_NATIVE_GENERATION_HISTORY"):
        monkeypatch.setenv(name, "1")

    class Unavailable:
        async def fetchrow(self, *args):
            raise RuntimeError("private-row-canary SQL details")

    with pytest.raises(ContractError) as error:
        await store.load_handoff_tx(Unavailable(), user_id=str(uuid4()), handoff_id=str(uuid4()))
    assert error.value.detail == "Native task handoff private storage is unavailable."
    assert error.value.__suppress_context__


@pytest_asyncio.fixture
async def handoff_db(continuation_db, monkeypatch):
    c = continuation_db
    await c.sandbox.migrate(through=49)
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    c.emit_calls = False
    final = payload(await post(c, native_user_text="Keep the exact public task.\r\nNo truncation."))
    c.source_scope = NativeInferenceScope(run_id=c.run.run_id,
        runtime_session_id=c.run.runtime_session_id, **await projection(c.service, c.run))
    c.source_ref = NativeContinuationRef(
        previous_inference_request_id=final["native_generation"]["inference_request_id"],
        expected_history_revision=0)
    c.target = target()
    yield c


async def create(c, **changes):
    async with c.db.transaction() as tx:
        return await store.create_handoff_tx(tx, **{
            "user_id": c.user, "source_scope": c.source_scope, "source_ref": c.source_ref,
            "target": c.target, "idempotency_key": "prepare-1", **changes,
        })


async def destination(c):
    run = await new_run(c.service, c.user)
    return NativeInferenceScope(run_id=run.run_id, runtime_session_id=run.runtime_session_id,
                                **await projection(c.service, run))


async def bind(c, record, scope, **changes):
    async with c.db.transaction() as tx:
        return await store.bind_destination_tx(tx, **{
            "user_id": c.user, "handoff_id": record.handoff_id,
            "handoff_sha256": record.handoff_sha256, "target": c.target,
            "destination_scope": scope, "idempotency_key": "commit-1", **changes,
        })


async def reserve(tx, c, record):
    """Synthetic coordinator linkage only; never a dispatch or new paid request."""
    await store.lock_handoff_runs_tx(tx, user_id=c.user, handoff_id=record.handoff_id)
    scope = record.destination_scope
    route_id, request_id = str(uuid4()), str(uuid4())
    await tx.execute("""INSERT INTO inference_requests
        (id,user_id,idempotency_key,payload_hash,status,inference_source,wallet_funding_source)
        VALUES($1::uuid,$2::uuid,$3,$4,'processing','wrapper','external')""",
        request_id, c.user, "gateway:" + request_id, "c" * 64)
    await tx.execute("""INSERT INTO inference_route_commands
        (id,user_id,idempotency_key,payload_hash,status,response_body_text,native_run_id,native_scope,
         gateway_request_id,native_history_revision)
        VALUES($1::uuid,$2::uuid,$3,$4,'processing','{}',$5::uuid,$6::jsonb,$7::uuid,0)""",
        route_id, c.user, "route:" + route_id, "d" * 64, scope.run_id,
        json.dumps({"scope_version": 1, "scope": scope.model_dump(),
                    "registration": json.loads(record.workspace_json)}), request_id)
    await tx.execute("UPDATE inference_requests SET native_route_command_id=$2::uuid WHERE id=$1::uuid",
                      request_id, route_id)
    await tx.execute("UPDATE agent_runs SET native_generation_claim_id=$2::uuid,native_history_revision=0 WHERE id=$1::uuid",
                      scope.run_id, route_id)
    return route_id, request_id


async def consume_tx(tx, c, record, route_id, request_id, **changes):
    return await store.consume_first_generation_tx(tx, **{
        "user_id": c.user, "handoff_id": record.handoff_id,
        "handoff_sha256": record.handoff_sha256, "target": c.target,
        "route_command_id": route_id, "request_id": request_id, **changes,
    })


async def insert_storage_row(tx, row):
    """Raw constraint/privilege probe, not an ownership-validated API import."""
    return await tx.fetchrow("""INSERT INTO public.native_task_handoffs
        SELECT * FROM jsonb_populate_record(NULL::public.native_task_handoffs, $1::jsonb)
        RETURNING *""", json.dumps(row, default=str))


@pytest.mark.asyncio
async def test_private_bounded_snapshot_concurrent_exact_replay_and_restart(handoff_db):
    c = handoff_db
    first, second = await asyncio.gather(create(c), create(c))
    assert first == second
    row = await c.db.fetchrow("SELECT * FROM native_task_handoffs")
    assert len(row["document_json"].encode()) <= 1_000_000
    assert row["document_sha256"] == first.document.sha256
    assert row["source_request_id"] == await c.db.fetchval("SELECT id FROM inference_requests")
    assert row["review_fingerprint"] == c.target.review_fingerprint
    assert row["expires_at"] <= row["review_expires_at"]
    assert first.document.values()["tasks"][0]["user_text"].endswith("\r\nNo truncation.")
    for secret in ("opaque-private-fixture", "reasoning_details", "owner_token", "source_snapshot"):
        assert secret not in first.document.to_json()
    assert repr(first) == "<StoredHandoff private>"
    c.db = await c.sandbox.reconnect()
    assert await create(c) == first
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "tools", "attachments", "web", "reasoning", "review", "review_expiry",
                                    "max_tokens", "ttl", "scope", "request", "revision"])
async def test_prepare_key_compares_exact_request_not_only_source_digest(handoff_db, change):
    c = handoff_db
    original = await create(c)
    changes = {
        "model": {"target": replace(c.target, model="anthropic/claude-sonnet-4")},
        "tools": {"target": replace(c.target, tool_definitions_sha256="0" * 64)},
        "attachments": {"target": replace(c.target, attachment_review_sha256="0" * 64)},
        "web": {"target": replace(c.target, enable_web_search=True)},
        "reasoning": {"target": replace(c.target, reasoning_effort="high")},
        "review": {"target": replace(c.target, review_fingerprint="0" * 64)},
        "review_expiry": {"target": replace(c.target, review_expires_at=c.target.review_expires_at + timedelta(seconds=1))},
        "max_tokens": {"target": replace(c.target, max_tokens=2048)},
        "ttl": {"ttl_seconds": 300},
        "scope": {"source_scope": c.source_scope.model_copy(update={"expected_run_version": 1000})},
        "request": {"source_ref": c.source_ref.model_copy(update={"previous_inference_request_id": str(uuid4())})},
        "revision": {"source_ref": c.source_ref.model_copy(update={"expected_history_revision": 1})},
    }[change]
    with pytest.raises(ContractError) as error:
        await create(c, **changes)
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    assert await create(c) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "session", "workspace", "revision", "projection", "cancel",
                                    "expiry", "unsettled", "invalid_scope", "invalid_ref"])
async def test_invalid_or_unowned_source_never_inserts(handoff_db, change):
    c = handoff_db
    args = {}
    if change == "owner":
        args["user_id"] = str(uuid4())
    elif change == "session":
        args["source_scope"] = c.source_scope.model_copy(update={"runtime_session_id": str(uuid4())})
    elif change == "workspace":
        await c.db.execute("UPDATE agent_runs SET workspace_fingerprint=$1", "sha256:" + "b" * 64)
    elif change == "revision":
        await c.db.execute("UPDATE agent_runs SET native_history_revision=1")
    elif change == "projection":
        await c.db.execute("UPDATE agent_runs SET version=version+1")
    elif change == "cancel":
        await c.db.execute("UPDATE agent_runs SET cancel_requested_at=now()")
    elif change == "expiry":
        args["target"] = replace(c.target, review_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    elif change == "unsettled":
        await c.db.execute("UPDATE inference_preauthorizations SET status='reserved'")
    elif change == "invalid_scope":
        args["source_scope"] = c.source_scope.model_copy(update={"expected_run_version": True})
    else:
        args["source_ref"] = c.source_ref.model_copy(update={"expected_history_revision": True})
    with pytest.raises(ContractError):
        await create(c, **args)
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 0


@pytest.mark.asyncio
async def test_commit_once_lost_ack_replay_survives_source_progress_and_restart(handoff_db):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    a, b = await asyncio.gather(bind(c, draft, scope), bind(c, draft, scope))
    assert a == b and a.destination_scope == scope
    assert a.document == draft.document and a.handoff_sha256 == draft.handoff_sha256
    await c.db.execute("UPDATE agent_runs SET version=version+1")
    c.db = await c.sandbox.reconnect()
    assert await bind(c, draft, scope) == a
    with pytest.raises(ContractError):
        await bind(c, draft, scope, idempotency_key="commit-2")
    assert len(c.calls) == 1


@pytest.mark.asyncio
async def test_distinct_confirmations_cannot_fork_one_source_revision(handoff_db):
    c = handoff_db
    drafts = [await create(c, idempotency_key="prepare-" + str(i)) for i in range(2)]
    scopes = [await destination(c) for _ in range(2)]
    results = await asyncio.gather(*(bind(c, draft, scope, idempotency_key="commit-" + str(i))
        for i, (draft, scope) in enumerate(zip(drafts, scopes, strict=True))), return_exceptions=True)
    assert sum(isinstance(r, store.StoredHandoff) for r in results) == 1
    assert sum(isinstance(r, ContractError) for r in results) == 1
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs WHERE destination_run_id IS NOT NULL") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_version", "source_revision", "source_cancel", "destination_version",
                                    "destination_owner", "destination_session", "destination_workspace",
                                    "destination_cancel", "fingerprint", "target", "source_as_destination"])
async def test_binding_requires_fresh_source_and_owned_registered_destination(handoff_db, change):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    args = {}
    if change == "source_version":
        await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", c.run.run_id)
    elif change == "source_revision":
        await c.db.execute("UPDATE agent_runs SET native_history_revision=1 WHERE id=$1::uuid", c.run.run_id)
    elif change in {"source_cancel", "destination_cancel"}:
        await c.db.execute("UPDATE agent_runs SET cancel_requested_at=now() WHERE id=$1::uuid",
                           c.run.run_id if change == "source_cancel" else scope.run_id)
    elif change == "destination_version":
        await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", scope.run_id)
    elif change == "destination_owner":
        other = await seed_user(c.db)
        await c.db.execute("UPDATE agent_runs SET user_id=$2::uuid WHERE id=$1::uuid", scope.run_id, other)
    elif change == "destination_session":
        scope = scope.model_copy(update={"runtime_session_id": str(uuid4())})
    elif change == "destination_workspace":
        await c.db.execute("UPDATE agent_runs SET workspace_root='/synthetic/other' WHERE id=$1::uuid", scope.run_id)
    elif change == "fingerprint":
        args["handoff_sha256"] = "0" * 64
    elif change == "target":
        args["target"] = replace(c.target, review_fingerprint="0" * 64)
    else:
        scope = c.source_scope
    with pytest.raises(ContractError):
        await bind(c, draft, scope, **args)
    assert await c.db.fetchval("SELECT destination_run_id FROM native_task_handoffs") is None


@pytest.mark.asyncio
async def test_commit_waits_for_source_lock_then_rejects_stale_revision(handoff_db):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    async with c.db.transaction() as tx:
        await tx.fetchrow("SELECT id FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        contender = asyncio.create_task(bind(c, draft, scope))
        try:
            done, _ = await asyncio.wait({contender}, timeout=0.05)
            assert not done
            await tx.execute("UPDATE agent_runs SET native_history_revision=1 WHERE id=$1::uuid", c.run.run_id)
        except BaseException:
            contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
            raise
    with pytest.raises(ContractError):
        await asyncio.wait_for(contender, 5)


@pytest.mark.asyncio
async def test_first_generation_consumption_replays_without_dispatch_or_relink(handoff_db):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    bound = await bind(c, draft, scope)
    async with c.db.transaction() as tx:
        route_id, request_id = await reserve(tx, c, bound)
        consumed = await consume_tx(tx, c, bound, route_id, request_id)
        assert await consume_tx(tx, c, bound, route_id, request_id) == consumed
    assert consumed.first_request_id == request_id
    assert consumed.first_route_command_id == route_id
    assert consumed.consumed_at is not None
    await c.db.execute("UPDATE agent_runs SET version=version+1")
    c.db = await c.sandbox.reconnect()
    async with c.db.transaction() as tx:
        assert await consume_tx(tx, c, bound, route_id, request_id) == consumed
    for new_route, new_request in ((str(uuid4()), request_id), (route_id, str(uuid4()))):
        with pytest.raises(ContractError):
            async with c.db.transaction() as tx:
                await consume_tx(tx, c, bound, new_route, new_request)
    assert len(c.calls) == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1


@pytest.mark.asyncio
async def test_consumption_failure_rolls_back_linkage_and_reservation(handoff_db):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    bound = await bind(c, draft, scope)
    await c.db.execute("ALTER TABLE native_task_handoffs ADD CONSTRAINT fixture_no_consume CHECK(first_request_id IS NULL)")
    with pytest.raises(ContractError, match="storage"):
        async with c.db.transaction() as tx:
            route_id, request_id = await reserve(tx, c, bound)
            await consume_tx(tx, c, bound, route_id, request_id)
    assert await c.db.fetchval("SELECT first_request_id FROM native_task_handoffs") is None
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 1
    assert await c.db.fetchval("SELECT native_generation_claim_id FROM agent_runs WHERE id=$1::uuid", scope.run_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unbound", "owner", "source_revision", "destination_version", "route_owner",
                                    "route_revision", "route_scope", "request_owner", "request_link", "request_status"])
async def test_consumption_rejects_unowned_stale_or_nonfirst_generation(handoff_db, change):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    if change == "unbound":
        with pytest.raises(ContractError):
            async with c.db.transaction() as tx:
                await consume_tx(tx, c, draft, str(uuid4()), str(uuid4()))
        return
    bound = await bind(c, draft, scope)
    other = await seed_user(c.db)
    with pytest.raises(ContractError):
        async with c.db.transaction() as tx:
            route_id, request_id = await reserve(tx, c, bound)
            args = {}
            if change == "owner":
                args["user_id"] = other
            elif change == "source_revision":
                await tx.execute("UPDATE agent_runs SET native_history_revision=1 WHERE id=$1::uuid", c.run.run_id)
            elif change == "destination_version":
                await tx.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", scope.run_id)
            elif change == "route_owner":
                await tx.execute("UPDATE inference_route_commands SET user_id=$2::uuid WHERE id=$1::uuid", route_id, other)
            elif change == "route_revision":
                await tx.execute("UPDATE inference_route_commands SET native_history_revision=1,previous_native_request_id=$2::uuid WHERE id=$1::uuid",
                                  route_id, c.source_ref.previous_inference_request_id)
            elif change == "route_scope":
                await tx.execute("UPDATE inference_route_commands SET native_scope=jsonb_set(native_scope,'{scope,runtime_session_id}',to_jsonb($2::text)) WHERE id=$1::uuid",
                                  route_id, str(uuid4()))
            elif change == "request_owner":
                await tx.execute("UPDATE inference_requests SET user_id=$2::uuid WHERE id=$1::uuid", request_id, other)
            elif change == "request_link":
                await tx.execute("UPDATE inference_requests SET native_route_command_id=NULL WHERE id=$1::uuid", request_id)
            else:
                await tx.execute("UPDATE inference_requests SET status='succeeded',response_body_text='{}' WHERE id=$1::uuid", request_id)
            await consume_tx(tx, c, bound, route_id, request_id, **args)
    assert await c.db.fetchval("SELECT first_request_id FROM native_task_handoffs") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["bind", "consume"])
async def test_expired_preview_cannot_grant_new_transition_but_replay_never_renews(handoff_db, at):
    c = handoff_db
    draft = await create(c, ttl_seconds=1)
    scope = await destination(c)
    bound = await bind(c, draft, scope) if at == "consume" else None
    await asyncio.sleep(max(0, (draft.expires_at - datetime.now(UTC)).total_seconds()) + 0.02)
    assert (await create(c, ttl_seconds=1)).expires_at == draft.expires_at
    with pytest.raises(ContractError, match="expired"):
        if at == "bind":
            await bind(c, draft, scope)
        else:
            async with c.db.transaction() as tx:
                route_id, request_id = await reserve(tx, c, bound)
                await consume_tx(tx, c, bound, route_id, request_id)
    assert await c.db.fetchval("SELECT first_request_id FROM native_task_handoffs") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["bind", "consume"])
@pytest.mark.parametrize("deadline", ["handoff", "review"])
async def test_successful_exact_replay_after_real_expiry_does_not_renew_or_grant(handoff_db, at, deadline):
    c = handoff_db
    scope, unused_scope = await destination(c), await destination(c)
    ttl = 2 if deadline == "handoff" else 600
    if deadline == "review":
        now = await c.db.fetchval("SELECT clock_timestamp()")
        c.target = replace(c.target, review_expires_at=now + timedelta(seconds=2))
    unused = await create(c, idempotency_key="unused-preview", ttl_seconds=ttl)
    draft = await create(c, ttl_seconds=ttl)
    result = await bind(c, draft, scope)
    if at == "consume":
        async with c.db.transaction() as tx:
            route_id, request_id = await reserve(tx, c, result)
            result = await consume_tx(tx, c, result, route_id, request_id)
    if deadline == "review":
        assert result.expires_at == result.target.review_expires_at
    else:
        assert result.expires_at < result.target.review_expires_at
    before = await c.db.fetch("SELECT * FROM native_task_handoffs ORDER BY id")
    requests_before = await c.db.fetchval("SELECT count(*) FROM inference_requests")
    routes_before = await c.db.fetchval("SELECT count(*) FROM inference_route_commands")
    now = await c.db.fetchval("SELECT clock_timestamp()")
    await asyncio.sleep(max(0, (max(result.expires_at, unused.expires_at) - now).total_seconds()) + 0.05)
    c.db = await c.sandbox.reconnect()
    assert await c.db.fetchval("SELECT clock_timestamp() >= $1::timestamptz", result.expires_at)
    assert await create(c, ttl_seconds=ttl) == result
    assert await bind(c, draft, scope) == result
    if at == "consume":
        async with c.db.transaction() as tx:
            assert await consume_tx(tx, c, result, route_id, request_id) == result
        with pytest.raises(ContractError) as error:
            async with c.db.transaction() as tx:
                await consume_tx(tx, c, result, route_id, str(uuid4()))
        assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(ContractError) as error:
        await bind(c, draft, scope, idempotency_key="not-the-successful-commit")
    assert error.value.code == APIErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(ContractError, match="expired"):
        await bind(c, unused, unused_scope, idempotency_key="unused-commit")
    assert await c.db.fetch("SELECT * FROM native_task_handoffs ORDER BY id") == before
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == requests_before
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == routes_before
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 1
    assert len(c.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("collision,constraint", [
    (None, None),
    ("prepare_key", "native_task_handoffs_user_id_prepare_key_key"),
    ("commit_key", "native_task_handoffs_user_id_commit_key_key"),
    ("source_history_revision", "native_task_handoff_source_consumption_uq"),
    ("destination_run_id", "native_task_handoffs_destination_run_id_key"),
    ("first_route_command_id", "native_task_handoffs_first_route_command_id_key"),
    ("first_request_id", "native_task_handoffs_first_request_id_key"),
])
async def test_sql_uniqueness_rejects_each_collision_independently(handoff_db, collision, constraint):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    bound = await bind(c, draft, scope)
    async with c.db.transaction() as tx:
        route_id, request_id = await reserve(tx, c, bound)
        await consume_tx(tx, c, bound, route_id, request_id)
    original = dict(await c.db.fetchrow("SELECT * FROM native_task_handoffs"))
    other_scope = await destination(c)
    source_route_id = await c.db.fetchval(
        "SELECT native_route_command_id FROM inference_requests WHERE id=$1::uuid",
        c.source_ref.previous_inference_request_id,
    )
    # Deliberately bypass application ownership checks to isolate SQL uniqueness.
    # All foreign keys/checks are valid; the control case proves that independently.
    candidate = {**original, "id": str(uuid4()), "prepare_key": "other-preview", "commit_key": "other-commit",
        "source_history_revision": original["source_history_revision"] + 1,
        "destination_run_id": other_scope.run_id,
        "destination_runtime_session_id": other_scope.runtime_session_id,
        "destination_scope_json": json.dumps(other_scope.model_dump()),
        "first_route_command_id": str(source_route_id),
        "first_request_id": c.source_ref.previous_inference_request_id}
    if collision is not None:
        candidate[collision] = original[collision]
        with pytest.raises(asyncpg.UniqueViolationError) as error:
            async with c.db.transaction() as tx:
                await insert_storage_row(tx, candidate)
        assert error.value.sqlstate == "23505"
        assert error.value.constraint_name == constraint
        assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1
    else:
        async with c.db.transaction() as tx:
            inserted = await insert_storage_row(tx, candidate)
        assert str(inserted["id"]) == candidate["id"]
        assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 2
    assert dict(await c.db.fetchrow("SELECT * FROM native_task_handoffs WHERE id=$1::uuid", draft.handoff_id)) == original
    assert len(c.calls) == 1


@pytest.mark.asyncio
async def test_snapshot_and_completed_bindings_are_sql_immutable(handoff_db):
    c = handoff_db
    draft, scope = await create(c), await destination(c)
    bound = await bind(c, draft, scope)
    async with c.db.transaction() as tx:
        route_id, request_id = await reserve(tx, c, bound)
        await consume_tx(tx, c, bound, route_id, request_id)
    for expression in ("document_json='{}'", "target_json='{}'", "review_fingerprint=repeat('0',64)",
                       "expires_at=expires_at+interval '1 second'", "commit_key='new-key'",
                       "first_request_id=NULL", "destination_run_id=NULL"):
        with pytest.raises(asyncpg.CheckViolationError):
            await c.db.execute("UPDATE native_task_handoffs SET " + expression)
    assert await c.db.fetchval("SELECT document_json FROM native_task_handoffs") == draft.document.to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["anon", "authenticated"])
async def test_private_table_denies_real_roles_including_owner_claim(handoff_db, role):
    c = handoff_db
    await create(c)
    await c.db.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    for sql in ("SELECT * FROM public.native_task_handoffs",
                "UPDATE public.native_task_handoffs SET document_json='{}'",
                "DELETE FROM public.native_task_handoffs"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as error:
            async with c.db.transaction() as tx:
                await tx.execute("SELECT set_config('request.jwt.claim.sub',$1,true)", c.user)
                await tx.execute(f"SET LOCAL ROLE {role}")
                await tx.execute(sql)
        assert error.value.sqlstate == "42501"
    assert await c.db.fetchval("SELECT relrowsecurity FROM pg_class WHERE oid='public.native_task_handoffs'::regclass")
    assert await c.db.fetchval("SELECT count(*) FROM pg_policies WHERE tablename='native_task_handoffs'") == 0


@pytest.mark.asyncio
async def test_service_role_actual_grants_row_access_and_immutable_updates(handoff_db):
    c = handoff_db
    role = await c.db.fetchrow("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='service_role'")
    assert role is not None, "Owned test cluster must provision service_role before migration 049"
    assert role["rolsuper"] is False and role["rolbypassrls"] is True, (
        "Require NOSUPERUSER BYPASSRLS service_role so ACL tests cannot pass via superuser bypass"
    )
    draft, scope = await create(c), await destination(c)
    original = dict(await c.db.fetchrow("SELECT * FROM native_task_handoffs"))
    allowed_updates = {"destination_run_id", "destination_runtime_session_id", "destination_scope_json",
        "destination_workspace_json", "commit_key", "committed_at", "first_route_command_id",
        "first_request_id", "consumed_at", "first_dispatch_json"}
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        actual = await c.db.fetchval(
            "SELECT has_table_privilege('service_role','public.native_task_handoffs',$1)", privilege,
        )
        assert actual is (privilege in {"SELECT", "INSERT"}), privilege
    columns = await c.db.fetch("""SELECT attname,
        has_column_privilege('service_role','public.native_task_handoffs',attname,'UPDATE') AS writable
        FROM pg_attribute WHERE attrelid='public.native_task_handoffs'::regclass
        AND attnum > 0 AND NOT attisdropped""")
    assert {row["attname"] for row in columns if row["writable"]} == allowed_updates
    # Schema traversal only. Never add the table/column grants under test.
    await c.db.execute("GRANT USAGE ON SCHEMA public TO service_role")
    candidate = {**original, "id": str(uuid4()), "prepare_key": "role-insert"}
    source_route_id = await c.db.fetchval(
        "SELECT native_route_command_id FROM inference_requests WHERE id=$1::uuid",
        c.source_ref.previous_inference_request_id,
    )
    async with c.db.transaction() as tx:
        await tx.execute("SET LOCAL ROLE service_role")
        assert await tx.fetchval("SELECT current_user") == "service_role"
        await tx.execute("SELECT set_config('request.jwt.claim.sub',$1,true)", str(uuid4()))
        assert await tx.fetchval("SELECT document_json FROM native_task_handoffs WHERE id=$1::uuid", draft.handoff_id) == draft.document.to_json()
        inserted = await insert_storage_row(tx, candidate)
        assert str(inserted["id"]) == candidate["id"]
        updated = await tx.fetchrow("""UPDATE native_task_handoffs
            SET destination_run_id=$2::uuid,destination_runtime_session_id=$3::uuid,
                destination_scope_json=$4,destination_workspace_json=workspace_json,
                commit_key='role-commit',committed_at=clock_timestamp()
            WHERE id=$1::uuid RETURNING *""",
            candidate["id"], scope.run_id, scope.runtime_session_id, json.dumps(scope.model_dump()))
        assert str(updated["destination_run_id"]) == scope.run_id
        consumed = await tx.fetchrow("""UPDATE native_task_handoffs
            SET first_route_command_id=$2::uuid,first_request_id=$3::uuid,consumed_at=clock_timestamp()
            WHERE id=$1::uuid RETURNING *""",
            candidate["id"], source_route_id, c.source_ref.previous_inference_request_id)
        assert str(consumed["first_request_id"]) == c.source_ref.previous_inference_request_id
    before = await c.db.fetch("SELECT * FROM native_task_handoffs ORDER BY id")
    for statement in (
        "UPDATE native_task_handoffs SET document_json=document_json",
        "UPDATE native_task_handoffs SET target_json=target_json",
        "UPDATE native_task_handoffs SET expires_at=expires_at",
        "UPDATE native_task_handoffs SET handoff_sha256=handoff_sha256",
        "DELETE FROM native_task_handoffs",
        "TRUNCATE native_task_handoffs",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as error:
            async with c.db.transaction() as tx:
                await tx.execute("SET LOCAL ROLE service_role")
                await tx.execute(statement)
        assert error.value.sqlstate == "42501"
    for expression in ("commit_key='rewrite'", "first_request_id=NULL"):
        with pytest.raises(asyncpg.CheckViolationError) as error:
            async with c.db.transaction() as tx:
                await tx.execute("SET LOCAL ROLE service_role")
                await tx.execute("UPDATE native_task_handoffs SET " + expression + " WHERE id=$1::uuid", candidate["id"])
        assert error.value.sqlstate == "23514"
    assert await c.db.fetch("SELECT * FROM native_task_handoffs ORDER BY id") == before


@pytest.mark.asyncio
async def test_loading_is_owner_only_and_safe_not_a_freshness_grant(handoff_db):
    c = handoff_db
    draft = await create(c)
    async with c.db.transaction() as tx:
        assert await store.load_handoff_tx(tx, user_id=c.user, handoff_id=draft.handoff_id) == draft
    with pytest.raises(ContractError):
        async with c.db.transaction() as tx:
            await store.load_handoff_tx(tx, user_id=str(uuid4()), handoff_id=draft.handoff_id)


@pytest.mark.asyncio
async def test_multigeneration_document_is_stored_exactly_from_verified_receipts(continuation_db, monkeypatch):
    c = continuation_db
    await c.sandbox.migrate(through=49)
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    first, final, observations = await completed(c)
    c.source_scope = NativeInferenceScope(run_id=c.run.run_id, runtime_session_id=c.run.runtime_session_id,
                                          **await projection(c.service, c.run))
    c.source_ref = NativeContinuationRef(previous_inference_request_id=final["native_generation"]["inference_request_id"],
                                        expected_history_revision=1)
    c.target = target()
    record = await create(c)
    expected = PortableTaskDocument.from_tasks([{"user_text": "Read my notes exactly.\r\nThen explain.",
        "attachment_refs": [], "generations": [{"assistant_text": first["text"], "observations": observations},
        {"assistant_text": final["text"], "observations": []}]}])
    assert record.document.to_json() == expected.to_json()
    assert len(c.calls) == 2
