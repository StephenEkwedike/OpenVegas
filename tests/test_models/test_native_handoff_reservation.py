"""Offline reservation boundaries; no database, credentials, or provider calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_generation import scope_document
from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_handoff import NativeHandoffRef
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from server.services import native_handoff_reservation as reservation
from server.services.native_handoff_provenance import ConsumedHandoff

pytestmark = pytest.mark.asyncio
FLAGS = ("OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
         "OPENVEGAS_NATIVE_GENERATION_HISTORY")


def uid(value):
    return str(UUID(int=value))


@pytest.fixture
def case(monkeypatch):
    for flag in FLAGS:
        monkeypatch.setenv(flag, "1")
    now = datetime.now(UTC)
    user, handoff = uid(1), uid(2)
    source = NativeInferenceScope(run_id=uid(10), runtime_session_id=uid(11),
        expected_run_version=0, expected_valid_actions_signature="sha256:" + "a" * 64)
    scope = NativeInferenceScope(run_id=uid(20), runtime_session_id=uid(21),
        expected_run_version=0, expected_valid_actions_signature="sha256:" + "b" * 64)
    workspace = {"workspace_root": "/synthetic", "workspace_fingerprint": "sha256:" + "c" * 64,
                 "git_root": None}
    target = store.HandoffTarget("openrouter", "fixture/exact-v1", True, False, None, 100,
                                "d" * 64, "e" * 64, "f" * 64, now + timedelta(minutes=10))
    document = PortableTaskDocument.from_tasks([{"user_text": "Earlier task", "attachment_refs": [],
        "generations": [{"assistant_text": "Earlier answer", "observations": []}]}])
    record = store.StoredHandoff(handoff_id=handoff, user_id=user, source_scope=source,
        source_ref=NativeContinuationRef(previous_inference_request_id=uid(30), expected_history_revision=0),
        workspace_json=store._workspace(workspace), document=document, target=target,
        prepare_key="prepare", prepare_request_json="{}", request_sha256="a" * 64,
        handoff_sha256="b" * 64, created_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5), destination_scope=scope,
        destination_workspace_json=store._workspace(workspace), commit_key="confirm", committed_at=now,
        first_route_command_id=None, first_request_id=None, consumed_at=None, first_dispatch_json=None)
    command = {"native_scope": scope.model_dump(), "native_handoff": {
        "handoff_id": handoff, "handoff_sha256": record.handoff_sha256}, "native_history": True,
        "native_continuation": None, "provider": target.provider, "model": target.model,
        "enable_tools": True, "enable_web_search": False, "reasoning_effort": None, "max_tokens": 100}
    runs = {s.run_id: {"id": s.run_id, "user_id": user, "runtime_session_id": s.runtime_session_id,
        **workspace, "native_handoff_id": handoff if s == scope else None,
        "native_generation_claim_id": None, "native_history_revision": None} for s in (source, scope)}
    c = SimpleNamespace(user=user, scope=scope, source=source, record=record, command=command,
                        runs=runs, events=[], after_hint=None, previous=None, now=now)

    async def fetchrow(sql, *args):
        if "FROM agent_runs" in sql:
            kind = "hint" if "to_jsonb" in sql else "lock" if "FOR UPDATE" in sql else "read"
            c.events.append((kind, args[0]))
            row = deepcopy(c.runs.get(args[0]))
            if kind == "hint" and c.after_hint:
                c.after_hint()
            return row if row and row["user_id"] == args[1] else None
        if "FROM inference_route_commands" in sql:
            assert "FOR UPDATE" not in sql
            c.events.append(("pointer", args[0]))
            return deepcopy(c.previous) if c.previous and c.previous["id"] == args[0] else None
        raise AssertionError(sql)

    async def fetchval(sql, *args):
        assert "clock_timestamp()" in sql
        c.events.append(("clock",))
        return c.now

    async def owned(tx, user_id, ident):
        c.events.append(("owned", ident))
        if user_id != c.record.user_id or ident != c.record.handoff_id:
            raise ContractError(APIErrorCode.HANDOFF_BLOCKED, "synthetic private row")
        return c.record

    async def fresh(tx, **kwargs):
        c.events.append(("fresh", kwargs.get("continuing", False)))

    async def provenance(tx, **kwargs):
        c.events.append(("provenance",))
        r = c.record
        return ConsumedHandoff(r.handoff_id, r.handoff_sha256, r.document, r.first_request_id, (r.handoff_id,))

    c.tx = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow), fetchval=AsyncMock(side_effect=fetchval))
    monkeypatch.setattr(store, "_owned", AsyncMock(side_effect=owned))
    monkeypatch.setattr(reservation, "require_fresh_projection_tx", AsyncMock(side_effect=fresh))
    monkeypatch.setattr(reservation, "verify_consumed_handoff_tx", AsyncMock(side_effect=provenance))
    return c


async def lock(c):
    return await reservation.lock_request_run_tx(c.tx, user_id=c.user, scope=c.scope, command=c.command)


async def validate(c, *, record=None):
    return await reservation.validate_new_handoff_tx(c.tx, user_id=c.user, scope=c.scope,
        command=c.command, run=c.runs[c.scope.run_id], record=c.record if record is None else record)


def continuing(c, revision=0):
    c.record = replace(c.record, first_route_command_id=uid(40), first_request_id=uid(41),
                       consumed_at=c.now, first_dispatch_json="immutable-proof-fixture")
    c.scope = c.scope.model_copy(update={"expected_run_version": 2})
    c.command["native_scope"] = c.scope.model_dump()
    c.command["native_continuation"] = {
        "previous_inference_request_id": uid(41), "expected_history_revision": revision}
    c.runs[c.scope.run_id].update(native_generation_claim_id=uid(40), native_history_revision=revision)
    c.previous = {"id": uid(40), "user_id": c.user, "native_run_id": c.scope.run_id,
        "native_scope": scope_document(c.record.destination_scope, c.runs[c.scope.run_id]),
        "native_history_revision": revision, "gateway_request_id": uid(41),
        "status": "succeeded", "response_status": 200}


async def test_bound_run_locks_ancestry_in_order_without_gate_or_expiry(case, monkeypatch):
    for flag in FLAGS:
        monkeypatch.delenv(flag)
    case.record = replace(case.record, expires_at=case.now - timedelta(days=1))
    run, record = await lock(case)
    assert record is case.record and run["id"] == case.scope.run_id
    assert [event[1] for event in case.events if event[0] == "lock"] == sorted(case.runs)
    assert not any(event[0] in {"fresh", "clock", "provenance", "pointer"} for event in case.events)


async def test_legacy_unbound_path_does_not_access_handoff_tables(case, monkeypatch):
    case.runs[case.scope.run_id]["native_handoff_id"] = None
    case.command.pop("native_handoff")
    case.command.pop("max_tokens")
    for flag in FLAGS:
        monkeypatch.delenv(flag)
    run, record = await lock(case)
    assert record is None
    await reservation.validate_new_handoff_tx(case.tx, user_id=case.user, scope=case.scope,
        command=case.command, run=run, record=record)
    store._owned.assert_not_awaited()
    assert case.events == [("hint", case.scope.run_id), ("lock", case.scope.run_id)]


async def test_new_binding_after_unbound_hint_aborts_without_late_ancestor_lock(case):
    case.runs[case.scope.run_id]["native_handoff_id"] = None
    case.command["native_handoff"] = None
    case.after_hint = lambda: case.runs[case.scope.run_id].update(native_handoff_id=case.record.handoff_id)
    with pytest.raises(ContractError):
        await lock(case)
    assert case.events == [("hint", case.scope.run_id), ("lock", case.scope.run_id)]
    store._owned.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("workspace_root", "/changed"),
    ("runtime_session_id", uid(99)), ("user_id", uid(99))])
async def test_registration_or_owner_change_after_hint_rejects(case, field, value):
    case.after_hint = lambda: case.runs[case.scope.run_id].update({field: value})
    with pytest.raises(ContractError):
        await lock(case)


@pytest.mark.parametrize("change", ["missing", "unbound", "id", "hash", "first_scope", "workspace"])
async def test_reference_and_registration_are_not_authority(case, change):
    if change == "missing": case.command.pop("native_handoff")
    elif change == "unbound": case.runs[case.scope.run_id]["native_handoff_id"] = None
    elif change == "id": case.command["native_handoff"]["handoff_id"] = uid(99)
    elif change == "hash": case.command["native_handoff"]["handoff_sha256"] = "c" * 64
    elif change == "first_scope":
        case.scope = case.scope.model_copy(update={"expected_run_version": 1})
        case.command["native_scope"] = case.scope.model_dump()
    else: case.runs[case.scope.run_id]["workspace_root"] = "/elsewhere"
    with pytest.raises(ContractError):
        await lock(case)


@pytest.mark.parametrize("field,value", [("provider", "other"), ("model", "fixture/other-v1"),
    ("enable_tools", 1), ("enable_web_search", 0), ("enable_web_search", True),
    ("reasoning_effort", "high"), ("max_tokens", True), ("max_tokens", "100"),
    ("max_tokens", 99), ("native_history", 1)])
async def test_exact_settings_and_types(case, field, value):
    case.command[field] = value
    with pytest.raises(ContractError):
        await lock(case)


async def test_validated_reference_instance_and_snapshot_survive_caller_mutation(case):
    case.command["native_handoff"] = NativeHandoffRef.model_validate(case.command["native_handoff"])
    def mutate():
        case.command["native_handoff"] = None
        case.command["model"] = "fixture/changed-v1"
        case.command["native_scope"]["run_id"] = uid(99)
    case.after_hint = mutate
    assert (await lock(case))[1] is case.record


async def test_first_new_work_checks_projection_and_fresh_clock(case):
    assert await validate(case) is None
    assert case.events[-2:] == [("fresh", False), ("clock",)]
    reservation.verify_consumed_handoff_tx.assert_not_awaited()


@pytest.mark.parametrize("flag", FLAGS)
async def test_new_work_is_gated_before_database_access(case, monkeypatch, flag):
    monkeypatch.setenv(flag, "0")
    with pytest.raises(ContractError):
        await validate(case)
    assert not case.events


@pytest.mark.parametrize("field,value", [("first_request_id", uid(41)),
    ("first_route_command_id", uid(40)), ("first_dispatch_json", "proof"),
    ("consumed_at", datetime.now(UTC))])
async def test_first_consumption_cannot_be_reserved_again(case, field, value):
    case.record = replace(case.record, **{field: value})
    with pytest.raises(ContractError):
        await validate(case)
    reservation.require_fresh_projection_tx.assert_not_awaited()


async def test_expiry_during_projection_wait_rejects(case, monkeypatch):
    async def expire(*args, **kwargs):
        case.now = case.record.expires_at
    monkeypatch.setattr(reservation, "require_fresh_projection_tx", expire)
    with pytest.raises(ContractError):
        await validate(case)


async def test_continuation_uses_current_projection_not_old_deadline(case):
    continuing(case)
    case.record = replace(case.record, expires_at=case.now - timedelta(days=1))
    run, record = await lock(case)
    assert record is case.record and run["native_history_revision"] == 0
    case.events.clear()
    await validate(case)
    assert [event[0] for event in case.events] == ["owned", "pointer", "provenance", "fresh", "pointer"]
    assert ("fresh", True) in case.events


@pytest.mark.parametrize("field,value", [("user_id", uid(99)), ("native_run_id", uid(99)),
    ("gateway_request_id", uid(99)), ("native_history_revision", True),
    ("status", "processing"), ("response_status", 500)])
async def test_corrupt_previous_pointer_never_enters_provenance_or_route_lock(case, field, value):
    continuing(case)
    case.previous[field] = value
    with pytest.raises(ContractError):
        await validate(case)
    reservation.verify_consumed_handoff_tx.assert_not_awaited()
    assert [event[0] for event in case.events] == ["owned", "pointer"]


@pytest.mark.parametrize("change", ["unconsumed", "revision", "bool_revision", "limit", "missing_route"])
async def test_continuation_requires_consumption_and_exact_bounded_revision(case, change):
    continuing(case)
    if change == "unconsumed": case.record = replace(case.record, first_dispatch_json=None)
    elif change == "revision": case.runs[case.scope.run_id]["native_history_revision"] = 2
    elif change == "bool_revision": case.runs[case.scope.run_id]["native_history_revision"] = False
    elif change == "limit": continuing(case, revision=127)
    else: case.previous = None
    with pytest.raises(ContractError):
        await validate(case)
    reservation.verify_consumed_handoff_tx.assert_not_awaited()


async def test_changed_pointer_after_provenance_is_rejected(case, monkeypatch):
    continuing(case)
    original = reservation.verify_consumed_handoff_tx
    async def changed(*args, **kwargs):
        value = await original(*args, **kwargs)
        case.previous["native_run_id"] = uid(99)
        return value
    monkeypatch.setattr(reservation, "verify_consumed_handoff_tx", changed)
    with pytest.raises(ContractError):
        await validate(case)


@pytest.mark.parametrize("field,value", [("handoff_id", uid(99)), ("handoff_sha256", "c" * 64),
                                      ("first_request_id", uid(99))])
async def test_verified_provenance_must_match_original_record(case, monkeypatch, field, value):
    continuing(case)
    original = reservation.verify_consumed_handoff_tx
    async def changed(*args, **kwargs):
        return replace(await original(*args, **kwargs), **{field: value})
    monkeypatch.setattr(reservation, "verify_consumed_handoff_tx", changed)
    with pytest.raises(ContractError):
        await validate(case)


async def test_gate_flip_after_await_rejects_new_work(case, monkeypatch):
    async def flip(*args, **kwargs):
        monkeypatch.setenv(FLAGS[0], "0")
    monkeypatch.setattr(reservation, "require_fresh_projection_tx", flip)
    with pytest.raises(ContractError):
        await validate(case)


@pytest.mark.parametrize("where", ["hint", "record", "provenance"])
async def test_private_failures_are_sanitized(case, monkeypatch, where):
    error = RuntimeError("PRIVATE_ROW_CANARY")
    if where == "hint": case.tx.fetchrow.side_effect = error
    elif where == "record": monkeypatch.setattr(store, "_owned", AsyncMock(side_effect=error))
    else:
        continuing(case)
        monkeypatch.setattr(reservation, "verify_consumed_handoff_tx", AsyncMock(side_effect=error))
    with pytest.raises(ContractError) as caught:
        await (validate(case) if where == "provenance" else lock(case))
    assert caught.value.code == APIErrorCode.HANDOFF_BLOCKED
    assert "PRIVATE_ROW_CANARY" not in str(caught.value)
    assert caught.value.__suppress_context__
