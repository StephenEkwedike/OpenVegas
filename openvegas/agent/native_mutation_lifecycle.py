"""Atomic approval and observation checks, not independent filesystem attestation."""
from __future__ import annotations

import re

from openvegas.agent.native_history import object_value
from openvegas.agent.native_mutation_service import (
    approval_context_hash,
    executable_arguments,
    load_preparation_tx,
)
from openvegas.agent.orchestration_contracts import canonical_json
from openvegas.agent.runtime_contracts import tool_payload_hash
from openvegas.contracts.errors import APIErrorCode, ContractError

_REASONS = frozenset({
    "native_runtime_unsupported", "native_runtime_path", "native_runtime_unsafe_file",
    "native_runtime_metadata", "native_runtime_source_limit", "native_runtime_source_encoding",
    "native_runtime_source_changed", "native_runtime_parent_changed", "native_runtime_busy",
    "native_runtime_plan_invalid", "native_runtime_io", "native_runtime_create_collision",
    "native_runtime_readback_mismatch", "native_runtime_interrupted", "native_runtime_temp_changed",
})


def _fail():
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Native mutation observation or approval does not match.")


def _observation(value):
    if value is None:
        return
    if (type(value) is not dict or set(value) != {"exists", "sha256", "bytes"}
            or type(value["exists"]) is not bool or type(value["bytes"]) is not int
            or not 0 <= value["bytes"] <= 32768):
        _fail()
    if value["exists"]:
        if type(value["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            _fail()
    elif value["sha256"] is not None or value["bytes"] != 0:
        _fail()


def validated_proof(plan, proof: dict, result_status: str) -> str:
    """Return the honest commit classification for a bounded runtime claim."""
    keys = {"kind", "contract_sha256", "relative_path", "observed_before", "observed_after", "outcome", "reason"}
    if (type(proof) is not dict or set(proof) != keys
            or proof["kind"] != "runtime_observed_file_v1"
            or type(proof["outcome"]) is not str
            or (proof["reason"] is not None and type(proof["reason"]) is not str)
            or proof["contract_sha256"] != plan.contract_sha256
            or proof["relative_path"] != plan.relative_path):
        _fail()
    before, after = proof["observed_before"], proof["observed_after"]
    _observation(before)
    _observation(after)
    if proof["outcome"] in {"applied", "no_change"}:
        if (result_status != "succeeded" or proof["reason"] is not None
                or (proof["outcome"] == "no_change") is not plan.no_change
                or before != {"exists": plan.before_exists, "sha256": plan.before_sha256, "bytes": plan.before_bytes}
                or after != {"exists": True, "sha256": plan.after_sha256, "bytes": plan.after_bytes}):
            _fail()
        return "committed"
    if result_status != "failed" or proof["reason"] not in _REASONS:
        _fail()
    if proof["outcome"] == "not_applied":
        return "commit_failed"
    if proof["outcome"] == "unknown":
        return "commit_unknown"
    _fail()


async def load_tool_preparation_tx(tx, *, run, tool_call_id, for_start=False):
    """Run is locked first; inspect without a tool lock before source/preparation."""
    if run.get("native_generation_claim_id") is None:
        return None
    preview = await tx.fetchrow(
        "SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid AND run_id=$2::uuid",
        tool_call_id, str(run["id"]),
    )
    if not preview:
        return None
    request = object_value(preview["request_payload_json"])
    arguments = object_value(request.get("arguments"))
    marker = arguments.get("native_mutation")
    if marker is None:
        if preview["tool_name"] == "fs_apply_patch":
            _fail()
        return None
    if type(marker) is not dict:
        _fail()
    preparation, plan = await load_preparation_tx(
        tx, run=run, preparation_id=marker.get("preparation_id"),
        contract_sha256=marker.get("contract_sha256"),
        require_unexpired=for_start, require_latest=for_start,
    )
    expected_args = executable_arguments(str(preparation["id"]), plan)
    expected_request = {"tool_name": "fs_apply_patch", "shell_mode": "mutating",
                        "arguments": expected_args, "timeout_sec": min(5, plan.document()["original_call"].get("timeout_sec", 30))}
    if (str(preparation["tool_call_id"]) != tool_call_id or preview["tool_class"] != "mutating"
            or preview["approval_required"] is not True
            or preview["payload_hash"] != tool_payload_hash("fs_apply_patch", expected_args, "mutating")
            or canonical_json(request) != canonical_json(expected_request)):
        _fail()
    return preparation, plan


async def require_consumed_approval_tx(tx, *, run, tool, preparation, plan):
    approval = await tx.fetchrow("SELECT * FROM agent_tool_approvals WHERE id=$1::uuid FOR UPDATE",
                                 str(preparation["approval_id"]) if preparation["approval_id"] else None)
    if (not approval or str(approval["run_id"]) != str(run["id"])
            or str(approval["tool_call_id"]) != str(tool["id"])
            or str(approval["actor_id"]) != str(run["user_id"])
            or str(approval["decision_actor_id"]) != str(run["user_id"])
            or approval["decision_source"] != "native_mutation_v1"
            or approval["decision_state"] != "consumed" or approval["consumed_at"] is None
            or approval["approval_context_hash"] != approval_context_hash(
                str(preparation["id"]), plan.contract_sha256, tool["payload_hash"])
            or approval["run_version_approved"] != tool["run_version"]
            or run["version"] != tool["run_version"] + 1):
        raise ContractError(APIErrorCode.APPROVAL_REQUIRED, "Approve the exact native file edit before starting it.")


async def store_observation_tx(tx, *, tool_call_id, preparation, plan, result_status, result_payload,
                               submission_hash, stdout, stderr):
    if stdout or stderr or type(result_payload) is not dict or set(result_payload) != {"native_mutation_proof"}:
        _fail()
    proof = result_payload["native_mutation_proof"]
    state = validated_proof(plan, proof, result_status)
    await tx.execute(
        "INSERT INTO native_mutation_observations "
        "(tool_call_id,preparation_id,result_submission_sha256,proof_json) VALUES($1::uuid,$2::uuid,$3,$4)",
        tool_call_id, str(preparation["id"]), submission_hash, canonical_json(proof),
    )
    await tx.execute("UPDATE agent_run_tool_calls SET commit_state=$2 WHERE id=$1::uuid", tool_call_id, state)
    return state


async def validate_observation_tx(tx, *, run, tool, preparation, plan):
    if str(tool["run_id"]) != str(run["id"]) or str(preparation["tool_call_id"]) != str(tool["id"]):
        _fail()
    result = object_value(tool["result_payload"])
    if set(result) != {"native_mutation_proof"} or tool["stdout"] or tool["stderr"]:
        _fail()
    state = validated_proof(plan, result["native_mutation_proof"], tool["status"])
    stored = await tx.fetchrow("SELECT * FROM native_mutation_observations WHERE tool_call_id=$1::uuid", str(tool["id"]))
    if (not stored or str(stored["preparation_id"]) != str(preparation["id"])
            or stored["result_submission_sha256"] != tool["result_submission_hash"]
            or canonical_json(object_value(stored["proof_json"])) != canonical_json(result["native_mutation_proof"])
            or tool["commit_state"] != state):
        _fail()
