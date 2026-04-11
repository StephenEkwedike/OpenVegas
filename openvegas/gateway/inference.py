"""AI Inference Gateway — routes, meters, and bills AI usage."""

from __future__ import annotations

import json
import io
import os
import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, AsyncGenerator

import httpx

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.catalog import ModelDisabled, ProviderCatalog
from openvegas.wallet.ledger import InsufficientBalance, WalletService

V_SCALE = Decimal("0.000001")


class Provider(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


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
    web_search_sources: list[str] | None = None
    web_search_retry_without_tool: bool = False


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

    async def infer(self, req: InferenceRequest) -> InferenceResult:
        ctx, replay = await self._prepare_inference_execution(req)
        if replay is not None:
            return replay

        try:
            result = await self._route_to_provider(req, ctx.provider_api_key)
        except Exception:
            await self._abort_inference_execution(ctx)
            raise
        return await self._finalize_inference_execution(ctx, req, result)

    async def stream_infer(self, req: InferenceRequest) -> AsyncGenerator[dict[str, Any], None]:
        ctx, replay = await self._prepare_inference_execution(req)
        if replay is not None:
            if str(replay.text or "").strip():
                yield {"type": "text_delta", "text": str(replay.text)}
            yield {"type": "completed", "result": replay}
            return

        result: InferenceResult | None = None
        try:
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
                result = await self._route_to_provider(req, ctx.provider_api_key)
                if str(result.text or "").strip():
                    yield {"type": "text_delta", "text": str(result.text)}
        except Exception:
            await self._abort_inference_execution(ctx)
            raise

        finalized = await self._finalize_inference_execution(ctx, req, result)
        yield {"type": "completed", "result": finalized}

    async def _prepare_inference_execution(
        self,
        req: InferenceRequest,
    ) -> tuple[_InferenceExecutionContext, InferenceResult | None]:
        model_config = await self.catalog.get_model(req.provider, req.model)
        if not model_config or not model_config["enabled"]:
            raise ModelDisabled(f"{req.model} is currently disabled")

        user_id = self._extract_user_id(req.account_id)
        payload_hash = self._payload_hash(req)
        provider_api_key = await self._resolve_provider_api_key(req.provider)

        max_v_cost = self._estimate_max_cost(model_config, req.max_tokens)
        reserve_v = max_v_cost

        if user_id:
            estimated_grant_v = await self._estimate_grant_cover_v(
                user_id=user_id,
                provider=req.provider,
                model_id=req.model,
                max_tokens=req.max_tokens,
                max_v_cost=max_v_cost,
            )
            reserve_v = max((max_v_cost - estimated_grant_v), Decimal("0")).quantize(V_SCALE)

        balance = await self.wallet.get_balance(req.account_id)
        if balance < reserve_v:
            raise InsufficientBalance(f"Need {reserve_v} $V reserved, have {balance} $V")

        request_id, replay = await self._begin_inference_request(
            user_id=user_id,
            idempotency_key=req.idempotency_key,
            payload_hash=payload_hash,
        )
        if replay is not None:
            return (
                _InferenceExecutionContext(
                    account_id=req.account_id,
                    model_config=model_config,
                    user_id=user_id,
                    provider_api_key=provider_api_key,
                    reserve_v=reserve_v,
                    request_id=request_id,
                    preauth_id="",
                    reservation_ref=request_id,
                ),
                replay,
            )

        preauth_id = str(uuid.uuid4())
        reservation_ref = request_id

        try:
            async with self.db.transaction() as tx:
                await tx.execute(
                    """
                    INSERT INTO inference_preauthorizations
                      (id, account_id, user_id, request_id, provider, model_id, reserved_v, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'reserved')
                    """,
                    preauth_id,
                    req.account_id,
                    user_id,
                    request_id,
                    req.provider,
                    req.model,
                    reserve_v,
                )
                if reserve_v > 0:
                    await self.wallet.reserve(
                        account_id=req.account_id,
                        amount=reserve_v,
                        reference_id=reservation_ref,
                        tx=tx,
                    )
        except Exception:
            await self._mark_request_failed(request_id=request_id)
            raise
        return (
            _InferenceExecutionContext(
                account_id=req.account_id,
                model_config=model_config,
                user_id=user_id,
                provider_api_key=provider_api_key,
                reserve_v=reserve_v,
                request_id=request_id,
                preauth_id=preauth_id,
                reservation_ref=reservation_ref,
            ),
            None,
        )

    async def _finalize_inference_execution(
        self,
        ctx: _InferenceExecutionContext,
        req: InferenceRequest,
        result: InferenceResult,
    ) -> InferenceResult:
        model_config = ctx.model_config
        user_id = ctx.user_id
        request_id = ctx.request_id
        preauth_id = ctx.preauth_id
        reservation_ref = ctx.reservation_ref
        reserve_v = ctx.reserve_v

        actual_v = self._calculate_v_cost(model_config, result.input_tokens, result.output_tokens)
        actual_usd = self._calculate_actual_usd(
            model_config, result.input_tokens, result.output_tokens
        )

        total_tokens = max(result.input_tokens + result.output_tokens, 0)
        usage_id = str(uuid.uuid4())

        grant_used_tokens = 0
        grant_used_v = Decimal("0")
        charge_v = actual_v

        async with self.db.transaction() as tx:
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
                grant_used_v = self._grant_coverage_v(actual_v, total_tokens, grant_used_tokens)
                charge_v = max((actual_v - grant_used_v), Decimal("0")).quantize(V_SCALE)

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

        return result

    async def _abort_inference_execution(
        self,
        ctx: _InferenceExecutionContext,
    ) -> None:
        if str(ctx.preauth_id or "").strip():
            await self._void_preauth(
                preauth_id=ctx.preauth_id,
                reservation_ref=ctx.reservation_ref,
                account_id=ctx.account_id,
                reserved_v=ctx.reserve_v,
            )
        await self._mark_request_failed(request_id=ctx.request_id)

    async def _begin_inference_request(
        self,
        *,
        user_id: str | None,
        idempotency_key: str | None,
        payload_hash: str,
    ) -> tuple[str, InferenceResult | None]:
        request_id = str(uuid.uuid4())
        idem_key = idempotency_key or request_id
        if not user_id:
            async with self.db.transaction() as tx:
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

        async with self.db.transaction() as tx:
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
                       final_charge_v, final_provider_cost_usd, provider_request_id
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
                if str(row["payload_hash"]) != payload_hash:
                    raise ContractError(
                        APIErrorCode.IDEMPOTENCY_CONFLICT,
                        "Idempotency key conflict: payload mismatch.",
                    )
                rid = str(row["id"])
                status = str(row["status"])
                if status == "succeeded" and row["response_status"] == 200 and row["response_body_text"]:
                    return rid, self._deserialize_result(row)
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

    async def _mark_request_failed(self, request_id: str) -> None:
        async with self.db.transaction() as tx:
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
                    {"error": APIErrorCode.PROVIDER_UNAVAILABLE.value, "detail": "Inference provider call failed"},
                    separators=(",", ":"),
                ),
            )

    async def _estimate_grant_cover_v(
        self,
        user_id: str,
        provider: str,
        model_id: str,
        max_tokens: int,
        max_v_cost: Decimal,
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
        estimated_total = max_tokens * 3
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
    ) -> None:
        async with self.db.transaction() as tx:
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

    async def _route_to_provider(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        """Route to the appropriate provider SDK."""
        if req.enable_tools and req.provider != "openai":
            raise ContractError(
                APIErrorCode.INVALID_TRANSITION,
                "Tool-calling mode is currently supported only for openai provider.",
            )
        if req.provider == "anthropic":
            return await self._call_anthropic(req, api_key)
        if req.provider == "openai":
            return await self._call_openai(req, api_key)
        if req.provider == "gemini":
            return await self._call_gemini(req, api_key)
        raise ValueError(f"Unknown provider: {req.provider}")

    async def _call_anthropic(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model=req.model,
            max_tokens=req.max_tokens,
            messages=req.messages,
        )
        return InferenceResult(
            text=msg.content[0].text,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            provider_request_id=getattr(msg, "id", None),
        )

    async def _call_openai(self, req: InferenceRequest, api_key: str) -> InferenceResult:
        client = self._build_openai_client(api_key)
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
            if req.enable_web_search and self._should_retry_without_web_tool(exc):
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
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        prompt = "\n".join(m.get("content", "") for m in req.messages)
        model = genai.GenerativeModel(req.model)
        resp = await model.generate_content_async(prompt)
        meta = resp.usage_metadata
        return InferenceResult(
            text=resp.text,
            input_tokens=meta.prompt_token_count if meta else 0,
            output_tokens=meta.candidates_token_count if meta else 0,
            provider_request_id=getattr(resp, "response_id", None),
        )

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
        """Resolve provider credentials with registry-first precedence."""
        runtime_env = os.getenv("OPENVEGAS_RUNTIME_ENV", os.getenv("ENV", "local")).strip() or "local"
        row = None
        try:
            row = await self.db.fetchrow(
                """
                SELECT key_alias
                FROM provider_credentials
                WHERE provider = $1
                  AND env = $2
                  AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                provider,
                runtime_env,
            )
        except Exception:
            row = None

        if row:
            key_alias = str(row["key_alias"]).strip()
            key = os.getenv(key_alias, "").strip()
            if key:
                return key
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE,
                f"No active provider credentials configured for {provider}.",
            )

        allow_env_fallback = runtime_env.lower() in {"local", "dev", "development", "test"}
        if allow_env_fallback:
            env_name = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "gemini": "GEMINI_API_KEY",
            }.get(provider, "")
            key = os.getenv(env_name, "").strip()
            if key:
                return key

        raise ContractError(
            APIErrorCode.PROVIDER_UNAVAILABLE,
            f"No active provider credentials configured for {provider}.",
        )

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
        return InferenceResult(
            text=str(payload.get("text", "")),
            input_tokens=int(payload.get("input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
            v_cost=Decimal(str(payload.get("v_cost", "0"))).quantize(V_SCALE),
            actual_cost_usd=Decimal(str(payload.get("actual_cost_usd", "0"))).quantize(V_SCALE),
            provider_request_id=payload.get("provider_request_id"),
            tool_calls=payload.get("tool_calls") if isinstance(payload.get("tool_calls"), list) else None,
            web_search_used=bool(payload.get("web_search_used", False)),
            web_search_sources=payload.get("web_search_sources") if isinstance(payload.get("web_search_sources"), list) else None,
            web_search_retry_without_tool=bool(payload.get("web_search_retry_without_tool", False)),
        )

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
