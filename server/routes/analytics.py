"""Public configuration and a bounded, anonymous activity allowlist.

Activity is observational only: never a source of wallet or referral eligibility.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

router = APIRouter(prefix="/analytics", tags=["analytics"])

INGESTION_HOSTS = frozenset({"https://us.i.posthog.com", "https://eu.i.posthog.com"})
PUBLIC_PAGES = (
    "/ui", "/ui/", "/ui/index.html",
    "/ui/how-it-works", "/ui/how-it-works.html",
    "/ui/pricing", "/ui/pricing.html",
    "/ui/faq", "/ui/faq.html",
    "/ui/how-to-play", "/ui/how-to-play.html",
)
PROJECT_TOKEN = re.compile(r"phc_[A-Za-z0-9]{20,128}\Z", re.ASCII)


class Activity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event: Literal["product_page_view", "product_action", "cli_command_started", "cli_command_finished"]
    surface: Literal["web", "cli"]
    feature: Literal["home", "how_it_works", "pricing", "faq", "how_to_play", "emotes", "tv",
                     "codex", "claude", "chat", "games", "integration"]
    action: Literal["view", "preview", "select", "play", "pause", "start", "finish"]
    outcome: Literal["none", "ok", "error", "cancelled"]
    session_id: str
    consent: StrictBool


_activity_windows: OrderedDict[str, tuple[float, int]] = OrderedDict()
_activity_global: tuple[float, int] = (0.0, 0)
_activity_inflight = 0


def _admit_activity(session: str) -> bool:
    """Per-worker memory and traffic bounds, not an abuse/authentication boundary."""
    global _activity_global
    now = time.monotonic()
    started, count = _activity_global
    if now - started >= 60:
        started, count = now, 0
    if count >= 120:
        return False
    window, used = _activity_windows.pop(session, (now, 0))
    if now - window >= 60:
        window, used = now, 0
    _activity_windows[session] = (window, used + 1)
    while len(_activity_windows) > 512:
        _activity_windows.popitem(last=False)
    if used >= 30:
        return False
    _activity_global = (started, count + 1)
    return True


def _activity_environment(request: Request) -> str:
    # Unknown deployments must not silently masquerade as production analytics.
    value = os.getenv("PUBLIC_POSTHOG_ENVIRONMENT", "")
    if value in {"local", "staging", "production", "test"}:
        return value
    return "local" if request.url.hostname in {"localhost", "127.0.0.1", "::1"} else "unclassified"


async def _send_activity(host: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.0, follow_redirects=False, trust_env=False) as client:
            async with asyncio.timeout(1.5):
                # Never forward request headers, cookies, IPs, user agents or auth.
                response = await client.post(host + "/i/v0/e", json=payload)
        return response.is_success
    except (httpx.HTTPError, TimeoutError):
        return False


@router.post("/activity")
async def activity(request: Request) -> Response:
    global _activity_inflight
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    token = os.getenv("PUBLIC_POSTHOG_PROJECT_TOKEN", "")
    host = os.getenv("PUBLIC_POSTHOG_HOST", "https://us.i.posthog.com")
    if not PROJECT_TOKEN.fullmatch(token) or host not in INGESTION_HOSTS:
        return Response(status_code=204, headers=headers)
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        return Response(status_code=415, headers=headers)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        return Response(status_code=403, headers=headers)
    body = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024:
                    return Response(status_code=413, headers=headers)
                body.extend(chunk)
    except TimeoutError:
        return Response(status_code=408, headers=headers)
    try:
        event = Activity.model_validate_json(bytes(body))
        session = UUID(event.session_id)
        if str(session) != event.session_id or session.version != 4 or not event.consent:
            raise ValueError("Invalid anonymous consent")
        cli = event.surface == "cli"
        if cli:
            valid = (event.feature in {"codex", "claude", "chat", "games", "integration", "emotes", "tv"}
                     and ((event.event == "cli_command_started" and event.action == "start" and event.outcome == "none")
                          or (event.event == "cli_command_finished" and event.action == "finish" and event.outcome != "none")))
        else:
            valid = (event.feature in {"home", "how_it_works", "pricing", "faq", "how_to_play", "emotes", "tv"}
                     and event.outcome == "none"
                     and ((event.event == "product_page_view" and event.action == "view")
                          or (event.event == "product_action" and event.action in {"preview", "select", "play", "pause"})))
        if not valid:
            raise ValueError("Invalid event combination")
    except (ValueError, ValidationError):
        # Pydantic errors echo inputs; never log or return those to analytics.
        return Response(status_code=400, headers=headers)
    if _activity_inflight >= 4 or not _admit_activity(str(session)):
        return Response(status_code=429, headers=headers)
    properties = {name: getattr(event, name) for name in ("surface", "feature", "action", "outcome")}
    properties.update({"environment": _activity_environment(request), "$process_person_profile": False,
                       "$geoip_disable": True, "$lib": "openvegas-activity", "schema_version": 1})
    _activity_inflight += 1
    try:
        accepted = await _send_activity(host, {"api_key": token, "distinct_id": str(session),
                                             "event": event.event, "properties": properties})
    finally:
        _activity_inflight -= 1
    return Response(status_code=202 if accepted else 503, headers=headers)


@router.get("/config")
async def analytics_config(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Deliberately no fallback to POSTHOG_API_KEY, personal keys, or generic secrets.
    token = os.getenv("PUBLIC_POSTHOG_PROJECT_TOKEN", "")
    host = os.getenv("PUBLIC_POSTHOG_HOST", "https://us.i.posthog.com")
    if not PROJECT_TOKEN.fullmatch(token) or host not in INGESTION_HOSTS:
        return {"enabled": False}
    return {
        "enabled": True,
        "project_token": token,
        "api_host": host,
        "replay_enabled": os.getenv("PUBLIC_POSTHOG_REPLAY_ENABLED", "0") == "1",
        "public_pages": list(PUBLIC_PAGES),
    }
