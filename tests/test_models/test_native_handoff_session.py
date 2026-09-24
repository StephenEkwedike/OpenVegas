"""In-memory handoff lifecycle only; fake typed clients, no HTTP or SQL."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from openvegas.agent.native_handoff_client import PendingHandoffError, PendingNativeHandoff
from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.contracts.native_handoff import NativeHandoffResponse

CANARY = "private-operation-canary"


def scope():
    return {"run_id": str(uuid4()), "runtime_session_id": str(uuid4()), "expected_run_version": 2,
            "expected_valid_actions_signature": "sha256:" + "a" * 64}


def selection(model="fixture/source-v1"):
    return {"provider": "openrouter", "model": model, "enable_tools": True, "enable_web_search": False,
            "reasoning_effort": None, "max_tokens": 1024}


def reply(current, *, revision=0, tools=False):
    ident = str(uuid4())
    return {"completion_status": "incomplete" if tools else "complete", "native_generation": {
        "scope_version": 1, "run_id": current["run_id"], "runtime_session_id": current["runtime_session_id"],
        "original_turn_scope_verified": True, "inference_request_id": ident,
        "history_revision": revision, "continuation_supported": tools},
        "tool_calls": [{"native_inference_request_id": ident, "provider_call_id": "read-1"}] if tools else []}


@pytest.fixture
def case():
    current = scope()
    old, target = selection(), selection("fixture/target-v1")
    options = {**old, "attachments": []}
    session = NativeGenerationSession()
    session.prepare(key="original", scope=current, options=options, history=True, user_text="Exact original task.")
    receipt = reply(current)
    session.validate_result(receipt)
    destination = {**scope(), "runtime_session_id": current["runtime_session_id"]}
    preview = {"handoff_id": str(uuid4()), "handoff_sha256": "b" * 64, "selection": deepcopy(target),
        "expires_at": "2026-01-01T00:00:00+00:00", "task_count": 1, "file_count": 0,
        "unique_file_count": 0, "observation_count": 0, "destination_scope": None}
    c = SimpleNamespace(session=session, scope=current, old=old, target=target, options=options,
                        destination=destination, receipt=receipt, preview=preview)
    c.pending = PendingNativeHandoff(source_session=session, source_scope=current, selection=target,
        old_selection=old, prepare_key="prepare-fixed", confirm_key="confirm-fixed")
    c.client = SimpleNamespace(
        native_handoff_prepare=AsyncMock(return_value=NativeHandoffResponse.model_validate(preview)),
        native_handoff_confirm=AsyncMock(return_value=NativeHandoffResponse.model_validate({
            **preview, "destination_scope": destination})))
    c.register = AsyncMock(return_value=destination)
    return c


async def staged(c):
    await c.pending.prepare(c.client)
    await c.pending.stage_destination(c.register)


async def adopted(c):
    await staged(c)
    await c.pending.confirm(c.client)
    return c.pending.adopt()


def test_completed_source_is_retained_and_exports_fresh_projection(case):
    c = case
    fresh = {**c.scope, "expected_run_version": 8, "expected_valid_actions_signature": "sha256:" + "c" * 64}
    checked, ref = c.session.handoff_source(fresh)
    assert checked.model_dump() == fresh
    assert ref.previous_inference_request_id == c.receipt["native_generation"]["inference_request_id"]
    fresh["run_id"] = str(uuid4())
    assert c.session.handoff_source(c.scope)[1] == ref
    assert c.session.finalized


@pytest.mark.parametrize("change", ["empty", "no_original", "tool_receipt", "uncertain", "invalid_result",
                                     "run", "runtime", "stale", "malformed"])
def test_source_rejects_unverified_or_mismatched_state(case, change):
    c = case
    session, current = c.session, deepcopy(c.scope)
    if change in {"empty", "no_original", "tool_receipt", "uncertain"}:
        session = NativeGenerationSession()
        if change != "empty":
            session.prepare(key="source", scope=current, options=c.options, history=True,
                            user_text=None if change == "no_original" else "Exact original task.")
            if change != "uncertain":
                session.validate_result(reply(current, tools=change == "tool_receipt"))
    elif change == "invalid_result":
        with pytest.raises(ValueError):
            session.validate_result({CANARY: CANARY})
    elif change == "run":
        current["run_id"] = str(uuid4())
    elif change == "runtime":
        current["runtime_session_id"] = str(uuid4())
    elif change == "stale":
        current["expected_run_version"] = 1
    else:
        current[CANARY] = CANARY
    with pytest.raises(ValueError) as error:
        session.handoff_source(current)
    assert CANARY not in str(error.value)


def test_latest_final_generation_not_original_is_exported(case):
    c = case
    session = NativeGenerationSession()
    session.prepare(key="first", scope=c.scope, options=c.options, history=True, user_text="Original")
    session.validate_result(reply(c.scope, tools=True))
    session.prepare(key="next", scope=c.scope, options=c.options, history=True, user_text="Original")
    latest = reply(c.scope, revision=1)
    session.validate_result(latest)
    _, ref = session.handoff_source(c.scope)
    assert ref.expected_history_revision == 1
    assert ref.previous_inference_request_id == latest["native_generation"]["inference_request_id"]


@pytest.mark.asyncio
async def test_commit_before_adoption_and_no_reset_before_first_dispatch(case):
    c = case
    old_ref = c.session.handoff_source(c.scope)[1]
    assert c.pending.state == "new" and c.pending.blocks_other_actions
    with pytest.raises(PendingHandoffError):
        c.pending.adopt()
    await staged(c)
    assert c.pending.state == "staged" and c.pending.confirmed is None
    with pytest.raises(PendingHandoffError):
        c.pending.adopt()
    ack = await c.pending.confirm(c.client)
    assert c.pending.state == "confirmed" and ack.destination_scope.model_dump() == c.destination
    session = c.pending.adopt()
    assert session is c.pending.adopt() and session is not c.session
    assert session.prepared_handoff and session.awaiting_first_dispatch and not session.history_active
    assert session.max_tokens == 1024 and session.confirmed_selection.model_dump() == c.target
    assert session.handoff_ref.handoff_id == ack.handoff_id
    assert c.pending.state == "adopted" and not c.pending.blocks_other_actions
    assert c.session.handoff_source(c.scope)[1] == old_ref
    await c.pending.confirm(c.client)
    c.client.native_handoff_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_and_continuation_freeze_budget_files_and_reference(case):
    c = case
    session = await adopted(c)
    options = {**c.target, "attachments": [str(uuid4())]}
    # The configured budget can be omitted by an older options producer.
    options.pop("max_tokens")
    first = session.prepare(key="first", scope=c.destination, options=options, history=True, user_text="New task")
    assert first["max_tokens"] == 1024 and first["native_handoff"] == session.handoff_ref.model_dump()
    assert first["native_user_text"] == "New task" and session.history_active
    original = deepcopy(first)
    first["native_handoff"]["handoff_sha256"] = "c" * 64
    assert session.prepare(key="first", scope={**c.destination, "expected_run_version": 3},
                           options=options, history=True, user_text="New task") == original
    session.validate_result(reply(c.destination, tools=True))
    assert not session.awaiting_first_dispatch
    next_context = session.prepare(key="next", scope=c.destination, options=options, history=True, user_text="New task")
    assert next_context["native_handoff"] == original["native_handoff"]
    assert next_context["max_tokens"] == 1024 and "native_user_text" not in next_context
    assert next_context["native_continuation"]["expected_history_revision"] == 0
    session.validate_result(reply(c.destination, revision=1))
    assert session.handoff_source(c.destination)[1].expected_history_revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["provider", "model", "enable_tools", "enable_web_search", "reasoning_effort",
                                    "max_tokens", "bool_tokens", "private", "files", "scope", "history", "user"])
async def test_configured_session_rejects_selection_changes_before_dispatch(case, change):
    c = case
    session = await adopted(c)
    kwargs = {"key": "first", "scope": deepcopy(c.destination), "options": {**c.target, "attachments": []},
              "history": True, "user_text": "New task"}
    if change == "scope":
        kwargs["scope"]["expected_run_version"] += 1
    elif change == "history":
        kwargs["history"] = False
    elif change == "user":
        kwargs["user_text"] = None
    elif change == "bool_tokens":
        kwargs["options"]["max_tokens"] = True
    else:
        name = "attachments" if change == "files" else change
        kwargs["options"][name] = {"provider": "other", "model": "fixture/other", "enable_tools": 1,
            "enable_web_search": True, "reasoning_effort": "high", "max_tokens": 99,
            "private": CANARY, "files": [CANARY]}[change]
    with pytest.raises(ValueError):
        session.prepare(**kwargs)
    assert session.awaiting_first_dispatch and not session.history_active
    with pytest.raises(ValueError):
        session.reserve(key="unbound", scope=c.destination)


@pytest.mark.asyncio
async def test_lost_prepare_ack_reuses_frozen_body_and_successful_prepare_is_cached(case):
    c = case
    seen = []

    async def prepare(request):
        seen.append(request.model_dump())
        if len(seen) == 1:
            raise ConnectionError(CANARY)
        return NativeHandoffResponse.model_validate(c.preview)

    c.client.native_handoff_prepare.side_effect = prepare
    with pytest.raises(PendingHandoffError) as error:
        await c.pending.prepare(c.client)
    assert CANARY not in str(error.value) and c.pending.state == "prepare_uncertain"
    c.target["model"] = "fixture/mutated"
    c.old["max_tokens"] = 99
    c.scope["expected_run_version"] = 9
    preview = await c.pending.prepare(c.client)
    assert preview == await c.pending.prepare(c.client)
    assert seen[0] == seen[1] and len(seen) == 2
    assert c.pending.old_selection.max_tokens == 1024
    await c.pending.stage_destination(c.register)
    assert preview == await c.pending.prepare(c.client)
    assert c.pending.state == "staged" and len(seen) == 2


@pytest.mark.asyncio
async def test_callback_and_client_receive_only_copies_and_stage_once(case):
    c = case
    await staged(c)
    body = c.pending.confirm_request.model_dump()
    c.destination["run_id"] = str(uuid4())
    leaked = c.pending.confirm_request
    object.__setattr__(leaked.destination_scope, "run_id", str(uuid4()))
    assert (await c.pending.stage_destination(c.register)).model_dump() == body["destination_scope"]
    c.register.assert_awaited_once()

    async def confirm(request):
        object.__setattr__(request.destination_scope, "run_id", str(uuid4()))
        return NativeHandoffResponse.model_validate({**c.preview, "destination_scope": body["destination_scope"]})

    c.client.native_handoff_confirm.side_effect = confirm
    await c.pending.confirm(c.client)
    assert c.pending.confirm_request.model_dump() == body


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["exception", "same_run", "invalid"])
async def test_destination_failure_preserves_source_and_can_restage(case, fault):
    c = case
    original = c.session.handoff_source(c.scope)
    await c.pending.prepare(c.client)
    if fault == "exception":
        c.register.side_effect = RuntimeError(CANARY)
    else:
        c.register.return_value = c.scope if fault == "same_run" else {CANARY: CANARY}
    with pytest.raises(PendingHandoffError) as error:
        await c.pending.stage_destination(c.register)
    assert CANARY not in str(error.value) and c.pending.state == "prepared"
    assert c.pending.confirm_request is None and c.session.handoff_source(c.scope) == original
    c.client.native_handoff_confirm.assert_not_awaited()
    c.register.side_effect, c.register.return_value = None, c.destination
    await c.pending.stage_destination(c.register)
    assert c.pending.state == "staged"


@pytest.mark.asyncio
async def test_lost_confirm_ack_blocks_all_but_identical_explicit_replay(case):
    c = case
    await staged(c)
    seen = []

    async def confirm(request):
        seen.append(request.model_dump())
        if len(seen) == 1:
            raise ConnectionError(CANARY)
        return NativeHandoffResponse.model_validate({**c.preview, "destination_scope": c.destination})

    c.client.native_handoff_confirm.side_effect = confirm
    with pytest.raises(PendingHandoffError) as error:
        await c.pending.confirm(c.client)
    assert CANARY not in str(error.value)
    assert c.pending.state == "confirm_uncertain" and c.pending.blocks_other_actions
    assert c.pending.confirmed is None and c.session.finalized
    for action in (c.pending.cancel, c.pending.adopt):
        with pytest.raises(PendingHandoffError):
            action()
    with pytest.raises(PendingHandoffError):
        await c.pending.prepare(c.client)
    with pytest.raises(PendingHandoffError):
        await c.pending.stage_destination(c.register)
    assert len(seen) == 1
    await c.pending.confirm(c.client)
    assert seen[0] == seen[1]
    c.register.assert_awaited_once()
    assert c.pending.adopt().prepared_handoff


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["handoff_id", "handoff_sha256", "selection", "destination_scope", "expires_at", "task_count", "private"])
async def test_mismatched_ack_is_uncertain_and_never_adopted(case, field):
    c = case
    await staged(c)
    changed = {**deepcopy(c.preview), "destination_scope": deepcopy(c.destination)}
    changed[field] = {"handoff_id": str(uuid4()), "handoff_sha256": "c" * 64,
        "selection": selection("fixture/wrong"), "destination_scope": scope(),
        "expires_at": "2027-01-01T00:00:00+00:00", "task_count": 2, "private": CANARY}[field]
    c.client.native_handoff_confirm.return_value = changed
    with pytest.raises(PendingHandoffError) as error:
        await c.pending.confirm(c.client)
    assert CANARY not in str(error.value) and c.pending.state == "confirm_uncertain"
    assert c.pending.confirmed is None and c.session.finalized


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["new", "prepared", "staged"])
async def test_cancel_before_confirm_never_changes_source(case, phase):
    c = case
    if phase != "new":
        await c.pending.prepare(c.client)
    if phase == "staged":
        await c.pending.stage_destination(c.register)
    c.pending.cancel()
    assert c.pending.state == "cancelled" and not c.pending.blocks_other_actions and c.session.finalized
    c.client.native_handoff_confirm.assert_not_awaited()
    with pytest.raises(PendingHandoffError):
        await c.pending.confirm(c.client)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "stage", "confirm"])
async def test_cancellation_during_await_does_not_adopt_or_revive_cancelled_operation(case, phase):
    c = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*_args):
        entered.set()
        await release.wait()
        if phase == "stage":
            return c.destination
        return NativeHandoffResponse.model_validate({**c.preview,
            "destination_scope": c.destination if phase == "confirm" else None})

    if phase == "prepare":
        c.client.native_handoff_prepare.side_effect = delayed
        task = asyncio.create_task(c.pending.prepare(c.client))
    else:
        await c.pending.prepare(c.client)
        if phase == "stage":
            task = asyncio.create_task(c.pending.stage_destination(delayed))
        else:
            await c.pending.stage_destination(c.register)
            c.client.native_handoff_confirm.side_effect = delayed
            task = asyncio.create_task(c.pending.confirm(c.client))
    await entered.wait()
    if phase == "confirm":
        with pytest.raises(PendingHandoffError):
            c.pending.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c.pending.state == "confirm_uncertain" and c.pending.blocks_other_actions
    else:
        c.pending.cancel()
        release.set()
        with pytest.raises(PendingHandoffError):
            await task
        assert c.pending.state == "cancelled"
    assert c.session.finalized and c.pending.confirmed is None


@pytest.mark.asyncio
async def test_source_mutation_during_prepare_prevents_staging(case):
    c = case

    async def changed(_request):
        with pytest.raises(ValueError):
            c.session.validate_result({})
        return NativeHandoffResponse.model_validate(c.preview)

    c.client.native_handoff_prepare.side_effect = changed
    with pytest.raises(PendingHandoffError):
        await c.pending.prepare(c.client)
    assert c.pending.preview is None and c.pending.state == "prepare_uncertain"
    with pytest.raises(PendingHandoffError):
        await c.pending.stage_destination(c.register)
    c.register.assert_not_awaited()
