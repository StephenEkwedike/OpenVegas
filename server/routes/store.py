"""Store routes — catalog, purchase settlement, and grant inspection."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from openvegas.store.service import (
    CosmeticUnavailable,
    EntitlementDenied,
    EntitlementExpired,
    IdempotencyConflict,
    StoreError,
    _emote_release_configuration,
)
from openvegas.wallet.ledger import InsufficientBalance
from server.middleware.auth import get_current_user
from server.services.dependencies import get_store_service
from server.services.emote_delivery import EmoteDeliveryUnavailable, load_delivery_pack

router = APIRouter(prefix="/store")
CurrentUser = Annotated[dict, Depends(get_current_user)]

PRIVATE_EMOTE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Vary": "Authorization",
    "X-Content-Type-Options": "nosniff",
}


class _PrivateEmoteRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                response = await handler(request)
            except StarletteHTTPException as exc:
                exc.headers = {**(exc.headers or {}), **PRIVATE_EMOTE_HEADERS}
                raise
            response.headers.update(PRIVATE_EMOTE_HEADERS)
            return response

        return private_handler


private_emotes = APIRouter(route_class=_PrivateEmoteRoute)


class StoreBuyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class StoreEquipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str | None = Field(default=None, min_length=1, max_length=32)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("/emotes/catalog")
async def cosmetic_catalog():
    catalog = await get_store_service().list_catalog(cosmetics_only=True)
    return {
        "items": [
            {
                "id": item["item_id"],
                "name": item["name"],
                "description": item["description"],
                "category": item["slot"],
                "cost_v": item["cost_v"],
                "planned_cost_v": item["planned_cost_v"],
                "planned_cost_usd": item["planned_cost_usd"],
                "purchasable": item["purchasable"],
                "preview_only": not item["purchasable"],
                **item["asset"],
            }
            for item in catalog.values()
            if item["slot"] in {"companion", "completion"}
        ]
    }


@router.get("/emotes/catalog/{item_id}/preview")
async def cosmetic_preview(item_id: str):
    try:
        return {"item": await get_store_service().preview(item_id)}
    except CosmeticUnavailable as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/list")
async def list_store(user: CurrentUser):
    del user
    svc = get_store_service()
    return {"items": await svc.list_catalog()}


@router.post("/buy")
async def buy_item(req: StoreBuyRequest, user: CurrentUser):
    svc = get_store_service()
    key = req.idempotency_key or f"cli-{uuid.uuid4().hex[:12]}"

    try:
        res = await svc.buy(user_id=user["user_id"], item_id=req.item_id, idempotency_key=key)
        return {
            "order_id": res.order_id,
            "status": res.status,
            "state": res.state,
            "item_id": res.item_id,
            "cost_v": str(res.cost_v),
            "grants": res.grants,
            "idempotency_key": key,
            "entitlement": res.entitlement,
            "already_owned": res.already_owned,
            "replayed": res.replayed,
        }
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InsufficientBalance as e:
        raise HTTPException(status_code=400, detail=str(e))
    except EntitlementExpired as e:
        raise HTTPException(status_code=410, detail=str(e)) from e
    except EntitlementDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except CosmeticUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except StoreError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/grants")
async def list_grants(user: CurrentUser):
    svc = get_store_service()
    grants = await svc.list_grants(user["user_id"])
    return {"grants": grants}


@private_emotes.get("/emotes/owned")
async def owned_cosmetics(user: CurrentUser):
    user_id = str(user["user_id"])
    return {**await get_store_service().list_owned(user_id), "account_id": user_id}


@private_emotes.get("/emotes/{item_id}/pack")
async def cosmetic_pack(item_id: str, user: CurrentUser):
    try:
        async with get_store_service().delivery_asset(str(user["user_id"]), item_id) as asset:
            configuration = _emote_release_configuration()
            payload = await asyncio.to_thread(load_delivery_pack, item_id, asset)
        if _emote_release_configuration() != configuration:
            raise EmoteDeliveryUnavailable()
        return payload
    except EntitlementExpired as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except EntitlementDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except CosmeticUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EmoteDeliveryUnavailable:
        raise HTTPException(status_code=503, detail="COSMETIC_DELIVERY_UNAVAILABLE") from None


@router.post("/emotes/equip")
async def equip_cosmetic(req: StoreEquipRequest, user: CurrentUser):
    try:
        return await get_store_service().equip(user["user_id"], req.slot, req.item_id)
    except EntitlementExpired as e:
        raise HTTPException(status_code=410, detail=str(e)) from e
    except EntitlementDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except CosmeticUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except StoreError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


router.include_router(private_emotes)
