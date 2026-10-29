"""Owned handoff reservation checks, never a dispatch or replay grant.

Call lock_request_run_tx before any run/route/gateway lock. Resolve the route
idempotency key next; only new work calls validate_new_handoff_tx, after that
key is held and before reserve_history_tx follows the previous generation.
The caller owns the transaction and must roll back on every failure. Neither
helper writes rows, reserves history, reads files, or authorizes provider I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_generation import (
    lock_run_tx,
    require_fresh_projection_tx,
    stored_scope,
)
from openvegas.agent.native_handoff_document import MAX_GENERATIONS
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_handoff import NativeHandoffRef, NativeHandoffSelection
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from server.services.native_handoff_provenance import ConsumedHandoff, verify_consumed_handoff_tx

_DETAIL = "Native handoff reservation could not be verified; no request was authorized."
_OPTIONS = ("provider", "model", "enable_tools", "enable_web_search", "reasoning_effort", "max_tokens")
_HINT = (
    "SELECT id,user_id,runtime_session_id,workspace_root,workspace_fingerprint,git_root,"
    "to_jsonb(agent_runs)->>'native_handoff_id' AS native_handoff_id "
    "FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid"
)


def _fail():
    raise ContractError(APIErrorCode.HANDOFF_BLOCKED, _DETAIL) from None


def _private(fn):
    @wraps(fn)
    async def guarded(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - SQL and DTO errors can contain private values.
            code = (APIErrorCode.STALE_PROJECTION if isinstance(exc, ContractError)
                    and exc.code == APIErrorCode.STALE_PROJECTION else APIErrorCode.HANDOFF_BLOCKED)
            raise ContractError(code, _DETAIL) from None
    return guarded


@dataclass(frozen=True, repr=False)
class _Intent:
    reference: NativeHandoffRef | None
    continuation: NativeContinuationRef | None
    selection: NativeHandoffSelection | None


def _dto(cls, value):
    if type(value) is cls:
        value = value.model_dump()
    if type(value) is not dict:
        _fail()
    return cls.model_validate(value)


def _snapshot(user_id, scope, command):
    user_id, scope = store._uuid(user_id), store._scope(scope)
    if type(command) is not dict:
        _fail()
    if command.get("native_scope") is not None and _dto(
            NativeInferenceScope, command["native_scope"]) != scope:
        _fail()
    raw = command.get("native_handoff")
    reference = _dto(NativeHandoffRef, raw) if raw is not None else None
    raw = command.get("native_continuation")
    continuation = _dto(NativeContinuationRef, raw) if raw is not None else None
    selection = None
    if reference is not None:
        if command.get("native_history") is not True or command.get("enable_tools") is not True:
            _fail()
        selection = NativeHandoffSelection.model_validate({
            name: command.get(name, False if name == "enable_web_search" else None)
            for name in _OPTIONS
        })
    return user_id, scope, _Intent(reference, continuation, selection)


def _identity(run, user_id, scope):
    if (not run or str(run["id"]) != scope.run_id or str(run["user_id"]) != user_id
            or str(run["runtime_session_id"]) != scope.runtime_session_id):
        _fail()
    incoming = run.get("native_handoff_id")
    incoming = store._uuid(str(incoming)) if incoming is not None else None
    return incoming, store._workspace(run)


def _bound(run, record, user_id, scope, intent):
    incoming, workspace = _identity(run, user_id, scope)
    ref = intent.reference
    if (type(record) is not store.StoredHandoff or ref is None or intent.selection is None
            or record.user_id != user_id or record.handoff_id != incoming
            or record.handoff_id != ref.handoff_id or record.handoff_sha256 != ref.handoff_sha256
            or record.destination_scope is None or record.committed_at is None
            or record.commit_key is None or record.destination_scope.run_id != scope.run_id
            or record.destination_scope.runtime_session_id != scope.runtime_session_id
            or record.destination_workspace_json != workspace or record.workspace_json != workspace
            or (intent.continuation is None and record.destination_scope != scope)):
        _fail()
    store._scope(record.destination_scope)
    store._target_json(record.target)
    if any(type(getattr(intent.selection, name)) is not type(getattr(record.target, name))
           or getattr(intent.selection, name) != getattr(record.target, name) for name in _OPTIONS):
        _fail()


@_private
async def lock_request_run_tx(
    tx: Any, *, user_id: str, scope: NativeInferenceScope, command: dict,
) -> tuple[Any, store.StoredHandoff | None]:
    """Ownership only, intentionally ungated so completed replay stays possible.

    Raw hints and options are snapshotted before awaits. An unbound-to-bound
    race aborts rather than discovering ancestors after locking the destination.
    No current projection, consumption state, or expiry is required for replay.
    """
    user_id, scope, intent = _snapshot(user_id, scope, command)
    hint = await tx.fetchrow(_HINT, scope.run_id, user_id)
    identity = _identity(hint, user_id, scope)
    if identity[0] is None:
        run = await lock_run_tx(tx, user_id=user_id, scope=scope)
        if _identity(run, user_id, scope) != identity or intent.reference is not None:
            _fail()
        return run, None
    runs = await store._runs(tx, user_id, scope)
    run = runs[scope.run_id]
    if _identity(run, user_id, scope) != identity:
        _fail()
    record = await store._owned(tx, user_id, identity[0])
    _bound(run, record, user_id, scope, intent)
    store._registration(record, runs)
    return run, record


async def _previous_route(tx, *, run, user_id, scope, ref):
    revision = run.get("native_history_revision")
    if (type(revision) is not int or revision != ref.expected_history_revision
            or not 0 <= revision < MAX_GENERATIONS - 1):
        _fail()
    route_id = store._uuid(str(run.get("native_generation_claim_id")))
    # No FOR UPDATE: ownership must be known before reserve_history_tx follows
    # this pointer. Provenance locks owned routes, never a corrupt foreign one.
    route = await tx.fetchrow(
        "SELECT id,user_id,native_run_id,native_scope,native_history_revision,"
        "gateway_request_id,status,response_status FROM inference_route_commands WHERE id=$1::uuid",
        route_id,
    )
    if (not route or str(route["id"]) != route_id or str(route["user_id"]) != user_id
            or str(route["native_run_id"]) != scope.run_id
            or type(route["native_history_revision"]) is not int
            or route["native_history_revision"] != revision
            or str(route["gateway_request_id"]) != ref.previous_inference_request_id
            or route["status"] != "succeeded" or route["response_status"] != 200):
        _fail()
    ownership = stored_scope(route)
    if (ownership["scope"]["run_id"] != scope.run_id
            or ownership["scope"]["runtime_session_id"] != scope.runtime_session_id
            or store._workspace(ownership["registration"]) != store._workspace(run)):
        _fail()


@_private
async def validate_new_handoff_tx(
    tx: Any, *, user_id: str, scope: NativeInferenceScope, command: dict,
    run: Any, record: store.StoredHandoff | None,
) -> None:
    """New work only, after the route key is held, before history reservation.

    Consumed ancestry proves retained context, not fresh file/model authority.
    The parent must still prepare a binding and reauthorize it in the gateway.
    """
    user_id, scope, intent = _snapshot(user_id, scope, command)
    run = dict(run)
    if record is None:
        if _identity(run, user_id, scope)[0] is not None or intent.reference is not None:
            _fail()
        return
    store._enabled()
    _bound(run, record, user_id, scope, intent)
    if await store._owned(tx, user_id, record.handoff_id) != record:
        _fail()
    if intent.continuation is None:
        if (any(value is not None for value in (
                record.first_request_id, record.first_route_command_id, record.first_dispatch_json,
                record.consumed_at, run.get("native_generation_claim_id"), run.get("native_history_revision")))):
            _fail()
        await require_fresh_projection_tx(tx, run=run, scope=scope)
        await store._now_unexpired(tx, record)
    else:
        if any(value is None for value in (
                record.first_request_id, record.first_route_command_id,
                record.first_dispatch_json, record.consumed_at)):
            _fail()
        await _previous_route(tx, run=run, user_id=user_id, scope=scope, ref=intent.continuation)
        incoming = await verify_consumed_handoff_tx(tx, user_id=user_id, scope=scope)
        if (type(incoming) is not ConsumedHandoff or incoming.handoff_id != record.handoff_id
                or incoming.handoff_sha256 != record.handoff_sha256
                or incoming.document.sha256 != record.document.sha256
                or incoming.first_request_id != record.first_request_id):
            _fail()
        await require_fresh_projection_tx(tx, run=run, scope=scope, continuing=True)
        await _previous_route(tx, run=run, user_id=user_id, scope=scope, ref=intent.continuation)
    store._enabled()
