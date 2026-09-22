"""Server-owned bindings to settled native calls, never execution authority.

Bindings use the existing private agent transcript table. They cannot be posted
as history by clients. Reload returns accepted call/result receipts, not a whole
conversation or portable hidden reasoning. The caller holds the agent run lock;
the inference row lock serializes cross-run attempts to bind the same call.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from openvegas.agent.orchestration_contracts import canonical_json
from openvegas.agent.runtime_contracts import result_submission_hash, tool_payload_hash
from openvegas.agent.tool_cas import redaction_required
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.conversation import _SECRET
from openvegas.gateway.openrouter import PROVIDER_CALL_ID, valid_model

KIND = "openvegas.native-tool-binding.v1"
MAX_CALLS = 128
MAX_BYTES = 1_000_000
DIRECT_TOOLS = {"Read": "fs_read", "List": "fs_list"}


def fail(detail: str = "Native tool history is incomplete or does not match this request.") -> None:
    raise ContractError(APIErrorCode.INVALID_TRANSITION, detail)


def require_uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        fail("Native tool reference must use a canonical request ID.")
    return value


def require_call_id(value: Any) -> str:
    if (not isinstance(value, str) or not PROVIDER_CALL_ID.fullmatch(value)
            or value.lower().startswith("sk-")):
        fail("Invalid native provider call reference.")
    return value


def object_value(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            if len(raw.encode("utf-8")) > MAX_BYTES:
                fail()
            raw = json.loads(raw)
        except (ValueError, RecursionError, UnicodeError):
            fail()
    if not isinstance(raw, dict):
        fail()
    try:
        encoded = json.dumps(raw, allow_nan=False, ensure_ascii=False).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        fail()
    if len(encoded) > MAX_BYTES:
        fail()
    return raw


def safe_receipt_content(value: Any) -> None:
    if redaction_required(value):
        fail("Sensitive or unsupported result content cannot be transferred.")
    pending = [value]
    visited = 0
    while pending:
        visited += 1
        if visited > 10000:
            fail("Native receipt structure exceeds its bound.")
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
            pending.extend(item.keys())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            if _SECRET.search(item) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", item):
                fail("Sensitive or unsupported result content cannot be transferred.")
    encoded = canonical_json(value)
    if _SECRET.search(encoded) or redaction_required(encoded):
        fail("Sensitive or unsupported result content cannot be transferred.")


def expected_runtime_call(call: dict, normalize: Callable) -> tuple[str, dict, str]:
    """Allow deterministic aliases/defaults, not inferred paths or patch rewrites."""
    name = DIRECT_TOOLS.get(call.get("tool_name"))
    if not name:
        fail("This native tool requires an unverified local transformation; no binding was created.")
    args = object_value(call.get("arguments"))
    if any(not isinstance(k, str) or type(v) not in {str, int, bool} for k, v in args.items()):
        fail()
    args = dict(args)
    if "path" in args and "filepath" in args and args["path"] != args["filepath"]:
        fail("Conflicting native path aliases cannot be bound.")
    mode = call.get("shell_mode", "read_only")
    if mode not in {"read_only", "mutating"}:
        fail()
    if mode != "read_only":
        fail("Read-only native tools cannot change execution mode.")
    return name, normalize(tool_name=name, arguments=args), mode


async def bind_native_call_tx(
    tx: Any, *, run: Any, inference_request_id: str, provider_call_id: str,
    tool_call_id: str, tool_name: str, arguments: dict, shell_mode: str, timeout_sec: int,
    normalize: Callable,
) -> None:
    request_id = require_uuid(inference_request_id)
    call_id = require_call_id(provider_call_id)
    if (run["state"] not in {"created", "running"} or run.get("cancel_requested_at")
            or (run.get("expires_at") is not None and run["expires_at"] <= datetime.now(UTC))):
        fail("Native references cannot be bound to an inactive or cancelling run.")
    # Runtime owner/session/projection and policy checks precede this function.
    source = await tx.fetchrow(
        "SELECT * FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
        request_id, str(run["user_id"]),
    )
    if not source or source["status"] != "succeeded" or source["response_status"] != 200:
        fail("No settled native inference is available for this user.")
    preauth = await tx.fetchrow(
        "SELECT provider,model_id,status FROM inference_preauthorizations "
        "WHERE request_id=$1 AND user_id=$2::uuid",
        request_id, str(run["user_id"]),
    )
    if (not preauth or preauth["provider"] != "openrouter" or preauth["status"] != "settled"
            or not valid_model(preauth["model_id"])):
        fail("Native binding requires a settled managed-provider request.")
    body = object_value(source["response_body_text"])
    if require_call_id(body.get("provider_request_id")) != str(source["provider_request_id"]):
        fail()
    calls = body.get("tool_calls")
    if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
        fail()
    ids = [require_call_id(object_value(c).get("provider_call_id")) for c in calls]
    if len(set(ids)) != len(ids) or call_id not in ids:
        fail("No unique native tool call matches this reference.")
    ordinal = ids.index(call_id)
    call = calls[ordinal]
    expected_name, expected_args, expected_mode = expected_runtime_call(call, normalize)
    expected_hash = tool_payload_hash(expected_name, expected_args, expected_mode)
    # The existing runtime deterministically caps Read/List at five seconds.
    # Compare the effective timeout, not just the older payload hash that omits it.
    if (expected_hash != tool_payload_hash(tool_name, arguments, shell_mode)
            or type(call.get("timeout_sec")) is not int
            or max(1, min(call["timeout_sec"], 5)) != timeout_sec):
        fail("Native tool request changed during local preprocessing; no binding was created.")
    # The locked inference row protects this global check across different runs.
    previous = await tx.fetchval(
        "SELECT id FROM agent_chat_turns WHERE content_json->>'kind'=$1 "
        "AND content_json->>'inference_request_id'=$2 "
        "AND content_json->>'provider_call_id'=$3 LIMIT 1",
        KIND, request_id, call_id,
    )
    if previous:
        raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Native call is already bound; do not execute it again.")
    count = await tx.fetchval(
        "SELECT count(*) FROM agent_chat_turns WHERE run_id=$1::uuid AND content_json->>'kind'=$2",
        str(run["id"]), KIND,
    )
    if count >= MAX_CALLS:
        fail("Native history reached its bound; explicitly start a new run.")
    payload = {
        "kind": KIND, "inference_request_id": request_id, "normalizer_version": 1,
        "provider": preauth["provider"], "model": preauth["model_id"],
        "provider_request_id": body["provider_request_id"], "provider_call_id": call_id,
        "call_ordinal": ordinal, "native_call": call,
        "runtime_payload_hash": expected_hash,
        "runtime_timeout_sec": timeout_sec,
        "runtime_session_id": str(run["runtime_session_id"]),
    }
    await tx.execute(
        "INSERT INTO agent_chat_turns (run_id,turn_no,role,content_json,tool_call_id) "
        "SELECT $1::uuid,COALESCE(MAX(turn_no),0)+1,'assistant',$2::jsonb,$3::uuid "
        "FROM agent_chat_turns WHERE run_id=$1::uuid",
        str(run["id"]), canonical_json(payload), tool_call_id,
    )


async def accepted_native_receipts_tx(tx: Any, *, run: Any, provider: str, model: str) -> list[dict]:
    if provider != "openrouter" or not valid_model(model):
        fail("Native receipts are scoped to the original reviewed provider and model.")
    rows = await tx.fetch(
        "SELECT content_json,tool_call_id FROM agent_chat_turns "
        "WHERE run_id=$1::uuid AND content_json->>'kind'=$2 ORDER BY turn_no LIMIT $3",
        str(run["id"]), KIND, MAX_CALLS + 1,
    )
    if len(rows) > MAX_CALLS:
        fail()
    receipts = []
    for row in rows:
        binding = object_value(row["content_json"])
        if (type(binding.get("normalizer_version")) is not int or binding["normalizer_version"] != 1
                or binding.get("provider") != provider or binding.get("model") != model
                or binding.get("runtime_session_id") != str(run["runtime_session_id"])):
            fail("Native receipts belong to another model or runtime session; nothing was transferred.")
        tool = await tx.fetchrow(
            "SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid AND run_id=$2::uuid",
            str(row["tool_call_id"]), str(run["id"]),
        )
        if (not tool or tool["payload_hash"] != binding.get("runtime_payload_hash")
                or tool["status"] not in {"succeeded", "failed", "timed_out", "blocked"}
                or not tool["result_submission_hash"] or not tool["finished_at"]
                or tool["commit_state"] not in {"not_applicable", "committed"}):
            fail("Native tool result is pending, cancelled or uncertain; no history was replayed.")
        request = object_value(tool["request_payload_json"])
        if (request.get("timeout_sec") != binding.get("runtime_timeout_sec")
                or request.get("tool_name") not in DIRECT_TOOLS.values()
                or request.get("shell_mode") != "read_only"
                or tool_payload_hash(request["tool_name"], object_value(request.get("arguments")),
                                     request["shell_mode"]) != tool["payload_hash"]):
            fail()
        callback = await tx.fetchval(
            "SELECT id FROM agent_run_events WHERE run_id=$1::uuid "
            "AND event_type=$2 AND payload->>'tool_call_id'=$3 "
            "AND payload->>'source'='runtime_callback' "
            "AND payload->'redaction_checked'='true'::jsonb "
            "AND payload->'redaction_required'='false'::jsonb LIMIT 1",
            str(run["id"]), "tool_finished_" + tool["status"], str(row["tool_call_id"]),
        )
        if not callback:
            fail("No accepted runtime callback is recorded; server timeouts are not tool results.")
        result = object_value(tool["result_payload"])
        for field in ["stdout", "stderr"]:
            value = tool[field] or ""
            if (tool[field + "_truncated"] or not isinstance(value, str)
                    or hashlib.sha256(value.encode()).hexdigest() != tool[field + "_sha256"]):
                fail("Native result was truncated or redacted; lossless continuation is unavailable.")
        expected = result_submission_hash(
            result_status=tool["status"], result_payload=result,
            stdout_sha256=tool["stdout_sha256"], stderr_sha256=tool["stderr_sha256"],
        )
        if expected != tool["result_submission_hash"]:
            fail()
        receipt = {
            "provider": provider, "model": model,
            "inference_request_id": binding["inference_request_id"],
            "provider_request_id": binding["provider_request_id"],
            "provider_call_id": binding["provider_call_id"],
            "call_ordinal": binding["call_ordinal"], "call": binding["native_call"],
            "runtime_tool_call_id": str(row["tool_call_id"]),
            "result": {"status": tool["status"], "payload": result,
                       "stdout": tool["stdout"] or "", "stderr": tool["stderr"] or ""},
            "result_submission_hash": expected,
        }
        safe_receipt_content(receipt)
        receipts.append(receipt)
        if len(canonical_json(receipts).encode()) > MAX_BYTES:
            fail("Native history exceeds its byte bound; nothing was truncated.")
    return receipts
