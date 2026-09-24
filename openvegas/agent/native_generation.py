"""Private generation ownership. Lock order: run, route command, gateway, tool.

The public DTO is not a claim. A claim is minted only after durable route reserve,
then rechecked in the gateway's wallet reservation transaction before dispatch.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeInferenceScope, validate_native_scope


def reject(detail: str) -> None:
    raise ContractError(APIErrorCode.INVALID_TRANSITION, detail)


def parse_scope(value: Any) -> NativeInferenceScope:
    try:
        return validate_native_scope(value)
    except (ValidationError, ValueError, TypeError):
        reject("Invalid native generation scope.")


def registration(run: Any) -> dict:
    return {name: run.get(name) for name in ("workspace_root", "workspace_fingerprint", "git_root")}


def scope_document(scope: NativeInferenceScope, run: Any) -> str:
    doc = json.dumps({"scope_version": 1, "scope": scope.model_dump(),
                      "registration": registration(run)}, sort_keys=True, separators=(",", ":"))
    if len(doc.encode()) > 8192:
        reject("Native registration exceeds its storage bound.")
    return doc


def stored_scope(row: Any) -> dict:
    try:
        doc = row["native_scope"]
        if isinstance(doc, str):
            if len(doc.encode()) > 8192:
                raise ValueError
            doc = json.loads(doc)
        if (type(doc) is not dict or set(doc) != {"scope_version", "scope", "registration"}
                or type(doc["scope_version"]) is not int or doc["scope_version"] != 1
                or type(doc["registration"]) is not dict
                or set(doc["registration"]) != {"workspace_root", "workspace_fingerprint", "git_root"}):
            raise ValueError
        parse_scope(doc["scope"])
        return doc
    except (KeyError, TypeError, ValueError):
        reject("Native ownership record is incomplete; reconciliation required.")


async def lock_run_tx(tx: Any, *, user_id: str, scope: NativeInferenceScope) -> Any:
    run = await tx.fetchrow(
        "SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
        scope.run_id, user_id,
    )
    if not run or str(run["runtime_session_id"]) != scope.runtime_session_id:
        reject("Native run owner/session does not match the registered scope.")
    if not run["workspace_root"] or not run["workspace_fingerprint"]:
        reject("Native generation requires a registered workspace.")
    return run


async def require_fresh_projection_tx(
    tx: Any, *, run: Any, scope: NativeInferenceScope, continuing: bool = False,
) -> None:
    if (run["state"] not in {"created", "running"} or run.get("cancel_requested_at")
            or (run.get("expires_at") is not None and run["expires_at"] <= datetime.now(UTC))):
        reject("Native generation requires an active, non-cancelling run.")
    # Use the authoritative projection implementation, not a second action model.
    from openvegas.agent.orchestration_contracts import valid_actions_signature
    from openvegas.agent.orchestration_service import AgentOrchestrationService

    actions = await AgentOrchestrationService(None)._derive_valid_actions_tx(
        tx=tx, run=run, actor_id=str(run["user_id"]), actor_role_class="user",
    )
    if (run["version"] != scope.expected_run_version
            or valid_actions_signature(run["version"], actions) != scope.expected_valid_actions_signature):
        raise ContractError(APIErrorCode.STALE_PROJECTION, "Native generation projection is stale.")
    if not continuing and (await tx.fetchrow("SELECT 1 FROM agent_run_tool_calls WHERE run_id=$1::uuid LIMIT 1", scope.run_id)
            or await tx.fetchrow("SELECT 1 FROM agent_chat_turns WHERE run_id=$1::uuid LIMIT 1", scope.run_id)):
        reject("Native follow-up is not implemented; only one generation on a fresh run is supported.")


@dataclass(frozen=True)
class NativeGenerationClaim:
    user_id: str
    route_command_id: str
    scope: NativeInferenceScope
    scope_json: str = field(repr=False)
    owner_token: str = field(repr=False)
    command_hash: str
    gateway_key: str = field(repr=False)
    history_revision: int | None = None
    previous_request_id: str | None = None
    continuation_payload_json: str | None = field(default=None, repr=False)
    history_inputs_json: str | None = field(default=None, repr=False)


async def lock_dispatch_claim_tx(tx: Any, claim: NativeGenerationClaim, req: Any) -> None:
    if type(claim) is not NativeGenerationClaim:
        reject("A server-issued native generation claim is required.")
    if (req.account_id != "user:" + claim.user_id or req.idempotency_key != claim.gateway_key
            or req.provider != "openrouter" or not req.enable_tools):
        reject("Native generation account or request does not match its claim.")
    run = await lock_run_tx(tx, user_id=claim.user_id, scope=claim.scope)
    from server.services.native_handoff_guard import binding_for
    binding = binding_for(req)
    if run.get("native_handoff_id") is not None or binding is not None:
        from server.services.native_handoff_guard import validate_bound_request
        binding = validate_bound_request(req)
        if str(run.get("native_handoff_id")) != binding.handoff_id:
            reject("Native handoff ownership is required; no context-free dispatch is allowed.")
    await require_fresh_projection_tx(tx, run=run, scope=claim.scope,
                                      continuing=claim.previous_request_id is not None)
    row = await tx.fetchrow(
        "SELECT * FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE", claim.route_command_id,
    )
    if (not row or str(run.get("native_generation_claim_id")) != claim.route_command_id
            or str(row["user_id"]) != claim.user_id or str(row["native_run_id"]) != claim.scope.run_id
            or row["payload_hash"] != claim.command_hash or row["status"] != "processing"
            or row["gateway_request_id"] is not None
            or stored_scope(row) != json.loads(claim.scope_json)
            or registration(run) != stored_scope(row)["registration"]):
        reject("Native generation is already dispatched or its ownership changed; no retry was made.")
    body = json.loads(row["response_body_text"])
    if body.get("owner_token") != claim.owner_token or body.get("gateway_key") != claim.gateway_key:
        reject("Native generation claim does not match its durable reservation.")
    if claim.history_revision is not None and (run.get("native_history_revision") != claim.history_revision
                or row.get("native_history_revision") != claim.history_revision
                or (str(row["previous_native_request_id"]) if row.get("previous_native_request_id") else None)
                != claim.previous_request_id):
        reject("Native history revision changed before dispatch.")


async def link_gateway_tx(tx: Any, claim: NativeGenerationClaim, request_id: str) -> None:
    row = await tx.fetchrow(
        "UPDATE inference_route_commands SET gateway_request_id=$2::uuid,updated_at=now() "
        "WHERE id=$1::uuid AND gateway_request_id IS NULL AND status='processing' RETURNING id",
        claim.route_command_id, request_id,
    )
    if row is None:
        reject("Native generation gateway linkage was already reserved.")
    await tx.execute(
        "UPDATE inference_requests SET native_route_command_id=$2::uuid WHERE id=$1::uuid",
        request_id, claim.route_command_id,
    )


async def verify_source_scope_tx(tx: Any, *, run: Any, source: Any, request_id: str) -> dict | None:
    """Caller holds run and source locks; linked ownership is immutable.

    Read the route without taking a later route lock. No ownership writer can
    remove/change linked rows, and registration writers must lock this run.
    """
    source_claim = source.get("native_route_command_id")
    run_claim = run.get("native_generation_claim_id")
    if source_claim is None and run_claim is None:
        return None
    if not source_claim or not run_claim:
        reject("Native generation belongs to another run, or lacks original scope ownership.")
    row = await tx.fetchrow("SELECT * FROM inference_route_commands WHERE id=$1::uuid", str(source_claim))
    if (not row or str(row["user_id"]) != str(run["user_id"])
            or str(row["native_run_id"]) != str(run["id"])
            or str(row["gateway_request_id"]) != request_id):
        reject("Native generation ownership linkage does not match this proposal.")
    if str(source_claim) != str(run_claim) and (
                row.get("native_history_revision") is None or run.get("native_history_revision") is None
                or row["native_history_revision"] >= run["native_history_revision"]
                or row["status"] != "succeeded"):
        reject("Native generation revision is not a completed owned ancestor.")
    doc = stored_scope(row)
    scope = parse_scope(doc["scope"])
    if (scope.run_id != str(run["id"]) or scope.runtime_session_id != str(run["runtime_session_id"])
            or doc["registration"] != registration(run)):
        reject("Native generation belongs to a different original workspace/session.")
    return {"scope_version": 1, **scope.model_dump()}
