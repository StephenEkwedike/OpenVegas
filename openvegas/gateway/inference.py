"""AI Inference Gateway — routes, meters, and bills AI usage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, AsyncGenerator

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.catalog import ModelDisabled as ModelDisabled
from openvegas.gateway.catalog import ProviderCatalog, validate_catalog_entry
from openvegas.gateway.providers import Provider as Provider
from openvegas.gateway.providers import (
    get_model_review,
    get_provider,
    model_capabilities,
    resolve_provider_api_key,
    validate_reasoning_effort,
)
from openvegas.wallet.ledger import InsufficientBalance, WalletService

V_SCALE = Decimal("0.000001")


@dataclass
class InferenceRequest:
    account_id: str   # full prefixed wallet ID: "user:<uuid>" or "agent:<uuid>"
    provider: str
    model: str
    messages: list[dict]
    max_tokens: int = 1024
    idempotency_key: str | None = None
    enable_tools: bool = False
    enable_web_search: bool = False
    strict_continuity: bool = False
    reasoning_effort: str | None = None
    # Internal snapshot from server preflight, never accepted from HTTP/CLI input.
    _managed_model_config: dict | None = field(default=None, init=False, repr=False)
    _managed_attachment_context: Any = field(default=None, init=False, repr=False)
    _managed_web_context: Any = field(default=None, init=False, repr=False)
    _managed_openrouter_dispatch: Any = field(default=None, init=False, repr=False)
    _native_generation_claim: Any = field(default=None, init=False, repr=False)
    _native_history_inputs: Any = field(default=None, init=False, repr=False)
    _native_history_required: bool = field(default=False, init=False, repr=False)
    _native_envelope_capture: Any = field(default=None, init=False, repr=False)
    _native_handoff_binding: Any = field(default=None, init=False, repr=False)
    _native_handoff_continuation_binding: Any = field(default=None, init=False, repr=False)


@dataclass
class InferenceResult:
    text: str
    input_tokens: int
    output_tokens: int
    v_cost: Decimal = Decimal("0")
    actual_cost_usd: Decimal = Decimal("0")
    provider_request_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    web_search_used: bool = False
    web_search_requests: int = 0
    web_search_cost_v: Decimal = Decimal("0")
    web_search_sources: list[str] | None = None
    web_search_retry_without_tool: bool = False
    completion_status: str = "unknown"
    # Set only by settlement/replay, never by a provider response or HTTP caller.
    inference_request_id: str | None = field(default=None, init=False)
    _managed_web_receipt: Any = field(default=None, init=False, repr=False)
    _managed_web_accounting: dict | None = field(default=None, init=False, repr=False)


@dataclass
class _InferenceExecutionContext:
    account_id: str
    model_config: dict[str, Any]
    user_id: str | None
    provider_api_key: str
    reserve_v: Decimal
    request_id: str
    preauth_id: str
    reservation_ref: str
    web_context: Any = None
    payload_hash: str = ""
    provider_request_id: str | None = None
    web_enable_tools: bool = False
    native_history_required: bool = False
    handoff_binding: Any = None


class AIGateway:
    """Routes inference requests, meters usage, and settles charges with grant-first policy."""

    def __init__(
        self,
        db: Any,
        wallet: WalletService,
        catalog: ProviderCatalog,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.db = db
        self.wallet = wallet
        self.catalog = catalog
        self.http_client = http_client

    @asynccontextmanager
    async def _transaction(self, tx=None):
        if tx is not None:
            yield tx
        else:
            async with self.db.transaction() as conn:
                yield conn

    async def infer(self, req: InferenceRequest) -> InferenceResult:
        req = self._snapshot_handoff_request(req)
        ctx, replay = await self._prepare_inference_execution(req)
        if replay is not None:
            return replay

        try:
            result = await self._route_to_provider(req, ctx.provider_api_key,
                **({"handoff_binding": ctx.handoff_binding} if getattr(ctx, "handoff_binding", None) is not None else {}))
            ctx.provider_request_id = result.provider_request_id
            return await self._finalize_inference_execution(ctx, req, result)
        except (Exception, asyncio.CancelledError) as error:
            ctx.provider_request_id = getattr(error, "provider_request_id", None) or getattr(ctx, "provider_request_id", None)
            await self._cleanup_inference_after_failure(ctx)
            raise

    async def stream_infer(self, req: InferenceRequest) -> AsyncGenerator[dict[str, Any], None]:
        req = self._snapshot_handoff_request(req)
        ctx, replay = await self._prepare_inference_execution(req)
        if replay is not None:
            if str(replay.text or "").strip():
                yield {"type": "text_delta", "text": str(replay.text)}
            yield {"type": "completed", "result": replay}
            return

        result: InferenceResult | None = None
        buffered = False
        try:
            if getattr(ctx, "handoff_binding", None) is not None:
                from server.services.native_handoff_guard import validate_bound_request
                validate_bound_request(req, expected=ctx.handoff_binding)
            if (
                req.provider == "openai"
                and (
                    self._prefers_openai_responses_api(req.model)
                    or self._messages_include_multimodal_content(req.messages)
                )
            ):
                async for event in self._stream_openai_responses(req=req, api_key=ctx.provider_api_key):
                    if str(event.get("type") or "") == "text_delta":
                        yield event
                        continue
                    candidate = event.get("result")
                    if isinstance(candidate, InferenceResult):
                        result = candidate
                if result is None:
                    raise ContractError(
                        APIErrorCode.PROVIDER_UNAVAILABLE,
                        "OpenAI streaming completed without a final response payload.",
                    )
            else:
                buffered = True
                result = await self._route_to_provider(req, ctx.provider_api_key,
                    **({"handoff_binding": ctx.handoff_binding} if getattr(ctx, "handoff_binding", None) is not None else {}))
                ctx.provider_request_id = result.provider_request_id
            ctx.provider_request_id = result.provider_request_id
            finalized = await self._finalize_inference_execution(ctx, req, result)
        except (Exception, asyncio.CancelledError, GeneratorExit) as error:
            ctx.provider_request_id = getattr(error, "provider_request_id", None) or getattr(ctx, "provider_request_id", None)
            # Closing a stream is not an Exception; release its durable wallet hold too.
            await self._cleanup_inference_after_failure(ctx)
            raise

        # Buffered providers already finished upstream. Persist settlement before
        # exposing their answer; closing the consumer is then a delivery event,
        # not a failed inference that releases a successfully consumed hold.
        if buffered and str(finalized.text or "").strip():
            yield {"type": "text_delta", "text": str(finalized.text)}
        yield {"type": "completed", "result": finalized}

    async def _prepare_inference_execution(
        self,
        req: InferenceRequest,
    ) -> tuple[_InferenceExecutionContext | None, InferenceResult | None]:
        native_claim = req._native_generation_claim
        from server.services.native_handoff_guard import binding_for
        handoff_binding = binding_for(req)
        from openvegas.agent.native_envelope import prepare_history_request
        native_history_required = prepare_history_request(req)
        if native_claim is not None:
            from openvegas.agent.native_generation import NativeGenerationClaim, reject
            if (type(native_claim) is not NativeGenerationClaim
                    or req.account_id != "user:" + native_claim.user_id
                    or req.provider != "openrouter"):
                reject("Invalid native generation claim/account; no request was sent.")
        managed_web = req.provider == "openrouter" and req.enable_web_search
        if managed_web and native_claim is None:
            replay = await self._replay_web_request(req)
            if replay is not None:
                return None, replay
        validate_reasoning_effort(req.provider, req.model, req.reasoning_effort)
        model_config = dict(await self.catalog.get_model(req.provider, req.model))
        validate_catalog_entry(req.provider, req.model, model_config)
        if req.provider == "openrouter":
            from openvegas.gateway.openrouter import build_payload

            # Always replace any earlier route snapshot using a fresh server review.
            req._managed_web_context = None
            build_payload(req, model_config, model_capabilities(req.provider, req.model))
            req._managed_model_config = dict(model_config)
            req._managed_model_config["response_model_ids"] = get_model_review(
                req.provider, req.model
            ).get("response_model_ids", [])
        if req.provider == "gemini":
            from openvegas.gateway.gemini import build_payload

            build_payload(req)
            context_limit = model_capabilities(req.provider, req.model)["context_window_tokens"]
            catalog_limit = model_config.get("max_tokens")
            input_bound = sum(len(m["content"].encode("utf-8")) + 32 for m in req.messages)
            if (type(catalog_limit) is not int or req.max_tokens > catalog_limit
                    or (req.strict_continuity and type(context_limit) is not int)
                    or (type(context_limit) is int
                        and input_bound + req.max_tokens + 256 > context_limit)):
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION,
                    "Gemini context/output budget is unreviewed or exceeded; start fresh explicitly.",
                )
        if req.provider == "mistral":
            from openvegas.gateway.mistral import validate_text_request

            validate_text_request(req)
            context_limit = model_capabilities(req.provider, req.model)["context_window_tokens"]
            catalog_limit = model_config.get("max_tokens")
            # Conservative byte budget; never truncate history or invent a tokenizer count.
            input_bound = sum(len(m["content"].encode("utf-8")) + 32 for m in req.messages)
            if (type(context_limit) is not int or input_bound + req.max_tokens + 256 > context_limit
                    or type(catalog_limit) is not int or req.max_tokens > catalog_limit):
                raise ContractError(APIErrorCode.INVALID_TRANSITION,
                                    "Mistral context/output budget exceeded; shorten context explicitly.")

        user_id = self._extract_user_id(req.account_id)
        payload_hash = self._payload_hash(req)
        provider_api_key = await self._resolve_provider_api_key(req.provider)

        max_v_cost = self._estimate_max_cost(model_config, req.max_tokens)
        estimated_total_tokens = None
        if req.strict_continuity or req.provider == "openrouter":
            if req.provider == "openrouter":
                from openvegas.gateway.openrouter import input_token_bound
                input_bound = input_token_bound(req)
            else:
                input_bound = sum(len(m["content"].encode("utf-8")) + 32 for m in req.messages) + 256
            estimated_total_tokens = input_bound + req.max_tokens
            max_v_cost = (
                (Decimal(input_bound) * Decimal(str(model_config["v_price_input_per_1m"]))
                 + Decimal(req.max_tokens) * Decimal(str(model_config["v_price_output_per_1m"])))
                / Decimal(1000000)
            ).quantize(V_SCALE, rounding=ROUND_CEILING)
        reserve_v = max_v_cost

        if managed_web:
            budget = req._managed_web_context.prepared.budget
            max_v_cost = budget.retail_reservation_v
            reserve_v = max_v_cost

        if user_id and not managed_web:
            estimated_grant_v = await self._estimate_grant_cover_v(
                user_id=user_id,
                provider=req.provider,
                model_id=req.model,
                max_tokens=req.max_tokens,
                max_v_cost=max_v_cost,
                **({"estimated_total_tokens": estimated_total_tokens} if estimated_total_tokens is not None else {}),
            )
            reserve_v = max((max_v_cost - estimated_grant_v), Decimal("0")).quantize(V_SCALE)

        ctx = None
        try:
            async with self.db.transaction() as tx:
                if handoff_binding is not None:
                    from server.services.native_handoff_guard import verify_dispatch_tx
                    await verify_dispatch_tx(tx, req, expected=handoff_binding)
                if native_claim is not None:
                    from openvegas.agent.native_generation import (
                        link_gateway_tx,
                        lock_dispatch_claim_tx,
                    )
                    await lock_dispatch_claim_tx(tx, native_claim, req)
                if native_history_required:
                    # Detect an unmigrated server before any upstream dispatch.
                    await tx.fetchrow("SELECT request_id,assistant_message_json,request_payload_json,history_inputs_json "
                                      "FROM native_generation_envelopes WHERE false")
                request_id, replay = await self._begin_inference_request(
                    user_id=user_id, idempotency_key=req.idempotency_key,
                    payload_hash=payload_hash, tx=tx,
                    **({"allow_retry": False} if managed_web or native_claim is not None else {}),
                )
                if native_claim is not None:
                    if replay is not None:
                        raise ContractError(APIErrorCode.HOLD_CONFLICT, "Native gateway replay requires route reconciliation.")
                    await link_gateway_tx(tx, native_claim, request_id)
                    if handoff_binding is not None:
                        from server.services.native_handoff_guard import (
                            consume_dispatch_tx,
                        )
                        await consume_dispatch_tx(tx, req, request_id, expected=handoff_binding)
                ctx = _InferenceExecutionContext(
                    account_id=req.account_id, model_config=model_config, user_id=user_id,
                    provider_api_key=provider_api_key, reserve_v=reserve_v, request_id=request_id,
                    preauth_id=str(uuid.uuid4()), reservation_ref="",
                    web_context=req._managed_web_context if managed_web else None,
                    payload_hash=payload_hash,
                    web_enable_tools=bool(req.enable_tools) if managed_web else False,
                    native_history_required=native_history_required,
                    handoff_binding=handoff_binding,
                )
                ctx.reservation_ref = f"infer-preauth:{ctx.preauth_id}"
                if replay is not None:
                    return ctx, replay

                previous = await tx.fetchrow(
                    "SELECT * FROM inference_preauthorizations WHERE request_id = $1 FOR UPDATE", request_id,
                )
                if previous:
                    if str(previous["account_id"]) != req.account_id or str(previous["status"]) in {
                        "settled", "refunded",
                    }:
                        raise ContractError(APIErrorCode.HOLD_CONFLICT, "Prior inference settlement requires reconciliation.")
                    previous_id = str(previous["id"])
                    previous_ref = f"infer-preauth:{previous_id}"
                    has_attempt_reserve = await tx.fetchrow(
                        "SELECT id FROM ledger_entries WHERE reference_id = $1 AND entry_type = 'reserve'",
                        previous_ref,
                    )
                    if not has_attempt_reserve:
                        # Legacy reservations used the logical request ID instead of an attempt ID.
                        previous_ref = request_id
                    await self._void_preauth(
                        preauth_id=previous_id, reservation_ref=previous_ref,
                        account_id=req.account_id, reserved_v=Decimal(str(previous["reserved_v"])), tx=tx,
                    )

                balance = await self.wallet.get_balance(req.account_id, tx=tx)
                if balance < reserve_v:
                    raise InsufficientBalance(f"Need {reserve_v} $V reserved, have {balance} $V")
                if previous:
                    # This row identifies the current attempt; its historical ledger stays immutable.
                    await tx.execute(
                        """UPDATE inference_preauthorizations SET id=$2, provider=$3, model_id=$4,
                           reserved_v=$5, settled_v=0, status='reserved', updated_at=now()
                           WHERE id=$1""",
                        previous["id"], ctx.preauth_id, req.provider, req.model, reserve_v,
                    )
                else:
                    await tx.execute(
                        """INSERT INTO inference_preauthorizations
                           (id, account_id, user_id, request_id, provider, model_id, reserved_v, status)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, 'reserved')""",
                        ctx.preauth_id, req.account_id, user_id, request_id, req.provider, req.model, reserve_v,
                    )
                if reserve_v > 0:
                    await self.wallet.reserve(
                        account_id=req.account_id, amount=reserve_v,
                        reference_id=ctx.reservation_ref, tx=tx,
                    )
                if managed_web:
                    from openvegas.gateway.openrouter_web import request_evidence

                    await tx.execute(
                        "UPDATE inference_requests SET response_body_text=$2 WHERE id=$1 AND status='processing'",
                        request_id, json.dumps({"managed_web_request": request_evidence(
                            ctx.web_context.prepared, request_hash=payload_hash,
                            enable_tools=ctx.web_enable_tools,
                        )}, separators=(",", ":")),
                    )
                if handoff_binding is not None:
                    from server.services.native_handoff_guard import validate_dispatch_deadline
                    validate_dispatch_deadline(req, expected=handoff_binding)
        except (Exception, asyncio.CancelledError):
            if ctx is not None:
                await self._cleanup_inference_after_failure(ctx)
            raise
        return ctx, None

    async def _finalize_inference_execution(
        self,
        ctx: _InferenceExecutionContext,
        req: InferenceRequest,
        result: InferenceResult,
    ) -> InferenceResult:
        if getattr(ctx, "handoff_binding", None) is not None:
            from server.services.native_handoff_guard import validate_bound_request
            validate_bound_request(req, expected=ctx.handoff_binding)
        model_config = ctx.model_config
        user_id = ctx.user_id
        request_id = ctx.request_id
        preauth_id = ctx.preauth_id
        reservation_ref = ctx.reservation_ref
        reserve_v = ctx.reserve_v

        actual_v = self._calculate_v_cost(model_config, result.input_tokens, result.output_tokens)
        web_context = getattr(ctx, "web_context", None)
        web_fee = Decimal("0")
        if req.provider == "openrouter" and req.enable_web_search:
            from openvegas.gateway.openrouter_web import ObservedReceipt

            receipt = result._managed_web_receipt
            if (web_context is None or req._managed_web_context is not web_context
                    or self._payload_hash(req) != ctx.payload_hash
                    or not isinstance(receipt, ObservedReceipt) or not receipt.settlement_authorized
                    or result.actual_cost_usd not in {receipt.actual_cost_usd, receipt.actual_cost_usd.quantize(V_SCALE)}
                    or (result.input_tokens, result.output_tokens,
                        result.web_search_requests, result.web_search_cost_v) != (
                        receipt.input_tokens, receipt.output_tokens,
                        receipt.web_search_requests, receipt.web_search_cost_v)):
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Web settlement snapshot mismatch.")
            web_context.payload(req, model_config, dispatch=False)
            web_fee = receipt.web_search_cost_v
            actual_v = receipt.retail_charge_candidate_v
        elif result.web_search_cost_v != 0 or web_context is not None:
            raise ContractError(APIErrorCode.HOLD_CONFLICT, "Unprepared web surcharge.")
        token_v = actual_v - web_fee
        actual_usd = self._calculate_actual_usd(
            model_config, result.input_tokens, result.output_tokens
        )
        if req.provider == "openrouter":
            # OpenRouter reports actual billed USD, including cache discounts.
            # Retail $V remains our catalog rate, not a client-supplied price.
            # Keep response/replay and both NUMERIC(...,6) ledger records identical.
            actual_usd = result.actual_cost_usd.quantize(V_SCALE)

        total_tokens = max(result.input_tokens + result.output_tokens, 0)
        usage_id = str(uuid.uuid4())

        grant_used_tokens = 0
        grant_used_v = Decimal("0")
        charge_v = actual_v

        native_envelope = None
        if getattr(ctx, "native_history_required", False):
            from openvegas.agent.native_envelope import validate_capture
            native_envelope = validate_capture(req, result, request_hash=ctx.payload_hash)

        async with self.db.transaction() as tx:
            if getattr(ctx, "handoff_binding", None) is not None:
                from server.services.native_handoff_guard import validate_bound_request
                validate_bound_request(req, expected=ctx.handoff_binding)
            if native_envelope is not None:
                from openvegas.agent.native_envelope import lock_envelope_owner_tx
                await lock_envelope_owner_tx(tx, req=req, request_id=request_id)
            request_row = await tx.fetchrow(
                "SELECT * FROM inference_requests WHERE id = $1 FOR UPDATE", request_id,
            )
            if request_row and str(request_row["status"]) == "succeeded":
                if getattr(ctx, "handoff_binding", None) is not None:
                    validate_bound_request(req, expected=ctx.handoff_binding)
                return self._deserialize_result(request_row)
            preauth = await tx.fetchrow(
                "SELECT status FROM inference_preauthorizations WHERE id = $1 FOR UPDATE", preauth_id,
            )
            if not request_row or str(request_row["status"]) != "processing" or not preauth or str(preauth["status"]) != "reserved":
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Inference attempt no longer owns its reservation.")
            if user_id and total_tokens > 0:
                grant_used_tokens = await self._consume_grants(
                    tx=tx,
                    user_id=user_id,
                    provider=req.provider,
                    model_id=req.model,
                    tokens_needed=total_tokens,
                    inference_usage_id=usage_id,
                    request_id=request_id,
                )
                grant_used_v = self._grant_coverage_v(token_v, total_tokens, grant_used_tokens)
                charge_v = max((actual_v - grant_used_v), Decimal("0")).quantize(V_SCALE)

            if web_context is not None:
                from openvegas.gateway.openrouter_web import (
                    settlement_evidence,
                    validate_stored_web_result,
                )

                if charge_v > reserve_v or charge_v < web_fee:
                    raise ContractError(APIErrorCode.HOLD_CONFLICT, "Web charge exceeds its reservation.")
                result.v_cost = charge_v
                result.actual_cost_usd = actual_usd
                result._managed_web_accounting = settlement_evidence(
                    web_context.prepared, result._managed_web_receipt,
                    request_hash=ctx.payload_hash, provider_request_id=result.provider_request_id,
                    completion_status=result.completion_status, grant_v=grant_used_v,
                    tool_calls=result.tool_calls,
                    enable_tools=ctx.web_enable_tools,
                )
                validate_stored_web_result(
                    json.loads(self._serialize_success_body(result)), request_hash=ctx.payload_hash,
                    model=req.model, reserved_v=reserve_v,
                )

            await self._settle_preauth(
                tx=tx,
                preauth_id=preauth_id,
                reservation_ref=reservation_ref,
                account_id=req.account_id,
                reserved_v=reserve_v,
                settle_v=charge_v,
            )

            await tx.execute(
                """
                INSERT INTO inference_usage
                  (id, request_id, user_id, account_id, actor_type, provider, model_id,
                   input_tokens, output_tokens, v_cost, actual_cost_usd,
                   inference_source, wallet_funding_source,
                   billed_v_input_per_1m, billed_v_output_per_1m,
                   billed_cost_input_per_1m, billed_cost_output_per_1m)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
                """,
                usage_id,
                request_id,
                user_id,
                req.account_id,
                "agent" if req.account_id.startswith("agent:") else "human",
                req.provider,
                req.model,
                result.input_tokens,
                result.output_tokens,
                charge_v,
                actual_usd,
                "wrapper",
                "external",
                Decimal(str(model_config["v_price_input_per_1m"])).quantize(V_SCALE),
                Decimal(str(model_config["v_price_output_per_1m"])).quantize(V_SCALE),
                Decimal(str(model_config["cost_input_per_1m"])).quantize(V_SCALE),
                Decimal(str(model_config["cost_output_per_1m"])).quantize(V_SCALE),
            )

            if user_id:
                await tx.execute(
                    """
                    INSERT INTO wallet_history_projection
                      (user_id, request_id, event_type, display_amount_v, display_status, occurred_at, metadata_json)
                    VALUES ($1, $2, 'ai_usage_charge', $3, $4, now(), $5::jsonb)
                    """,
                    user_id,
                    request_id,
                    -charge_v,
                    "completed",
                    json.dumps(
                        {
                            "provider": req.provider,
                            "model_id": req.model,
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                            **({"managed_web_accounting": result._managed_web_accounting,
                                "web_search_requests": result.web_search_requests,
                                "web_search_cost_v": str(result.web_search_cost_v)}
                               if web_context is not None else {}),
                        },
                        separators=(",", ":"),
                    ),
                )

            reward_v = Decimal("0")
            if user_id and self._wrapper_rewards_enabled():
                reward_v = self._calculate_wrapper_reward(charge_v)
                if reward_v > 0:
                    preauth = await tx.fetchrow(
                        "SELECT status FROM inference_preauthorizations WHERE id = $1 FOR UPDATE",
                        preauth_id,
                    )
                    if not preauth or str(preauth["status"]) != "settled":
                        raise ContractError(
                            APIErrorCode.HOLD_CONFLICT,
                            "Wrapper reward requires settled hold state.",
                        )
                    await tx.execute(
                        """
                        INSERT INTO wrapper_reward_events
                          (user_id, inference_usage_id, inference_source, wallet_funding_source, reward_v, reason)
                        VALUES ($1, $2, 'wrapper', 'reward', $3, $4)
                        """,
                        user_id,
                        usage_id,
                        reward_v,
                        "wrapper_usage_reward",
                    )
                    await self.wallet.reward_wrapper(
                        req.account_id,
                        reward_v,
                        usage_id,
                        tx=tx,
                    )
                    await tx.execute(
                        """
                        INSERT INTO wallet_history_projection
                          (user_id, request_id, event_type, display_amount_v, display_status, occurred_at, metadata_json)
                        VALUES ($1, $2, 'wrapper_reward', $3, 'completed', now(), $4::jsonb)
                        """,
                        user_id,
                        request_id,
                        reward_v,
                        json.dumps({"inference_usage_id": usage_id}, separators=(",", ":")),
                    )

            result.v_cost = charge_v
            result.actual_cost_usd = actual_usd

            await tx.execute(
                """
                UPDATE inference_requests
                SET status = 'succeeded',
                    response_status = 200,
                    response_body_text = $2,
                    final_charge_v = $3,
                    final_provider_cost_usd = $4,
                    provider_request_id = $5,
                    updated_at = now()
                WHERE id = $1
                """,
                request_id,
                self._serialize_success_body(result, reward_v=reward_v),
                charge_v,
                actual_usd,
                result.provider_request_id,
            )

            if native_envelope is not None:
                from openvegas.agent.native_envelope import persist_native_envelope_tx
                await persist_native_envelope_tx(tx, req=req, request_id=request_id, envelope=native_envelope)

            if getattr(ctx, "handoff_binding", None) is not None:
                # A legitimate response can arrive after dispatch expiry. Bind
                # identity/content through settlement, not the old deadline.
                validate_bound_request(req, expected=ctx.handoff_binding)

        result.inference_request_id = request_id
        return result

    async def _cleanup_inference_after_failure(self, ctx: _InferenceExecutionContext) -> None:
        # Finish bounded cleanup even if an HTTP disconnect cancels the caller again.
        cleanup = asyncio.create_task(asyncio.wait_for(self._abort_inference_execution(ctx), timeout=10))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    async def _abort_inference_execution(
        self,
        ctx: _InferenceExecutionContext,
    ) -> None:
        async with self.db.transaction() as tx:
            request_row = await tx.fetchrow(
                "SELECT status FROM inference_requests WHERE id = $1 FOR UPDATE", ctx.request_id,
            )
            # A failed commit acknowledgement is not proof that settlement rolled back.
            if not request_row or str(request_row["status"]) != "processing":
                return
            preauth = await tx.fetchrow(
                "SELECT status FROM inference_preauthorizations WHERE id = $1 FOR UPDATE", ctx.preauth_id,
            )
            if not preauth or str(preauth["status"]) not in {"reserved", "voided"}:
                return
            await self._void_preauth(
                preauth_id=ctx.preauth_id,
                reservation_ref=ctx.reservation_ref,
                account_id=ctx.account_id,
                reserved_v=ctx.reserve_v,
                tx=tx,
            )
            web_context = getattr(ctx, "web_context", None)
            failure_fields = {}
            if web_context is not None:
                from openvegas.gateway.openrouter_web import request_evidence

                failure_fields = {
                    "managed_web_request": request_evidence(
                        web_context.prepared, request_hash=ctx.payload_hash, enable_tools=ctx.web_enable_tools,
                    ),
                    "provider_request_id": getattr(ctx, "provider_request_id", None),
                }
            await self._mark_request_failed(
                request_id=ctx.request_id, tx=tx,
                **({"web_failure": failure_fields} if failure_fields else {}),
            )

    async def _replay_web_request(self, req: InferenceRequest) -> InferenceResult | None:
        """Read completed web work before fresh review/credentials; never redispatch uncertainty."""
        user_id = self._extract_user_id(req.account_id)
        if not user_id or not isinstance(req.idempotency_key, str) or not 1 <= len(req.idempotency_key) <= 200:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Web requires a user idempotency key.")
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT * FROM inference_requests WHERE user_id = $1 AND idempotency_key = $2 FOR UPDATE",
                user_id, req.idempotency_key,
            )
            if row is None:
                return None
            if row.get("native_route_command_id") is not None:
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Native generation requires scoped route replay.")
            if row["payload_hash"] != self._payload_hash(req):
                raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key conflict: payload mismatch.")
            if row["status"] == "succeeded" and row["response_status"] == 200:
                result = self._deserialize_result(row)
                if result._managed_web_accounting is None:
                    raise ContractError(APIErrorCode.HOLD_CONFLICT, "Web replay lacks accounting evidence.")
                return result
            raise ContractError(
                APIErrorCode.HOLD_CONFLICT,
                "Prior web attempt requires reconciliation; no automatic retry was made.",
            )

    async def _begin_inference_request(
        self,
        *,
        user_id: str | None,
        idempotency_key: str | None,
        payload_hash: str,
        tx=None,
        allow_retry: bool = True,
    ) -> tuple[str, InferenceResult | None]:
        request_id = str(uuid.uuid4())
        idem_key = idempotency_key or request_id
        if not user_id:
            async with self._transaction(tx) as tx:
                await tx.execute(
                    """
                    INSERT INTO inference_requests
                      (id, user_id, idempotency_key, payload_hash, status, inference_source, wallet_funding_source)
                    VALUES ($1, $2, $3, $4, 'processing', 'wrapper', 'external')
                    """,
                    request_id,
                    user_id,
                    idem_key,
                    payload_hash,
                )
            return request_id, None

        async with self._transaction(tx) as tx:
            inserted = await tx.fetchrow(
                """
                INSERT INTO inference_requests
                  (id, user_id, idempotency_key, payload_hash, status, inference_source, wallet_funding_source)
                VALUES ($1, $2, $3, $4, 'processing', 'wrapper', 'external')
                ON CONFLICT (user_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                request_id,
                user_id,
                idem_key,
                payload_hash,
            )
            if inserted:
                return request_id, None

            row = await tx.fetchrow(
                """
                SELECT id, payload_hash, status, response_status, response_body_text, updated_at,
                       final_charge_v, final_provider_cost_usd, provider_request_id,
                       to_jsonb(inference_requests)->>'native_route_command_id' AS native_route_command_id
                FROM inference_requests
                WHERE user_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                user_id,
                idem_key,
            )
            if not row:
                raise ContractError(
                    APIErrorCode.HOLD_CONFLICT,
                    "Inference request idempotency state could not be resolved.",
                )
            if row:
                if row.get("native_route_command_id") is not None:
                    raise ContractError(APIErrorCode.HOLD_CONFLICT,
                                        "Native generation can only be replayed through its authenticated route.")
                if str(row["payload_hash"]) != payload_hash:
                    raise ContractError(
                        APIErrorCode.IDEMPOTENCY_CONFLICT,
                        "Idempotency key conflict: payload mismatch.",
                    )
                rid = str(row["id"])
                status = str(row["status"])
                if status == "succeeded" and row["response_status"] == 200 and row["response_body_text"]:
                    return rid, self._deserialize_result(row)
                if not allow_retry:
                    raise ContractError(APIErrorCode.HOLD_CONFLICT, "Prior inference attempt requires reconciliation.")
                if status == "processing" and not self._is_stale(row.get("updated_at")):
                    raise ContractError(
                        APIErrorCode.HOLD_CONFLICT,
                        "Inference request is already processing.",
                    )
                await tx.execute(
                    """
                    UPDATE inference_requests
                    SET status = 'processing',
                        response_status = NULL,
                        response_body_text = NULL,
                        final_charge_v = NULL,
                        final_provider_cost_usd = NULL,
                        provider_request_id = NULL,
                        updated_at = now()
                    WHERE id = $1
                    """,
                    rid,
                )
                return rid, None
            return request_id, None

    async def _mark_request_failed(self, request_id: str, *, tx=None, web_failure: dict | None = None) -> None:
        async with self._transaction(tx) as tx:
            await tx.execute(
                """
                UPDATE inference_requests
                SET status = 'failed',
                    response_status = 500,
                    response_body_text = $2,
                    updated_at = now()
                WHERE id = $1
                  AND status = 'processing'
                """,
                request_id,
                json.dumps(
                    {"error": APIErrorCode.PROVIDER_UNAVAILABLE.value, "detail": "Inference provider call failed",
                     **(web_failure or {})},
                    separators=(",", ":"),
                ),
            )
            if web_failure and web_failure.get("provider_request_id"):
                await tx.execute(
                    "UPDATE inference_requests SET provider_request_id=$2 WHERE id=$1 AND status='failed'",
                    request_id, web_failure["provider_request_id"],
                )

    async def _estimate_grant_cover_v(
        self,
        user_id: str,
        provider: str,
        model_id: str,
        max_tokens: int,
        max_v_cost: Decimal,
        estimated_total_tokens: int | None = None,
    ) -> Decimal:
        row = await self.db.fetchrow(
            """
            SELECT COALESCE(SUM(tokens_remaining), 0) AS remaining
            FROM inference_token_grants
            WHERE user_id = $1
              AND provider = $2
              AND model_id = $3
              AND tokens_remaining > 0
            """,
            user_id,
            provider,
            model_id,
        )
        remaining = int(row["remaining"]) if row else 0
        estimated_total = max_tokens * 3 if estimated_total_tokens is None else estimated_total_tokens
        if estimated_total <= 0 or remaining <= 0:
            return Decimal("0")

        ratio = min(Decimal(remaining) / Decimal(estimated_total), Decimal("1"))
        return (max_v_cost * ratio).quantize(V_SCALE)

    async def _consume_grants(
        self,
        tx: Any,
        user_id: str,
        provider: str,
        model_id: str,
        tokens_needed: int,
        inference_usage_id: str,
        request_id: str,
    ) -> int:
        remaining = tokens_needed
        consumed = 0

        rows = await tx.fetch(
            """
            SELECT id, tokens_remaining
            FROM inference_token_grants
            WHERE user_id = $1
              AND provider = $2
              AND model_id = $3
              AND tokens_remaining > 0
            ORDER BY created_at ASC
            FOR UPDATE
            """,
            user_id,
            provider,
            model_id,
        )

        for row in rows:
            if remaining <= 0:
                break

            available = int(row["tokens_remaining"])
            use = min(available, remaining)
            updated = await tx.fetchrow(
                """
                UPDATE inference_token_grants
                SET tokens_remaining = tokens_remaining - $2, updated_at = now()
                WHERE id = $1 AND tokens_remaining >= $2
                RETURNING id
                """,
                row["id"],
                use,
            )
            if not updated:
                continue

            await tx.execute(
                """
                INSERT INTO inference_grant_usages
                  (grant_id, inference_usage_id, request_id, provider, model_id, tokens_used)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                row["id"],
                inference_usage_id,
                request_id,
                provider,
                model_id,
                use,
            )
            consumed += use
            remaining -= use

        return consumed

    async def _settle_preauth(
        self,
        tx: Any,
        preauth_id: str,
        reservation_ref: str,
        account_id: str,
        reserved_v: Decimal,
        settle_v: Decimal,
    ) -> None:
        reserved_v = Decimal(str(reserved_v)).quantize(V_SCALE)
        settle_v = Decimal(str(settle_v)).quantize(V_SCALE)

        if reserved_v > 0:
            settle_from_reserve = min(settle_v, reserved_v)
            await self.wallet.settle_reservation(
                account_id=account_id,
                reservation_ref=reservation_ref,
                settle_amount=settle_from_reserve,
                tx=tx,
            )
        else:
            settle_from_reserve = Decimal("0")

        extra = (settle_v - settle_from_reserve).quantize(V_SCALE)
        if extra > 0:
            await self.wallet.redeem(
                account_id=account_id,
                amount=extra,
                reference_id=f"{reservation_ref}:extra",
                tx=tx,
            )

        final_settled = (settle_from_reserve + max(extra, Decimal("0"))).quantize(V_SCALE)
        status = "settled" if final_settled > 0 else "refunded"
        await tx.execute(
            """
            UPDATE inference_preauthorizations
            SET settled_v = $2,
                status = $3,
                updated_at = now()
            WHERE id = $1
            """,
            preauth_id,
            final_settled,
            status,
        )

    async def _void_preauth(
        self,
        preauth_id: str,
        reservation_ref: str,
        account_id: str,
        reserved_v: Decimal,
        *,
        tx=None,
    ) -> None:
        async with self._transaction(tx) as tx:
            row = await tx.fetchrow(
                "SELECT status, account_id, reserved_v FROM inference_preauthorizations WHERE id = $1 FOR UPDATE",
                preauth_id,
            )
            if not row or str(row["status"]) != "reserved":
                return
            if str(row["account_id"]) != account_id or Decimal(str(row["reserved_v"])) != reserved_v:
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Inference reservation identity mismatch.")
            if reserved_v > 0:
                await self.wallet.settle_reservation(
                    account_id=account_id,
                    reservation_ref=reservation_ref,
                    settle_amount=Decimal("0"),
                    tx=tx,
                )
            await tx.execute(
                """
                UPDATE inference_preauthorizations
                SET settled_v = 0,
                    status = 'voided',
                    updated_at = now()
                WHERE id = $1
                  AND status = 'reserved'
                """,
                preauth_id,
            )

    @staticmethod
    def _extract_user_id(account_id: str) -> str | None:
        if account_id.startswith("user:"):
            return account_id.split(":", 1)[1]
        return None

    @staticmethod
    def _grant_coverage_v(actual_v: Decimal, total_tokens: int, used_tokens: int) -> Decimal:
        if total_tokens <= 0 or used_tokens <= 0:
            return Decimal("0")
        ratio = min(Decimal(used_tokens) / Decimal(total_tokens), Decimal("1"))
        return (actual_v * ratio).quantize(V_SCALE)

    def _estimate_max_cost(self, mc: dict, max_tokens: int) -> Decimal:
        v_in = Decimal(str(mc["v_price_input_per_1m"]))
        v_out = Decimal(str(mc["v_price_output_per_1m"]))
        return (
            (Decimal(max_tokens) * 2 * v_in + Decimal(max_tokens) * v_out)
            / Decimal("1000000")
        ).quantize(V_SCALE)

    def _calculate_v_cost(self, mc: dict, input_tokens: int, output_tokens: int) -> Decimal:
        v_in = Decimal(str(mc["v_price_input_per_1m"]))
        v_out = Decimal(str(mc["v_price_output_per_1m"]))
        cost = (Decimal(input_tokens) * v_in + Decimal(output_tokens) * v_out) / Decimal("1000000")
        return cost.quantize(V_SCALE)

    def _calculate_actual_usd(self, mc: dict, input_tokens: int, output_tokens: int) -> Decimal:
        c_in = Decimal(str(mc["cost_input_per_1m"]))
        c_out = Decimal(str(mc["cost_output_per_1m"]))
        cost = (Decimal(input_tokens) * c_in + Decimal(output_tokens) * c_out) / Decimal("1000000")
        return cost.quantize(Decimal("0.000001"))

    @staticmethod
    def _snapshot_handoff_request(req):
        if (getattr(req, "_native_handoff_binding", None) is None
                and getattr(req, "_native_handoff_continuation_binding", None) is None):
            return req
        import copy
        # Do not share mutable messages, model snapshots, or nested private
        # prepared contexts with a caller while catalog/wallet operations await.
        return copy.deepcopy(req)

    async def _route_to_provider(self, req: InferenceRequest, api_key: str, *, handoff_binding=None) -> InferenceResult:
        """Route to the appropriate provider SDK."""
        validate_reasoning_effort(req.provider, req.model, req.reasoning_effort)
        descriptor = get_provider(req.provider)
        if req.enable_tools and not descriptor.tools:
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "Tool-calling mode is unavailable in this provider adapter.",
            )
        if handoff_binding is not None:
            from server.services.native_handoff_guard import validate_dispatch_deadline
            validate_dispatch_deadline(req, expected=handoff_binding)
            if req.provider != "openrouter":
                raise ContractError(APIErrorCode.HANDOFF_BLOCKED, "Unsupported handoff transport.")
            return await self._call_openrouter(req, api_key, handoff_binding=handoff_binding)
        return await getattr(self, descriptor.adapter_method)(req, api_key)

    async def _call_mistral(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        from openvegas.gateway.mistral import complete

        return InferenceResult(**await complete(req, api_key, self.http_client))

    async def _call_openrouter(self, req: InferenceRequest, api_key: str, *, handoff_binding=None) -> InferenceResult:
        from openvegas.gateway.openrouter import complete

        if req._managed_model_config is None:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "OpenRouter requires server catalog preflight.")
        values = await complete(
            req, api_key, model_config=req._managed_model_config,
            capabilities=model_capabilities(req.provider, req.model),
            parse_tool=self._parse_local_tool_call, client=self.http_client,
            **({"handoff_binding": handoff_binding} if handoff_binding is not None else {}),
        )
        receipt = values.pop("_managed_web_receipt", None)
        result = InferenceResult(**values)
        result._managed_web_receipt = receipt
        return result

    async def _call_anthropic(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        import anthropic

        options = {"max_retries": 0, "timeout": 60.0} if req.strict_continuity else {}
        client = anthropic.AsyncAnthropic(api_key=api_key, **options)
        msg = await client.messages.create(
            model=req.model,
            max_tokens=req.max_tokens,
            messages=req.messages,
        )
        return InferenceResult(
            text=("".join(getattr(block, "text", "") for block in msg.content)
                  if req.strict_continuity else msg.content[0].text),
            completion_status=("complete" if getattr(msg, "stop_reason", None) == "end_turn"
                               and all(getattr(block, "type", None) == "text" for block in msg.content)
                               else "incomplete"),
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            provider_request_id=getattr(msg, "id", None),
        )

    async def _call_openai(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        client = self._build_openai_client(api_key)
        if req.strict_continuity:
            client = client.with_options(max_retries=0, timeout=60.0)
        if self._prefers_openai_responses_api(req.model) or self._messages_include_multimodal_content(req.messages):
            return await self._call_openai_responses(client=client, req=req)
        return await self._call_openai_chat_completions(client=client, req=req)

    async def _call_openai_chat_completions(
        self,
        *,
        client: Any,
        req: InferenceRequest,
    ) -> InferenceResult:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_completion_tokens": req.max_tokens,
            "messages": req.messages,
        }
        if req.enable_tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "call_local_tool",
                        "description": "Request local workspace tool execution.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "tool_name": {
                                    "type": "string",
                                    "enum": [
                                        "Read",
                                        "Search",
                                        "Write",
                                        "FindAndReplace",
                                        "InsertAtEnd",
                                        "Bash",
                                        "List",
                                    ],
                                },
                                "arguments": {"type": "object"},
                                "shell_mode": {"type": "string", "enum": ["read_only", "mutating"]},
                                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 300},
                            },
                            "required": ["tool_name", "arguments"],
                        },
                    },
                }
            ]
            kwargs["tool_choice"] = "auto"
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            if req.strict_continuity:
                self._raise_openai_request_error(exc)
            msg = str(exc)
            # Some models/sdks only accept max_tokens while others require max_completion_tokens.
            if "Unsupported parameter" in msg and "'max_completion_tokens'" in msg:
                retry = dict(kwargs)
                retry.pop("max_completion_tokens", None)
                retry["max_tokens"] = req.max_tokens
                try:
                    resp = await client.chat.completions.create(**retry)
                except Exception as retry_exc:
                    self._raise_openai_request_error(retry_exc)
            elif "Unsupported parameter" in msg and "'max_tokens'" in msg:
                retry = dict(kwargs)
                retry.pop("max_tokens", None)
                retry["max_completion_tokens"] = req.max_tokens
                try:
                    resp = await client.chat.completions.create(**retry)
                except Exception as retry_exc:
                    self._raise_openai_request_error(retry_exc)
            else:
                self._raise_openai_request_error(exc)
        msg = resp.choices[0].message
        parsed_tool_calls: list[dict[str, Any]] = []
        if req.enable_tools and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls or []:
                fn = getattr(tc, "function", None)
                if not fn:
                    continue
                parsed = self._parse_local_tool_call(
                    function_name=str(getattr(fn, "name", "") or ""),
                    raw_arguments=str(getattr(fn, "arguments", "") or ""),
                )
                if parsed:
                    parsed_tool_calls.append(parsed)
        return InferenceResult(
            text=msg.content or "",
            completion_status=("complete" if getattr(resp.choices[0], "finish_reason", None) == "stop"
                               else "incomplete"),
            input_tokens=int(getattr(resp.usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(resp.usage, "completion_tokens", 0) or 0),
            provider_request_id=getattr(resp, "id", None),
            tool_calls=parsed_tool_calls or None,
        )

    async def _call_openai_responses(self, *, client: Any, req: InferenceRequest) -> InferenceResult:
        kwargs = self._build_openai_responses_request(req)

        web_search_retry_without_tool = False
        try:
            resp = await client.responses.create(**kwargs)
        except Exception as exc:
            if not req.strict_continuity and req.enable_web_search and self._should_retry_without_web_tool(exc):
                retry = dict(kwargs)
                retry_tools = [
                    t for t in list(retry.get("tools", []))
                    if str((t or {}).get("type") or "").strip().lower() != "web_search_preview"
                ]
                if retry_tools:
                    retry["tools"] = retry_tools
                else:
                    retry.pop("tools", None)
                    retry.pop("tool_choice", None)
                web_search_retry_without_tool = True
                try:
                    resp = await client.responses.create(**retry)
                except Exception as retry_exc:
                    self._raise_openai_request_error(retry_exc)
            else:
                self._raise_openai_request_error(exc)
        parsed_tool_calls = self._extract_openai_response_tool_calls(resp) if req.enable_tools else None
        return self._build_openai_responses_result(
            resp=resp,
            tool_calls=parsed_tool_calls,
            web_search_retry_without_tool=web_search_retry_without_tool,
        )

    async def _stream_openai_responses(
        self,
        *,
        req: InferenceRequest,
        api_key: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        payload = self._build_openai_responses_request(req)
        payload["stream"] = True
        text_chunks: list[str] = []
        completed_response: Any = None
        web_search_retry_without_tool = False

        async def _consume_stream(stream_payload: dict[str, Any]) -> None:
            nonlocal completed_response
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            async def _stream_events(client: httpx.AsyncClient) -> AsyncGenerator[dict[str, Any], None]:
                async with client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers=headers,
                    json=stream_payload,
                    timeout=None,
                ) as resp:
                    if resp.status_code >= 400:
                        detail_bytes = await resp.aread()
                        detail = detail_bytes.decode("utf-8", errors="ignore").strip() or resp.reason_phrase
                        raise RuntimeError(detail or "OpenAI streaming request failed")

                    data_lines: list[str] = []
                    async for raw_line in resp.aiter_lines():
                        line = str(raw_line or "")
                        if not line:
                            if not data_lines:
                                continue
                            raw = "\n".join(data_lines).strip()
                            data_lines = []
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                payload_obj = json.loads(raw)
                            except Exception:
                                continue
                            if isinstance(payload_obj, dict):
                                yield payload_obj
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line.split(":", 1)[1].lstrip())

                    if data_lines:
                        raw = "\n".join(data_lines).strip()
                        if raw and raw != "[DONE]":
                            try:
                                payload_obj = json.loads(raw)
                            except Exception:
                                payload_obj = None
                            if isinstance(payload_obj, dict):
                                yield payload_obj

            if self.http_client is not None:
                source = _stream_events(self.http_client)
            else:
                async with httpx.AsyncClient(follow_redirects=True, timeout=None) as temp_client:
                    source = _stream_events(temp_client)
                    async for event in source:
                        event_type = str(event.get("type") or "").strip().lower()
                        if event_type == "response.output_text.delta":
                            delta = str(event.get("delta") or event.get("text") or "")
                            if delta:
                                text_chunks.append(delta)
                                yield {"type": "text_delta", "text": delta}
                            continue
                        if event_type == "response.completed":
                            completed_response = event.get("response")
                            continue
                        if event_type in {"error", "response.error", "response.failed"}:
                            raise RuntimeError(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
                return

            async for event in source:
                event_type = str(event.get("type") or "").strip().lower()
                if event_type == "response.output_text.delta":
                    delta = str(event.get("delta") or event.get("text") or "")
                    if delta:
                        text_chunks.append(delta)
                        yield {"type": "text_delta", "text": delta}
                    continue
                if event_type == "response.completed":
                    completed_response = event.get("response")
                    continue
                if event_type in {"error", "response.error", "response.failed"}:
                    raise RuntimeError(json.dumps(event, separators=(",", ":"), ensure_ascii=False))

        try:
            async for event in _consume_stream(payload):
                yield event
        except Exception as exc:
            if req.enable_web_search and self._should_retry_without_web_tool(exc):
                retry = dict(payload)
                retry_tools = [
                    t for t in list(retry.get("tools", []))
                    if str((t or {}).get("type") or "").strip().lower() != "web_search_preview"
                ]
                if retry_tools:
                    retry["tools"] = retry_tools
                else:
                    retry.pop("tools", None)
                    retry.pop("tool_choice", None)
                text_chunks = []
                completed_response = None
                web_search_retry_without_tool = True
                async for event in _consume_stream(retry):
                    yield event
            else:
                self._raise_openai_request_error(exc)

        if completed_response is None:
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE,
                "OpenAI streaming completed without a final response payload.",
            )

        parsed_tool_calls = self._extract_openai_response_tool_calls(completed_response) if req.enable_tools else None
        result = self._build_openai_responses_result(
            resp=completed_response,
            tool_calls=parsed_tool_calls,
            web_search_retry_without_tool=web_search_retry_without_tool,
        )
        if text_chunks and not str(result.text or "").strip():
            result.text = "".join(text_chunks).strip()
        yield {"type": "completed", "result": result}

    @staticmethod
    def _prefers_openai_responses_api(model_id: str) -> bool:
        m = str(model_id or "").strip().lower()
        # GPT-5 and Codex-family models use the Responses API.
        return m.startswith("gpt-5") or ("codex" in m)

    @staticmethod
    def _messages_to_openai_responses_input(messages: list[dict]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        allowed_roles = {"user", "assistant", "system", "developer"}
        for msg in messages:
            role = str((msg or {}).get("role") or "user").strip().lower()
            if role not in allowed_roles:
                continue
            content = (msg or {}).get("content", "")
            normalized_parts: list[dict[str, Any]] = []

            def _append_text(raw: Any) -> None:
                text = str(raw or "")
                if not text.strip():
                    return
                normalized_parts.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": text,
                    }
                )

            if isinstance(content, str):
                _append_text(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = str(part.get("type", "")).strip().lower()
                    if ptype in {"text", "input_text", "output_text"}:
                        _append_text(part.get("text", ""))
                        continue
                    if ptype == "input_image" and role in {"user", "system", "developer"}:
                        image_url = str(part.get("image_url") or "").strip()
                        image_b64 = str(part.get("image_base64") or "").strip()
                        if image_url:
                            normalized_parts.append({"type": "input_image", "image_url": image_url})
                            continue
                        if image_b64:
                            # OpenAI Responses expects input_image.image_url.
                            mime_type = str(part.get("mime_type") or "").strip()
                            mime_clean = str(mime_type.split(";", 1)[0] or "").strip() or "image/png"
                            normalized_parts.append(
                                {
                                    "type": "input_image",
                                    "image_url": f"data:{mime_clean};base64,{image_b64}",
                                }
                            )
            elif content is not None:
                _append_text(content)

            if not normalized_parts:
                continue
            out.append({"role": role, "content": normalized_parts})
        return out

    @staticmethod
    def _messages_include_multimodal_content(messages: list[dict]) -> bool:
        for msg in list(messages or []):
            content = (msg or {}).get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get("type", "")).strip().lower()
                if ptype in {"input_image", "input_file"}:
                    return True
        return False

    @staticmethod
    def _response_includes_web_search(resp: Any) -> bool:
        for item in AIGateway._iter_openai_output_items(resp):
            item_type = str(AIGateway._openai_field(item, "type", "") or "").strip().lower()
            if item_type in {"web_search_call", "web_search_preview"}:
                return True
        return False

    def _extract_openai_response_tool_calls(self, resp: Any) -> list[dict[str, Any]] | None:
        parsed_tool_calls: list[dict[str, Any]] = []
        for item in self._iter_openai_output_items(resp):
            item_type = str(self._openai_field(item, "type", "") or "").strip().lower()
            if item_type != "function_call":
                continue
            fn_name = str(self._openai_field(item, "name", "") or "")
            raw_args = str(self._openai_field(item, "arguments", "") or "")
            parsed = self._parse_local_tool_call(
                function_name=fn_name,
                raw_arguments=raw_args,
            )
            if parsed:
                parsed_tool_calls.append(parsed)
        return parsed_tool_calls or None

    @staticmethod
    def _web_sources_max_from_env() -> int:
        raw = str(os.getenv("OPENVEGAS_CHAT_WEB_SEARCH_SOURCES_MAX", "8")).strip()
        try:
            return max(1, min(50, int(raw)))
        except Exception:
            return 8

    @staticmethod
    def _extract_openai_web_sources(resp: Any, *, max_sources: int = 8) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        def _collect_url(candidate: Any) -> bool:
            url = str(candidate or "").strip()
            if not url or url in seen:
                return False
            seen.add(url)
            urls.append(url)
            return len(urls) >= max_sources

        for item in AIGateway._iter_openai_output_items(resp):
            for attr_name in ("url", "source", "source_url"):
                if _collect_url(AIGateway._openai_field(item, attr_name, "")):
                    return urls
            for part in list(AIGateway._openai_field(item, "content", []) or []):
                for attr_name in ("url", "source", "source_url"):
                    if _collect_url(AIGateway._openai_field(part, attr_name, "")):
                        return urls
                annotations = AIGateway._openai_field(part, "annotations", None) or []
                for ann in annotations:
                    if _collect_url(AIGateway._openai_field(ann, "url", "")):
                        return urls
        return urls

    @staticmethod
    def _should_retry_without_web_tool(exc: Exception) -> bool:
        err_obj = getattr(exc, "error", None)
        err: dict[str, Any] = err_obj if isinstance(err_obj, dict) else {}

        code = str(getattr(exc, "code", "") or err.get("code", "")).strip().lower()
        param = str(getattr(exc, "param", "") or err.get("param", "")).strip().lower()
        message = str(getattr(exc, "message", "") or err.get("message", "") or exc).strip().lower()

        if "web_search" in param:
            return True
        if code in {"invalid_tool", "unsupported_tool"} and "web_search" in message:
            return True
        if code == "invalid_request_error" and "web_search" in message:
            return True
        if "web_search_preview" in message and "unsupported" in message:
            return True
        return False

    @staticmethod
    def _parse_local_tool_call(*, function_name: str, raw_arguments: str) -> dict[str, Any] | None:
        try:
            args = json.loads(raw_arguments or "{}")
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        tool_name = str(args.get("tool_name") or "").strip()
        if not tool_name and function_name and function_name != "call_local_tool":
            tool_name = function_name
        if not tool_name:
            return None
        args_obj = args.get("arguments", {}) if isinstance(args.get("arguments"), dict) else {}
        if not args_obj:
            args_obj = {k: v for k, v in args.items() if k not in {"tool_name", "shell_mode", "timeout_sec"}}
        return {
            "tool_name": tool_name,
            "arguments": args_obj if isinstance(args_obj, dict) else {},
            "shell_mode": str(args.get("shell_mode") or "read_only"),
            "timeout_sec": int(args.get("timeout_sec") or 30),
        }

    @staticmethod
    def _extract_openai_responses_text(resp: Any) -> str:
        out_text = str(AIGateway._openai_field(resp, "output_text", "") or "").strip()
        if out_text:
            return out_text
        chunks: list[str] = []
        for item in AIGateway._iter_openai_output_items(resp):
            if str(AIGateway._openai_field(item, "type", "")).lower() != "message":
                continue
            for part in list(AIGateway._openai_field(item, "content", []) or []):
                if str(AIGateway._openai_field(part, "type", "")).lower() in {"text", "output_text"}:
                    value = AIGateway._openai_field(part, "text", "")
                    if value:
                        chunks.append(str(value))
        return "\n".join(chunks).strip()

    @staticmethod
    def _openai_field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _iter_openai_output_items(resp: Any) -> list[Any]:
        items = AIGateway._openai_field(resp, "output", [])
        if isinstance(items, list):
            return items
        return list(items or [])

    def _build_openai_client(self, api_key: str) -> Any:
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if self.http_client is not None:
            kwargs["http_client"] = self.http_client
        try:
            return AsyncOpenAI(**kwargs)
        except TypeError:
            kwargs.pop("http_client", None)
            return AsyncOpenAI(**kwargs)

    def _build_openai_responses_request(self, req: InferenceRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "input": self._messages_to_openai_responses_input(req.messages),
            "max_output_tokens": req.max_tokens,
        }
        tools: list[dict[str, Any]] = []
        if req.enable_tools:
            tools.append(
                {
                    "type": "function",
                    "name": "call_local_tool",
                    "description": "Request local workspace tool execution.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tool_name": {
                                "type": "string",
                                "enum": [
                                    "Read",
                                    "Search",
                                    "Write",
                                    "FindAndReplace",
                                    "InsertAtEnd",
                                    "Bash",
                                    "List",
                                ],
                            },
                            "arguments": {"type": "object"},
                            "shell_mode": {"type": "string", "enum": ["read_only", "mutating"]},
                            "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 300},
                        },
                        "required": ["tool_name", "arguments"],
                    },
                }
            )
        if req.enable_web_search and os.getenv("OPENVEGAS_OPENAI_WEB_SEARCH_ENABLED", "1").strip() in {"1", "true", "yes", "on"}:
            tools.append({"type": "web_search_preview"})
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _build_openai_responses_result(
        self,
        *,
        resp: Any,
        tool_calls: list[dict[str, Any]] | None = None,
        web_search_retry_without_tool: bool = False,
    ) -> InferenceResult:
        usage = self._openai_field(resp, "usage", None)
        web_sources_max = self._web_sources_max_from_env()
        web_search_sources = self._extract_openai_web_sources(resp, max_sources=web_sources_max)
        web_search_used = self._response_includes_web_search(resp) or bool(web_search_sources)
        return InferenceResult(
            text=self._extract_openai_responses_text(resp),
            completion_status=("complete" if self._openai_field(resp, "status", None) == "completed"
                               and not self._openai_field(resp, "incomplete_details", None)
                               and not self._extract_openai_response_tool_calls(resp)
                               and not web_search_used else "incomplete"),
            input_tokens=int(self._openai_field(usage, "input_tokens", 0) or 0),
            output_tokens=int(self._openai_field(usage, "output_tokens", 0) or 0),
            provider_request_id=self._openai_field(resp, "id", None),
            tool_calls=tool_calls,
            web_search_used=web_search_used,
            web_search_sources=web_search_sources or None,
            web_search_retry_without_tool=web_search_retry_without_tool,
        )

    @staticmethod
    def _raise_openai_request_error(exc: Exception) -> None:
        msg = str(exc or "").strip()
        detail = msg if len(msg) <= 500 else msg[:500]
        lowered = detail.lower()
        if "invalid_request_error" in lowered or "unsupported parameter" in lowered or "badrequesterror" in lowered:
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                f"OpenAI request rejected: {detail}",
            ) from exc
        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE,
            f"OpenAI request failed: {detail}",
        ) from exc

    async def _call_gemini(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        from openvegas.gateway.gemini import complete

        return InferenceResult(**await complete(req, api_key, self.http_client))

    async def generate_image(
        self,
        *,
        account_id: str,
        provider: str,
        model: str,
        prompt: str,
        size: str = "1024x1024",
    ) -> dict[str, Any]:
        del account_id  # Reserved for future billing tie-in.
        if str(provider or "").strip().lower() != "openai":
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Image generation currently supports openai only.")
        api_key = await self._resolve_provider_api_key("openai")
        client = self._build_openai_client(api_key)
        started = time.perf_counter()
        resp = await client.images.generate(
            model=str(model or "gpt-image-1"),
            prompt=str(prompt or ""),
            size=str(size or "1024x1024"),
        )
        latency_ms = float((time.perf_counter() - started) * 1000.0)
        item = (getattr(resp, "data", None) or [None])[0]
        image_url = getattr(item, "url", None) if item is not None else None
        image_b64 = getattr(item, "b64_json", None) if item is not None else None
        revised_prompt = getattr(item, "revised_prompt", None) if item is not None else None
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
            "image_count": int(len(getattr(resp, "data", None) or [])),
        }
        provider_request_id = str(getattr(resp, "_request_id", "") or getattr(resp, "id", "") or "").strip() or None
        return {
            "provider": "openai",
            "model": str(model or "gpt-image-1"),
            "image_url": str(image_url or "") or None,
            "image_base64": str(image_b64 or "") or None,
            "revised_prompt": str(revised_prompt or "") or None,
            "usage": usage,
            "diagnostics": {
                "provider_request_id": provider_request_id,
                "latency_ms": latency_ms,
                "size": str(size or "1024x1024"),
            },
        }

    async def transcribe_audio(
        self,
        *,
        provider: str,
        model: str,
        filename: str,
        mime_type: str,
        audio_bytes: bytes,
        language: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        if str(provider or "").strip().lower() != "openai":
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Speech-to-text currently supports openai only.")
        if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Audio payload is empty.")

        api_key = await self._resolve_provider_api_key("openai")
        client = self._build_openai_client(api_key)
        started = time.perf_counter()
        file_obj = io.BytesIO(bytes(audio_bytes))
        file_obj.name = str(filename or "audio.wav")
        kwargs: dict[str, Any] = {}
        if str(language or "").strip():
            kwargs["language"] = str(language).strip()
        if str(prompt or "").strip():
            kwargs["prompt"] = str(prompt).strip()
        resp = await client.audio.transcriptions.create(
            model=str(model or "gpt-4o-mini-transcribe"),
            file=file_obj,
            **kwargs,
        )
        latency_ms = float((time.perf_counter() - started) * 1000.0)
        text = str(getattr(resp, "text", "") or "").strip()
        if not text and isinstance(resp, dict):
            text = str(resp.get("text") or "").strip()

        return {
            "provider": "openai",
            "model": str(model or "gpt-4o-mini-transcribe"),
            "filename": str(filename or ""),
            "mime_type": str(mime_type or "application/octet-stream"),
            "text": text,
            "diagnostics": {
                "latency_ms": latency_ms,
                "input_bytes": int(len(audio_bytes)),
                "empty_text": not bool(text),
            },
        }

    async def create_realtime_session(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
    ) -> dict[str, Any]:
        if str(provider or "").strip().lower() != "openai":
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Realtime sessions currently support openai only.")
        api_key = await self._resolve_provider_api_key("openai")
        payload = {
            "model": str(model or "gpt-4o-realtime-preview"),
            "voice": str(voice or "alloy"),
        }
        timeout_sec = float(os.getenv("OPENVEGAS_REALTIME_TIMEOUT_SEC", "8"))
        if self.http_client is not None:
            resp = await self.http_client.post(
                "https://api.openai.com/v1/realtime/sessions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=max(1.0, timeout_sec),
            )
        else:
            async with httpx.AsyncClient(follow_redirects=True, timeout=max(1.0, timeout_sec)) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/realtime/sessions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        if resp.status_code >= 400:
            detail = resp.text
            raise ContractError(APIErrorCode.PROVIDER_UNAVAILABLE, f"Realtime session failed: {detail[:500]}")
        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            body = {"raw": body}
        return body

    async def _resolve_provider_api_key(self, provider: str) -> str:
        return await resolve_provider_api_key(self.db, provider)

    @staticmethod
    def _wrapper_rewards_enabled() -> bool:
        return os.getenv("WRAPPER_REWARDS_ENABLED", "0") == "1"

    @staticmethod
    def _calculate_wrapper_reward(charge_v: Decimal) -> Decimal:
        ratio = Decimal(str(os.getenv("WRAPPER_REWARD_RATIO", "0")))
        if ratio <= 0:
            return Decimal("0")
        return (Decimal(str(charge_v)) * ratio).quantize(V_SCALE)

    @staticmethod
    def _payload_hash(req: InferenceRequest) -> str:
        canonical = json.dumps(
            {
                "provider": req.provider,
                "model": req.model,
                "messages": req.messages,
                "max_tokens": req.max_tokens,
                "enable_tools": bool(req.enable_tools),
                "enable_web_search": bool(req.enable_web_search),
                **({"strict_continuity": True} if req.strict_continuity else {}),
                **({"reasoning_effort": req.reasoning_effort} if req.reasoning_effort is not None else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _serialize_success_body(result: InferenceResult, *, reward_v: Decimal = Decimal("0")) -> str:
        return json.dumps(
            {
                "text": result.text,
                "completion_status": result.completion_status,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "v_cost": str(result.v_cost),
                "actual_cost_usd": str(result.actual_cost_usd),
                "reward_v": str(reward_v),
                "provider_request_id": result.provider_request_id,
                "tool_calls": result.tool_calls or [],
                "web_search_used": bool(result.web_search_used),
                "web_search_sources": list(result.web_search_sources or []),
                "web_search_retry_without_tool": bool(result.web_search_retry_without_tool),
                **({"web_search_requests": result.web_search_requests,
                    "web_search_cost_v": str(result.web_search_cost_v),
                    "managed_web_accounting": result._managed_web_accounting}
                   if result._managed_web_accounting is not None else {}),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize_result(row: Any) -> InferenceResult:
        raw = row.get("response_body_text")
        if not raw:
            raise ContractError(
                APIErrorCode.HOLD_CONFLICT,
                "Idempotent replay body missing for succeeded request.",
            )
        payload = json.loads(str(raw))
        if "managed_web_accounting" in payload:
            from openvegas.gateway.openrouter_web import (
                WebValidationError,
                validate_stored_web_result,
            )

            try:
                validate_stored_web_result(payload, request_hash=row.get("payload_hash"))
                if (Decimal(payload["v_cost"]) != Decimal(str(row["final_charge_v"]))
                        or Decimal(payload["actual_cost_usd"]) != Decimal(str(row["final_provider_cost_usd"]))
                        or payload["provider_request_id"] != row["provider_request_id"]):
                    raise WebValidationError("stored_web_row_mismatch")
            except (WebValidationError, KeyError, ValueError):
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Stored web settlement requires reconciliation.") from None
        result = InferenceResult(
            text=str(payload.get("text", "")),
            completion_status=str(payload.get("completion_status", "unknown")),
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
            v_cost=Decimal(str(payload.get("v_cost", "0"))).quantize(V_SCALE),
            actual_cost_usd=Decimal(str(payload.get("actual_cost_usd", "0"))).quantize(V_SCALE),
            provider_request_id=payload.get("provider_request_id"),
            tool_calls=payload.get("tool_calls") if isinstance(payload.get("tool_calls"), list) else None,
            web_search_used=bool(payload.get("web_search_used", False)),
            web_search_sources=payload.get("web_search_sources") if isinstance(payload.get("web_search_sources"), list) else None,
            web_search_retry_without_tool=bool(payload.get("web_search_retry_without_tool", False)),
            web_search_requests=payload.get("web_search_requests", 0),
            web_search_cost_v=Decimal(str(payload.get("web_search_cost_v", "0"))),
        )
        result._managed_web_accounting = payload.get("managed_web_accounting")
        result.inference_request_id = str(row["id"]) if row.get("id") is not None else None
        return result

    @staticmethod
    def _is_stale(updated_at: datetime | None) -> bool:
        stale_sec = int(os.getenv("INFERENCE_REQUEST_STALE_SEC", "120"))
        if stale_sec <= 0:
            stale_sec = 120
        if updated_at is None:
            return True
        ts = updated_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts > timedelta(seconds=stale_sec)
