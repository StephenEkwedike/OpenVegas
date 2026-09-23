"""Opt-in client preparation/dispatch for exact native file mutations.

The caller owns native-history checks, plan/exclude policy, authenticated approval
and the one-shot execution claim. Preparation is not permission to write. Private
snapshots/plans never enter diagnostics or ToolExecutionResult payloads.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from uuid import UUID

from openvegas.agent.local_tools import ToolExecutionResult
from openvegas.agent.native_mutation import (
    build_mutation_plan,
    validate_plan,
    validate_relative_path,
)
from openvegas.agent.runtime_write import capture_source, execute_native_mutation
from openvegas.agent.tool_cas import redaction_required
from openvegas.client import APIError
from openvegas.contracts.errors import APIErrorCode, ContractError

_META = ("version", "relative_path", "before_exists", "before_sha256", "before_bytes", "after_sha256", "after_bytes")
_PUBLIC = {"preparation_id", "contract_sha256", "tool_name", "shell_mode", "timeout_sec", "arguments", "diff",
           "before_exists", "before_sha256", "before_bytes", "after_sha256", "after_bytes", "no_change",
           "expires_at", "evidence_kind"}


def _fail(reason: str = "contract", *, uncertain: bool = False):
    # Only internal constant reasons are supplied here; never include source or server detail.
    raise ContractError(APIErrorCode.MUTATION_UNCERTAIN if uncertain else APIErrorCode.INVALID_TRANSITION,
                        f"Native mutation {reason}; no automatic retry is permitted.") from None


def _enabled():
    if os.getenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS", "0") != "1":
        _fail("disabled")


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        _fail()
    parsed = UUID(value)
    if not parsed.int or str(parsed) != value:
        _fail()
    return value


def _json(value):
    """Bound before serialization; exact JSON types prevent bool/int equivalence."""
    pending = [(value, 0)]
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 256 or depth > 6:
            _fail()
        if type(item) is dict:
            if len(item) > 32 or any(type(key) is not str or len(key) > 256 for key in item):
                _fail()
            for key, entry in item.items():
                size += len(key.encode("utf-8"))
                pending.append((entry, depth + 1))
        elif type(item) is str:
            if len(item) > 524288:
                _fail()
            size += len(item.encode("utf-8"))
        elif type(item) is int:
            if not -(2**63) < item < 2**63:
                _fail()
        elif item is not None and type(item) is not bool:
            _fail()
        if size > 524288:
            _fail()
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > 524288:
        _fail()
    return raw


def _validated_response(response, plan):
    # Snapshot the returned object too: caller/client mutation must not change the approved bytes.
    response = json.loads(_json(response))
    if type(response) is not dict or set(response) != _PUBLIC:
        _fail()
    preparation_id = _uuid(response["preparation_id"])
    expiry = response["expires_at"]
    if type(expiry) is not str or len(expiry) > 64:
        _fail()
    expires = datetime.fromisoformat(expiry)
    if expires.tzinfo is None or expires <= datetime.now(UTC):
        _fail("expired preparation")
    document = plan.document()
    arguments = {"native_mutation": {**{key: document[key] for key in _META},
        "preparation_id": preparation_id, "contract_sha256": plan.contract_sha256}, "patch": plan.patch}
    expected = {
        "preparation_id": preparation_id, "contract_sha256": plan.contract_sha256,
        "tool_name": "fs_apply_patch", "shell_mode": "mutating",
        "timeout_sec": min(5, document["original_call"].get("timeout_sec", 30)),
        "arguments": arguments, "diff": plan.patch,
        **{key: document[key] for key in ("before_exists", "before_sha256", "before_bytes", "after_sha256", "after_bytes", "no_change")},
        "expires_at": expiry, "evidence_kind": "runtime_observed_file_v1",
    }
    if _json(response) != _json(expected):
        _fail("commitment mismatch")
    return response


async def prepare_native_mutation(client, *, run_id, runtime_session_id, expected_run_version,
                                  expected_valid_actions_signature, idempotency_key, call, workspace_root) -> tuple[dict, dict]:
    """Capture just-in-time, then make exactly one preparation request, never a retry."""
    _enabled()
    try:
        original = json.loads(_json(call))
        if type(original) is not dict or type(original.get("arguments")) is not dict:
            _fail()
        _uuid(run_id)
        _uuid(runtime_session_id)
        request_id = _uuid(original.get("native_inference_request_id"))
        provider_call_id = original.get("provider_call_id")
        if (type(provider_call_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", provider_call_id)
                or type(expected_run_version) is not int or not 0 <= expected_run_version < 2**63
                or type(expected_valid_actions_signature) is not str
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_valid_actions_signature)
                or type(idempotency_key) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", idempotency_key)):
            _fail()
        if original.get("tool_name") not in {"Write", "FindAndReplace", "InsertAtEnd"}:
            _fail()
        path = validate_relative_path(original["arguments"].get("filepath"))
        snapshot = await asyncio.to_thread(capture_source, workspace_root, path)
        # Only the route-added request reference is outside the stored provider projection.
        projected = {key: value for key, value in original.items() if key != "native_inference_request_id"}
        plan = build_mutation_plan(projected, snapshot)
        if redaction_required(plan.document()):
            _fail("source policy rejected")
    except Exception:  # noqa: BLE001 - fixed safe boundary, cancellation is not swallowed.
        _fail("preparation validation failed")
    try:
        response = await client.agent_native_mutation_prepare(
            run_id=run_id, runtime_session_id=runtime_session_id,
            native_inference_request_id=request_id, native_provider_call_id=provider_call_id,
            expected_run_version=expected_run_version,
            expected_valid_actions_signature=expected_valid_actions_signature,
            idempotency_key=idempotency_key, observed_source=snapshot, plan_mode=False,
        )
    except APIError as error:
        status = error.status if type(error.status) is int and 100 <= error.status <= 599 else 502
        raise APIError(status, "Native mutation preparation failed; no write was attempted.") from None
    except Exception:  # noqa: BLE001 - transport errors can contain URLs/bodies/secrets.
        _fail("preparation request failed")
    try:
        prepared = _validated_response(response, plan)
        runtime_call = {**original, "tool_name": "fs_apply_patch", "shell_mode": "mutating",
            "timeout_sec": prepared["timeout_sec"], "arguments": prepared["arguments"]}
        return runtime_call, plan.document()
    except Exception:  # noqa: BLE001 - malformed response details must stay private.
        _fail("commitment validation failed")


async def execute_prepared_mutation(workspace_root, plan_document) -> ToolExecutionResult:
    """Call only AFTER approval/start. Cancelling cannot stop the worker thread.

    Cancellation propagates. A caller losing this result MUST pause as unknown;
    never wrap this in a timeout that claims the write was killed or retries it.
    """
    _enabled()
    try:
        plan = validate_plan(json.loads(_json(plan_document)))
        if redaction_required(plan.document()):
            _fail("execution policy rejected")
    except Exception:  # noqa: BLE001 - invalid private documents never enter errors.
        _fail("execution validation failed")
    try:
        proof = await asyncio.to_thread(execute_native_mutation, workspace_root, plan.document())
        return ToolExecutionResult(
            result_status="succeeded" if proof["outcome"] in {"applied", "no_change"} else "failed",
            result_payload={"native_mutation_proof": proof}, stdout="", stderr="",
        )
    except Exception:  # noqa: BLE001 - worker failure is conservatively uncertain.
        _fail("execution result unavailable", uncertain=True)
