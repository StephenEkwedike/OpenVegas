"""Private storage primitives, not a prepare/confirm/dispatch coordinator.

Every API requires an existing transaction and an authenticated server user ID.
Never expose records as HTTP DTOs or accept source history from HTTP. Creation
assembles the source from owned SQL evidence. The trusted coordinator supplies a
freshly reviewed target, reauthorizes files at preview AND dispatch, bounds the
complete input, obtains confirmation and binds the actual dispatch payload.

Lock all involved runs in UUID order BEFORE calling other locking subsystems.
Then source history precedes the handoff row. Destination ownership rows are
read under its run lock. Callers must roll back on any exception. Exact replay
returns old storage state, including after expiry; it is NOT execution authority.
No API makes provider, wallet, filesystem, or network calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from uuid import uuid4

from openvegas.agent.native_generation import (
    registration,
    require_fresh_projection_tx,
    stored_scope,
)
from openvegas.agent.native_handoff_document import PortableTaskDocument
from openvegas.agent.native_handoff_source import assemble_task_tx
from openvegas.capabilities import REASONING_EFFORTS
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from openvegas.gateway.openrouter import valid_model

_SAFE = {
    "disabled": "Native task handoff storage is disabled.",
    "invalid": "Native task handoff storage input is invalid.",
    "ownership": "Native task handoff ownership or registration does not match.",
    "stale": "Native task handoff source or destination revision is stale.",
    "conflict": "Native task handoff replay or consumption conflicts.",
    "expired": "Native task handoff or destination review expired.",
    "integrity": "Native task handoff integrity check failed.",
    "storage": "Native task handoff private storage is unavailable.",
}


def _fail(reason="invalid"):
    code = {"stale": APIErrorCode.STALE_PROJECTION,
            "conflict": APIErrorCode.IDEMPOTENCY_CONFLICT}.get(reason, APIErrorCode.HANDOFF_BLOCKED)
    raise ContractError(code, _SAFE[reason]) from None


def _enabled():
    if any(os.getenv(name, "0") != "1" for name in (
        "OPENVEGAS_NATIVE_TASK_HANDOFF", "OPENVEGAS_NATIVE_GENERATION_SCOPE",
        "OPENVEGAS_NATIVE_GENERATION_HISTORY",
    )):
        _fail("disabled")


def _private_api(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        _enabled()
        try:
            return await fn(*args, **kwargs)
        except ContractError as exc:
            if exc.detail in _SAFE.values():
                raise
            _fail("integrity")
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            _fail()
        except Exception as exc:  # noqa: BLE001 - private SQL errors must not escape this boundary
            # SQL diagnostics can include entire private row contents. Never relay
            # them, including uniqueness/check failures or unavailable schema.
            _fail("conflict" if getattr(exc, "sqlstate", None) == "23505" else "storage")
    return wrapped


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _uuid(value):
    if type(value) is not str:
        _fail()
    return NativeInferenceScope.canonical_uuid(value)


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail()
    return value


def _key(value):
    if type(value) is not str or not re.fullmatch(r"[!-~]{1,200}", value):
        _fail()
    return value


def _scope(value):
    if type(value) is not NativeInferenceScope:
        _fail()
    return NativeInferenceScope.model_validate(value.model_dump())


def _ref(value):
    if type(value) is not NativeContinuationRef:
        _fail()
    return NativeContinuationRef.model_validate(value.model_dump())


def _date(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _fail()
    return value.astimezone(UTC)


@dataclass(frozen=True, repr=False)
class HandoffTarget:
    """Server-reviewed values, NOT proof of review or an importable HTTP claim.

    review_fingerprint must cover the coordinator's exact-model capability/catalog
    review; tool_definitions_sha256 covers the fresh destination tool definitions.
    attachment_review_sha256 binds the server's owner/document/model/review/payload
    receipt, including a no-files receipt. It is not reusable file authorization.
    Storage only validates shape and binds exact values. No review is inferred.
    """

    provider: str
    model: str
    enable_tools: bool
    enable_web_search: bool
    reasoning_effort: str | None
    max_tokens: int
    tool_definitions_sha256: str
    attachment_review_sha256: str
    review_fingerprint: str
    review_expires_at: datetime

    def __post_init__(self):
        if (type(self.provider) is not str or self.provider != "openrouter"
                or type(self.model) is not str or len(self.model) > 256 or not valid_model(self.model)
                or self.enable_tools is not True or type(self.enable_web_search) is not bool
                or (self.reasoning_effort is not None and
                    (type(self.reasoning_effort) is not str or self.reasoning_effort not in REASONING_EFFORTS))
                or type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 1_000_000):
            _fail()
        _digest(self.tool_definitions_sha256)
        _digest(self.attachment_review_sha256)
        _digest(self.review_fingerprint)
        object.__setattr__(self, "review_expires_at", _date(self.review_expires_at))

    def __repr__(self):
        return "<HandoffTarget private>"

    def to_json(self):
        self.__post_init__()
        return _json({**vars(self), "review_expires_at": self.review_expires_at.isoformat()})


def _target(raw):
    value = json.loads(raw)
    value["review_expires_at"] = datetime.fromisoformat(value["review_expires_at"])
    target = HandoffTarget(**value)
    if target.to_json() != raw:
        _fail("integrity")
    return target


def _target_json(target):
    if type(target) is not HandoffTarget:
        _fail()
    return target.to_json()


def _workspace(run):
    value = registration(run)
    for name in ("workspace_root", "git_root"):
        path = value[name]
        if path is None and name == "git_root":
            continue
        if (type(path) is not str or not path or len(path.encode("utf-8")) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            _fail("ownership")
    fp = value["workspace_fingerprint"]
    if type(fp) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", fp):
        _fail("ownership")
    raw = _json(value)
    if len(raw.encode("utf-8")) > 16384:
        _fail("ownership")
    return raw


@dataclass(frozen=True, repr=False)
class StoredHandoff:
    handoff_id: str
    user_id: str
    source_scope: NativeInferenceScope
    source_ref: NativeContinuationRef
    workspace_json: str
    document: PortableTaskDocument
    target: HandoffTarget
    prepare_key: str
    prepare_request_json: str
    request_sha256: str
    handoff_sha256: str
    created_at: datetime
    expires_at: datetime
    destination_scope: NativeInferenceScope | None
    destination_workspace_json: str | None
    commit_key: str | None
    committed_at: datetime | None
    first_route_command_id: str | None
    first_request_id: str | None
    consumed_at: datetime | None
    first_dispatch_json: str | None

    def __repr__(self):
        return "<StoredHandoff private>"


def _fingerprint(*, handoff_id, request_json, workspace_json, document, created_at, expires_at):
    return _sha(_json({"version": 1, "id": handoff_id, "request": json.loads(request_json),
                      "workspace": json.loads(workspace_json), "document_sha256": document.sha256,
                      "created_at": created_at.isoformat(), "expires_at": expires_at.isoformat()}))


def _decode(row):
    if not row or row["version"] != 1:
        _fail("ownership")
    scope = NativeInferenceScope.model_validate_json(row["source_scope_json"])
    ref = NativeContinuationRef(previous_inference_request_id=str(row["source_request_id"]),
                                expected_history_revision=row["source_history_revision"])
    target = _target(row["target_json"])
    document = PortableTaskDocument(row["document_json"])
    created, expires = _date(row["created_at"]), _date(row["expires_at"])
    raw = row["prepare_request_json"]
    request = json.loads(raw)
    if (set(request) != {"version", "user_id", "source_scope", "source_ref", "target", "ttl_seconds"}
            or type(request["version"]) is not int or request["version"] != 1
            or type(request["ttl_seconds"]) is not int or not 1 <= request["ttl_seconds"] <= 600
            or _json(request) != raw or _sha(raw) != row["request_sha256"]
            or _uuid(request["user_id"]) != str(row["user_id"])
            or request["source_scope"] != scope.model_dump() or request["source_ref"] != ref.model_dump()
            or _json(request["target"]) != row["target_json"]
            or scope.run_id != str(row["source_run_id"])
            or scope.runtime_session_id != str(row["source_runtime_session_id"])
            or document.to_json() != row["document_json"] or document.sha256 != row["document_sha256"]
            or target.review_fingerprint != row["review_fingerprint"]
            or target.review_expires_at != row["review_expires_at"]
            or not created < expires <= min(created + timedelta(seconds=request["ttl_seconds"]),
                                             target.review_expires_at)
            or _workspace(json.loads(row["workspace_json"])) != row["workspace_json"]
            or _fingerprint(handoff_id=str(row["id"]), request_json=raw,
                            workspace_json=row["workspace_json"], document=document,
                            created_at=created, expires_at=expires) != row["handoff_sha256"]):
        _fail("integrity")
    dest = (NativeInferenceScope.model_validate_json(row["destination_scope_json"])
            if row["destination_scope_json"] is not None else None)
    if dest and (dest.run_id != str(row["destination_run_id"])
                 or dest.runtime_session_id != str(row["destination_runtime_session_id"])
                 or row["destination_workspace_json"] != row["workspace_json"]):
        _fail("integrity")
    record = StoredHandoff(str(row["id"]), str(row["user_id"]), scope, ref, row["workspace_json"],
        document, target, _key(row["prepare_key"]), raw, row["request_sha256"], row["handoff_sha256"],
        created, expires, dest, row["destination_workspace_json"], row["commit_key"], row["committed_at"],
        str(row["first_route_command_id"]) if row["first_route_command_id"] else None,
        str(row["first_request_id"]) if row["first_request_id"] else None, row["consumed_at"],
        row["first_dispatch_json"])
    if (record.first_dispatch_json is not None
            and (record.first_request_id is None or _dispatch_proof(record,
                record.first_route_command_id, record.first_request_id,
                json.loads(record.first_dispatch_json)) != record.first_dispatch_json)):
        _fail("integrity")
    return record


async def _runs(tx, user_id, *scopes):
    scopes_by_id = {}
    for scope in scopes:
        scope = _scope(scope)
        if scope.run_id in scopes_by_id and scopes_by_id[scope.run_id] != scope:
            _fail("conflict")
        scopes_by_id[scope.run_id] = scope
    # Discover immutable incoming links before taking any row lock. A source
    # can itself be a destination; locking just the newest pair inverts order
    # when its earlier context is later verified inside the same transaction.
    from openvegas.agent.native_handoff_document import MAX_TASKS
    snapshots, records, edges = {}, {}, {}
    pending = list(scopes_by_id)
    while pending:
        run_id = pending.pop()
        scope = scopes_by_id[run_id]
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid",
                                run_id, user_id)
        if not run or str(run["runtime_session_id"]) != scope.runtime_session_id:
            _fail("ownership")
        incoming = str(run["native_handoff_id"]) if run.get("native_handoff_id") else None
        snapshots[run_id] = (incoming, _workspace(run))
        if incoming is None:
            continue
        record = await _owned(tx, user_id, incoming)
        if (record.destination_scope is None or record.destination_scope.run_id != run_id
                or record.destination_scope.runtime_session_id != scope.runtime_session_id):
            _fail("ownership")
        records[incoming] = record
        parent = record.source_scope
        edges[run_id] = parent.run_id
        if parent.run_id in scopes_by_id:
            if scopes_by_id[parent.run_id].runtime_session_id != parent.runtime_session_id:
                _fail("conflict")
        else:
            if len(scopes_by_id) >= MAX_TASKS + 2:
                _fail("integrity")
            scopes_by_id[parent.run_id] = parent
            pending.append(parent.run_id)
    runs = {}
    for run_id, scope in sorted(scopes_by_id.items()):
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
                                run_id, user_id)
        if not run or str(run["runtime_session_id"]) != scope.runtime_session_id:
            _fail("ownership")
        incoming = str(run["native_handoff_id"]) if run.get("native_handoff_id") else None
        old_incoming, workspace = snapshots[run_id]
        if _workspace(run) != workspace:
            _fail("ownership")
        if incoming != old_incoming:
            # A concurrent confirmation may bind a previously fresh destination
            # while this waiter acquires the already known source/destination
            # locks. Accept only a parent graph already included in that order.
            # Never acquire an undiscovered ancestor after taking a row lock.
            if old_incoming is not None or incoming is None:
                _fail("ownership")
            current = await _owned(tx, user_id, incoming)
            if (current.destination_scope is None or current.destination_scope.run_id != run_id
                    or current.destination_scope.runtime_session_id != scope.runtime_session_id
                    or current.source_scope.run_id not in scopes_by_id
                    or scopes_by_id[current.source_scope.run_id].runtime_session_id
                        != current.source_scope.runtime_session_id):
                _fail("ownership")
            records[incoming] = current
            edges[run_id] = current.source_scope.run_id
        runs[run_id] = run
    for start in edges:
        seen, current = set(), start
        while current in edges:
            if current in seen:
                _fail("integrity")
            seen.add(current)
            current = edges[current]
    for record in records.values():
        current = await _owned(tx, user_id, record.handoff_id)
        if (current.handoff_sha256 != record.handoff_sha256
                or current.destination_scope != record.destination_scope
                or current.source_scope != record.source_scope):
            _fail("conflict")
        _registration(current, runs)
    return runs


async def _owned(tx, user_id, handoff_id):
    row = await tx.fetchrow("SELECT * FROM native_task_handoffs WHERE id=$1::uuid AND user_id=$2::uuid",
                            _uuid(handoff_id), _uuid(user_id))
    return _decode(row)


def _registration(record, runs):
    if _workspace(runs[record.source_scope.run_id]) != record.workspace_json:
        _fail("ownership")
    if record.destination_scope and _workspace(runs[record.destination_scope.run_id]) != record.workspace_json:
        _fail("ownership")
    if record.destination_scope and str(runs[record.destination_scope.run_id].get("native_handoff_id")) != record.handoff_id:
        _fail("ownership")


async def _lock_record(tx, record):
    row = await tx.fetchrow("SELECT * FROM native_task_handoffs WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
                            record.handoff_id, record.user_id)
    return _decode(row)


async def _fresh_source(tx, record, runs):
    _registration(record, runs)
    source = await assemble_task_tx(tx, user_id=record.user_id, scope=record.source_scope,
                                    source_ref=record.source_ref)
    if source.document.to_json() != record.document.to_json():
        _fail("integrity")


async def _now_unexpired(tx, record):
    now = await tx.fetchval("SELECT clock_timestamp()")
    if now >= record.expires_at or now >= record.target.review_expires_at:
        _fail("expired")
    return now


def _compare(record, target, handoff_sha256):
    if (_digest(handoff_sha256) != record.handoff_sha256
            or _target_json(target) != record.target.to_json()):
        _fail("conflict")


@_private_api
async def create_handoff_tx(tx: Any, *, user_id: str, source_scope: NativeInferenceScope,
                            source_ref: NativeContinuationRef, target: HandoffTarget,
                            idempotency_key: str, ttl_seconds: int = 600) -> StoredHandoff:
    """Assemble and insert immutable preview. Same key requires exact input replay.

    An expired/stale replay returns its original snapshot, not a renewed review.
    No document, workspace, source model or historical events are accepted inputs.
    """
    user_id, source_scope, source_ref = _uuid(user_id), _scope(source_scope), _ref(source_ref)
    key, target_json = _key(idempotency_key), _target_json(target)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600:
        _fail()
    request_json = _json({"version": 1, "user_id": user_id, "source_scope": source_scope.model_dump(),
                          "source_ref": source_ref.model_dump(), "target": json.loads(target_json),
                          "ttl_seconds": ttl_seconds})
    runs = await _runs(tx, user_id, source_scope)
    existing = await tx.fetchrow("SELECT * FROM native_task_handoffs WHERE user_id=$1::uuid AND prepare_key=$2",
                                 user_id, key)
    if existing:
        record = _decode(existing)
        if record.prepare_request_json != request_json:
            _fail("conflict")
        if _workspace(runs[source_scope.run_id]) != record.workspace_json:
            _fail("ownership")
        return record
    source = await assemble_task_tx(tx, user_id=user_id, scope=source_scope, source_ref=source_ref)
    workspace_json = _workspace(runs[source_scope.run_id])
    created = await tx.fetchval("SELECT clock_timestamp()")
    expires = min(created + timedelta(seconds=ttl_seconds), target.review_expires_at)
    if expires <= created:
        _fail("expired")
    handoff_id = str(uuid4())
    fingerprint = _fingerprint(handoff_id=handoff_id, request_json=request_json,
        workspace_json=workspace_json, document=source.document, created_at=created, expires_at=expires)
    row = await tx.fetchrow("""INSERT INTO native_task_handoffs
        (id,user_id,source_run_id,source_runtime_session_id,source_history_revision,source_request_id,
         source_scope_json,workspace_json,document_json,document_sha256,target_json,review_fingerprint,
         review_expires_at,prepare_key,prepare_request_json,request_sha256,handoff_sha256,created_at,expires_at)
        VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6::uuid,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
        ON CONFLICT (user_id,prepare_key) DO NOTHING RETURNING *""",
        handoff_id, user_id, source_scope.run_id, source_scope.runtime_session_id,
        source.history_revision, source.request_id, _json(source_scope.model_dump()), workspace_json,
        source.document.to_json(), source.document.sha256, target_json, target.review_fingerprint,
        target.review_expires_at, key, request_json, _sha(request_json), fingerprint, created, expires)
    if row is None:
        # A different source run can race for the same user's key.
        row = await tx.fetchrow("SELECT * FROM native_task_handoffs WHERE user_id=$1::uuid AND prepare_key=$2",
                                user_id, key)
    record = _decode(row)
    if record.prepare_request_json != request_json or record.document.to_json() != source.document.to_json():
        _fail("conflict")
    return record


@_private_api
async def load_handoff_tx(tx: Any, *, user_id: str, handoff_id: str) -> StoredHandoff:
    """Owner-only integrity-checked snapshot, not a freshness/dispatch grant."""
    return await _owned(tx, user_id, handoff_id)


@_private_api
async def lock_handoff_runs_tx(tx: Any, *, user_id: str, handoff_id: str) -> StoredHandoff:
    """Lock a bound handoff's run pair BEFORE route/gateway reservation locks.

    Use at the start of a future dispatch transaction, then reserve/link and call
    consume_first_generation_tx in that same transaction. This is not permission
    to dispatch and does not substitute for current review/file validation.
    """
    record = await _owned(tx, user_id, handoff_id)
    if record.destination_scope is None:
        _fail("conflict")
    runs = await _runs(tx, user_id, record.source_scope, record.destination_scope)
    _registration(record, runs)
    return await _owned(tx, user_id, handoff_id)


@_private_api
async def bind_destination_tx(tx: Any, *, user_id: str, handoff_id: str,
                               handoff_sha256: str, target: HandoffTarget,
                               destination_scope: NativeInferenceScope,
                               idempotency_key: str) -> StoredHandoff:
    """One-way storage commit to a fresh registered destination, without dispatch.

    Caller performs confirmation and current review/file validation in the same
    transaction. Exact replay keeps the original destination, never creates one.
    """
    record = await _owned(tx, user_id, handoff_id)
    _compare(record, target, handoff_sha256)
    destination_scope, key = _scope(destination_scope), _key(idempotency_key)
    if destination_scope.run_id == record.source_scope.run_id:
        _fail("conflict")
    if record.destination_scope and (record.destination_scope != destination_scope or record.commit_key != key):
        _fail("conflict")
    runs = await _runs(tx, user_id, record.source_scope, destination_scope)
    _registration(record, runs)
    if _workspace(runs[destination_scope.run_id]) != record.workspace_json:
        _fail("ownership")
    # Run locks serialize all writers, including different previews of this source.
    current = await _owned(tx, user_id, handoff_id)
    if current.destination_scope:
        if current.destination_scope != destination_scope or current.commit_key != key:
            _fail("conflict")
        return current
    await _fresh_source(tx, record, runs)
    destination = runs[destination_scope.run_id]
    await require_fresh_projection_tx(tx, run=destination, scope=destination_scope)
    if (destination.get("native_handoff_id") is not None
            or destination.get("native_generation_claim_id") is not None
            or destination.get("native_history_revision") is not None
            or await tx.fetchval("SELECT 1 FROM inference_route_commands WHERE native_run_id=$1::uuid LIMIT 1",
                                 destination_scope.run_id)):
        _fail("stale")
    record = await _lock_record(tx, record)
    now = await _now_unexpired(tx, record)
    row = await tx.fetchrow("""UPDATE native_task_handoffs SET destination_run_id=$2::uuid,
        destination_runtime_session_id=$3::uuid,destination_scope_json=$4,destination_workspace_json=$5,
        commit_key=$6,committed_at=$7 WHERE id=$1::uuid AND destination_run_id IS NULL RETURNING *""",
        record.handoff_id, destination_scope.run_id, destination_scope.runtime_session_id,
        _json(destination_scope.model_dump()), record.workspace_json, key, now)
    linked = await tx.fetchrow("UPDATE agent_runs SET native_handoff_id=$2::uuid "
        "WHERE id=$1::uuid AND native_handoff_id IS NULL RETURNING id",
        destination_scope.run_id, record.handoff_id)
    if linked is None:
        _fail("conflict")
    return _decode(row)


@_private_api
async def consume_first_generation_tx(tx: Any, *, user_id: str, handoff_id: str,
                                       handoff_sha256: str, target: HandoffTarget,
                                       route_command_id: str, request_id: str,
                                       dispatch_proof: dict | None = None) -> StoredHandoff:
    """Link exactly one owned revision-zero generation inside its reservation tx.

    Call AFTER durable route/gateway linkage, BEFORE transaction commit/provider
    dispatch. This checks ownership, not the eventual provider payload. Coordinator
    must bind portable context/settings/tools and reauthorize files before calling.
    On any failure the caller must roll back the complete reservation transaction.
    """
    route_command_id, request_id = _uuid(route_command_id), _uuid(request_id)
    record = await _owned(tx, user_id, handoff_id)
    _compare(record, target, handoff_sha256)
    proof_json = _dispatch_proof(record, route_command_id, request_id, dispatch_proof)
    if record.destination_scope is None:
        _fail("conflict")
    runs = await _runs(tx, user_id, record.source_scope, record.destination_scope)
    _registration(record, runs)
    current = await _owned(tx, user_id, handoff_id)
    if current.first_request_id:
        if ((current.first_request_id, current.first_route_command_id) != (request_id, route_command_id)
                or current.first_dispatch_json != proof_json):
            _fail("conflict")
        return current
    await _fresh_source(tx, record, runs)
    scope = record.destination_scope
    run = runs[scope.run_id]
    await require_fresh_projection_tx(tx, run=run, scope=scope, continuing=True)
    route = await tx.fetchrow("SELECT * FROM inference_route_commands WHERE id=$1::uuid", route_command_id)
    request = await tx.fetchrow("SELECT * FROM inference_requests WHERE id=$1::uuid", request_id)
    if (not route or not request or str(route["user_id"]) != user_id
            or str(route["native_run_id"]) != scope.run_id
            or str(route["gateway_request_id"]) != request_id
            or str(run["native_generation_claim_id"]) != route_command_id or run["native_history_revision"] != 0
            or route["native_history_revision"] != 0 or route["previous_native_request_id"] is not None
            or route["status"] != "processing" or request["status"] != "processing"
            or str(request["user_id"]) != user_id or str(request["native_route_command_id"]) != route_command_id
            or stored_scope(route) != {"scope_version": 1, "scope": scope.model_dump(),
                                       "registration": json.loads(record.workspace_json)}):
        _fail("ownership")
    if (await tx.fetchval("SELECT count(*) FROM inference_route_commands WHERE native_run_id=$1::uuid", scope.run_id) != 1
            or await tx.fetchval("SELECT 1 FROM agent_run_tool_calls WHERE run_id=$1::uuid LIMIT 1", scope.run_id)
            or await tx.fetchval("SELECT 1 FROM agent_chat_turns WHERE run_id=$1::uuid LIMIT 1", scope.run_id)):
        _fail("stale")
    if dispatch_proof is not None and request["payload_hash"] != dispatch_proof["request_sha256"]:
        _fail("integrity")
    record = await _lock_record(tx, record)
    now = await _now_unexpired(tx, record)
    row = await tx.fetchrow("""UPDATE native_task_handoffs SET first_route_command_id=$2::uuid,
        first_request_id=$3::uuid,consumed_at=$4,first_dispatch_json=$5
        WHERE id=$1::uuid AND first_request_id IS NULL RETURNING *""",
        handoff_id, route_command_id, request_id, now, proof_json)
    return _decode(row)


def _dispatch_proof(record, route_id, request_id, proof):
    if proof is None:
        return None  # Storage-only linkage is never evidence of provider dispatch.
    fixed = {"version": 1, "user_id": record.user_id,
             "destination_run_id": record.destination_scope.run_id if record.destination_scope else None,
             "destination_runtime_session_id": record.destination_scope.runtime_session_id if record.destination_scope else None,
             "route_command_id": route_id, "request_id": request_id,
             "handoff_id": record.handoff_id, "handoff_sha256": record.handoff_sha256,
             "document_sha256": record.document.sha256, "target_sha256": _sha(record.target.to_json())}
    hashes = {"claim_sha256", "request_sha256", "payload_sha256", "input_sha256"}
    if (type(proof) is not dict or set(proof) != set(fixed) | hashes | {"authorization_expires_at"}
            or type(proof["version"]) is not int or any(proof[key] != val for key, val in fixed.items())):
        _fail("integrity")
    expiry = _date(datetime.fromisoformat(proof["authorization_expires_at"]))
    if not record.created_at < expiry <= min(record.expires_at, record.target.review_expires_at):
        _fail("integrity")
    for key in hashes:
        _digest(proof[key])
    return _json(proof)
