"""Bounded signed bridge issuance and authenticated redemption; default off."""
import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.middleware.auth import get_current_user
from server.routes.ui_auth import _assert_same_origin
from server.services.dependencies import get_db, get_redis
from server.services.marketing_credits import MarketingCreditService, PromotionError, enabled, ready, verify_bridge


class PromotionRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                if self.path != "/promotions/config":
                    ready()
                async with asyncio.timeout(10):
                    if request.method == "POST":
                        if request.headers.get("content-type", "").split(";")[0] != "application/json":
                            raise HTTPException(415, "JSON required.")
                        if request.headers.get("origin"):
                            _assert_same_origin(request)
                        body = bytearray()
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > 2048:
                                raise HTTPException(413, "Request too large.")
                            body.extend(chunk)
                        request._body = bytes(body)
                    response = await original(request)
            except PromotionError as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status)
            except StarletteHTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except (RequestValidationError, ValidationError, ValueError):
                response = JSONResponse({"detail": "Invalid promotion request."}, status_code=422)
            except Exception:
                response = JSONResponse({"detail": "Promotion service unavailable; retry safely."}, status_code=503)
            response.headers.update({"Cache-Control": "private, no-store", "Pragma": "no-cache",
                                     "Referrer-Policy": "no-referrer", "Vary": "Authorization, Cookie"})
            return response
        return handler


router = APIRouter(prefix="/promotions", tags=["promotions"], route_class=PromotionRoute)


async def rate(identity, maximum):
    count = await get_redis().eval("""local n=redis.call('INCR',KEYS[1]);
        if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n""",
        1, "ov:promotions:" + identity)
    if not isinstance(count, int) or isinstance(count, bool):
        raise PromotionError(503, "Promotion limits unavailable.")
    if count > maximum:
        raise PromotionError(429, "Too many requests; try again later.")


class Redeem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^ovp_[a-f0-9]{64}$")


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: UUID
    event_id: str = Field(pattern=r"^[A-Za-z0-9:_.-]{1,200}$")
    recipient_id: str = Field(pattern=r"^[A-Za-z0-9:_.-]{1,200}$")


@router.get("/config")
async def config():
    return {"enabled": enabled(), "amount_v": "50.000000", "code_valid_days": 7}


@router.post("/bridge/issue")
async def issue(request: Request):
    body = await request.body()
    verify_bridge(body, request.headers.get("x-ov-timestamp"), request.headers.get("x-ov-signature"))
    await rate("bridge", 60)
    payload = Issue.model_validate(json.loads(body))
    return await MarketingCreditService(get_db()).issue(payload.campaign_id, payload.event_id, payload.recipient_id)


@router.post("/redeem")
async def redeem(payload: Redeem, user=Depends(get_current_user)):
    if user.get("account_type") != "human" or user.get("role") != "authenticated":
        raise PromotionError(403, "A human account is required.")
    identity = str(UUID(user["user_id"]))
    await rate("redeem:" + identity, 5)
    return await MarketingCreditService(get_db()).redeem(identity, payload.code)
