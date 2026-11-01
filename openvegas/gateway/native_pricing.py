"""Server-owned native billing policy, using the existing credit conversion."""
import hashlib
import json
import os
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from openvegas.gateway.providers import get_model_review
from openvegas.payments.conversion import v_per_usd

SCALE = Decimal("0.000001")


def amount(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or not 0 <= value <= 1_000_000:
            raise ValueError
        return value
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid native billing amount") from None


def quote(model, provider="openrouter"):
    multiplier = amount(os.getenv("OPENVEGAS_NATIVE_PRICE_MULTIPLIER", "1.40"))
    if not Decimal("1.20") <= multiplier <= Decimal(3):
        raise ValueError("Native price multiplier must be between 1.20 and 3")
    rate = v_per_usd()
    pricing = {"model": model["model_id"], "v_per_usd": str(rate),
               "multiplier": str(multiplier), "v_per_supplier_usd": str(rate * multiplier),
               "supplier_input_usd_per_million": str(amount(model["cost_input_per_1m"])),
               "supplier_output_usd_per_million": str(amount(model["cost_output_per_1m"])),
               "policy": "verified_supplier_cost_plus_markup_v1"}
    if provider != "openrouter":
        from openvegas.gateway.native_direct import review
        pricing.update(provider=provider, native_transport=(
            "messages" if provider == "anthropic" else "responses") + "_direct_v1",
            policy="reviewed_direct_token_cost_plus_markup_v1",
            direct_metering=review(provider, model["model_id"], model))
        media = get_model_review(provider, model["model_id"]).get("native_media")
        if media is not None:
            if (type(media) is not dict or media.get("pricing") != "input_tokens_only"
                    or type(media.get("modalities")) is not list or not media["modalities"]
                    or any(m not in {"image", "pdf"} for m in media["modalities"])):
                raise ValueError("Unreviewed direct media fees")
            pricing["native_media"] = media
        pricing["fingerprint"] = hashlib.sha256(json.dumps(pricing, sort_keys=True).encode()).hexdigest()
        return pricing
    transport = get_model_review("openrouter", model["model_id"]).get("native_transport")
    if transport is not None:
        if transport != "responses_openai_v1" or not model["model_id"].startswith("openai/"):
            raise ValueError("Unreviewed native transport")
        pricing["native_transport"] = transport
    media = get_model_review("openrouter", model["model_id"]).get("native_media")
    if media is not None:
        if (type(media) is not dict or media.get("pricing") != "input_tokens_only"
                or type(media.get("modalities")) is not list or not media["modalities"]
                or any(m not in {"image", "pdf"} for m in media["modalities"])
                or not (transport == "responses_openai_v1" or model["model_id"].startswith("anthropic/"))):
            raise ValueError("Unreviewed native media fees")
        pricing["native_media"] = {"pricing": "input_tokens_only", "modalities": sorted(set(media["modalities"]))}
    web = get_model_review("openrouter", model["model_id"]).get("native_web")
    if web is not None:
        if (transport != "responses_openai_v1" or type(web) is not dict
                or web != {"tool": "web_search", "max_calls": 1,
                           "supplier_usd_per_call": "0.01", "max_model_rounds": 2}
                or type(web.get("max_calls")) is not int or type(web.get("max_model_rounds")) is not int):
            raise ValueError("Unreviewed native web fees or limits")
        pricing["native_web"] = dict(web)
    pricing["fingerprint"] = hashlib.sha256(json.dumps(pricing, sort_keys=True).encode()).hexdigest()
    return pricing


def charge(supplier_usd, pricing):
    return (amount(supplier_usd) * amount(pricing["v_per_supplier_usd"])).quantize(SCALE, rounding=ROUND_CEILING)


def web_fee(pricing, calls):
    """Frozen per-request tool reservation; zero preserves older dispatch records."""
    if type(calls) is not int or calls not in (0, 1):
        raise ValueError("Invalid native search limit")
    if not calls:
        return Decimal(0)
    web = pricing.get("native_web", {})
    if web != {"tool": "web_search", "max_calls": 1,
               "supplier_usd_per_call": "0.01", "max_model_rounds": 2}:
        raise ValueError("Unreviewed native web reservation")
    return amount(web["supplier_usd_per_call"]) * calls


def contribution(multiplier, *, topup_usd=20, supplier_fee="0.055", card_fee="0.029", fixed_fee="0.30"):
    """Scenario, not net profit: excludes tax, hosting, fraud, promos and game economics."""
    markup, topup = amount(multiplier), amount(topup_usd)
    if markup < 1 or topup <= 0:
        raise ValueError("Invalid pricing scenario")
    revenue = topup
    supplier = revenue / markup * (1 + amount(supplier_fee))
    processing = revenue * amount(card_fee) + amount(fixed_fee)
    return {"revenue_usd": str(revenue), "supplier_with_funding_fee_usd": str(supplier),
            "card_fee_usd": str(processing), "contribution_usd": str(revenue - supplier - processing),
            "contribution_margin": str((revenue - supplier - processing) / revenue)}
