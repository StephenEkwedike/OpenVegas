"""Read one known OpenRouter generation's metadata, never generate or reconcile.

Run: python -m scripts.inspect_openrouter_receipt --generation-id gen-... \
    --expected-model provider/model
Only OPENROUTER_API_KEY from the process environment is used; .env is not loaded.
One HTTPS GET, no redirects/retries. Exit 0 means a verified receipt snapshot;
exit 2 means unknown. Neither result authorizes redispatch or changes any funds.
Even a zero-cost receipt does not prove account reconciliation or final settlement.

API: https://openrouter.ai/docs/api/api-reference/generations/get-generation
Identity is exact, not alias-resolved. Costs are OpenRouter's total_cost/usage,
not upstream_inference_cost or a reconstructed token estimate. BYOK is excluded.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from decimal import Decimal

import httpx

ENDPOINT = "https://openrouter.ai/api/v1/generation"
MAX_BYTES = 65_536
TOTAL_TIMEOUT_SEC = 12.0
_TERMINAL_REASONS = {"stop", "length", "tool_calls", "content_filter", "error"}


def _report(reason: str, *, status: str = "unknown", cost: str | None = None) -> dict:
    return {
        "action": "inspect_only",
        "scope": "openrouter_generation_receipt_snapshot_only",
        "status": status,
        "reason": reason,
        "cost_usd": cost,
        "redispatch_authorized": False,
        "customer_funds_changed": False,
        "account_reconciled": False,
        "final_settlement_verified": False,
    }


def _number(token: str) -> Decimal:
    # Bound decimal expansion without rounding or passing through binary float.
    if len(token) > 128:
        raise ValueError
    value = Decimal(token)
    if not value.is_finite() or abs(value.as_tuple().exponent) > 100:
        raise ValueError
    return value


def _reject_constant(_token: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _classify(body: bytes, generation_id: str, expected_model: str) -> dict:
    try:
        payload = json.loads(
            body.decode("utf-8"), parse_float=_number, parse_int=_number,
            parse_constant=_reject_constant, object_pairs_hook=_unique_object,
        )
    except (ValueError, ArithmeticError, RecursionError):
        return _report("malformed_json")
    if not isinstance(payload, dict) or "error" in payload:
        return _report("malformed_receipt")
    data = payload.get("data")
    if not isinstance(data, dict):
        return _report("malformed_receipt")
    if not isinstance(data.get("id"), str) or not isinstance(data.get("model"), str):
        return _report("incomplete_identity")
    if data["id"] != generation_id or data["model"] != expected_model:
        return _report("mismatched_identity")
    if type(data.get("is_byok")) is not bool or type(data.get("cancelled")) is not bool:
        return _report("incomplete_billing_state")
    if data["is_byok"]:
        return _report("external_billing_not_supported")
    finish = data.get("finish_reason")
    if finish is not None and not isinstance(finish, str):
        return _report("malformed_finish_reason")
    if not data["cancelled"] and finish not in _TERMINAL_REASONS:
        return _report("incomplete_generation")
    for field in ("total_cost", "usage"):
        if data.get(field) is None:
            return _report("incomplete_cost")
        value = data[field]
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            return _report("invalid_cost")
    if data["total_cost"] != data["usage"]:
        return _report("inconsistent_cost")
    cost = data["total_cost"]
    formatted = format(cost, "f") if cost else "0"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return {
        **_report(
            "matched_managed_receipt",
            status="verified_billed" if cost > 0 else "verified_non_billed",
            cost=formatted,
        ),
        "cancelled": data["cancelled"],
    }


async def inspect_receipt(generation_id: str, expected_model: str) -> dict:
    if (
        not isinstance(generation_id, str) or len(generation_id) > 128
        or not re.fullmatch(r"gen-[0-9A-Za-z-]+", generation_id)
        or not isinstance(expected_model, str) or len(expected_model) > 256
        or not re.fullmatch(r"[A-Za-z0-9._:+-]+/[A-Za-z0-9._:+-]+", expected_model)
    ):
        return _report("invalid_expected_identity")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if (
        not key or len(key) > 1024 or not key.isascii() or not key.isprintable()
        or any(c.isspace() for c in key)
    ):
        return _report("missing_or_invalid_api_key")
    # Keep even supplied identities out of stdout; digests bind the operator input.
    binding = {
        "generation_id_sha256": hashlib.sha256(generation_id.encode()).hexdigest(),
        "expected_model_sha256": hashlib.sha256(expected_model.encode()).hexdigest(),
    }
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SEC):
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(5.0),
            ) as client:
                async with client.stream(
                    "GET", ENDPOINT, params={"id": generation_id},
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        reason = "not_found" if response.status_code == 404 else "http_error"
                        return {**_report(reason), **binding, "http_status": response.status_code}
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return {**_report("unsupported_encoding"), **binding}
                    if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                        return {**_report("unexpected_content_type"), **binding}
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > MAX_BYTES:
                            return {**_report("response_too_large"), **binding}
                        body.extend(chunk)
                    return {**_classify(bytes(body), generation_id, expected_model), **binding}
    except (TimeoutError, httpx.TimeoutException):
        return {**_report("timeout"), **binding}
    except httpx.HTTPError:
        return {**_report("network_error"), **binding}


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's normal error includes unknown arguments, possibly a secret.
        self.exit(2, json.dumps(_report("invalid_arguments"), sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--expected-model", required=True)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(inspect_receipt(args.generation_id, args.expected_model))
    except KeyboardInterrupt:
        report = _report("interrupted")
    except Exception:  # noqa: BLE001 - CLI boundary must redact unexpected driver errors.
        # Local TLS/config/driver exceptions may contain credentials or body text.
        report = _report("inspection_failed")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"].startswith("verified_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
