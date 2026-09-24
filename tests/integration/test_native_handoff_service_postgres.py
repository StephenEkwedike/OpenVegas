"""Owned coordinator transactions, real local SQL and synthetic settled history.

No handoff HTTP route, real provider request, or billing dispatch is enabled.
"""
import asyncio
import json
import os
from dataclasses import asdict
from uuid import uuid4

import pytest

from openvegas.contracts.errors import ContractError
from server.services.native_handoff_service import HandoffSelection, NativeHandoffService
from tests.integration.test_native_handoff_store_postgres import (
    continuation_db as continuation_db,
    handoff_db as handoff_db,
    destination,
)
from tests.integration.test_openrouter_postgres import MODELS

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fresh_runtime_flags():
    # Each test provisions a different synthetic runtime. Do not inherit the
    # cached startup configuration from another integration test's application.
    from server.services.dependencies import current_flags
    current_flags.cache_clear()
    yield
    current_flags.cache_clear()


async def prepare(c, **changes):
    return await NativeHandoffService(c.db).prepare(**{
        "user_id": c.user, "source_scope": c.source_scope, "source_ref": c.source_ref,
        "selection": HandoffSelection(MODELS[1], max_tokens=100), "idempotency_key": "preview-one",
        **changes,
    })


async def confirm(c, preview, scope, **changes):
    return await NativeHandoffService(c.db).confirm(**{
        "user_id": c.user, "handoff_id": preview.handoff_id,
        "handoff_sha256": preview.handoff_sha256, "destination_scope": scope,
        "idempotency_key": "confirm-one", **changes,
    })


async def test_prepare_and_concurrent_commit_lost_ack_do_not_reserve_or_dispatch(handoff_db, monkeypatch):
    c = handoff_db
    preview = await prepare(c)
    assert preview == await prepare(c)
    assert preview.task_count == 1 and preview.file_count == preview.observation_count == 0
    assert preview.destination_scope is None and preview.selection.model == MODELS[1]
    assert "Synthetic native answer" not in repr(asdict(preview))
    scope = await destination(c)
    first, second = await asyncio.gather(confirm(c, preview, scope), confirm(c, preview, scope))
    assert first == second and first.destination_scope == scope
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    c.db = await c.sandbox.reconnect()
    assert await confirm(c, preview, scope) == first
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 1
    assert await c.db.fetchval("SELECT count(*) FROM inference_preauthorizations") == 1
    assert len(c.calls) == 1


@pytest.mark.parametrize("change", ["review", "price", "disabled", "credentials", "feature", "source",
                                    "owner", "digest", "scope", "context"])
async def test_failed_confirmation_leaves_snapshot_and_source_unchanged(handoff_db, monkeypatch, change):
    c = handoff_db
    preview, scope = await prepare(c), await destination(c)
    args = {}
    if change == "review": monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    elif change == "price":
        await c.db.execute("UPDATE provider_catalog SET v_price_input_per_1m=111 WHERE model_id=$1", MODELS[1])
    elif change == "disabled":
        await c.db.execute("UPDATE provider_catalog SET enabled=false WHERE model_id=$1", MODELS[1])
    elif change == "credentials":
        await c.db.execute("UPDATE provider_credentials SET status='disabled'")
    elif change == "feature": monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "0")
    elif change == "source": await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", c.run.run_id)
    elif change == "owner": args["user_id"] = str(uuid4())
    elif change == "digest": args["handoff_sha256"] = "0" * 64
    elif change == "scope": args["destination_scope"] = c.source_scope
    else:
        reviews = json.loads(os.environ["OPENVEGAS_MODEL_REVIEWS_JSON"])
        reviews["openrouter:" + MODELS[1]]["context_window_tokens"] = 1
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps(reviews))
    with pytest.raises(ContractError, match="current selection was not changed"):
        await confirm(c, preview, scope, **args)
    assert await c.db.fetchval("SELECT destination_run_id FROM native_task_handoffs") is None
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests") == 1
    assert len(c.calls) == 1


async def test_conflicting_confirmations_cannot_create_two_destinations(handoff_db):
    c = handoff_db
    preview, a, b = await prepare(c), await destination(c), await destination(c)
    results = await asyncio.gather(confirm(c, preview, a), confirm(c, preview, b), return_exceptions=True)
    assert sum(isinstance(result, ContractError) for result in results) == 1
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs WHERE destination_run_id IS NOT NULL") == 1
    assert len(c.calls) == 1


async def test_prepare_reuse_with_other_target_is_rejected_without_new_row(handoff_db):
    c = handoff_db
    await prepare(c)
    with pytest.raises(ContractError):
        await prepare(c, selection=HandoffSelection(MODELS[2], max_tokens=100))
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1


async def test_lost_prepare_ack_recovers_original_snapshot_without_renewal(handoff_db, monkeypatch):
    c = handoff_db
    before = await prepare(c)
    await c.db.execute("UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid", c.run.run_id)
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    assert await prepare(c) == before
    assert len(c.calls) == 1


@pytest.mark.parametrize("enabled", ["0", "1"])
async def test_bound_destination_cannot_omit_handoff_even_when_gate_disabled(handoff_db, monkeypatch, enabled):
    from server.services.inference_replay import InferenceReplayService
    c = handoff_db
    preview, scope = await prepare(c), await destination(c)
    await confirm(c, preview, scope)
    assert str(await c.db.fetchval("SELECT native_handoff_id FROM agent_runs WHERE id=$1::uuid", scope.run_id)) == preview.handoff_id
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", enabled)
    with pytest.raises(ContractError, match="destination dispatch is not enabled"):
        await InferenceReplayService(c.db).begin(user_id=c.user, idempotency_key="omit-handoff",
            command={**c.command, "native_scope": scope.model_dump()}, allow_native_history=True)
    assert await c.db.fetchval("SELECT count(*) FROM inference_route_commands") == 1


async def test_bound_workspace_cannot_be_rebound_or_binding_removed(handoff_db):
    import asyncpg
    c = handoff_db
    preview, scope = await prepare(c), await destination(c)
    await confirm(c, preview, scope)
    with pytest.raises(ContractError, match="immutable"):
        await c.service.register_workspace(user_id=c.user, run_id=scope.run_id,
            runtime_session_id=scope.runtime_session_id, workspace_root="/substituted",
            workspace_fingerprint="sha256:" + "a" * 64)
    with pytest.raises(asyncpg.CheckViolationError):
        await c.db.execute("UPDATE agent_runs SET native_handoff_id=NULL WHERE id=$1::uuid", scope.run_id)


async def test_owned_media_prepare_confirm_with_single_pool_connection(continuation_db, monkeypatch):
    from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
    from tests.integration.test_native_continuation_postgres import setup_media, payload, post, projection
    from tests.test_models.test_openrouter_web_gateway import MODEL
    c = continuation_db
    await c.sandbox.migrate(through=49)
    await setup_media(c, monkeypatch, web=False, attachment=True)
    c.emit_calls = False
    result = payload(await post(c, native_user_text="Keep this file exactly."))
    c.source_scope = NativeInferenceScope(run_id=c.run.run_id, runtime_session_id=c.run.runtime_session_id,
                                          **await projection(c.service, c.run))
    c.source_ref = NativeContinuationRef(previous_inference_request_id=result["native_generation"]["inference_request_id"],
                                         expected_history_revision=0)
    scope = await destination(c)
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    c.sandbox._max_size = 1
    c.db = await c.sandbox.reconnect()
    preview = await asyncio.wait_for(prepare(c, selection=HandoffSelection(MODEL, max_tokens=100)), 5)
    assert preview.file_count == preview.unique_file_count == 1
    bound = await asyncio.wait_for(confirm(c, preview, scope), 5)
    assert bound.destination_scope == scope
    assert len(c.calls) == 1
