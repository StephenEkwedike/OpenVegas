"""Provider catalog — reads from Supabase provider_catalog table."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.providers import (
    get_model_review,
    get_provider,
    model_capabilities,
    resolve_provider_api_key,
)


class ModelDisabled(Exception):
    pass


def validate_catalog_entry(provider: str, model_id: str, row: dict | None) -> None:
    descriptor = get_provider(provider)
    if not row or row.get("enabled") is not True:
        raise ModelDisabled(f"{provider}/{model_id} is unknown or disabled")
    for field in (
        "cost_input_per_1m",
        "cost_output_per_1m",
        "v_price_input_per_1m",
        "v_price_output_per_1m",
    ):
        try:
            value = Decimal(str(row[field]))
            if not value.is_finite() or value < 0:
                raise ValueError("invalid price")
        except (KeyError, ValueError, InvalidOperation):
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE, "Model pricing requires operator review."
            ) from None
    if descriptor.requires_review:
        review = get_model_review(provider, model_id)
        if (
            review.get("account_access") is not True
            or review.get("completion_chat") is not True
            or model_capabilities(provider, model_id)["context_window_tokens"] is None
        ):
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE,
                f"{descriptor.label} requires a current exact-model access, chat, and context review.",
            )
        if type(row.get("max_tokens")) is not int or row["max_tokens"] < 1:
            raise ContractError(
                APIErrorCode.PROVIDER_UNAVAILABLE,
                f"{descriptor.label} output budget requires operator review.",
            )
        if provider == "openrouter":
            from openvegas.gateway.openrouter import valid_model

            if not valid_model(model_id):
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION,
                    "Choose an exact OpenRouter model, not a router or dynamic alias.",
                )
            aliases = review.get("response_model_ids", [])
            if (
                not isinstance(aliases, list)
                or len(aliases) > 2
                or any(not valid_model(alias) for alias in aliases)
            ):
                raise ContractError(
                    APIErrorCode.PROVIDER_UNAVAILABLE,
                    "OpenRouter response model IDs require operator review.",
                )
            reviewed_limit = review.get("max_tokens")
            if reviewed_limit is not None and (
                type(reviewed_limit) is not int or row["max_tokens"] > reviewed_limit
            ):
                raise ContractError(
                    APIErrorCode.PROVIDER_UNAVAILABLE,
                    "OpenRouter output budget exceeds its review.",
                )
            try:
                matched = all(
                    Decimal(str(review.get(field))) == Decimal(str(row[field]))
                    for field in ("cost_input_per_1m", "cost_output_per_1m")
                )
            except InvalidOperation:
                matched = False
            if not matched:
                raise ContractError(
                    APIErrorCode.PROVIDER_UNAVAILABLE,
                    "OpenRouter catalog pricing changed or is unreviewed; operator review required.",
                )


class ProviderCatalog:
    """Interface to the provider_catalog Supabase table.
    Disabling a model blocks routing instantly without a deploy."""

    def __init__(self, db: Any):
        self.db = db

    async def get_model(self, provider: str, model_id: str) -> dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM provider_catalog WHERE provider = $1 AND model_id = $2",
            provider,
            model_id,
        )
        return dict(row) if row else None

    async def get_pricing(self, provider: str, model_id: str) -> dict:
        row = await self.get_model(provider, model_id)
        if not row:
            raise ValueError(f"Unknown model: {provider}/{model_id}")
        return row

    async def describe_model(self, provider: str, model_id: str) -> dict:
        row = await self.get_model(provider, model_id)
        return await self._describe_row(provider, model_id, row)

    async def _describe_row(self, provider: str, model_id: str, row: dict | None) -> dict:
        descriptor = dict(row or {"provider": provider, "model_id": model_id})
        descriptor.update(
            available=False, availability="unavailable", credential_source="server_managed"
        )
        try:
            validate_catalog_entry(provider, model_id, row)
            await resolve_provider_api_key(self.db, provider)
        except (ContractError, ModelDisabled) as exc:
            descriptor["unavailable_reason"] = str(exc)
        else:
            descriptor.update(
                available=True, availability="configured_not_live_verified", unavailable_reason=None
            )
        try:
            descriptor["capabilities"] = model_capabilities(provider, model_id)
        except ContractError:
            descriptor["capabilities"] = {}
        return descriptor

    async def list_descriptors(self, provider: str | None = None) -> list[dict]:
        if provider is not None:
            get_provider(provider)
        rows = await self.list_models(provider=provider)
        return [await self._describe_row(row["provider"], row["model_id"], row) for row in rows]

    async def validate_selection(
        self,
        provider: str,
        model_id: str,
        *,
        required_capabilities: list[str] | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        descriptor = await self.describe_model(provider, model_id)
        if not descriptor["available"]:
            raise ContractError(APIErrorCode.PROVIDER_UNAVAILABLE, descriptor["unavailable_reason"])
        caps = descriptor["capabilities"]
        for feature in required_capabilities or []:
            if caps.get(feature) is not True:
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION,
                    f"{feature} is unsupported or unreviewed; disable it or select another model.",
                )
        if max_tokens is not None:
            limit = descriptor.get("max_tokens")
            if (
                type(max_tokens) is not int
                or max_tokens < 1
                or (type(limit) is int and max_tokens > limit)
            ):
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION, "Output budget exceeds catalog limit."
                )
        return descriptor

    async def list_models(
        self, provider: str | None = None, enabled_only: bool = True
    ) -> list[dict]:
        query = "SELECT * FROM provider_catalog WHERE 1=1"
        params: list = []
        if provider:
            params.append(provider)
            query += f" AND provider = ${len(params)}"
        if enabled_only:
            query += " AND enabled = TRUE"
        query += " ORDER BY provider, model_id"
        rows = await self.db.fetch(query, *params)
        return [dict(r) for r in rows]

    async def toggle_model(self, provider: str, model_id: str, enabled: bool):
        await self.db.execute(
            "UPDATE provider_catalog SET enabled = $1, updated_at = now() "
            "WHERE provider = $2 AND model_id = $3",
            enabled,
            provider,
            model_id,
        )

    async def log_usage(
        self,
        account_id: str,
        user_id: str | None,
        provider: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        v_cost: Decimal,
        actual_cost: Decimal,
        *,
        tx=None,
    ):
        actor_type = "agent" if account_id.startswith("agent:") else "human"
        conn = tx or self.db
        await conn.execute(
            "INSERT INTO inference_usage "
            "(user_id, account_id, actor_type, provider, model_id, input_tokens, output_tokens, v_cost, actual_cost_usd) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            user_id,
            account_id,
            actor_type,
            provider,
            model_id,
            input_tokens,
            output_tokens,
            v_cost,
            actual_cost,
        )
