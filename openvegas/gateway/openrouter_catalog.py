"""Public OpenRouter discovery and operator review, never routing authorization.

Official contracts:
https://openrouter.ai/docs/guides/overview/models
https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties
https://openrouter.ai/docs/guides/routing/provider-selection
https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
https://openrouter.ai/docs/guides/best-practices/prompt-caching

Public top-provider prices are observations, not account access guarantees or
quotes for every endpoint. Reviewed rows stay disabled until a separate operator
installation. No credentials, .env, account configuration or database is read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

import httpx

MODELS_URL = "https://openrouter.ai/api/v1/models"
MAX_BYTES = 8 * 1024 * 1024
MAX_MODELS = 4096
MAX_REVIEWS = 100
FETCH_TIMEOUT = 15
MAX_TOKENS = 10_000_000
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
DECIMAL_STRING = re.compile(r"(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,24})?(?:[eE][+-]?[0-9]{1,2})?\Z")
UNIT_FEES = frozenset(
    {
        "request",
        "image",
        "web_search",
        "audio",
        "input_audio",
        "output_audio",
        "input_audio_cache",
        "internal_reasoning",
    }
)
CACHE_WRITE_FEES = frozenset({"input_cache_write", "input_cache_write_1h"})
CACHE_FEES = CACHE_WRITE_FEES | {"input_cache_read"}
SCOPED_OUT_FEES = frozenset(
    {
        "image",
        "web_search",
        "audio",
        "input_audio",
        "output_audio",
        "input_audio_cache",
    }
)


class ReviewError(ValueError):
    """Bounded, nonsecret diagnostics safe for an operator terminal."""


def exact_model_id(value: object) -> str:
    if not isinstance(value, str) or not MODEL_ID.fullmatch(value):
        raise ReviewError(
            "An exact author/model ID is required; variants and wildcards are refused"
        )
    owner, model = value.split("/")
    if owner == "openrouter" or re.search(
        r"(^|[-_.])(latest|auto|router)([-_.]|$)", model, re.IGNORECASE
    ):
        raise ReviewError("Dynamic routers and latest/auto aliases are refused")
    return value


def decimal_price(value: object) -> Decimal:
    """Parse documented decimal strings without accepting floats or NaN."""
    if not isinstance(value, str) or not DECIMAL_STRING.fullmatch(value):
        raise ReviewError("Prices must be finite nonnegative decimal strings")
    try:
        price = Decimal(value)
        if not price.is_finite() or price < 0 or price > Decimal("99999999.99"):
            raise ReviewError("Price exceeds supported bounds")
        return price
    except InvalidOperation:
        raise ReviewError("Invalid decimal price") from None


def _fixed(value: Decimal) -> str:
    return format(value, "f")


def usd_per_million(value: object) -> str:
    with localcontext() as context:
        context.prec = 80
        result = decimal_price(value) * 1_000_000
    if result > Decimal("999999.9999"):
        raise ReviewError("USD rate exceeds the provider_catalog NUMERIC(10,4) bound")
    return _fixed(result)


def _stored_price(value: object, *, retail: bool) -> str:
    amount = decimal_price(value)
    scale, maximum = (
        (Decimal("0.01"), Decimal("99999999.99"))
        if retail
        else (
            Decimal("0.0001"),
            Decimal("999999.9999"),
        )
    )
    with localcontext() as context:
        context.prec = 80
        if amount > maximum or amount != amount.quantize(scale):
            raise ReviewError(
                "Rate cannot be stored exactly in current catalog precision; no rounding performed"
            )
    return _fixed(amount)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ReviewError("Duplicate JSON keys are refused")
        result[key] = value
    return result


def parse_json(data: bytes, *, limit: int = MAX_BYTES) -> dict:
    if not isinstance(data, bytes) or not 0 < len(data) <= limit:
        raise ReviewError("JSON is empty or exceeds its size limit")
    try:
        result = json.loads(
            data,
            object_pairs_hook=_pairs,
            parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ReviewError("Nonfinite JSON value")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ReviewError("Invalid JSON object; duplicate/nonfinite values are refused") from None
    if not isinstance(result, dict):
        raise ReviewError("A JSON object is required")
    return result


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ReviewError("Use an explicit timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except ValueError:
        raise ReviewError("Use an explicit timezone-aware ISO timestamp") from None


def _now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ReviewError("Clock must be timezone aware")
    return current.astimezone(UTC)


def _positive_int(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TOKENS:
        raise ReviewError("Token limits must be positive bounded integers")
    return value


def _strings(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 64
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
            for item in value
        )
    ):
        raise ReviewError("Invalid model capability list")
    return sorted(set(value))


def _candidate(model: dict, *, now: datetime) -> dict:
    model_id = exact_model_id(model.get("id"))
    name = model.get("name", model_id)
    if not isinstance(name, str) or not 1 <= len(name) <= 200 or not name.isprintable():
        name = model_id
    candidate = {
        "model_id": model_id,
        "display_name": name,
        "account_access": None,
        "availability": "public_listing_not_account_access",
        "rejection_reasons": [],
        "warnings": [],
        "pricing_observed": {},
        "missing_optional_fee_fields": [],
        "pricing_scope": {
            "input": "text_only",
            "output": "text_only",
            "plugins": False,
            "explicit_prompt_cache_write": False,
            "request_usd_cap": "0",
            "scoped_out_nonzero_fees": [],
        },
    }
    reasons = candidate["rejection_reasons"]
    canonical = model.get("canonical_slug")
    try:
        candidate["canonical_slug"] = exact_model_id(canonical)
    except ReviewError:
        candidate["canonical_slug"] = None
        candidate["warnings"].append("Canonical response ID unavailable; no alias will be inferred")
    architecture = model.get("architecture")
    try:
        if not isinstance(architecture, dict):
            raise ReviewError("Missing architecture")
        inputs = _strings(architecture.get("input_modalities"))
        outputs = _strings(architecture.get("output_modalities"))
        parameters = _strings(model.get("supported_parameters"))
        candidate.update(
            input_modalities=inputs, output_modalities=outputs, supported_parameters=parameters
        )
        if "text" not in inputs or outputs != ["text"]:
            reasons.append("Text input and text-only output are required by the current transport")
        if "max_tokens" not in parameters:
            reasons.append("Current transport requires advertised max_tokens support")
        candidate["advertised_capabilities"] = {
            "tools": {"tools", "tool_choice"} <= set(parameters),
            "image_input": "image" in inputs,
        }
    except ReviewError:
        reasons.append("Missing or invalid modality/parameter metadata")
        candidate["advertised_capabilities"] = {"tools": False, "image_input": False}
    top = model.get("top_provider")
    try:
        if not isinstance(top, dict):
            raise ReviewError("Missing top provider")
        context_limit = min(
            _positive_int(model.get("context_length")), _positive_int(top.get("context_length"))
        )
        output_limit = min(_positive_int(top.get("max_completion_tokens")), context_limit)
        candidate.update(context_window_tokens=context_limit, max_tokens=output_limit)
    except ReviewError:
        reasons.append("Missing or invalid advertised context/output bound; no limit invented")
        candidate.update(context_window_tokens=None, max_tokens=None)
    expiration = model.get("expiration_date")
    candidate["expiration_date"] = None
    if expiration is not None:
        try:
            if not isinstance(expiration, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", expiration
            ):
                raise ValueError
            end = datetime.fromisoformat(expiration).replace(tzinfo=UTC)
            candidate["expiration_date"] = end.isoformat()
            if end <= now:
                reasons.append("Model is expired/deprecated")
        except ValueError:
            reasons.append("Unrecognized expiration date")

    pricing = model.get("pricing")
    if not isinstance(pricing, dict) or len(pricing) > 32:
        reasons.append("Missing or invalid pricing object")
    else:
        for key, raw in pricing.items():
            if key == "overrides":
                if not isinstance(raw, list) or raw:
                    reasons.append(
                        "Conditional pricing overrides are unsupported; rates cannot be flattened"
                    )
                candidate["pricing_observed"][key] = (
                    "none" if raw == [] else "requires_separate_review"
                )
                continue
            if key not in UNIT_FEES | CACHE_FEES | {"prompt", "completion"}:
                reasons.append("Unknown pricing field; monetary meaning requires adapter review")
                continue
            try:
                value = decimal_price(raw)
                candidate["pricing_observed"][key] = _fixed(value)
                if key in SCOPED_OUT_FEES and value != 0:
                    candidate["pricing_scope"]["scoped_out_nonzero_fees"].append(key)
                    candidate["warnings"].append(
                        f"Nonzero {key} rate retained; feature is forbidden on this text-only route"
                    )
                elif key == "internal_reasoning" and value != 0:
                    if value > decimal_price(pricing.get("completion")):
                        reasons.append("internal_reasoning exceeds the output token price cap")
                    else:
                        candidate["warnings"].append(
                            "Reasoning is charged within output usage: operator must verify max_tokens bounds combined reasoning and visible output on the selected endpoint"
                        )
                        candidate["pricing_scope"]["reasoning_in_output_token_budget"] = True
                elif key in UNIT_FEES and value != 0:
                    reasons.append(f"Nonzero {key} fee is unsupported by token-only billing")
            except ReviewError:
                reasons.append(f"Invalid {key} fee; never interpreted as zero")
        for key, output in (("prompt", "cost_input_per_1m"), ("completion", "cost_output_per_1m")):
            try:
                converted = usd_per_million(pricing.get(key))
                candidate[output] = converted
                _stored_price(converted, retail=False)
            except ReviewError:
                reasons.append(f"Missing, invalid or unrepresentable {key} token price")
        for key in CACHE_FEES & pricing.keys():
            try:
                if key in CACHE_WRITE_FEES and decimal_price(pricing[key]) != 0:
                    candidate["pricing_scope"]["scoped_out_nonzero_fees"].append(key)
                    candidate["warnings"].append(
                        "Cache-write rate retained: operator must verify the chosen route has no automatic paid cache writes; no cache_control opt-in is allowed"
                    )
                elif decimal_price(pricing[key]) > decimal_price(pricing.get("prompt")):
                    reasons.append(
                        f"{key} exceeds the input price cap; no extra cache charge permitted"
                    )
            except ReviewError:
                reasons.append(f"Invalid {key} token pricing")
        candidate["missing_optional_fee_fields"] = sorted((UNIT_FEES | CACHE_FEES) - pricing.keys())
        if "request" not in pricing:
            candidate["warnings"].append(
                "Request fee is not listed; explicit zero-request-price routing cap is required"
            )
        if candidate["missing_optional_fee_fields"]:
            candidate["warnings"].append(
                "Missing optional fees are unknown, not zero; non-text features remain disabled"
            )
    candidate["eligible_for_review"] = not reasons
    candidate["pricing_scope"]["scoped_out_nonzero_fees"].sort()
    return candidate


def candidate_report(
    payload: bytes, *, observed_at: str | None = None, now: datetime | None = None
) -> dict:
    current = _now(now)
    root = parse_json(payload)
    models = root.get("data")
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_MODELS:
        raise ReviewError("Expected a bounded nonempty data array")
    if observed_at is not None and _time(observed_at) > current:
        raise ReviewError("Observation timestamp cannot be in the future")
    seen, candidates, rejected = set(), [], []
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            rejected.append({"index": index, "reason": "Model entry is not an object"})
            continue
        model_id = model.get("id")
        if isinstance(model_id, str):
            if model_id in seen:
                raise ReviewError("Duplicate model IDs make a catalog snapshot ambiguous")
            seen.add(model_id)
        try:
            candidates.append(_candidate(model, now=current))
        except ReviewError as exc:
            rejected.append({"index": index, "reason": str(exc)})
    return {
        "schema_version": 1,
        "kind": "openrouter_public_candidates",
        "source_url": MODELS_URL,
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "observed_at": _time(observed_at).isoformat() if observed_at else None,
        "account_access_inferred": False,
        "models": sorted(candidates, key=lambda item: item["model_id"]),
        "rejected_entries": rejected,
        "notice": "Public discovery only. No account access, retail markup, routing enablement or live-provider verification inferred.",
    }


async def fetch_public_models(*, transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """One explicit unauthenticated GET; no redirects, proxies, cookies or retry."""
    try:
        async with (
            asyncio.timeout(FETCH_TIMEOUT),
            httpx.AsyncClient(
                timeout=FETCH_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client,
            client.stream(
                "GET",
                MODELS_URL,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response,
        ):
            if response.status_code != 200:
                raise ReviewError(
                    "Public model listing unavailable; no authentication, redirect or retry attempted"
                )
            if (
                response.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                raise ReviewError("Public listing did not return JSON")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ReviewError("Compressed listings are refused to bound decoding")
            length = response.headers.get("content-length")
            if length is not None and (
                not re.fullmatch(r"[0-9]{1,10}", length) or not 0 < int(length) <= MAX_BYTES
            ):
                raise ReviewError("Public listing exceeds its size limit")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_BYTES:
                    raise ReviewError("Public listing exceeds its size limit")
                data.extend(chunk)
        payload = bytes(data)
        parse_json(payload)
        return payload
    except (httpx.HTTPError, TimeoutError):
        raise ReviewError(
            "Public listing request failed or timed out; no retry attempted"
        ) from None


def reviewed_bundle(
    payload: bytes,
    plan: dict,
    *,
    observed_at: str,
    ack_account_access: bool = False,
    ack_retail_prices: bool = False,
    now: datetime | None = None,
) -> dict:
    """Build disabled rows plus an exact-ID review map; never install either."""
    current = _now(now)
    report = candidate_report(payload, observed_at=observed_at, now=current)
    observed = _time(observed_at)
    if current - observed > timedelta(hours=24):
        raise ReviewError("Catalog observation is stale; obtain and review a new public snapshot")
    if ack_account_access is not True or ack_retail_prices is not True:
        raise ReviewError("Explicit account-access and retail-price acknowledgements are required")
    if (
        not isinstance(plan, dict)
        or set(plan) != {"schema_version", "source_sha256", "models"}
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
    ):
        raise ReviewError("Invalid operator review plan schema")
    if plan["source_sha256"] != report["source_sha256"]:
        raise ReviewError("Review plan does not match exact public catalog bytes")
    entries = plan["models"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_REVIEWS:
        raise ReviewError("Review requires a bounded nonempty list of exact models")
    public = {item["model_id"]: item for item in report["models"]}
    rows, reviews, seen = [], {}, set()
    fields = {
        "model_id",
        "account_access",
        "completion_chat",
        "context_window_tokens",
        "max_tokens",
        "cost_input_per_1m",
        "cost_output_per_1m",
        "v_price_input_per_1m",
        "v_price_output_per_1m",
        "capabilities",
        "reviewed_at",
        "expires_at",
        "pricing_scope_acknowledged",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"response_model_ids"} != fields:
            raise ReviewError(
                "Review must explicitly state access, chat, bounds, capabilities, dates and all four prices"
            )
        model_id = exact_model_id(entry["model_id"])
        candidate = public.get(model_id)
        if model_id in seen or not candidate or not candidate["eligible_for_review"]:
            raise ReviewError("Duplicate, missing or ineligible exact-model review")
        seen.add(model_id)
        if entry["account_access"] is not True or entry["completion_chat"] is not True:
            raise ReviewError(
                "Operator must verify this managed account can use chat for the exact model"
            )
        if entry["pricing_scope_acknowledged"] is not True:
            raise ReviewError(
                "Operator must confirm text-only/no-plugin/no-paid-cache-write routing and combined reasoning/output limits"
            )
        reviewed, expires = _time(entry["reviewed_at"]), _time(entry["expires_at"])
        if not observed <= reviewed <= current < expires or expires - reviewed > timedelta(days=30):
            raise ReviewError(
                "Review must be current, after its snapshot, and expire within 30 days"
            )
        if candidate["expiration_date"] and expires > _time(candidate["expiration_date"]):
            raise ReviewError("Review outlives the advertised model expiration")
        context_limit = _positive_int(entry["context_window_tokens"])
        output_limit = _positive_int(entry["max_tokens"])
        if context_limit > candidate["context_window_tokens"] or output_limit > min(
            candidate["max_tokens"], context_limit
        ):
            raise ReviewError("Reviewed bounds exceed advertised context/output ceilings")
        caps = entry["capabilities"]
        if (
            not isinstance(caps, dict)
            or set(caps) != {"tools", "image_input", "web_search"}
            or any(type(flag) is not bool for flag in caps.values())
        ):
            raise ReviewError("Explicit boolean tools/image_input/web_search review is required")
        if caps["image_input"] or caps["web_search"]:
            raise ReviewError("Current OpenRouter adapter is text/local-tools only")
        if caps["tools"] and not candidate["advertised_capabilities"]["tools"]:
            raise ReviewError("Tools/tool_choice support is not advertised for this exact model")
        prices = {}
        for key in (
            "cost_input_per_1m",
            "cost_output_per_1m",
            "v_price_input_per_1m",
            "v_price_output_per_1m",
        ):
            prices[key] = _stored_price(entry[key], retail=key.startswith("v_"))
            if key.startswith("cost_") and Decimal(prices[key]) != Decimal(candidate[key]):
                raise ReviewError("Reviewed USD rate differs from the observed public token price")
        aliases = entry.get("response_model_ids", [model_id])
        if (
            not isinstance(aliases, list)
            or not 1 <= len(aliases) <= 2
            or any(not isinstance(alias, str) for alias in aliases)
            or len(set(aliases)) != len(aliases)
        ):
            raise ReviewError("Response model IDs must be explicitly reviewed exact IDs")
        allowed = {model_id, candidate["canonical_slug"]}
        if (
            any(exact_model_id(alias) not in allowed for alias in aliases)
            or model_id not in aliases
        ):
            raise ReviewError(
                "Response alias is not the selected ID or its advertised canonical slug"
            )
        rows.append(
            {
                "provider": "openrouter",
                "model_id": model_id,
                "display_name": candidate["display_name"],
                "enabled": False,
                "max_tokens": output_limit,
                **prices,
            }
        )
        reviews[f"openrouter:{model_id}"] = {
            "account_access": True,
            "account_access_source": "operator_attestation",
            "pricing_scope_acknowledged": True,
            "completion_chat": True,
            "context_window_tokens": context_limit,
            "max_tokens": output_limit,
            "capabilities": dict(caps),
            "reviewed_at": reviewed.isoformat(),
            "expires_at": expires.isoformat(),
            **prices,
            "response_model_ids": aliases,
            **(
                {"canonical_slug": candidate["canonical_slug"]}
                if candidate["canonical_slug"]
                else {}
            ),
            "pricing_policy": "text_tokens_only_zero_request_cap_no_plugins",
            "pricing_scope": candidate["pricing_scope"],
            "observed_pricing": candidate["pricing_observed"],
            "missing_optional_fee_fields": candidate["missing_optional_fee_fields"],
            "source_sha256": report["source_sha256"],
            "observed_at": observed.isoformat(),
        }
    return {
        "schema_version": 1,
        "kind": "openrouter_reviewed_bundle",
        "source_url": MODELS_URL,
        "source_sha256": report["source_sha256"],
        "observed_at": observed.isoformat(),
        "provider_catalog": sorted(rows, key=lambda row: row["model_id"]),
        "model_reviews": dict(sorted(reviews.items())),
        "installed": False,
        "live_verified": False,
        "notice": "Disabled candidates only. Account access is operator-attested, not inferred from public models. No DB writes, markup inference or paid calls.",
    }


def review_template(report: dict, model_ids: list[str]) -> dict:
    """An intentionally incomplete plan; no account, rate markup or dates guessed."""
    if (
        not isinstance(model_ids, list)
        or not model_ids
        or len(model_ids) > MAX_REVIEWS
        or any(not isinstance(value, str) for value in model_ids)
        or len(set(model_ids)) != len(model_ids)
    ):
        raise ReviewError("Select a bounded nonempty list of unique exact model IDs")
    public = {item["model_id"]: item for item in report["models"]}
    models = []
    for model_id in model_ids:
        exact_model_id(model_id)
        row = public.get(model_id)
        if not row or not row["eligible_for_review"]:
            raise ReviewError(
                "Selected model is absent or ineligible; inspect the candidate report"
            )
        models.append(
            {
                "model_id": model_id,
                "account_access": False,
                "pricing_scope_acknowledged": False,
                "completion_chat": False,
                "context_window_tokens": None,
                "max_tokens": None,
                "cost_input_per_1m": row["cost_input_per_1m"],
                "cost_output_per_1m": row["cost_output_per_1m"],
                "v_price_input_per_1m": None,
                "v_price_output_per_1m": None,
                "capabilities": {"tools": False, "image_input": False, "web_search": False},
                "reviewed_at": None,
                "expires_at": None,
                "response_model_ids": [model_id],
            }
        )
    return {"schema_version": 1, "source_sha256": report["source_sha256"], "models": models}
