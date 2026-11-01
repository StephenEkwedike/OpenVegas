"""Direct native protocols with reviewed token/cache prices, never client keys."""
from decimal import Decimal

from openvegas.gateway.native_pricing import amount
from openvegas.gateway.providers import get_model_review

ENDPOINTS = {
    "anthropic": ("messages", "https://api.anthropic.com/v1/messages"),
    "openai": ("responses", "https://api.openai.com/v1/responses"),
}


def review(provider, model, config):
    if provider not in ENDPOINTS:
        raise ValueError("Unreviewed direct native provider")
    value = get_model_review(provider, model)
    host = "claude" if provider == "anthropic" else "codex"
    if (value.get("account_access") is not True or host not in value.get("native_hosts", [])
            or value.get("native_transport") != ENDPOINTS[provider][0] + "_direct_v1"
            or type(value.get("context_window_tokens")) is not int
            or not 1 <= value["context_window_tokens"] <= 10_000_000
            or type(value.get('native_flat_price_context_tokens')) is not int
            or not 1 <= value['context_window_tokens'] <= value['native_flat_price_context_tokens'] <= 10_000_000):
        raise ValueError("Exact direct model needs native review")
    aliases = value.get('response_model_ids', [])
    if (type(aliases) is not list or len(aliases) > 2 or any(
            type(alias) is not str or not 1 <= len(alias) <= 160 for alias in aliases)):
        raise ValueError('Direct response identity requires review')
    rates = value.get("native_metering")
    if any(amount(value.get(field)) != amount(config[field]) for field in (
        "cost_input_per_1m", "cost_output_per_1m"
    )):
        raise ValueError("Direct catalog prices differ from review")
    keys = {"cached_input_usd_per_million"}
    if provider == "anthropic":
        keys |= {"cache_write_5m_usd_per_million", "cache_write_1h_usd_per_million"}
    if type(rates) is not dict or set(rates) != keys:
        raise ValueError("Direct cache pricing requires explicit review")
    normalized = {key: str(amount(rates[key])) for key in sorted(keys)}
    incoming = amount(config["cost_input_per_1m"])
    if amount(rates["cached_input_usd_per_million"]) > incoming:
        raise ValueError("Cached price exceeds reservation")
    if provider == "anthropic" and any(
        amount(rates[key]) > incoming * 2 for key in keys if key.startswith("cache_write")
    ):
        raise ValueError("Cache write price exceeds reservation")
    return normalized


def count(usage, key, *, default=None):
    value = usage.get(key, default)
    if type(value) is not int or not 0 <= value <= 100_000_000:
        raise ValueError("Missing or invalid direct usage count")
    return value


def metered_cost(provider, usage, pricing, response):
    if provider not in ENDPOINTS or pricing.get("provider") != provider:
        raise ValueError("Direct receipt provider mismatch")
    if usage.get("service_tier") not in (None, "standard", "default") or response.get(
        "service_tier"
    ) not in (None, "standard", "default"):
        raise ValueError("Unreviewed direct service tier")
    for key in ("server_tool_use", "server_tool_use_details"):
        hosted = usage.get(key)
        if hosted is not None and (type(hosted) is not dict or any(
            name not in {'web_search_requests', 'web_fetch_requests'} or type(n) is not int or n != 0
            for name, n in hosted.items()
        )):
            raise ValueError("Unreviewed hosted tool usage")
    if any(isinstance(item, dict) and item.get("type") not in (
        "message", "reasoning", "function_call", "custom_tool_call"
    ) for item in response.get("output", [])):
        raise ValueError("Unreviewed hosted output")
    inputs, outputs = count(usage, "input_tokens"), count(usage, "output_tokens")
    rates = pricing["direct_metering"]
    cost = Decimal(outputs) * amount(pricing["supplier_output_usd_per_million"])
    if provider == "openai":
        details = usage.get("input_tokens_details") or {}
        if type(details) is not dict:
            raise ValueError("Invalid direct cache details")
        # Newer models bill writes separately. Never silently price them as
        # ordinary input under an older cached-read-only review.
        if count(details, "cache_write_tokens", default=0):
            raise ValueError("Unreviewed direct cache write usage")
        writes = details.get("cache_write_tokens_details")
        if writes is not None and (
            type(writes) is not dict
            or any(name not in {"ttl_30m_tokens", "ttl_12h_tokens"}
                   or type(n) is not int or n != 0 for name, n in writes.items())
        ):
            raise ValueError("Unreviewed direct cache write duration")
        cached = count(details, "cached_tokens", default=0)
        if cached > inputs:
            raise ValueError("Cached count exceeds input usage")
        cost += (inputs - cached) * amount(pricing["supplier_input_usd_per_million"])
        cost += cached * amount(rates["cached_input_usd_per_million"])
    else:
        cached = count(usage, "cache_read_input_tokens", default=0)
        written = count(usage, "cache_creation_input_tokens", default=0)
        creation = usage.get("cache_creation") or {}
        if type(creation) is not dict:
            raise ValueError("Invalid cache creation breakdown")
        short = count(creation, "ephemeral_5m_input_tokens", default=0)
        long = count(creation, "ephemeral_1h_input_tokens", default=0)
        if short + long != written:
            raise ValueError("Cache creation duration is unverified")
        cost += inputs * amount(pricing["supplier_input_usd_per_million"])
        cost += cached * amount(rates["cached_input_usd_per_million"])
        cost += short * amount(rates["cache_write_5m_usd_per_million"])
        cost += long * amount(rates["cache_write_1h_usd_per_million"])
    return amount(cost / 1_000_000)


def transport(provider, protocol, key, beta):
    expected, endpoint = ENDPOINTS[provider]
    if protocol != expected:
        raise ValueError("Direct protocol mismatch")
    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        if beta:
            headers["anthropic-beta"] = beta
    else:
        headers["Authorization"] = "Bearer " + key
    return endpoint, headers
