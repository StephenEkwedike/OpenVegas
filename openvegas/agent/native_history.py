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

from openvegas.agent.orchestration_contracts import canonical_json, valid_actions_signature
from openvegas.agent.runtime_contracts import result_submission_hash, tool_payload_hash
from openvegas.agent.tool_cas import redaction_required
from openvegas.agent.native_generation import verify_source_scope_tx
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
        if not isinstance(value, str) or len(value) != 36 or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        fail("Native tool reference must use a canonical request ID.")
    return value


def require_call_id(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 256 or not PROVIDER_CALL_ID.fullmatch(value)
            or value.lower().startswith("sk-")):
        fail("Invalid native provider call reference.")
    return value


def object_value(raw: Any) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail()
            result[key] = value
        return result

    if isinstance(raw, str):
        try:
            if len(raw) > MAX_BYTES or len(raw.encode("utf-8")) > MAX_BYTES:
                fail()
            raw = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, RecursionError, UnicodeError):
            fail()
    if not isinstance(raw, dict):
        fail()
    pending = [(raw, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > 10000 or depth > 32:
            fail()
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                fail()
            pending.extend((item, depth + 1) for item in value.values())
        elif type(value) is list:
            pending.extend((item, depth + 1) for item in value)
        elif value is not None and type(value) not in {str, int, float, bool}:
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
    native_name = call.get("tool_name")
    if not isinstance(native_name, str):
        fail("Invalid native tool name.")
    name = DIRECT_TOOLS.get(native_name)
    if not name:
        fail("This native tool requires an unverified local transformation; no binding was created.")
    args = object_value(call.get("arguments"))
    if any(not isinstance(k, str) or type(v) not in {str, int, bool} for k, v in args.items()):
        fail()
    args = dict(args)
    if "path" in args and "filepath" in args and args["path"] != args["filepath"]:
        fail("Conflicting native path aliases cannot be bound.")
    mode = call.get("shell_mode", "read_only")
    if not isinstance(mode, str) or mode not in {"read_only", "mutating"}:
        fail()
    if mode != "read_only":
        fail("Read-only native tools cannot change execution mode.")
    return name, normalize(tool_name=name, arguments=args), mode


def require_active_native_run(run: Any) -> None:
    if (run["state"] not in {"created", "running"} or run.get("cancel_requested_at")
            or (run.get("expires_at") is not None and run["expires_at"] <= datetime.now(UTC))):
        fail("Native references cannot be bound or recovered for an inactive or cancelling run.")


def native_proposal_request(request: dict) -> dict:
    """Bound the normalized identity, including the client's original projection."""
    request = object_value(request)
    for field in ("user_id", "run_id", "runtime_session_id", "native_inference_request_id"):
        require_uuid(request.get(field))
    require_call_id(request.get("native_provider_call_id"))
    key = request.get("idempotency_key")
    signature = request.get("expected_valid_actions_signature")
    version = request.get("expected_run_version")
    if (not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,200}", key)
            or not isinstance(signature, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", signature)
            or type(version) is not int or not 0 <= version < 2**63
            or type(request.get("plan_mode")) is not bool):
        fail("Invalid native proposal key, projection or plan mode.")
    return request


def _binding_digest(binding: dict) -> str:
    return hashlib.sha256(canonical_json({
        key: value for key, value in binding.items() if key != "proposal_replay_sha256"
    }).encode()).hexdigest()


async def replay_native_proposal_tx(
    tx: Any, *, run: Any, idempotency_key: str, request: dict | None, normalize: Callable,
) -> dict | None:
    """Caller holds the authenticated run lock and has checked the current session.

    A replay is only a receipt of proposal creation, not permission to start.
    Do not re-derive its envelope from current actions or mutable inference rows.
    """
    if request is not None:
        require_active_native_run(run)
    rows = await tx.fetch(
        "SELECT run_id,role,content_json,tool_call_id FROM agent_chat_turns "
        "WHERE run_id=$1::uuid AND ("
        "content_json->'proposal_replay'->'request'->>'idempotency_key'=$2 OR "
        "(content_json->>'inference_request_id'=$3 AND content_json->>'provider_call_id'=$4)) "
        "ORDER BY turn_no LIMIT 2",
        str(run["id"]), idempotency_key,
        request["native_inference_request_id"] if request else None,
        request["native_provider_call_id"] if request else None,
    )
    if not rows:
        return None
    require_active_native_run(run)
    if len(rows) != 1:
        fail("Native proposal replay is ambiguous; nothing was recovered.")
    row = rows[0]
    binding = object_value(row["content_json"])
    replay = object_value(binding.get("proposal_replay"))
    stored_request = native_proposal_request(object_value(replay.get("request")))
    if request is None or canonical_json(stored_request) != canonical_json(request):
        raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Native proposal key or request changed.")
    if run.get("native_generation_claim_id") is not None or binding.get("generation_scope") is not None:
        _, generation_scope = await lock_native_source_tx(
            tx, run=run, request_id=request["native_inference_request_id"],
        )
        if binding.get("generation_scope") != generation_scope:
            fail("Native proposal original generation ownership does not match.")
    if (type(replay.get("version")) is not int or replay["version"] != 1
            or type(replay.get("response_status")) is not int or replay["response_status"] != 200
            or binding.get("proposal_replay_sha256") != _binding_digest(binding)
            or binding.get("kind") != KIND or row["role"] != "assistant"
            or str(row["run_id"]) != request["run_id"]
            or str(run["user_id"]) != request["user_id"]
            or binding.get("runtime_session_id") != request["runtime_session_id"]
            or binding.get("inference_request_id") != request["native_inference_request_id"]
            or binding.get("provider_call_id") != request["native_provider_call_id"]
            or type(binding.get("normalizer_version")) is not int or binding["normalizer_version"] != 1
            or binding.get("provider") != "openrouter" or not valid_model(binding.get("model"))):
        fail("Native proposal replay evidence is corrupt or incomplete.")
    tool_id = require_uuid(str(row["tool_call_id"]))
    require_call_id(binding.get("provider_request_id"))
    call = object_value(binding.get("native_call"))
    name, arguments, mode = expected_runtime_call(call, normalize)
    expected_hash = tool_payload_hash(name, arguments, mode)
    original = object_value(request.get("tool_request"))
    if (require_call_id(call.get("provider_call_id")) != request["native_provider_call_id"]
            or type(binding.get("call_ordinal")) is not int or not 0 <= binding["call_ordinal"] < 16
            or type(call.get("timeout_sec")) is not int
            or original != {"tool_name": name, "arguments": arguments, "shell_mode": mode,
                            "timeout_sec": max(1, min(call["timeout_sec"], 5))}
            or binding.get("runtime_payload_hash") != expected_hash
            or binding.get("runtime_timeout_sec") != original["timeout_sec"]):
        fail()
    body_text = replay.get("response_body_text")
    if not isinstance(body_text, str):
        fail()
    body = object_value(body_text)
    tool = await tx.fetchrow(
        "SELECT * FROM agent_run_tool_calls WHERE id=$1::uuid AND run_id=$2::uuid FOR UPDATE",
        tool_id, str(run["id"]),
    )
    if not tool or tool["status"] != "proposed" or tool["commit_state"] != "not_applicable":
        fail("Native proposal is no longer unstarted; it must not be executed again.")
    proposed = object_value(body.get("tool_request"))
    # Transcript storage never carries execution authority. Recover the original
    # token only from its locked, unstarted tool row, and verify both commitments.
    token = tool["execution_token"]
    if ("execution_token" in proposed or not isinstance(token, str)
            or not re.fullmatch(r"[0-9a-f]{32}", token)
            or replay.get("token_sha256") != hashlib.sha256(token.encode()).hexdigest()):
        fail("Native proposal token commitment does not match its tool row.")
    proposed["execution_token"] = token
    if (replay.get("response_sha256") != hashlib.sha256(canonical_json(body).encode()).hexdigest()
            or tool["payload_hash"] != expected_hash
            or tool["tool_name"] != name or tool["tool_class"] != "read_only"
            or tool["approval_required"] is not False
            or tool["run_version"] != request["expected_run_version"]
            or canonical_json(object_value(tool["request_payload_json"])) != canonical_json(original)
            or canonical_json(proposed) != canonical_json(dict(
                original, tool_call_id=tool_id, execution_token=token,
                payload_hash=expected_hash, requires_approval=False))
            or body.get("run_id") != request["run_id"] or body.get("error") is not None
            or type(body.get("run_version")) is not int
            or body["run_version"] != request["expected_run_version"]
            or not isinstance(body.get("current_state"), str)
            or body["current_state"] not in {"created", "running"}
            or type(body.get("projection_version")) is not int or body["projection_version"] < 0
            or not isinstance(body.get("valid_actions"), list)
            or any(not isinstance(action, dict) for action in body["valid_actions"])
            or body.get("valid_actions_signature") != valid_actions_signature(
                body["run_version"], body["valid_actions"])):
        fail("Native proposal no longer matches its immutable tool request.")
    return body


async def lock_native_source_tx(tx: Any, *, run: Any, request_id: str) -> tuple[Any, dict | None]:
    # Scoped runs lock their route before the gateway. Legacy rows have no marker.
    if run.get("native_generation_claim_id") is not None:
        await tx.fetchrow("SELECT id FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE",
                          str(run["native_generation_claim_id"]))
    source = await tx.fetchrow(
        "SELECT * FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
        request_id, str(run["user_id"]),
    )
    if not source or source["status"] != "succeeded" or source["response_status"] != 200:
        fail("No settled native inference is available for this user.")
    scope = await verify_source_scope_tx(tx, run=run, source=source, request_id=request_id)
    return source, scope


async def bind_native_call_tx(
    tx: Any, *, run: Any, inference_request_id: str, provider_call_id: str,
    tool_call_id: str, tool_name: str, arguments: dict, shell_mode: str, timeout_sec: int,
    normalize: Callable, proposal_request: dict | None = None, proposal_response: dict | None = None,
    locked_source: tuple[Any, dict | None] | None = None,
) -> None:
    request_id = require_uuid(inference_request_id)
    call_id = require_call_id(provider_call_id)
    require_active_native_run(run)
    # Runtime owner/session/projection and policy checks precede this function.
    source, generation_scope = locked_source or await lock_native_source_tx(tx, run=run, request_id=request_id)
    preauth = await tx.fetchrow(
        "SELECT provider,model_id,status,account_id,settled_v FROM inference_preauthorizations "
        "WHERE request_id=$1 AND user_id=$2::uuid",
        request_id, str(run["user_id"]),
    )
    zero_charge_settled = False
    if (preauth and preauth["status"] == "refunded" and preauth.get("settled_v") == 0
            and preauth.get("account_id") == "user:" + str(run["user_id"])
            and source.get("inference_source") == "wrapper" and source.get("final_charge_v") == 0):
        # A fully grant-covered successful generation refunds its wallet hold.
        # Require committed zero-charge usage, not merely a refunded/voided hold.
        zero_charge_settled = bool(await tx.fetchrow(
            "SELECT id FROM inference_usage WHERE request_id=$1::uuid AND user_id=$2::uuid "
            "AND account_id=$3 AND provider=$4 AND model_id=$5 AND v_cost=0 LIMIT 1",
            request_id, str(run["user_id"]), preauth["account_id"], preauth["provider"], preauth["model_id"],
        ))
    if (not preauth or preauth["provider"] != "openrouter"
            or (preauth["status"] != "settled" and not zero_charge_settled)
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
    if generation_scope is not None:
        payload["generation_scope"] = generation_scope
    if proposal_request is not None or proposal_response is not None:
        response_text = canonical_json(object_value(proposal_response))
        snapshot = json.loads(response_text)
        proposed = object_value(snapshot.get("tool_request"))
        token = proposed.pop("execution_token", None)
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            fail("Native proposal response is missing its original token.")
        payload["proposal_replay"] = {
            "version": 1, "request": native_proposal_request(proposal_request),
            "response_status": 200,
            "response_body_text": canonical_json(snapshot),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "response_sha256": hashlib.sha256(response_text.encode()).hexdigest(),
        }
        payload["proposal_replay_sha256"] = _binding_digest(payload)
    object_value(payload)
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
