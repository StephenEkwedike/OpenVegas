"""Read-only business facts and narrowly scoped Supabase MFA session operations."""

from __future__ import annotations

import asyncio
import base64
import math
import os
import re
import time
from collections import deque
from datetime import UTC, datetime
from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from openvegas.capacity import GUIDANCE
from openvegas.capacity import monitor as capacity_monitor
from openvegas.telemetry import get_dashboard_slices, get_ops_alerts, get_run_metrics_summary
from server.middleware.admin import (
    auth_request,
    get_admin_identity,
    require_admin,
    verify_admin_token,
)
from server.routes.ui_auth import _set_refresh_cookie
from server.services.dependencies import get_db

_requests = deque()
_inflight = 0
_snapshot_cache = None
_snapshot_at = 0.0
_snapshot_lock = asyncio.Lock()
_trend = deque(maxlen=60)
_process_started = time.monotonic()
_instance = os.urandom(8).hex()


class BoundedAdminRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            global _inflight
            now = time.monotonic()
            while _requests and _requests[0] < now - 60:
                _requests.popleft()
            if len(_requests) >= 240 or _inflight >= 8:
                return JSONResponse(
                    {"detail": "Console request limit"},
                    429,
                    headers={"Cache-Control": "no-store", "Retry-After": "60"},
                )
            _requests.append(now)
            _inflight += 1
            try:
                async with asyncio.timeout(20):
                    size = 0
                    chunks = []
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > 4096:
                            raise HTTPException(413, "Request body too large")
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    response = await handler(request)
            except RequestValidationError:
                # Pydantic errors otherwise echo submitted OTPs/questions.
                response = JSONResponse({"detail": "Invalid request fields"}, 422)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, exc.status_code)
            except TimeoutError:
                response = JSONResponse({"detail": "Console request timed out"}, 503)
            except Exception:  # noqa: BLE001 - Never expose upstream errors or secrets.
                response = JSONResponse({"detail": "Console temporarily unavailable"}, 503)
            finally:
                _inflight -= 1
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "X-Content-Type-Options": "nosniff",
                }
            )
            return response

        return guarded


router = APIRouter(prefix="/business", tags=["business"], route_class=BoundedAdminRoute)
AdminIdentity = Annotated[dict, Depends(get_admin_identity)]
VerifiedAdmin = Annotated[dict, Depends(require_admin)]


def _token(request: Request) -> str:
    return request.headers["authorization"][7:]


async def _factors(request: Request, identity: dict) -> list:
    user = await auth_request("GET", "/user", token=_token(request))
    if user.get("id") != identity["user_id"]:
        raise HTTPException(401, "Session identity mismatch")
    factors = user.get("factors", [])
    if not isinstance(factors, list) or len(factors) > 50:
        raise HTTPException(503, "Factor list unavailable")
    return [f for f in factors if isinstance(f, dict)]


@router.get("/mfa")
async def mfa_state(request: Request, identity: AdminIdentity):
    factors = await _factors(request, identity)
    return {
        "aal": identity["aal"],
        "expires_at": identity["expires_at"],
        "factors": [
            {"id": str(UUID(f["id"])), "status": f["status"]}
            for f in factors
            if f.get("factor_type") == "totp" and f.get("status") in {"verified", "unverified"}
        ],
    }


@router.post("/mfa/enroll")
async def mfa_enroll(request: Request, identity: AdminIdentity):
    factors = await _factors(request, identity)
    if any(f.get("status") == "verified" for f in factors) and identity["aal"] != "aal2":
        raise HTTPException(403, "Verify your existing factor before enrolling another")
    for factor in factors:
        if factor.get("friendly_name") != "OpenVegas business":
            continue
        if factor.get("status") != "unverified" or factor.get("factor_type") != "totp":
            raise HTTPException(409, "Enrollment exists; use your existing authenticator")
        # A failed response can leave a factor whose QR was never displayed.
        # Only replace our own unfinished TOTP; never remove a verified factor.
        factor_id = str(UUID(factor["id"]))
        await auth_request("DELETE", f"/factors/{factor_id}", token=_token(request))
    body = await auth_request(
        "POST",
        "/factors",
        token=_token(request),
        data={"factor_type": "totp", "friendly_name": "OpenVegas business"},
    )
    uri = body.get("totp", {}).get("uri", "")
    if not isinstance(uri, str) or not uri.startswith("otpauth://totp/") or len(uri) > 1000:
        raise HTTPException(503, "Enrollment unavailable")
    # Render a PNG locally, rather than injecting upstream SVG markup into the DOM.
    output = BytesIO()
    qrcode.make(uri).save(output, format="PNG")
    return {
        "factor_id": str(UUID(body["id"])),
        "qr_png": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(),
    }


class FactorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor_id: UUID


class VerifyRequest(FactorRequest):
    challenge_id: UUID
    code: str = Field(pattern=r"^[0-9]{6}$")


async def _owned_factor(request: Request, identity: dict, factor_id: UUID):
    factors = await _factors(request, identity)
    if not any(f.get("id") == str(factor_id) and f.get("factor_type") == "totp" for f in factors):
        raise HTTPException(403, "TOTP factor unavailable")


@router.post("/mfa/challenge")
async def mfa_challenge(payload: FactorRequest, request: Request, identity: AdminIdentity):
    await _owned_factor(request, identity, payload.factor_id)
    body = await auth_request(
        "POST", f"/factors/{payload.factor_id}/challenge", token=_token(request), data={}
    )
    return {"challenge_id": str(UUID(body["id"]))}


@router.post("/mfa/verify")
async def mfa_verify(payload: VerifyRequest, request: Request, identity: AdminIdentity):
    await _owned_factor(request, identity, payload.factor_id)
    body = await auth_request(
        "POST",
        f"/factors/{payload.factor_id}/verify",
        token=_token(request),
        data={"challenge_id": str(payload.challenge_id), "code": payload.code},
    )
    access = body.get("access_token", "")
    upgraded = await verify_admin_token(access)
    if upgraded["user_id"] != identity["user_id"] or upgraded["aal"] != "aal2":
        raise HTTPException(403, "MFA session upgrade rejected")
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not 1 <= len(refresh) <= 4096:
        raise HTTPException(503, "MFA session upgrade unavailable")
    response = JSONResponse(
        {"access_token": access, "expires_at": upgraded["expires_at"], "aal": "aal2"}
    )
    # Replace the old AAL1 refresh cookie as well as the browser's access token.
    _set_refresh_cookie(response, refresh)
    return response


AGGREGATE_SQL = """
SELECT
  (SELECT count(*) FROM public.profiles) AS profile_count,
  count(*) AS paid_topup_count,
  COALESCE(sum(amount_usd), 0) AS paid_topup_usd,
  max(updated_at) AS latest_topup_update
FROM public.fiat_topups
WHERE status = 'paid' AND mode = 'stripe' AND currency = 'usd'
"""

FINANCE_NOTE = (
    "Recorded paid USD Stripe topups only; simulated, reversed and unpaid rows excluded. "
    "Cash collected proxy, not recognized revenue or profit. Stripe test/live provenance, "
    "fees, refunds, disputes, tax and subscription receipts are not reconciled here. "
    "Verify live cash in Stripe before financial decisions."
)
CAPACITY = GUIDANCE
ALERT_METRICS = {
    "turn_latency_ms_p95",
    "tool_fail_rate",
    "fallback_rate",
    "avg_cost_usd",
    "http_5xx_rate",
    "http_latency_ms_p95",
    "upload_failure_rate",
    "auth_refresh_failure_rate",
    "payment_failure_rate",
}


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _now():
    return datetime.now(UTC).isoformat()


async def _aggregate():
    try:
        async with asyncio.timeout(3):
            async with get_db().transaction() as connection:
                await connection.execute("SET TRANSACTION READ ONLY")
                await connection.execute("SET LOCAL statement_timeout = '2000ms'")
                row = await connection.fetchrow(AGGREGATE_SQL)
        if row is None:
            raise ValueError("no database")
        amount = _number(row["paid_topup_usd"])
        if amount is None:
            raise ValueError("invalid aggregate")
        return {
            "status": "available",
            "observed_at": _now(),
            "profile_count": int(row["profile_count"]),
            "paid_topup_count": int(row["paid_topup_count"]),
            "paid_topup_usd": str(row["paid_topup_usd"]),
            "latest_topup_update": row["latest_topup_update"].isoformat()
            if row["latest_topup_update"]
            else None,
            "source": "public.profiles + public.fiat_topups (all-time recorded rows)",
            "note": FINANCE_NOTE,
        }
    except Exception:  # noqa: BLE001 - Database errors can contain credentials or row values.
        return {
            "status": "unknown",
            "observed_at": None,
            "profile_count": None,
            "paid_topup_count": None,
            "paid_topup_usd": None,
            "latest_topup_update": None,
            "source": "Database aggregate unavailable",
            "note": FINANCE_NOTE,
        }


async def dashboard_snapshot():
    global _snapshot_cache, _snapshot_at
    async with _snapshot_lock:
        now = time.monotonic()
        if _snapshot_cache is not None and now - _snapshot_at < 15:
            return _snapshot_cache
        stamp = _now()
        ops = get_ops_alerts()
        summary = get_run_metrics_summary()
        slices = get_dashboard_slices()
        capacity = await asyncio.to_thread(capacity_monitor.snapshot)
        http = ops.get("http_summary", {})
        http_count = _number(http.get("http_request_count"))
        run_count = _number(summary.get("run_count"))
        runtime = {
            "observed_at": stamp,
            "scope": "process-local",
            "instance": _instance,
            "http_request_count": http_count,
            "http_latency_ms_p95": _number(http.get("http_latency_ms_p95")) if http_count else None,
            "http_5xx_rate": _number(http.get("http_5xx_rate")) if http_count else None,
            "run_count": run_count,
            "turn_latency_ms_p95": _number(summary.get("turn_latency_ms_p95"))
            if run_count
            else None,
            "patch_same_intent_failures": _number(
                slices.get("tool_apply_patch_same_intent_fail_total")
            ),
            "load_1m": capacity["resources"]["host_load_1m"],
            "cpu_count": os.cpu_count(),
            "console_uptime_seconds": round(now - _process_started),
            "note": "Process-local telemetry, not fleet totals. HTTP: last <=5000 requests; "
            "runs: last <=2000. Observation time is not event time. Host load is "
            "OS runnable-queue load, not CPU utilization or container quota.",
        }
        alerts = [
            {
                "metric": a["metric"],
                "severity": a["severity"],
                "observed": _number(a.get("observed")),
                "threshold": _number(a.get("threshold")),
                "status": "silenced" if a.get("status") == "silenced" else "fired",
            }
            for a in ops.get("alerts", [])[:20]
            if a.get("metric") in ALERT_METRICS and a.get("severity") in {"critical", "warning"}
        ]
        _trend.append({k: runtime[k] for k in ("observed_at", "http_latency_ms_p95", "load_1m")})
        _snapshot_cache = {
            "observed_at": stamp,
            "refresh_seconds": 30,
            "cache_seconds": 15,
            "runtime": runtime,
            "trend": list(_trend),
            "alerts": alerts,
            "capacity": capacity,
            "business": await _aggregate(),
            "capacity_guidance": CAPACITY,
            "unknowns": [
                "Recognized revenue",
                "Profit",
                "Fleet utilization",
                "Live Stripe cash reconciliation",
                "External service metrics",
            ],
        }
        _snapshot_at = time.monotonic()
        return _snapshot_cache


@router.get("/summary")
async def business_summary(_: VerifiedAdmin):
    return await dashboard_snapshot()


@router.get('/journeys')
async def business_journeys(_: VerifiedAdmin):
    from server.services.product_journeys import summary
    environment = os.getenv('PUBLIC_POSTHOG_ENVIRONMENT', 'unclassified')
    if environment not in {'local', 'test', 'staging', 'production'}:
        return {'status': 'unavailable', 'environment': 'unclassified'}
    return await summary(environment)


Topic = Literal["overview", "users", "cash", "latency", "load", "alerts", "capacity", "unknown"]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    topic: Topic = "overview"


@router.post("/questions")
async def business_question(payload: Question, _: VerifiedAdmin):
    words = set(re.findall(r"[a-z]+", payload.question.lower()))
    # A closed intent vocabulary, never a tool/SQL/URL/shell interpreter.
    forbidden = {
        "delete",
        "update",
        "insert",
        "drop",
        "execute",
        "shell",
        "refund",
        "deploy",
        "transfer",
        "charge",
        "email",
        "transcript",
        "password",
        "secret",
        "token",
    }
    topic = "unknown"
    if not words & forbidden:
        for candidate, vocabulary in (
            ("unknown", {"profit", "revenue", "forecast"}),
            ("capacity", {"scale", "scaling", "capacity", "cheapest"}),
            ("users", {"users", "profiles", "customers"}),
            ("cash", {"cash", "topups", "payments", "collected"}),
            ("latency", {"latency", "slow", "speed"}),
            ("load", {"load", "cpu", "traffic", "memory", "inflight"}),
            ("alerts", {"alerts", "errors", "health"}),
            ("overview", {"overview", "summary"}),
        ):
            if words & vocabulary:
                topic = candidate
                break
        else:
            if words and words <= {
                "and",
                "the",
                "what",
                "about",
                "that",
                "it",
                "more",
                "why",
                "details",
                "please",
                "explain",
                "source",
                "sources",
                "fresh",
                "how",
                "is",
            }:
                topic = payload.topic
    data = await dashboard_snapshot()
    business, runtime = data["business"], data["runtime"]
    capacity = data["capacity"]
    resources, window = capacity["resources"], capacity["http_window"]

    def value(v):
        return "unknown" if v is None else str(v)

    answers = {
        "overview": f"Profiles: {value(business['profile_count'])}. Recorded paid topups USD: "
        f"{value(business['paid_topup_usd'])}. {FINANCE_NOTE}",
        "users": f"Registered profiles: {value(business['profile_count'])}. This counts profiles, "
        "not all auth registrations, active users or paying customers.",
        "cash": f"Recorded paid USD topups: {value(business['paid_topup_usd'])} across "
        f"{value(business['paid_topup_count'])} topups. {FINANCE_NOTE}",
        "latency": f"HTTP five-minute window: {window['status']}; p95 headers: "
        f"{value(window['latency_ms_p95'] if window['status'] == 'available' else None)} ms. "
        f"Historical run p95: {value(runtime['turn_latency_ms_p95'])} ms. {runtime['note']}",
        "load": f"Host one-minute load: {value(runtime['load_1m'])}; process CPU cores used: "
        f"{value(resources['process_cpu_cores'])} over {value(resources['cpu_interval_seconds'])} seconds; "
        f"current process RSS bytes: {value(resources['process_rss_bytes'])}; inflight HTTP: "
        f"{value(capacity['inflight'])}, verified bound: {value(capacity['inflight_limit'])}. {capacity['note']}",
        "alerts": f"{len(data['alerts'])} retained-history alerts (including silenced); "
        f"{len(capacity['warnings'])} current capacity warnings; HTTP window: {window['status']}. "
        "No alerts is not evidence of fleet health. See the panels for thresholds and unknowns.",
        "capacity": f"Local capacity: {capacity['status']}; HTTP window: {window['status']}. "
        + " ".join(CAPACITY),
        "unknown": "Unknown or unsupported. This limited fact helper cannot execute actions, "
        "query customer records, browse links, or establish revenue/profit. "
        "Ask about profiles, cash, latency, load, alerts or capacity.",
    }
    return {
        "mode": "limited_fact_helper",
        "answer": answers[topic],
        "topic": topic,
        "sources": [
            {"name": business["source"], "observed_at": business["observed_at"]},
            {"name": "process-local telemetry", "observed_at": runtime["observed_at"]},
            {"name": "process-local capacity", "observed_at": capacity["observed_at"]},
        ],
        "followups": ["Show cash", "Show latency", "Explain capacity", "What is unknown?"],
    }
