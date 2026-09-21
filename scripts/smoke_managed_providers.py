"""One explicitly authorized, bounded OpenAI adapter check; never tops up or retries.

Other providers require operator credentials and a separate reviewed model budget.
No production DB, customer wallet or stored conversation is touched by this check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

MODEL = "gpt-4.1-nano"
MAX_INPUT = 1_047_576  # Entire documented context: deliberately conservative ceiling.
MAX_OUTPUT = 32
INPUT_USD_PER_M = Decimal("0.10")
OUTPUT_USD_PER_M = Decimal("0.40")
MAX_COST = (MAX_INPUT * INPUT_USD_PER_M + MAX_OUTPUT * OUTPUT_USD_PER_M) / 1_000_000
PRICE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-4.1-nano"
MESSAGES = [
    {"role": "user", "content": "Remember this test word: amber."},
    {"role": "assistant", "content": "I will remember amber."},
    {"role": "user", "content": "Reply with only the test word."},
]


async def smoke(api_key: str, *, transport=None) -> dict:
    import httpx

    from openvegas.gateway.inference import AIGateway, InferenceRequest

    class SingleRequest(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = transport or httpx.AsyncHTTPTransport(retries=0)
            self.calls = 0
            self.status = None

        async def handle_async_request(self, request):
            payload = json.loads(request.content)
            if (
                self.calls
                or request.method != "POST"
                or str(request.url) != "https://api.openai.com/v1/chat/completions"
                or payload
                != {"model": MODEL, "max_completion_tokens": MAX_OUTPUT, "messages": MESSAGES}
            ):
                raise ValueError("Unapproved request or retry blocked")
            self.calls += 1
            response = await self.inner.handle_async_request(request)
            self.status = response.status_code
            return response

        async def aclose(self):
            await self.inner.aclose()

    wire = SingleRequest()
    result = {
        "provider": "openai",
        "model": MODEL,
        "scope": "live adapter + role-preserving history, not full billing/CLI E2E",
        "reserved_upper_bound_usd": str(MAX_COST),
        "pricing_source": PRICE_SOURCE,
    }
    async with httpx.AsyncClient(
        transport=wire, trust_env=False, timeout=30, follow_redirects=False
    ) as client:
        gateway = AIGateway(None, None, None, http_client=client)
        request = InferenceRequest(
            account_id="user:isolated-provider-smoke",
            provider="openai",
            model=MODEL,
            messages=MESSAGES,
            max_tokens=MAX_OUTPUT,
            strict_continuity=True,
        )
        try:
            async with asyncio.timeout(40):
                response = await gateway._route_to_provider(request, api_key)
            counts = response.input_tokens, response.output_tokens
            if (
                any(type(n) is not int or n < 0 for n in counts)
                or counts[0] > MAX_INPUT
                or counts[1] > MAX_OUTPUT
            ):
                raise ValueError("Provider returned usage outside approved bounds")
            result.update(
                status="passed"
                if response.completion_status == "complete"
                and response.text.strip().lower() == "amber"
                else "failed_contract",
                input_tokens=counts[0],
                output_tokens=counts[1],
                cost_usd_at_reviewed_rates=str(
                    (counts[0] * INPUT_USD_PER_M + counts[1] * OUTPUT_USD_PER_M) / 1_000_000
                ),
                completion_status=response.completion_status,
                history_answer_correct=response.text.strip().lower() == "amber",
            )
        except Exception as exc:  # noqa: BLE001 - sanitize provider failures
            result.update(
                status="failed_or_uncertain", error_type=type(exc).__name__, no_retry=True
            )
    result["requests_sent"] = wire.calls
    result["http_status"] = wire.status
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, help="Explicit operator credential file, never printed"
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="New report only; preexisting attempts are never retried",
    )
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal(0))
    args = parser.parse_args(argv)
    if not args.budget_usd.is_finite() or not 0 <= args.budget_usd <= 1:
        parser.error("This check permits at most the approved US$1 budget")
    if not args.allow_paid:
        print(
            f"DRY RUN: one {MODEL} request, at most {MAX_OUTPUT} output tokens, conservative ceiling US${MAX_COST}. No call made."
        )
        return 0
    if args.budget_usd < MAX_COST:
        parser.error("Budget is below the conservative maximum; no request made")
    # Reading credentials does not export unrelated dotenv settings into the process.
    values = dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values

        values.update(dotenv_values(args.env_file))
    key = str(values.get("OPENAI_API_KEY") or "").strip()
    if not key:
        parser.error("Managed OpenAI credential unavailable; no request made")
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "budget_usd": str(args.budget_usd),
        "reserved_usd": str(MAX_COST),
        "status": "reserved_before_call",
        "authorization": "Owner authorized up to US$1 total, no top-ups or retries on 2026-09-21",
        "other_providers": {
            p: "credential_present_model_review_required"
            if values.get(k)
            else "blocked_missing_managed_credential"
            for p, k in (
                ("anthropic", "ANTHROPIC_API_KEY"),
                ("gemini", "GEMINI_API_KEY"),
                ("mistral", "MISTRAL_API_KEY"),
            )
        },
    }
    try:
        # A crash still leaves the reservation: never automatically spend again.
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            # Keep one descriptor; don't reopen a replaced report path.
            report["result"] = asyncio.run(smoke(key))
            report["status"] = "finished"
            stream.seek(0)
            json.dump(report, stream, indent=2)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        print("smoke: report unavailable/already exists; no automatic retry", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["result"]["status"] == "passed" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
