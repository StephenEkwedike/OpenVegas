"""Host-scoped private delivery; mount this router separately in server.main."""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from openvegas.store.service import (
    CosmeticUnavailable,
    EntitlementDenied,
    EntitlementExpired,
    _emote_release_configuration,
)
from server.routes.integrations import HostAccount, enabled
from server.routes.store import _PrivateEmoteRoute
from server.services.dependencies import get_store_service
from server.services.emote_delivery import EmoteDeliveryUnavailable, load_delivery_pack

router = APIRouter(prefix="/integrations/cosmetics", tags=["integrations"],
                   route_class=_PrivateEmoteRoute, dependencies=[Depends(enabled)])


@router.get("/owned")
async def owned(user: HostAccount):
    user_id = str(user["user_id"])
    return {**await get_store_service().list_owned(user_id), "account_id": user_id}


@router.get("/{item_id}/pack")
async def pack(item_id: Annotated[str, Path(min_length=1, max_length=96,
                                          pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")],
               user: HostAccount):
    if ".." in item_id:
        raise HTTPException(400, "Invalid cosmetic identifier.")
    try:
        async with get_store_service().delivery_asset(str(user["user_id"]), item_id) as asset:
            configuration = _emote_release_configuration()
            payload = await asyncio.to_thread(load_delivery_pack, item_id, asset)
        if _emote_release_configuration() != configuration:
            raise EmoteDeliveryUnavailable()
        return payload
    except EntitlementExpired:
        raise HTTPException(410, "Cosmetic entitlement expired.") from None
    except EntitlementDenied:
        raise HTTPException(403, "Cosmetic is not owned by this account.") from None
    except CosmeticUnavailable:
        raise HTTPException(409, "Cosmetic is not available.") from None
    except EmoteDeliveryUnavailable:
        raise HTTPException(503, "COSMETIC_DELIVERY_UNAVAILABLE") from None
