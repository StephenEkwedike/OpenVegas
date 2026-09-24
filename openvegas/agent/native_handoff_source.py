"""Read-only task-boundary assembly from owned, settled native generations.

Caller supplies an existing transaction. Locks stay held until the caller records
or compares a handoff. This is not a public history-import API and does not switch
models, authorize uploads, execute tools, reserve money, or call a provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openvegas.agent.native_continuation import original_user_text
from openvegas.agent.native_envelope import load_native_envelope_tx
from openvegas.agent.native_generation import (
    registration,
    require_fresh_projection_tx,
    stored_scope,
)
from openvegas.agent.native_handoff_document import MAX_GENERATIONS, PortableTaskDocument
from openvegas.agent.native_history import load_native_tool_results_tx, object_value
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope


def _fail():
    raise ContractError(
        APIErrorCode.HANDOFF_BLOCKED,
        "The owned task is not at a verified completed boundary; no model was switched.",
    ) from None


def _verify_web(public, *, envelope, model, reserved_v):
    from openvegas.gateway.openrouter_web import (
        WebLimits,
        WebValidationError,
        validate_citations,
        validate_stored_web_result,
    )

    inputs = envelope.history_inputs()
    enabled = inputs["settings"]["enable_web_search"]
    assistant = envelope.assistant_message()
    if enabled is False:
        count = public.get("web_search_requests", 0)
        if (public.get("web_search_used") is not False or public.get("web_search_sources") != []
                or type(count) is not int or count != 0 or public.get("managed_web_accounting") is not None
                or assistant.get("annotations")):
            _fail()
        return
    if enabled is not True:
        _fail()
    try:
        receipt = validate_stored_web_result(public, request_hash=envelope.request_hash,
                                            model=model, reserved_v=reserved_v)
        # The old native public binding predates citations. Cross-check the
        # settlement evidence against the hash-verified original annotations.
        citations = validate_citations(assistant, search_requests=receipt.web_search_requests,
                                       limits=WebLimits(max_results=10, max_characters=10000))
        if citations != receipt.citations:
            _fail()
    except WebValidationError:
        _fail()


@dataclass(frozen=True)
class PortableTaskSource:
    run_id: str
    runtime_session_id: str
    request_id: str
    history_revision: int
    document: PortableTaskDocument = field(repr=False)


async def assemble_task_tx(
    tx: Any, *, user_id: str, scope: NativeInferenceScope, source_ref: NativeContinuationRef,
) -> PortableTaskSource:
    """Keep only public data after original-call/receipt/settlement verification.

The document's file IDs/digests are references, not permission to use the bytes.
Prepare AND dispatch must separately resolve ownership, expiry and media support.
"""
    try:
        NativeInferenceScope.canonical_uuid(user_id)
        if type(scope) is not NativeInferenceScope or type(source_ref) is not NativeContinuationRef:
            _fail()
        # Public handoff coordinators may lock a source/destination graph before
        # reaching this assembler. Always use that same global ancestor order.
        from openvegas.agent.native_handoff_store import _runs
        runs = await _runs(tx, user_id, scope)
        run = runs[scope.run_id]
        incoming = None
        if run.get("native_handoff_id") is not None:
            from server.services.native_handoff_provenance import verify_consumed_handoff_tx
            incoming = await verify_consumed_handoff_tx(tx, user_id=user_id, scope=scope)
        await require_fresh_projection_tx(tx, run=run, scope=scope, continuing=True)
        revision = run.get("native_history_revision")
        if (type(revision) is not int or not 0 <= revision < MAX_GENERATIONS
                or revision != source_ref.expected_history_revision or not run.get("native_generation_claim_id")):
            _fail()
        # Run lock serializes callbacks, new generations and workspace changes.
        # Acquire all route locks before any gateway/preauthorization locks.
        routes = await tx.fetch(
            "SELECT * FROM inference_route_commands WHERE native_run_id=$1::uuid "
            "ORDER BY native_history_revision,id LIMIT $2 FOR UPDATE",
            scope.run_id, MAX_GENERATIONS + 1,
        )
        if (len(routes) != revision + 1 or str(routes[-1]["id"]) != str(run["native_generation_claim_id"])
                or str(routes[-1]["gateway_request_id"]) != source_ref.previous_inference_request_id):
            _fail()
        pending = await tx.fetchval(
            "SELECT id FROM agent_run_tool_calls WHERE run_id=$1::uuid AND "
            "(status NOT IN ('succeeded','failed','blocked') OR "
            "commit_state NOT IN ('not_applicable','committed')) LIMIT 1",
            scope.run_id,
        )
        if pending is not None:
            _fail()
        generations, previous_id, original_inputs = [], None, None
        observed_count = 0
        for ordinal, route in enumerate(routes):
            scope_record = stored_scope(route)
            parent = str(route["previous_native_request_id"]) if route["previous_native_request_id"] else None
            if (route["native_history_revision"] != ordinal or parent != previous_id
                    or str(route["user_id"]) != user_id or route["status"] != "succeeded"
                    or route["response_status"] != 200 or not route["gateway_request_id"]
                    or scope_record["registration"] != registration(run)
                    or scope_record["scope"]["run_id"] != scope.run_id
                    or scope_record["scope"]["runtime_session_id"] != scope.runtime_session_id):
                _fail()
            request_id = str(route["gateway_request_id"])
            source = await tx.fetchrow(
                "SELECT * FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid FOR UPDATE",
                request_id, user_id,
            )
            if not source or str(source["native_route_command_id"]) != str(route["id"]):
                _fail()
            preauth = await tx.fetchrow(
                "SELECT model_id,reserved_v FROM inference_preauthorizations "
                "WHERE request_id=$1 AND user_id=$2::uuid AND provider='openrouter'",
                request_id, user_id,
            )
            if not preauth:
                _fail()
            model = preauth["model_id"]
            envelope = await load_native_envelope_tx(
                tx, user_id=user_id, run_id=scope.run_id, runtime_session_id=scope.runtime_session_id,
                request_id=request_id, provider="openrouter", model=model,
            )
            inputs = envelope.history_inputs()
            original_user_text(inputs)
            if inputs.get("incoming_handoff") != (incoming.provenance() if incoming else None):
                _fail()
            if original_inputs is None:
                original_inputs = inputs
            elif inputs != original_inputs:
                _fail()
            public = object_value(source["response_body_text"])
            _verify_web(public, envelope=envelope, model=model, reserved_v=preauth["reserved_v"])
            calls = public.get("tool_calls") or []
            if type(calls) is not list or type(public.get("text")) is not str:
                _fail()
            observations = []
            if ordinal == revision:
                if envelope.finish_reason != "stop" or public.get("completion_status") != "complete" or calls:
                    _fail()
            else:
                if envelope.finish_reason != "tool_calls" or public.get("completion_status") != "incomplete" or not calls:
                    _fail()
                results = await load_native_tool_results_tx(
                    tx, run=run, source=source, request_id=request_id,
                    assistant_message=envelope.assistant_message(),
                )
                if len(results) != len(calls):
                    _fail()
                for call, result in zip(calls, results, strict=True):
                    # IDs/signatures/dispatch policy remain in private receipts.
                    # The original arguments and complete accepted public result
                    # are historical DATA, never destination-native tool calls.
                    observations.append({"tool_name": call["tool_name"], "arguments": call["arguments"],
                                         "result": object_value(result["content"])})
            observed_count += len(observations)
            generation = {"assistant_text": public["text"], "observations": observations}
            if public.get("web_search_used") or public.get("web_search_sources"):
                generation.update(web_search_used=public.get("web_search_used"),
                                  web_search_sources=public.get("web_search_sources"))
            generations.append(generation)
            previous_id = request_id
        if observed_count != await tx.fetchval(
            "SELECT count(*) FROM agent_run_tool_calls WHERE run_id=$1::uuid", scope.run_id,
        ):
            _fail()
        inherited = incoming.document.values()["tasks"] if incoming else []
        document = PortableTaskDocument.from_tasks([*inherited, {
            "user_text": original_user_text(original_inputs),
            "attachment_refs": original_inputs["attachment_refs"], "generations": generations,
        }])
        return PortableTaskSource(scope.run_id, scope.runtime_session_id, previous_id, revision, document)
    except (KeyError, TypeError, ValueError, ContractError):
        _fail()
