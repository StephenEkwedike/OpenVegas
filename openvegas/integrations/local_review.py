"""Explicit local artwork studies, never an entitlement or premium-cache fallback."""
from __future__ import annotations

import json

from openvegas.emotes.resources import PackRepository
from .cosmetics import MAX_RASTER_BYTES, raster_asset


def q50_snapshot():
    pack = PackRepository().load("q50-jewelry-motion")
    completion = raster_asset(pack, item_id="local-review-q50", slot="completion")
    idle_index = completion["reduced_motion_frame"]
    companion = {**completion, "slot": "companion", "frames": [completion["frames"][idle_index]],
                 "reduced_motion_frame": 0,
                 "clips": {name: {"frames": [0], "frame_ms": 1000, "duration_ms": 1000}
                           for name in ("idle", "waiting")}}
    for asset in (companion, completion):
        asset["access"] = "local_artwork_review"
    payload = {"schema_version": 1, "available": True, "account_id": "local-artwork-review",
               "ttl_ms": 30000, "review_id": "q50", "slots": {
                   "companion": companion, "completion": completion},
               "presentation": {"mode": "claude_raster", "native_verified": False},
               "notice": "Local Q50 artwork review; not owned, purchased or for sale. Billing unchanged."}
    if len(json.dumps(payload)) > MAX_RASTER_BYTES:
        raise ValueError("Local artwork review exceeds native payload limit")
    return payload
