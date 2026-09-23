"""Private, same-model continuation assembled only from committed server records.

The caller owns the run lock. Route locks precede gateway locks; tool callbacks
also own that run lock, so every accepted result is stable during reservation.
"""
from __future__ import annotations

import json
from typing import Any

from openvegas.agent.native_generation import (
    NativeGenerationClaim,
    registration,
    reject,
    require_fresh_projection_tx,
    stored_scope,
)
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope


def frozen_settings(req: Any, *, max_tokens: int) -> dict:
    return {"provider": req.provider, "model": req.model, "prompt": req.prompt,
            "enable_tools": req.enable_tools, "enable_web_search": req.enable_web_search,
            "reasoning_effort": req.reasoning_effort, "attachments": list(req.attachments),
            "max_tokens": max_tokens}


def check_options(command: dict, inputs: dict) -> None:
    settings = inputs.get("settings")
    if not isinstance(settings, dict):
        reject("Native history settings are missing; no continuation was sent.")
    for name, default in (("provider", None), ("model", None), ("enable_tools", False),
                          ("enable_web_search", False), ("reasoning_effort", None), ("attachments", [])):
        if name not in settings or command.get(name, default) != settings[name]:
            reject("Native continuation cannot change provider/model, files, tools, web or reasoning settings.")
    if (type(settings.get("prompt")) is not str or type(settings.get("max_tokens")) is not int
            or settings["max_tokens"] <= 0):
        reject("Native history request settings are invalid.")


async def reserve_history_tx(
    tx: Any, *, run: Any, scope: NativeInferenceScope, command: dict,
) -> tuple[int, str | None, str | None, str | None]:
    ref_raw = command.get("native_continuation")
    if ref_raw is None:
        if run.get("native_generation_claim_id") is not None or run.get("native_history_revision") is not None:
            reject("This run already owns a generation; supply its exact continuation revision.")
        await require_fresh_projection_tx(tx, run=run, scope=scope)
        return 0, None, None, None
    ref = NativeContinuationRef.model_validate(ref_raw)
    if (run.get("native_history_revision") != ref.expected_history_revision
            or not run.get("native_generation_claim_id")):
        reject("Native history revision is stale or unverified; no retry was made.")
    await require_fresh_projection_tx(tx, run=run, scope=scope, continuing=True)
    previous = await tx.fetchrow(
        "SELECT * FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE",
        str(run["native_generation_claim_id"]),
    )
    if (not previous or str(previous.get("native_run_id")) != scope.run_id
            or str(previous.get("user_id")) != str(run["user_id"])
            or previous.get("native_history_revision") != ref.expected_history_revision
            or str(previous.get("gateway_request_id")) != ref.previous_inference_request_id
            or previous["status"] != "succeeded" or previous["response_status"] != 200
            or stored_scope(previous)["registration"] != registration(run)):
        reject("Previous native generation is unfinished or does not own this history revision.")
    source = await tx.fetchrow(
        "SELECT * FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
        ref.previous_inference_request_id, str(run["user_id"]),
    )
    if (not source or source["status"] != "succeeded" or source["response_status"] != 200
            or str(source.get("native_route_command_id")) != str(previous["id"])):
        reject("Previous native generation has not successfully settled.")
    from openvegas.agent.native_envelope import load_native_envelope_tx
    from openvegas.agent.native_history import load_native_tool_results_tx

    envelope = await load_native_envelope_tx(
        tx, user_id=str(run["user_id"]), run_id=scope.run_id,
        runtime_session_id=scope.runtime_session_id, request_id=ref.previous_inference_request_id,
        provider=command["provider"], model=command["model"],
    )
    assistant = envelope.assistant_message()
    if envelope.finish_reason != "tool_calls" or not assistant.get("tool_calls"):
        reject("Only a complete native tool-call turn can continue; final/truncated output is not retried.")
    inputs = envelope.history_inputs()
    check_options(command, inputs)
    results = await load_native_tool_results_tx(
        tx, run=run, source=source, request_id=ref.previous_inference_request_id,
        assistant_message=assistant,
    )
    payload = envelope.request_payload()
    messages = payload.get("messages")
    if type(messages) is not list or not messages or len(messages) + 1 + len(results) > 200:
        reject("Native history exceeds its bound; nothing was truncated.")
    payload["messages"] = [*messages, assistant, *results]
    # Each hop includes its complete immutable ancestry. Bound, never summarize
    # encrypted/signature-bearing messages or silently lose an earlier tool call.
    from openvegas.agent.native_envelope import MAX_INPUTS_BYTES
    from openvegas.gateway.openrouter import MAX_REQUEST_BYTES
    from server.services.inference_replay import _bounded_json
    _bounded_json(payload, MAX_REQUEST_BYTES)
    _bounded_json(inputs, MAX_INPUTS_BYTES)
    # Validate with the shared bound, but preserve original object-member order.
    # Owned media blocks are compared byte-for-byte by the media validator.
    return (ref.expected_history_revision + 1, ref.previous_inference_request_id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


async def generation_receipt_tx(tx: Any, *, claim: NativeGenerationClaim, request_id: str) -> dict:
    from openvegas.agent.native_envelope import load_native_envelope_tx

    preauth = await tx.fetchrow(
        "SELECT provider,model_id FROM inference_preauthorizations WHERE request_id=$1 AND user_id=$2::uuid",
        request_id, claim.user_id,
    )
    if not preauth:
        reject("Native generation has no settled model identity.")
    envelope = await load_native_envelope_tx(
        tx, user_id=claim.user_id, run_id=claim.scope.run_id,
        runtime_session_id=claim.scope.runtime_session_id, request_id=request_id,
        provider=preauth["provider"], model=preauth["model_id"],
    )
    receipt = {"history_revision": claim.history_revision,
               "continuation_supported": envelope.finish_reason == "tool_calls"
               and envelope.continuation_safe}
    if envelope.finish_reason == "tool_calls" and not receipt["continuation_supported"]:
        receipt["continuation_block_reason"] = envelope.continuation_block_reason
    return receipt


def restore_request(req: Any, claim: NativeGenerationClaim) -> Any:
    """Ignore caller continuation prose; retained original prompt is policy input."""
    if claim.history_inputs_json is None:
        return req
    inputs = json.loads(claim.history_inputs_json)
    check_options(req.model_dump(mode="json"), inputs)
    return req.model_copy(update={"prompt": inputs["settings"]["prompt"]})


async def apply_request_history(prepared: Any, claim: NativeGenerationClaim) -> None:
    from openvegas.agent.native_envelope import history_inputs

    request = prepared.inference_request
    if claim.history_revision is None:
        return
    settings = frozen_settings(prepared.req, max_tokens=request.max_tokens)
    inputs = history_inputs(attachment_refs=list(prepared.attachment_refs or []), settings=settings)
    if claim.continuation_payload_json is not None:
        original = json.loads(claim.history_inputs_json)
        if original != {"attachment_refs": list(prepared.attachment_refs or []), "settings": settings}:
            reject("Native retained files or request settings changed; no continuation was sent.")
        # Fresh preparation reauthorizes each original upload and capabilities;
        # then discard its newly assembled text in favor of exact private history.
        payload = json.loads(claim.continuation_payload_json)
        request.messages = payload["messages"]
    request._native_history_inputs = inputs
