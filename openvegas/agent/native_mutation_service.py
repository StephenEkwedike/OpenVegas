"""Private native mutation preparation; never a remote filesystem attestation.

Only transformations over authenticated runtime observations are verified here.
No provider calls, disk reads, approval consumption, or tool execution occurs.
Internal loaders return private records and must never be used as HTTP responses.
Lock order: run -> route -> gateway/envelope -> preparation -> tool -> approval.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from openvegas.agent.native_envelope import load_native_envelope_tx
from openvegas.agent.native_history import (
    _original_call_projection,
    _workspace_snapshot,
    lock_native_source_tx,
    object_value,
    require_active_native_run,
    require_call_id,
    require_uuid,
)
from openvegas.agent.native_mutation import (
    MutationPlan,
    NativeMutationError,
    build_mutation_plan,
    validate_plan,
)
from openvegas.agent.orchestration_contracts import canonical_json, valid_actions_signature
from openvegas.agent.runtime_contracts import tool_payload_hash
from openvegas.agent.tool_cas import redaction_required
from openvegas.contracts.errors import APIErrorCode, ContractError

_BOUND = 524_288
_SAFE = {
    "disabled": "Native mutation preparation is disabled.",
    "invalid": "Native mutation request is invalid.",
    "ownership": "Native mutation ownership or original source does not match.",
    "stale": "Native mutation projection is stale.",
    "conflict": "Native mutation idempotency or original call conflicts.",
    "expired": "Native mutation preparation expired.",
    "integrity": "Native mutation preparation integrity check failed.",
    "storage": "Native mutation private storage is unavailable.",
    "approval": "Native mutation approval does not match its proposed tool.",
}


def _fail(reason="invalid"):
    code = {"stale": APIErrorCode.STALE_PROJECTION, "conflict": APIErrorCode.IDEMPOTENCY_CONFLICT,
            "approval": APIErrorCode.APPROVAL_REQUIRED}.get(reason, APIErrorCode.INVALID_TRANSITION)
    raise ContractError(code, _SAFE[reason]) from None


def _enabled():
    if any(os.getenv(name, "0") != "1" for name in (
        "OPENVEGAS_NATIVE_MUTATIONS", "OPENVEGAS_NATIVE_GENERATION_HISTORY",
        "OPENVEGAS_NATIVE_GENERATION_SCOPE",
    )):
        _fail("disabled")


def _json(value, bound=_BOUND):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > bound:
            _fail()
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _key(value):
    if type(value) is not str or not re.fullmatch(r"[!-~]{1,200}", value):
        _fail()
    return value


def _projection_args(version, signature):
    if (type(version) is not int or not 0 <= version < 2**63 or type(signature) is not str
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", signature)):
        _fail()


def _observe(value):
    if (type(value) is not dict or set(value) != {"exists", "content_utf8"}
            or type(value["exists"]) is not bool
            or (not value["exists"] and value["content_utf8"] is not None)
            or (value["exists"] and (type(value["content_utf8"]) is not str
                                     or len(value["content_utf8"]) > 32768))):
        _fail()
    return json.loads(_json(value, 262144))


def _reject_sensitive(*values):
    # Never redact committed source or patch bytes: reject the transformation.
    if redaction_required(list(values)):
        raise NativeMutationError("native_mutation_sensitive")


def _schema(value, schema, depth=0):
    """Validate the original stored bounded object/primitive tool schema, not today's schema."""
    if depth > 6 or type(schema) is not dict:
        _fail("integrity")
    allowed = {"type", "properties", "required", "additionalProperties", "description", "enum", "minimum", "maximum"}
    if set(schema) - allowed:
        _fail("integrity")
    kind = schema.get("type")
    types = {"object": dict, "string": str, "integer": int, "boolean": bool}
    if kind not in types or type(value) is not types[kind]:
        _fail("integrity")
    if "enum" in schema and value not in schema["enum"]:
        _fail("integrity")
    if kind == "integer" and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        _fail("integrity")
    if kind == "object":
        fields = schema.get("properties", {})
        if (type(fields) is not dict or set(value) - fields.keys()
                or not set(schema.get("required", [])) <= value.keys()):
            _fail("integrity")
        for name, item in value.items():
            _schema(item, fields[name], depth + 1)


def _require_readable_run(run, *, historical_observation=False):
    # Receipt verification after an uncertain commit is not a new execution grant.
    if (historical_observation and run["state"] == "interrupted"
            and run.get("state_reason_code") == "mutation_uncertain"
            and run.get("is_resumable") is False
            and not run.get("cancel_requested_at")
            and (run.get("expires_at") is None or run["expires_at"] > datetime.now(UTC))):
        return
    require_active_native_run(run)


async def _call_tx(tx, *, run, request_id, call_id, require_latest=True,
                   historical_observation=False):
    _require_readable_run(run, historical_observation=historical_observation)
    source, scope = await lock_native_source_tx(tx, run=run, request_id=request_id)
    route_revision = await tx.fetchval("SELECT native_history_revision FROM inference_route_commands WHERE id=$1::uuid", source["native_route_command_id"])
    if (scope is None or run.get("native_history_revision") is None
            or route_revision is None or (require_latest and route_revision != run["native_history_revision"])
            or (require_latest and str(source["native_route_command_id"]) != str(run["native_generation_claim_id"]))):
        _fail("ownership")
    model = await tx.fetchval("SELECT model_id FROM inference_preauthorizations WHERE request_id=$1", request_id)
    envelope = await load_native_envelope_tx(tx, user_id=str(run["user_id"]), run_id=str(run["id"]),
        runtime_session_id=str(run["runtime_session_id"]), request_id=request_id, provider="openrouter", model=model)
    if not envelope.continuation_safe:
        _fail("ownership")
    raw_calls = envelope.assistant_message().get("tool_calls")
    if type(raw_calls) is not list or not 1 <= len(raw_calls) <= 16:
        _fail("integrity")
    projected = [_original_call_projection(call, model) for call in raw_calls]
    ids = [item["provider_call_id"] for item in projected]
    if len(set(ids)) != len(ids) or call_id not in ids:
        _fail("ownership")
    public = object_value(source["response_body_text"])
    if canonical_json(projected) != canonical_json(public.get("tool_calls")):
        _fail("integrity")
    ordinal = ids.index(call_id)
    call = projected[ordinal]
    if call["tool_name"] not in {"Write", "FindAndReplace", "InsertAtEnd"}:
        _fail()
    function = raw_calls[ordinal]["function"]
    definitions = envelope.request_payload().get("tools")
    if type(definitions) is not list:
        _fail("integrity")
    matching = [d["function"] for d in definitions if type(d) is dict and d.get("type") == "function"
                and type(d.get("function")) is dict and d["function"].get("name") == function["name"]]
    if len(matching) != 1:
        _fail("integrity")
    _schema(object_value(function["arguments"]), matching[0]["parameters"])
    return call, ordinal


def executable_arguments(preparation_id, plan):
    document = plan.document()
    fields = ("version", "relative_path", "before_exists", "before_sha256", "before_bytes", "after_sha256", "after_bytes")
    return {"native_mutation": {**{key: document[key] for key in fields},
            "preparation_id": preparation_id, "contract_sha256": document["contract_sha256"]}, "patch": document["patch"]}


def _public(row, plan):
    d = plan.document()
    return {"preparation_id": str(row["id"]), "contract_sha256": d["contract_sha256"],
            "tool_name": "fs_apply_patch", "shell_mode": "mutating",
            "timeout_sec": min(5, d["original_call"].get("timeout_sec", 30)),
            "arguments": executable_arguments(str(row["id"]), plan), "diff": d["patch"],
            "before_exists": d["before_exists"], "before_sha256": d["before_sha256"],
            "before_bytes": d["before_bytes"], "after_sha256": d["after_sha256"],
            "after_bytes": d["after_bytes"], "no_change": d["no_change"],
            "expires_at": row["expires_at"].isoformat(), "evidence_kind": "runtime_observed_file_v1"}


async def load_preparation_tx(tx, *, run, preparation_id, runtime_session_id=None,
                              contract_sha256=None, native_inference_request_id=None,
                              native_provider_call_id=None, require_unexpired=True, require_latest=True) -> tuple[dict, MutationPlan]:
    """Caller MUST hold the owned run lock; returns PRIVATE record and validated plan.

    Relaxed expiry/latest checks are only for later server receipt verification,
    never to authorize a new proposal/approval/start. This function never approves.
    """
    _enabled()
    try:
        require_uuid(preparation_id)
        historical_observation = require_latest is False and require_unexpired is False
        _require_readable_run(run, historical_observation=historical_observation)
        if runtime_session_id is not None and str(run["runtime_session_id"]) != runtime_session_id:
            _fail("ownership")
        row = await tx.fetchrow("SELECT * FROM native_mutation_preparations WHERE id=$1::uuid AND run_id=$2::uuid AND user_id=$3::uuid",
                                preparation_id, str(run["id"]), str(run["user_id"]))
        if not row or str(row["runtime_session_id"]) != str(run["runtime_session_id"]):
            _fail("ownership")
        request_id, call_id = str(row["native_inference_request_id"]), row["native_provider_call_id"]
        if ((native_inference_request_id is not None and request_id != native_inference_request_id)
                or (native_provider_call_id is not None and call_id != native_provider_call_id)):
            _fail("ownership")
        call, ordinal = await _call_tx(tx, run=run, request_id=request_id, call_id=call_id,
            require_latest=require_latest, historical_observation=historical_observation)
        row = await tx.fetchrow("SELECT * FROM native_mutation_preparations WHERE id=$1::uuid FOR UPDATE", preparation_id)
        if require_unexpired and row["expires_at"] <= datetime.now(UTC):
            _fail("expired")
        if row["workspace_json"] != _json(_workspace_snapshot(run), 16384) or row["original_ordinal"] != ordinal:
            _fail("ownership")
        if len(row["plan_json"].encode()) > _BOUND or len(row["observed_source_json"].encode()) > 262144:
            _fail("integrity")
        observed = _observe(json.loads(row["observed_source_json"]))
        document = json.loads(row["plan_json"])
        _reject_sensitive(call, observed, document)
        plan = validate_plan(document)
        rebuilt = build_mutation_plan(call, observed)
        if (_json(plan.document()) != _json(rebuilt.document()) or plan.contract_sha256 != row["contract_sha256"]
                or (contract_sha256 is not None and contract_sha256 != plan.contract_sha256)):
            _fail("integrity")
        return dict(row), plan
    except NativeMutationError:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Native mutation transformation rejected.") from None
    except ContractError as error:
        if error.detail in _SAFE.values():
            raise
        _fail("ownership")
    except Exception:  # noqa: BLE001 - SQL diagnostics can contain private source bytes.
        _fail("storage")


class NativeMutationService:
    def __init__(self, db):
        self.db = db

    async def _run(self, tx, user_id, run_id, runtime_session_id):
        for value in (user_id, run_id, runtime_session_id):
            require_uuid(value)
        run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE", run_id, user_id)
        if not run or str(run["runtime_session_id"]) != runtime_session_id:
            _fail("ownership")
        require_active_native_run(run)
        return run

    async def _projection(self, tx, run, version, signature):
        from openvegas.agent.orchestration_service import AgentOrchestrationService

        _projection_args(version, signature)
        actions = await AgentOrchestrationService(self.db)._derive_valid_actions_tx(
            tx=tx, run=run, actor_id=str(run["user_id"]), actor_role_class="user")
        if version != run["version"] or signature != valid_actions_signature(run["version"], actions):
            _fail("stale")

    async def prepare(self, *, user_id, run_id, runtime_session_id, native_inference_request_id,
                      native_provider_call_id, expected_run_version, expected_valid_actions_signature,
                      idempotency_key, observed_source, plan_mode=False) -> dict:
        _enabled()
        try:
            if plan_mode is not False:
                _fail()
            require_uuid(native_inference_request_id)
            require_call_id(native_provider_call_id)
            _key(idempotency_key)
            _projection_args(expected_run_version, expected_valid_actions_signature)
            observed = _observe(observed_source)
            request_hash = _hash({"user_id": user_id, "run_id": run_id, "runtime_session_id": runtime_session_id,
                "request_id": native_inference_request_id, "call_id": native_provider_call_id,
                "version": expected_run_version, "signature": expected_valid_actions_signature,
                "idempotency_key": idempotency_key, "observed_source": observed, "plan_mode": False})
            async with self.db.transaction() as tx:
                run = await self._run(tx, user_id, run_id, runtime_session_id)
                await self._projection(tx, run, expected_run_version, expected_valid_actions_signature)
                call, ordinal = await _call_tx(tx, run=run, request_id=native_inference_request_id, call_id=native_provider_call_id)
                _reject_sensitive(call, observed)
                plan = build_mutation_plan(call, observed)
                _reject_sensitive(plan.document())
                rows = await tx.fetch("SELECT * FROM native_mutation_preparations WHERE (user_id=$1::uuid AND idempotency_key=$2) OR (native_inference_request_id=$3::uuid AND native_provider_call_id=$4) FOR UPDATE",
                                      user_id, idempotency_key, native_inference_request_id, native_provider_call_id)
                if rows:
                    if len(rows) != 1 or rows[0]["request_sha256"] != request_hash or rows[0]["idempotency_key"] != idempotency_key:
                        _fail("conflict")
                    row, stored = await load_preparation_tx(tx, run=run, preparation_id=str(rows[0]["id"]),
                                                          contract_sha256=plan.contract_sha256)
                    return _public(row, stored)
                bound = await tx.fetchval("SELECT 1 FROM agent_chat_turns WHERE content_json->>'inference_request_id'=$1 AND content_json->>'provider_call_id'=$2 LIMIT 1",
                                          native_inference_request_id, native_provider_call_id)
                if bound:
                    _fail("conflict")
                now = await tx.fetchval("SELECT now()")
                if run.get("expires_at") is not None:
                    expiry = min(now + timedelta(minutes=10), run["expires_at"])
                else:
                    expiry = now + timedelta(minutes=10)
                row = await tx.fetchrow("""INSERT INTO native_mutation_preparations
                    (user_id,run_id,runtime_session_id,native_inference_request_id,native_provider_call_id,original_ordinal,
                     workspace_json,observed_source_json,plan_json,contract_sha256,request_sha256,idempotency_key,created_at,expires_at)
                    VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING *""",
                    user_id, run_id, runtime_session_id, native_inference_request_id, native_provider_call_id, ordinal,
                    _json(_workspace_snapshot(run), 16384), _json(observed, 262144), _json(plan.document()),
                    plan.contract_sha256, request_hash, idempotency_key, now, expiry)
                return _public(row, plan)
        except NativeMutationError:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Native mutation transformation rejected.") from None
        except ContractError as error:
            if error.detail in _SAFE.values():
                raise
            _fail("ownership")
        except Exception:  # noqa: BLE001 - SQL diagnostics can contain private source bytes.
            _fail("storage")

    async def approve(self, *, user_id, run_id, runtime_session_id, preparation_id, tool_call_id,
                      contract_sha256, expected_run_version, expected_valid_actions_signature, idempotency_key) -> dict:
        """Record the exact owner decision, not consumption or execution permission.

        Caller must consume via AgentOrchestrationService.consume_approval and use
        its new projection before start. A replay is a receipt, not another grant.
        """
        _enabled()
        try:
            for value in (preparation_id, tool_call_id):
                require_uuid(value)
            _key(idempotency_key)
            _projection_args(expected_run_version, expected_valid_actions_signature)
            if type(contract_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", contract_sha256):
                _fail()
            request_hash = _hash({"user_id": user_id, "run_id": run_id, "runtime_session_id": runtime_session_id,
                "preparation_id": preparation_id, "tool_call_id": tool_call_id, "contract_sha256": contract_sha256,
                "expected_run_version": expected_run_version,
                "expected_valid_actions_signature": expected_valid_actions_signature, "idempotency_key": idempotency_key})
            async with self.db.transaction() as tx:
                run = await self._run(tx, user_id, run_id, runtime_session_id)
                row, plan = await load_preparation_tx(tx, run=run, preparation_id=preparation_id,
                    runtime_session_id=runtime_session_id, contract_sha256=contract_sha256)
                tool = await tx.fetchrow("SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid AND run_id=$2::uuid FOR UPDATE", tool_call_id, run_id)
                public = _public(row, plan)
                expected = {key: public[key] for key in ("tool_name", "arguments", "shell_mode", "timeout_sec")}
                payload_hash = tool_payload_hash("fs_apply_patch", expected["arguments"], "mutating")
                if (not tool or str(row["tool_call_id"]) != tool_call_id or tool["tool_name"] != "fs_apply_patch"
                        or tool["tool_class"] != "mutating" or tool["approval_required"] is not True
                        or tool["payload_hash"] != payload_hash
                        or canonical_json(object_value(tool["request_payload_json"])) != canonical_json(expected)):
                    _fail("approval")
                context = approval_context_hash(preparation_id, contract_sha256, payload_hash)
                replay = await tx.fetchrow("SELECT * FROM native_mutation_approval_commands WHERE user_id=$1::uuid AND idempotency_key=$2", user_id, idempotency_key)
                if replay:
                    if (replay["request_sha256"] != request_hash or str(replay["preparation_id"]) != preparation_id
                            or str(replay["tool_call_id"]) != tool_call_id or str(replay["approval_id"]) != str(row["approval_id"])):
                        _fail("conflict")
                    approval = await tx.fetchrow("SELECT * FROM agent_tool_approvals WHERE id=$1::uuid FOR UPDATE", str(replay["approval_id"]))
                    if (not approval or approval["approval_context_hash"] != context
                            or str(approval["actor_id"]) != user_id or str(approval["decision_actor_id"]) != user_id
                            or approval["run_version_approved"] != expected_run_version):
                        _fail("approval")
                    response = json.loads(replay["response_json"])
                    _json(response, 16384)
                    if response.get("approval_id") != str(approval["id"]):
                        _fail("integrity")
                    return response
                if row["approval_id"] is not None or tool["status"] != "proposed":
                    _fail("conflict")
                await self._projection(tx, run, expected_run_version, expected_valid_actions_signature)
                if tool["run_version"] != run["version"]:
                    _fail("stale")
                approval_id = str(uuid4())
                await tx.execute("""INSERT INTO agent_tool_approvals
                    (id,run_id,tool_call_id,actor_id,decision_actor_id,decision_source,run_version_approved,approval_context_hash,decision_state)
                    VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$4::uuid,'native_mutation_v1',$5,$6,'approved')""",
                    approval_id, run_id, tool_call_id, user_id, run["version"], context)
                await tx.execute("UPDATE native_mutation_preparations SET approval_id=$2::uuid WHERE id=$1::uuid", preparation_id, approval_id)
                from openvegas.agent.orchestration_service import AgentOrchestrationService

                response = await AgentOrchestrationService(self.db)._success_envelope_tx(
                    tx=tx, run=run, actor_id=user_id, actor_role_class="user")
                response.update(approval_id=approval_id, preparation_id=preparation_id, contract_sha256=contract_sha256, run_id=run_id)
                await tx.execute("""INSERT INTO native_mutation_approval_commands
                    (user_id,idempotency_key,preparation_id,tool_call_id,approval_id,request_sha256,response_json)
                    VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5::uuid,$6,$7)""",
                    user_id, idempotency_key, preparation_id, tool_call_id, approval_id, request_hash, _json(response, 16384))
                return response
        except NativeMutationError:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Native mutation transformation rejected.") from None
        except ContractError as error:
            if error.detail in _SAFE.values():
                raise
            _fail("ownership")
        except Exception:  # noqa: BLE001 - SQL diagnostics can contain private source bytes.
            _fail("storage")


def approval_context_hash(preparation_id, contract_sha256, payload_hash):
    """Shared server commitment; no approval decision can change executable bytes."""
    return _hash({"preparation_id": preparation_id, "contract_sha256": contract_sha256, "tool_payload_hash": payload_hash})
