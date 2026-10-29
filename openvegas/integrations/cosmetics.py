"""Thin native presentation adapter for the existing private emote library.

Only the direct Claude helper returns raster bytes. MCP/Codex receive status,
not private frames or a claim that an embedded/native renderer is running.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
from PIL import Image

from openvegas.emotes.manifest import PackError, safe_token
from openvegas.emotes.remote import LEASE_SECONDS, RemoteError, RemoteLibrary
from openvegas.emotes.render import _cells, fit_frame
from openvegas.emotes.resources import Catalog, PackRepository
from openvegas.emotes.selection import SelectionStore

from .credentials import profile

MAX_RASTER_BYTES = 8 * 1024 * 1024
MAX_RASTER_FRAMES = 128
SLOTS = ("companion", "completion")


def presentation_status(host):
    return {
        "mode": "claude_raster" if host == "claude" else "text_only",
        "native_verified": False,
        "automatic_rewards": False,
        "completion_trigger": "host_answer_only" if host == "claude" else "unavailable",
        "message": (
            "Claude terminal Raster requires fresh ownership and a supported Mod; native UX is unverified."
            if host == "claude" else
            "Purchased/entitled cosmetics remain text controls in Codex. "
            "For in-session public preview animation, install with ov integration pets --install, "
            "then select an OpenVegas preview using /pets in a supported graphics terminal. "
            "The native pet cache is not used for private premium delivery. Native UX is unverified."
        ),
    }


class _HostAssets:
    def __init__(self, broker, host, identity):
        self.broker, self.host = broker, host
        self.backend_scope, self.account = identity

    def identity(self):
        return self.backend_scope, self.account

    async def owned(self):
        value = await self.broker.cosmetics_owned(self.host)
        if not isinstance(value, dict) or not isinstance(value.get("equipped"), dict):
            raise RemoteError("Invalid cosmetic selection response")
        rows = value.get("entitlements")
        if not isinstance(rows, list) or len(rows) > 256 or any(not isinstance(row, dict) for row in rows):
            raise RemoteError("Invalid cosmetic ownership response")
        # RemoteLibrary validates account/status/version and rechecks after download.
        # A native pane needs at most the two equipped packs, not the entire shop.
        selected = value["equipped"]
        return {**value, "entitlements": [row for row in rows if row.get("slot") in SLOTS
                and row.get("item_id") is not None
                and row.get("item_id") == selected.get(row.get("slot"))]}

    async def pack(self, item_id):
        return await self.broker.cosmetics_pack(self.host, item_id)


class _HostLibrary(RemoteLibrary):
    def text_slots(self, equipped):
        # Text-only refresh has no decoded resources to authorize; reuse the
        # validated entries/deadlines rather than the original response rows.
        with self._state_lock:
            if not self._fresh():
                raise RemoteError("Online ownership expired")
            now = self.clock()
            slots = dict.fromkeys(SLOTS)
            for pack_id, entry in self._entitlements.items():
                if (entry.slot in slots and entry.item_id == equipped.get(entry.slot)
                        and now < self._deadlines.get(pack_id, 0.0)):
                    slots[entry.slot] = {"item_id": entry.item_id, "pack_id": pack_id,
                                         "available_version": entry.version}
            return slots


def raster_asset(pack, *, item_id, slot):
    """Serialize authored frames and timing using the existing cell renderer."""
    if slot not in pack.manifest.tags:
        raise RemoteError("Cosmetic slot does not match the delivered pack")
    names = ("idle", "waiting") if slot == "companion" else ("complete",)
    indices = sorted({pack.manifest.reduced_motion_frame} | {
        index for name in names for index in pack.manifest.animations[name].frames})
    if len(indices) > MAX_RASTER_FRAMES:
        raise RemoteError("Cosmetic exceeds native animation limits")
    frames, size = [], 0
    for index in indices:
        fitted = fit_frame(pack.frame(index), max_columns=48, max_rows=24)
        # Enlarge the previous presentation, including small source canvases.
        # Uniform dimensions across frames preserve motion and pointer geometry.
        fitted = fitted.resize((round(fitted.width * 1.4), round(fitted.height * 1.4)),
                               Image.Resampling.NEAREST)
        numbers = []
        for glyph, fg, bg in _cells(fitted):
            if glyph != "\n":
                numbers.extend((ord(glyph), int(fg[1:], 16) if fg else 0x1000000,
                                int(bg[1:], 16) if bg else 0x1000000))
        encoded = base64.b64encode(struct.pack("<" + "I" * len(numbers), *numbers)).decode("ascii")
        size += len(encoded)
        if size > MAX_RASTER_BYTES:
            raise RemoteError("Cosmetic exceeds native rendering limits")
        frames.append(encoded)
    clips = {}
    for name in names:
        clip = pack.manifest.animations[name]
        clips[name] = {"frames": [indices.index(i) for i in clip.frames], "frame_ms": clip.frame_ms,
                       "duration_ms": clip.frame_ms * len(clip.frames)}
    return {"item_id": item_id, "pack_id": pack.manifest.pack_id, "version": pack.manifest.version,
            "display_name": pack.manifest.display_name, "slot": slot, "access": "entitlement_verified",
            "columns": fitted.width, "rows": (fitted.height + 1) // 2, "frames": frames,
            "clips": clips, "reduced_motion_frame": indices.index(pack.manifest.reduced_motion_frame)}


class NativeCosmetics:
    """One bounded online snapshot. The recipient must enforce its short TTL.

    Cache/selection live under the integration profile, never the public bundle
    or standalone companion preference. The existing cache cannot grant access.
    """
    def __init__(self, broker, host, *, root=None, clock=time.monotonic):
        if host not in {"claude", "codex"}:
            raise ValueError("Unsupported cosmetic host")
        self.broker, self.host, self.clock = broker, host, clock
        self.root = root if root is not None else profile() / "integrations" / "cosmetics"
        self.library = None

    def clear(self):
        if self.library:
            self.library.invalidate()

    def close(self):
        if self.library:
            self.library.close()

    async def snapshot(self):
        started = self.clock()
        self.clear()
        try:
            initial = await self.broker.cosmetics_owned(self.host)
            if not isinstance(initial, dict):
                raise RemoteError("Invalid cosmetic response")
            api = _HostAssets(self.broker, self.host, (self.broker.backend, initial.get("account_id")))
            scope = hashlib.sha256(json.dumps([api.backend_scope, api.account, self.host]).encode()).hexdigest()
            directory = self.root / scope
            self.close()
            self.library = _HostLibrary(api, directory / "cache", SelectionStore(directory / "selection"),
                                       clock=self.clock, operation_timeout=8, request_timeout=3)
            result = await (self.library.sync() if self.host == "claude" else self.library.refresh())
            catalog = self.library.catalog(Catalog())
            repository = self.library.repository(PackRepository())
            slots = (self.library.text_slots(result["owned"]["equipped"])
                     if self.host == "codex" else {})
            for slot in SLOTS:
                if self.host == "codex":
                    continue
                pack_id = result["selected_slots"].get(slot)
                if pack_id is None:
                    slots[slot] = None
                    continue
                safe_token(pack_id)
                if not self.library.authorize(pack_id):
                    raise RemoteError("Online ownership expired")
                item_id = result["owned"]["equipped"][slot]
                loaded = repository.load(catalog.resource_for(pack_id))
                slots[slot] = raster_asset(loaded, item_id=item_id, slot=slot)
            # Do not restart a lease after slow serialization. JS subtracts IPC time too.
            ttl = int(min(LEASE_SECONDS - (self.clock() - started),
                          self.library.remaining_lease_seconds()) * 1000)
            if not 0 < ttl <= LEASE_SECONDS * 1000 or any(
                    not self.library.authorize(pack_id) for pack_id in result["selected_slots"].values() if pack_id):
                raise RemoteError("Online ownership expired")
            payload = {"schema_version": 1, "available": True, "ttl_ms": ttl,
                       "account_id": initial["account_id"], "slots": slots,
                       "presentation": presentation_status(self.host)}
            if len(json.dumps(payload)) > MAX_RASTER_BYTES:
                raise RemoteError("Cosmetics exceed native rendering limits")
            return payload
        except BaseException:
            self.clear()
            raise


async def snapshot(broker, host):
    native = NativeCosmetics(broker, host)
    try:
        return await native.snapshot()
    except (PackError, ValueError):
        return {"available": False, "slots": {}, "ttl_ms": 0,
                "presentation": presentation_status(host), "message": "Owned cosmetics unavailable; art cleared."}
    finally:
        native.close()
