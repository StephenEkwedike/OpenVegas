"""Separate consent purpose; never repurpose existing product analytics events."""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.middleware.auth import get_current_user
from server.routes.ui_auth import _assert_same_origin
from server.services.dependencies import get_db, get_redis
from server.services.data_contributions import (
    Contribution, ContributionError, ContributionService, NOTICE, NOTICE_VERSION, enabled,
)


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                async with asyncio.timeout(10):
                    if request.method == "POST":
                        if not request.headers.get("origin"):
                            raise HTTPException(403, "Same-origin request required.")
                        _assert_same_origin(request)
                        if request.headers.get("content-type", "").split(";")[0] != "application/json":
                            raise HTTPException(415, "JSON required.")
                        body = bytearray()
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > 2048:
                                raise HTTPException(413, "Metadata request too large.")
                            body.extend(chunk)
                        request._body = bytes(body)
                    response = await original(request)
            except ContributionError as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status)
            except StarletteHTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({"detail": "Invalid metadata request."}, status_code=422)
            except Exception:
                response = JSONResponse({"detail": "Contribution service unavailable."}, status_code=503)
            response.headers.update({"Cache-Control": "private, no-store", "Pragma": "no-cache",
                                     "Vary": "Authorization, Cookie, Sec-GPC, DNT",
                                     "X-Content-Type-Options": "nosniff"})
            return response
        return handle


router = APIRouter(prefix="/data-contributions", tags=["data-contributions"], route_class=PrivateRoute)
User = Annotated[dict, Depends(get_current_user)]


def service():
    return ContributionService(get_db())


def privacy_signal(request):
    return request.headers.get("sec-gpc") == "1" or request.headers.get("dnt") == "1"


async def limit(user):
    count = await get_redis().eval("""
        local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n
        """, 1, "ov:contributions:rate:" + str(user["user_id"]))
    if type(count) is not int:
        raise ContributionError(503, "Request limiter unavailable.")
    if count > 30:
        raise ContributionError(429, "Too many requests.")


class Preference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    opted_in: StrictBool
    notice_version: Literal["metadata-pilot-2026-10-20"] | None = None
    adult_and_personal_work_only: StrictBool = False


@router.get("/config")
async def config():
    return {"enabled": enabled(), "notice_version": NOTICE_VERSION, "notice": NOTICE,
            "content_collection": False, "sales_enabled": False, "enterprise_enabled": False}


@router.get("/me")
async def mine(request: Request, user: User):
    if privacy_signal(request):
        await service().preference(user["user_id"], False)
    await limit(user)
    return await service().status(user["user_id"])


@router.post("/preference")
async def preference(value: Preference, request: Request, user: User):
    # Withdrawal remains available when collection is disabled or the write limiter is down.
    if not value.opted_in or privacy_signal(request):
        result = await service().preference(user["user_id"], False)
        if value.opted_in:
            raise ContributionError(403, "Privacy signal prevents opt-in; pilot records removed.")
        return result
    await limit(user)
    if value.notice_version != NOTICE_VERSION or not value.adult_and_personal_work_only:
        raise ContributionError(400, "Read the current notice and confirm adult, personal-work participation.")
    return await service().preference(user["user_id"], True)


@router.post("/metadata")
async def metadata(value: Contribution, request: Request, user: User):
    if privacy_signal(request):
        await service().preference(user["user_id"], False)
        raise ContributionError(403, "Privacy signal prevents collection; pilot records removed.")
    await limit(user)
    return await service().submit(user["user_id"], value)
