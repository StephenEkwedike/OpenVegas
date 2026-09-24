"""Verify consumed handoff ancestry, never grant another dispatch or file access.

Enter before any run/route/gateway locks. Every ancestor run is locked in one
global order, then every first route, before inspecting settled provider evidence.
This primitive is private until continuation and public coordinator wiring pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openvegas.agent import native_handoff_store as store
from openvegas.agent.native_envelope import load_native_envelope_tx
from openvegas.agent.native_generation import NativeGenerationClaim, scope_document, stored_scope
from openvegas.agent.native_handoff_document import MAX_GENERATIONS, MAX_TASKS, PortableTaskDocument
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeInferenceScope
from server.services.native_handoff_attachments import _hash


def _fail():
    raise ContractError(APIErrorCode.HANDOFF_BLOCKED,
        "The original model handoff could not be verified; no history was reused.") from None


@dataclass(frozen=True, repr=False)
class ConsumedHandoff:
    handoff_id: str
    handoff_sha256: str
    document: PortableTaskDocument = field(repr=False)
    first_request_id: str
    ancestor_handoff_ids: tuple[str, ...]

    def __repr__(self):
        return "<ConsumedHandoff private>"

    def provenance(self):
        return {"handoff_id": self.handoff_id, "handoff_sha256": self.handoff_sha256,
                "document_sha256": self.document.sha256}


async def _lock_ancestry(tx, *, user_id, scope):
    """Read hints first, then lock globally and reject any changed binding."""
    store._uuid(user_id)
    store._scope(scope)
    scopes, snapshots, records = {}, {}, []
    current = scope
    while True:
        if current.run_id in scopes or len(scopes) > MAX_TASKS:
            _fail()
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid",
                                current.run_id, user_id)
        if not run or str(run["runtime_session_id"]) != current.runtime_session_id:
            _fail()
        identity = run.get("native_handoff_id")
        scopes[current.run_id] = current
        snapshots[current.run_id] = (str(identity) if identity else None, store._workspace(run))
        if identity is None:
            break
        record = await store._owned(tx, user_id, str(identity))
        if (record.destination_scope is None
                or record.destination_scope.run_id != current.run_id
                or record.destination_scope.runtime_session_id != current.runtime_session_id
                or record.first_dispatch_json is None or record.first_request_id is None
                or record.first_route_command_id is None):
            _fail()
        records.append(record)
        current = record.source_scope
    if not records:
        _fail()
    runs = {}
    for run_id, current in sorted(scopes.items()):
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
                                run_id, user_id)
        if (not run or str(run["runtime_session_id"]) != current.runtime_session_id
                or (str(run["native_handoff_id"]) if run.get("native_handoff_id") else None,
                    store._workspace(run)) != snapshots[run_id]):
            _fail()
        runs[run_id] = run
    for record in records:
        if await store._owned(tx, user_id, record.handoff_id) != record:
            _fail()
        store._registration(record, runs)
    # Acquiring these after the first gateway lock would invert native callback
    # order. All shared run locks are already held before taking any route lock.
    limit = (MAX_TASKS + 1) * MAX_GENERATIONS
    locked = await tx.fetch("SELECT id FROM inference_route_commands "
        "WHERE native_run_id=ANY($1::uuid[]) ORDER BY id LIMIT $2 FOR UPDATE", sorted(runs), limit + 1)
    if len(locked) > limit:
        _fail()
    locked_ids = {str(row["id"]) for row in locked}
    routes = {}
    for route_id in sorted(record.first_route_command_id for record in records):
        if route_id in routes or route_id not in locked_ids:
            _fail()
        routes[route_id] = await tx.fetchrow(
            "SELECT * FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE", route_id)
    return records, runs, routes


async def _verify_first(tx, *, record, route):
    scope = record.destination_scope
    if (not route or str(route["user_id"]) != record.user_id
            or str(route["native_run_id"]) != scope.run_id
            or str(route["gateway_request_id"]) != record.first_request_id
            or route["native_history_revision"] != 0 or route["previous_native_request_id"] is not None
            or route["status"] != "succeeded" or route["response_status"] != 200):
        _fail()
    ownership = stored_scope(route)
    if ownership != {"scope_version": 1, "scope": scope.model_dump(),
                     "registration": json.loads(record.destination_workspace_json)}:
        _fail()
    from server.services.inference_replay import _envelope
    body = _envelope(route, user_id=record.user_id, key=route["idempotency_key"],
                     digest=route["payload_hash"])
    if body["state"] != "completed" or body["gateway_request_id"] != record.first_request_id:
        _fail()
    claim = NativeGenerationClaim(record.user_id, record.first_route_command_id, scope,
        scope_document(scope, ownership["registration"]), body["owner_token"],
        route["payload_hash"], body["gateway_key"], 0)
    from server.services.native_handoff_dispatch import _claim_hash
    proof = json.loads(record.first_dispatch_json)
    envelope = await load_native_envelope_tx(tx, user_id=record.user_id, run_id=scope.run_id,
        runtime_session_id=scope.runtime_session_id, request_id=record.first_request_id,
        provider=record.target.provider, model=record.target.model,
        expected_route_command_id=record.first_route_command_id)
    inputs, payload = envelope.history_inputs(), envelope.request_payload()
    if (proof["claim_sha256"] != _claim_hash(claim)
            or proof["request_sha256"] != envelope.request_hash
            or proof["payload_sha256"] != _hash(payload)
            or proof["input_sha256"] != store._sha(envelope.history_inputs_json)
            or inputs.get("incoming_handoff") != {
                "handoff_id": record.handoff_id, "handoff_sha256": record.handoff_sha256,
                "document_sha256": record.document.sha256}
            or set(inputs) != {"attachment_refs", "settings", "incoming_handoff"}
            or not record.created_at <= record.committed_at <= record.consumed_at
                < store._date(datetime.fromisoformat(proof["authorization_expires_at"]))):
        _fail()
    settings = inputs["settings"]
    if any(settings.get(key) != getattr(record.target, key) for key in (
            "provider", "model", "enable_tools", "enable_web_search", "reasoning_effort", "max_tokens")):
        _fail()
    if (payload.get("model") != record.target.model or payload.get("max_tokens") != record.target.max_tokens
            or _hash(payload.get("tools")) != record.target.tool_definitions_sha256):
        _fail()


async def verify_consumed_handoff_tx(
    tx: Any, *, user_id: str, scope: NativeInferenceScope,
) -> ConsumedHandoff:
    """Authenticate historical context; reviews/files must be authorized anew.

The original ten-minute dispatch deadline is checked at recorded consumption,
not against the current clock. Expired historical proof is not a new permission.
No provider, wallet, upload read, mutation or public DTO is produced here.
"""
    store._enabled()
    try:
        records, _, routes = await _lock_ancestry(tx, user_id=user_id, scope=scope)
        prior_tasks = []
        for record in reversed(records):
            await _verify_first(tx, record=record, route=routes[record.first_route_command_id])
            tasks = record.document.values()["tasks"]
            if len(tasks) != len(prior_tasks) + 1 or tasks[:-1] != prior_tasks:
                _fail()
            prior_tasks = tasks
        latest = records[0]
        return ConsumedHandoff(latest.handoff_id, latest.handoff_sha256, latest.document,
                               latest.first_request_id, tuple(r.handoff_id for r in reversed(records)))
    except Exception:  # noqa: BLE001 - private SQL/parser errors must never reach HTTP or logs.
        _fail()
