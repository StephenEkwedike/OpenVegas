"""Bounded, authenticated referral API. No endpoint accepts session completion."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.middleware.auth import get_current_user
from server.routes.ui_auth import _assert_same_origin
from server.services.dependencies import get_db, get_redis, get_store_service
from server.services.referrals import ReferralError, ReferralService, TICKET_SECONDS, digest, enabled, require_enabled

HEADERS = {"Cache-Control": "private, no-store", "Pragma": "no-cache",
           "Vary": "Authorization, Cookie", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff"}
COOKIE = "ov_referral_ticket"


class ReferralRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                if self.path != "/referrals/config":
                    require_enabled()
                async with asyncio.timeout(10):
                    if request.method == "POST":
                        if not request.headers.get("origin"):
                            raise HTTPException(403, "Same-origin browser request required.")
                        _assert_same_origin(request)
                        if request.headers.get("content-type", "").split(";")[0] != "application/json":
                            raise HTTPException(415, "JSON required.")
                        body = bytearray()
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > 1024:
                                raise HTTPException(413, "Referral request too large.")
                            body.extend(chunk)
                        request._body = bytes(body)
                    response = await original(request)
            except ReferralError as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status)
            except StarletteHTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except RequestValidationError:
                # Do not echo invitation tokens or other untrusted request fields.
                response = JSONResponse({"detail": "Invalid referral request."}, status_code=422)
            except TimeoutError:
                response = JSONResponse({"detail": "Referral service is busy; retry safely."}, status_code=503)
            except Exception:
                response = JSONResponse({"detail": "Referral service is unavailable."}, status_code=503)
            response.headers.update(HEADERS)
            return response

        return handler


router = APIRouter(prefix="/referrals", tags=["referrals"], route_class=ReferralRoute)
User = Annotated[dict, Depends(get_current_user)]


def service():
    return ReferralService(get_db(), get_store_service())


async def rate(key, maximum):
    # Existing Redis, shared across workers; a missing limiter fails closed.
    count = await get_redis().eval("""
        local n=redis.call('INCR',KEYS[1])
        if n==1 then redis.call('EXPIRE',KEYS[1],60) end
        return n
    """, 1, "ov:referrals:rate:" + key)
    if not isinstance(count, int):
        raise ReferralError(503, "Referral request limits are unavailable.")
    if count > maximum:
        raise ReferralError(429, "Too many referral requests. Retry in a minute.")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Visit(Input):
    code: str = Field(pattern=r"^[a-f0-9]{32}$")


class Claim(Input):
    kind: Literal["trial", "reward"]
    item_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
    idempotency_key: UUID


@router.get("/config")
async def config():
    return {"enabled": enabled(), "required_friends": 5, "trial_days": 7}


@router.post("/visit")
async def visit(payload: Visit, request: Request):
    await rate("visits", 300)
    await rate("visit:" + digest(request.client.host if request.client else "unknown"), 20)
    ticket = await service().visit(payload.code, request.cookies.get(COOKIE))
    response = JSONResponse({"invitation_saved": True, "attribution_window_seconds": TICKET_SECONDS})
    response.set_cookie(COOKIE, ticket, max_age=TICKET_SECONDS, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/referrals")
    return response


@router.post("/attribute")
async def attribute(payload: Input, request: Request, user: User):
    del payload
    await rate("account:" + str(user["user_id"]), 60)
    return await service().attribute(user["user_id"], request.cookies.get(COOKIE, ""))


@router.get("/me")
async def mine(user: User):
    await rate("account:" + str(user["user_id"]), 60)
    return await service().status(user["user_id"])


@router.post("/claim")
async def claim(payload: Claim, user: User):
    await rate("claim:" + str(user["user_id"]), 10)
    return await service().claim(user["user_id"], payload.kind, payload.item_id, payload.idempotency_key)
