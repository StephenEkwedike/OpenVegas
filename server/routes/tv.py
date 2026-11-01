"""Default-off TV preview and private, review-only creator submissions."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from threading import Lock
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from server.middleware.admin import require_admin
from server.middleware.auth import get_current_user
from server.services.dependencies import get_billing_service, get_db, get_wallet
from server.services.tv_billing import (
    PAID_PROGRAMS,
    Consent,
    LeaseRequest,
    TVBilling,
    billing_enabled,
    require_billing,
)
from server.services.tv_payments import TVPayments, checkout_enabled
from server.services.tv_studio import IntakeError, Review, StudioIntake, Submission, validated_call

PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization",
    "X-Content-Type-Options": "nosniff",
}
MAX_INFLIGHT = 8
REQUESTS_PER_MINUTE = 240
VALIDATIONS_PER_MINUTE = 30
REQUEST_SECONDS = 12
_admission_lock = Lock()
_requests = deque()
_validations = deque()
_inflight = 0


def _admit(validation: bool):
    global _inflight
    now = time.monotonic()
    with _admission_lock:
        for history in (_requests, _validations):
            while history and history[0] <= now - 60:
                history.popleft()
        if (
            _inflight >= MAX_INFLIGHT
            or len(_requests) >= REQUESTS_PER_MINUTE
            or (validation and len(_validations) >= VALIDATIONS_PER_MINUTE)
        ):
            raise HTTPException(
                429, "TV request limit; retry in a minute.", headers={"Retry-After": "60"}
            )
        _requests.append(now)
        if validation:
            _validations.append(now)
        _inflight += 1


def _release():
    global _inflight
    with _admission_lock:
        _inflight -= 1


def public_enabled() -> bool:
    return os.getenv("OPENVEGAS_TV_ENABLED", "0") == "1"


async def website_features():
    # Only public visibility, never server settings, credentials or billing state.
    return JSONResponse({"ovtv": public_enabled()}, headers={"Cache-Control": "no-store"})


def enabled():
    if not public_enabled():
        raise HTTPException(404, "Page not found", headers=PRIVATE_HEADERS)


def intake_enabled():
    enabled()
    if os.getenv("OPENVEGAS_TV_STUDIO_ENABLED", "0") != "1":
        raise HTTPException(503, "Creator intake is closed. You can export a local draft.")


class BoundedTVRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        validation = self.path.endswith("/studio/validate")
        intake = "/studio/applications" in self.path or "/studio/review" in self.path

        async def handler(request: Request):
            admitted = False
            try:
                # Reject closed features and aggregate pressure before consuming any body.
                intake_enabled() if intake else enabled()
                # Cancellation remains possible after the sales switch is disabled.
                if (
                    ("/billing/" in self.path or "/playback/" in self.path)
                    and request.method != "DELETE"
                    and not self.path.endswith("/portal")
                ):
                    require_billing()
                _admit(validation)
                admitted = True
                limit = (
                    0
                    if request.method in {"GET", "HEAD"}
                    else 8192
                    if "/studio/review" in self.path
                    or "/billing/" in self.path
                    or "/playback/" in self.path
                    else 1_050_000
                )
                length = request.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise HTTPException(400, "Invalid Content-Length.")
                    if len(length) > 10 or int(length) > limit:
                        raise HTTPException(413, "TV request body is too large.")
                async with asyncio.timeout(REQUEST_SECONDS):
                    body = bytearray()
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > limit:
                            raise HTTPException(413, "TV request body is too large.")
                        body.extend(chunk)
                    request._body = bytes(body)
                    response = await original(request)
            except IntakeError as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
            except RequestValidationError:
                response = JSONResponse({"detail": "Invalid TV request fields."}, status_code=422)
            except Exception:  # noqa: BLE001 - Responses must not expose submitted work or upstream secrets.
                # Never echo submitted work, credentials, or database errors.
                response = JSONResponse(
                    {"detail": "TV service unavailable; no success is assumed."}, status_code=503
                )
            finally:
                if admitted:
                    _release()
            response.headers.update(PRIVATE_HEADERS)
            return response

        return handler


router = APIRouter(
    prefix="/tv", tags=["TV"], route_class=BoundedTVRoute, dependencies=[Depends(enabled)]
)
User = Annotated[dict, Depends(get_current_user)]
Admin = Annotated[dict, Depends(require_admin)]


@router.get("/catalog")
async def tv_catalog():
    from openvegas.tv.catalog import catalog

    value = catalog()
    return {
        **value,
        "billing": {
            **value["billing"],
            "enabled": billing_enabled() and bool(PAID_PROGRAMS),
            "checkout_enabled": billing_enabled() and checkout_enabled() and bool(PAID_PROGRAMS),
            "delivery_ready": False,
        },
        "studio_intake_enabled": os.getenv("OPENVEGAS_TV_STUDIO_ENABLED", "0") == "1",
    }


@router.get("/episodes/{episode_id}")
async def tv_episode(episode_id: str):
    from openvegas.tv.catalog import get_episode

    try:
        return get_episode(episode_id)
    except (ValueError, KeyError, FileNotFoundError):
        raise HTTPException(404, "Episode is not available.") from None


@router.post("/studio/validate")
async def validate_storyboard(payload: dict):
    from openvegas.tv.schema import validate_episode

    try:
        episode = await validated_call(validate_episode, payload)
    except (ValueError, TypeError):
        raise HTTPException(
            422, "Invalid animatic. Run ov tv studio validate for format details."
        ) from None
    return {"valid": True, "episode": episode, "published": False, "charged_v": "0"}


@router.post("/studio/applications", dependencies=[Depends(intake_enabled)])
async def submit(payload: Submission, user: User):
    return await StudioIntake(get_db()).submit(user["user_id"], payload)


@router.get("/studio/applications", dependencies=[Depends(intake_enabled)])
async def mine(user: User):
    return {"applications": await StudioIntake(get_db()).mine(user["user_id"])}


@router.get("/studio/applications/{submission_id}", dependencies=[Depends(intake_enabled)])
async def application_detail(submission_id: UUID, user: User):
    return await StudioIntake(get_db()).detail(submission_id, user_id=user["user_id"])


@router.get("/studio/review", dependencies=[Depends(intake_enabled)])
async def review_queue(_: Admin):
    return {"applications": await StudioIntake(get_db()).pending()}


@router.get("/studio/review/{submission_id}", dependencies=[Depends(intake_enabled)])
async def review_detail(submission_id: UUID, _: Admin):
    return await StudioIntake(get_db()).detail(submission_id, user_id=None)


@router.post("/studio/review/{submission_id}", dependencies=[Depends(intake_enabled)])
async def review(submission_id: UUID, payload: Review, admin: Admin):
    return await StudioIntake(get_db()).review(submission_id, admin["user_id"], payload)


def viewing_service():
    return TVBilling(get_db(), get_wallet())


@router.get("/billing/status")
async def viewing_status(user: User):
    return await viewing_service().status(user["user_id"])


@router.post("/billing/checkout")
async def viewing_checkout(payload: Consent, user: User):
    return await TVPayments(get_billing_service()).checkout(user["user_id"], payload)


@router.post("/billing/portal")
async def viewing_portal(payload: Consent, user: User):
    return await TVPayments(get_billing_service()).cancel_portal(user["user_id"], payload)


@router.post("/playback/leases")
async def viewing_lease(payload: LeaseRequest, user: User):
    return await viewing_service().create_lease(user["user_id"], payload)


@router.get("/playback/leases/{lease_id}")
async def viewing_lease_status(lease_id: UUID, user: User):
    return await viewing_service().authorize(user["user_id"], lease_id)


@router.delete("/playback/leases/{lease_id}")
async def viewing_stop(lease_id: UUID, user: User):
    return await viewing_service().stop(user["user_id"], lease_id)
