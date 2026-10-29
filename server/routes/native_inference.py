"""Opt-in native protocol forwarding; no substitute coding-agent loop."""
from __future__ import annotations

import json
import os
from contextlib import aclosing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field, StrictBool

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway.catalog import ModelDisabled, validate_catalog_entry
from openvegas.gateway.inference import InferenceResult
from openvegas.gateway.native_pricing import amount, quote
from openvegas.gateway.native_wire import (
    MAX_REQUEST,
    NativeGateway,
    NativeSupplierBalanceError,
    frame,
    prepare_request,
    reservation,
    validate_preflight,
)
from openvegas.gateway.providers import get_model_review
from openvegas.wallet.ledger import InsufficientBalance
from server.routes.integrations import Account, Input, bearer, enabled, service
from server.routes.store import _PrivateEmoteRoute
from server.services.dependencies import get_gateway, get_redis
from server.services.integration_auth import digest
from server.services.integration_billing import NativeBudget


async def billing_enabled():
    if os.getenv("OPENVEGAS_NATIVE_BILLING_ENABLED", "0").lower() not in {"1", "true"}:
        raise HTTPException(503, "$V-funded native inference is not enabled on this backend.")


router = APIRouter(prefix="/integrations/native", tags=["native-billing"], route_class=_PrivateEmoteRoute,
                   dependencies=[Depends(enabled), Depends(billing_enabled)])


def budget():
    return NativeBudget(get_redis())


def gateway():
    base = get_gateway()
    return NativeGateway(base.db, base.wallet, base.catalog)


class Selection(Input):
    host: Literal["claude", "codex"]
    model: str = Field(min_length=3, max_length=160, pattern=r"^[a-zA-Z0-9._/-]+$")
    max_tokens: int = Field(default=2048, ge=1, le=128000, strict=True)
    provider: Literal["openrouter", "openai", "anthropic"] = "openrouter"


class Consent(Selection):
    limit_v: str = Field(pattern=r"^\d{1,6}(\.\d{1,6})?$")
    confirmed: StrictBool
    pricing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


async def selection(req, user):
    if user.get("kind") != "device":
        raise HTTPException(403, "Use an enrolled device to approve $V spending.")
    # Claude Code expects Claude's protocol/model semantics. Responses is
    # scoped to OpenAI models until another family passes native-host review.
    prefix = "anthropic/" if req.host == "claude" else "openai/"
    if req.provider != "openrouter" and req.provider != ("anthropic" if req.host == "claude" else "openai"):
        raise HTTPException(422, "Provider does not match the native host.")
    if req.provider == "openrouter" and not req.model.startswith(prefix):
        raise HTTPException(422, "Choose a model native to the selected host.")
    try:
        model = await get_gateway().catalog.get_model(req.provider, req.model)
        validate_catalog_entry(req.provider, req.model, model)
        if req.host not in get_model_review(req.provider, req.model).get("native_hosts", []):
            raise HTTPException(503, "This exact model has not passed the native host route review. No spending was started.")
        if req.max_tokens > model["max_tokens"]:
            raise HTTPException(422, "Output limit exceeds the reviewed model limit.")
        selected = {"host": req.host, "model": req.model, "max_tokens": req.max_tokens,
                    "pricing": quote(model, req.provider)}
        if req.provider != "openrouter":
            selected["provider"] = req.provider
        return selected
    except (ModelDisabled, ContractError, ValueError):
        raise HTTPException(503, "Exact model access and pricing need review; no spending was started.") from None


@router.post("/quote")
async def price(req: Selection, user: Account):
    return await selection(req, user)


@router.get("/options")
async def options(host: Literal["claude", "codex"], user: Account):
    """Only server-configured, reviewed direct routes; listing spends nothing."""
    provider = "anthropic" if host == "claude" else "openai"
    rows = await get_gateway().catalog.list_descriptors(provider=provider)
    choices = []
    for row in rows:
        if row.get("available") is not True:
            continue
        try:
            choices.append(await selection(Selection(host=host, provider=provider,
                model=row["model_id"], max_tokens=min(2048, row["max_tokens"])), user))
        except (HTTPException, ValueError, TypeError, KeyError):
            continue
    from server.services.dependencies import get_wallet
    balance = await get_wallet().get_balance("user:" + user["user_id"])
    return {"host": host, "provider": provider, "balance_v": str(balance), "models": choices,
            "notice": "Direct API billing; no subscription quota transfer or automatic fallback."}


@router.post("/sessions")
async def start(req: Consent, request: Request, user: Account):
    if not req.confirmed or amount(req.limit_v) <= 0:
        raise HTTPException(400, "A positive spending ceiling and explicit consent are required.")
    selected = await selection(req, user)
    if req.pricing_fingerprint != selected["pricing"]["fingerprint"]:
        raise HTTPException(409, "Prices changed; review and approve a fresh quote.")
    await service().rate("native-session:" + user["user_id"], 10)
    return await budget().create(bearer(request), **selected, limit_v=req.limit_v)


@router.get("/session")
async def status(request: Request):
    value = await budget().resolve(bearer(request))
    return {k: value[k] for k in ("user_id", "host", "model", "budget_v", "used_or_reserved_v", "requests", "session_id")}


@router.delete("/session")
async def close(request: Request):
    return await budget().close(bearer(request))


def error_body(protocol, message):
    if protocol == "messages":
        return {"type": "error", "error": {"type": "api_error", "message": message}}
    return {"error": {"type": "server_error", "code": "openvegas_native_error", "message": message}}


def unique_pairs(pairs):
    data = {}
    for k, v in pairs:
        if k in data:
            raise ValueError("Duplicate JSON field")
        data[k] = v
    return data


async def run(request, protocol):
    from server.services.native_stream import admit
    slot = admit()
    if slot is None:
        return JSONResponse(error_body(protocol, 'Native inference is at capacity. No request was sent or credits reserved.'), status_code=503)
    try:
        return await _run(request, protocol, slot)
    finally:
        slot.release()


async def _run(request, protocol, slot):
    try:
        token = bearer(request)
        session = await budget().resolve(token)
        if protocol == "messages" and request.headers.get("anthropic-version", "2023-06-01") != "2023-06-01":
            raise HTTPException(400, "This Anthropic protocol version has not been reviewed.")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_REQUEST:
                raise HTTPException(413, "Native request too large.")
        body = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        prepared, key = prepare_request(session, body, protocol, beta=request.headers.get("anthropic-beta", ""))
        engine = gateway()
        # Reject unsupported work before reserving session allowance. Completed
        # replays remain available even after the current model review expires.
        replay = await engine._replay_native(prepared)
        if replay is None:
            if session["host"] not in get_model_review(prepared.provider, prepared.model).get("native_hosts", []):
                raise HTTPException(503, "Native host approval was withdrawn. No provider request was sent.")
            model = await engine.catalog.get_model(prepared.provider, prepared.model)
            validate_catalog_entry(prepared.provider, prepared.model, model)
            validate_preflight(prepared, model)
        newly_reserved = await budget().reserve(token, key, reservation(prepared))
        if not newly_reserved and replay is None:
            raise HTTPException(409, "This request is already pending or failed. No automatic retry was made.")
        prepared._host_budget = {"token_digest": digest(token), "request_key": key,
                                 "reserved_v": str(reservation(prepared))}
        iterator = engine.execute(prepared)
        # Obtain the first event before committing HTTP status so auth/wallet/
        # preflight failures are native errors rather than broken 200 streams.
        try:
            first = await anext(iterator)
        except BaseException:
            if newly_reserved and not prepared._host_dispatch_started and replay is None:
                # A concurrent duplicate cannot dispatch: only the reservation
                # owner enters execute above. Keep failed attempts counted.
                await budget().settle(token, key, 0)
            raise
    except HTTPException as exc:
        return JSONResponse(error_body(protocol, str(exc.detail)), status_code=exc.status_code,
                            headers={"x-should-retry": "false"})
    except (ValueError, TypeError, RecursionError):
        return JSONResponse(error_body(protocol, "Invalid native request."), status_code=400)
    except InsufficientBalance:
        return JSONResponse(error_body(protocol, "Insufficient $V for this request's reservation. No provider request was sent."), status_code=402)
    except ModelDisabled:
        return JSONResponse(error_body(protocol, "Selected model is unavailable. No provider request was sent."), status_code=503)
    except NativeSupplierBalanceError as exc:
        return JSONResponse(error_body(protocol, exc.detail), status_code=402,
                            headers={"x-should-retry": "false"})
    except ContractError as exc:
        # SDKs retry 409/5xx by default. A retained hold is not a transient
        # conflict: another attempt cannot resolve it and must not look busy.
        code = {APIErrorCode.INSUFFICIENT_BALANCE: 402, APIErrorCode.INVALID_TRANSITION: 400,
                APIErrorCode.HOLD_CONFLICT: 422, APIErrorCode.IDEMPOTENCY_CONFLICT: 422}.get(exc.code, 502)
        return JSONResponse(error_body(protocol, exc.detail), status_code=code,
                            headers={"x-should-retry": "false"})

    if prepared._host_wire.streaming:
        from server.services.native_stream import NativeStream
        stream = NativeStream(iterator, first, lambda cost: budget().settle(token, key, cost),
            frame("error", error_body(protocol, "Request was interrupted or could not be settled. Check wallet history; do not automatically retry.")))
        slot.attach(stream.task)
        return StreamingResponse(stream.output(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    async with aclosing(iterator):
        if not isinstance(first, InferenceResult):
            raise HTTPException(502, "Unexpected native response.")
        await budget().settle(token, key, first.v_cost)
        return JSONResponse(first._host_wire_response["body"])


@router.post("/v1/messages")
async def messages(request: Request):
    return await run(request, "messages")


@router.post("/v1/responses")
async def responses(request: Request):
    return await run(request, "responses")
