"""Content-free, server-journaled direct-provider metering for crash recovery."""

from openvegas.gateway.native_direct import ENDPOINTS, count, metered_cost
from openvegas.gateway.native_pricing import amount


def usage_facts(provider, usage):
    facts = {key: count(usage, key) for key in ("input_tokens", "output_tokens")}
    if provider == "openai":
        facts["input_tokens_details"] = {
            "cached_tokens": count(usage.get("input_tokens_details") or {}, "cached_tokens", default=0)
        }
    else:
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            facts[key] = count(usage, key, default=0)
        facts["cache_creation"] = {
            key: count(usage.get("cache_creation") or {}, key, default=0)
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        }
    return facts


def journal(dispatch, provider_id, usage, response):
    """Called only after wire identity/completion validation, before settlement."""
    provider, pricing = dispatch["provider"], dispatch["pricing"]
    cost = metered_cost(provider, usage, pricing, response)
    facts = usage_facts(provider, usage)
    inputs = facts["input_tokens"]
    if provider == "anthropic":
        inputs += facts["cache_read_input_tokens"] + facts["cache_creation_input_tokens"]
    receipt = {
        "version": 1, "provider": provider, "id": provider_id,
        "model": dispatch["model"], "payload_hash": dispatch["payload_hash"],
        "pricing_fingerprint": pricing["fingerprint"], "usage": facts,
        "finish_reason": response.get("stop_reason") if provider == "anthropic" else response.get("status"),
        "native_tokens_prompt": inputs, "native_tokens_completion": facts["output_tokens"],
        "total_cost": str(cost),
    }
    return validate(receipt, dispatch, provider_id)


def validate(receipt, dispatch, provider_id):
    """Recompute charges from frozen prices; never accept operator-supplied costs."""
    provider, pricing = dispatch.get("provider"), dispatch["pricing"]
    keys = {"version", "provider", "id", "model", "payload_hash", "pricing_fingerprint",
            "usage", "finish_reason", "native_tokens_prompt", "native_tokens_completion", "total_cost"}
    if (type(receipt) is not dict or set(receipt) != keys
            or type(receipt["version"]) is not int or receipt["version"] != 1
            or provider not in ENDPOINTS or receipt["provider"] != provider
            or dispatch["protocol"] != ENDPOINTS[provider][0]
            or receipt["id"] != provider_id or receipt["model"] != dispatch["model"]
            or receipt["payload_hash"] != dispatch["payload_hash"]
            or receipt["pricing_fingerprint"] != pricing["fingerprint"]):
        raise ValueError("Invalid direct receipt binding")
    endings = ({"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"}
               if provider == "anthropic" else {"completed", "incomplete"})
    if receipt["finish_reason"] not in endings or type(receipt["usage"]) is not dict:
        raise ValueError("Unconfirmed direct receipt completion")
    facts = usage_facts(provider, receipt["usage"])
    if facts != receipt["usage"]:
        raise ValueError("Unexpected direct receipt fields")
    cost = metered_cost(provider, facts, pricing, {})
    inputs = facts["input_tokens"]
    if provider == "anthropic":
        inputs += facts["cache_read_input_tokens"] + facts["cache_creation_input_tokens"]
    for key, expected, bound in (
        ("native_tokens_prompt", inputs, "input_bound"),
        ("native_tokens_completion", facts["output_tokens"], "output_bound"),
    ):
        if type(receipt[key]) is not int or receipt[key] != expected or not 0 <= expected <= dispatch[bound]:
            raise ValueError("Direct receipt usage exceeds binding")
    if type(receipt["total_cost"]) is not str or amount(receipt["total_cost"]) != cost:
        raise ValueError("Direct receipt cost differs from frozen metering")
    return {**receipt, "usage": facts, "total_cost": str(cost)}
