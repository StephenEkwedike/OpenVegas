"""AI Inference Gateway — routes, meters, and bills AI usage."""

from __future__ import annotations

import json
import os
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

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


@dataclass
class InferenceResult:
    text: str
    input_tokens: int
    output_tokens: int
    v_cost: Decimal = Decimal("0")
    actual_cost_usd: Decimal = Decimal("0")
    provider_request_id: str | None = None


class AIGateway:
    """Routes inference requests, meters usage, and settles charges with grant-first policy."""

    def __init__(self, db: Any, wallet: WalletService, catalog: ProviderCatalog):
        self.db = db
        self.wallet = wallet
        self.catalog = catalog

    async def infer(self, req: InferenceRequest) -> InferenceResult:
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
            return replay

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

        try:
            result = await self._route_to_provider(req, provider_api_key)
        except Exception:
            await self._void_preauth(
                preauth_id=preauth_id,
                reservation_ref=reservation_ref,
                account_id=req.account_id,
                reserved_v=reserve_v,
            )
            await self._mark_request_failed(request_id=request_id)
            raise

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
        import openai

        client = openai.AsyncOpenAI(api_key=api_key)
        resp = await client.chat.completions.create(
            model=req.model,
            max_tokens=req.max_tokens,
            messages=req.messages,
        )
        return InferenceResult(
            text=resp.choices[0].message.content or "",
            input_tokens=resp.usage.prompt_tokens,
            output_tokens=resp.usage.completion_tokens,
            provider_request_id=getattr(resp, "id", None),
        )

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
