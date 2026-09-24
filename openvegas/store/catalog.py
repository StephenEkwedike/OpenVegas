"""Redemption store catalog."""

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from openvegas.payments.conversion import v_per_usd

STORE_CATALOG = {
    "ai_starter": {
        "name": "Starter AI Pack",
        "description": "50k tokens on GPT-4o-mini or Gemini Flash",
        "cost_v": Decimal("5.00"),
        "type": "ai_pack",
        "tokens": 50_000,
        "models": ["gpt-4o-mini", "gemini-2.0-flash"],
    },
    "ai_pro": {
        "name": "Pro AI Pack",
        "description": "25k tokens on Claude Sonnet or GPT-4o",
        "cost_v": Decimal("20.00"),
        "type": "ai_pack",
        "tokens": 25_000,
        "models": ["claude-sonnet-4-20250514", "gpt-4o"],
    },
    "theme_cyberpunk": {
        "name": "Cyberpunk Terminal Theme",
        "description": "Neon colors + glitch effects",
        "cost_v": Decimal("15.00"),
        "type": "cosmetic",
    },
    "theme_retro": {
        "name": "Retro Arcade Theme",
        "description": "Green phosphor CRT look",
        "cost_v": Decimal("10.00"),
        "type": "cosmetic",
    },
    "victory_fireworks": {
        "name": "Win Animation: Fireworks",
        "description": "ASCII fireworks on every win",
        "cost_v": Decimal("8.00"),
        "type": "cosmetic",
    },
    "horse_skin_unicorn": {
        "name": "Unicorn Horse Skin",
        "description": "Your horse displays as a unicorn",
        "cost_v": Decimal("12.00"),
        "type": "cosmetic",
    },
    "tournament_pass": {
        "name": "Weekend Tournament Pass",
        "description": "Entry to the Saturday Night Horse Derby",
        "cost_v": Decimal("25.00"),
        "type": "tournament",
    },
}

# Historical prices are retained for order compatibility, not approval to sell.
# No cosmetic in the current catalog has verified art/delivery approval.
for _sku, _slot in {
    "theme_cyberpunk": "theme",
    "theme_retro": "theme",
    "victory_fireworks": "victory",
    "horse_skin_unicorn": "horse_skin",
}.items():
    STORE_CATALOG[_sku].update(
        slot=_slot,
        approval_status="pending",
        sale_enabled=False,
    )

# Artwork and US$5 one-time prices approved; native compatibility/delivery stay gated.
# These exact prototype sheets/manifests are intentionally public. Future paid
# pack bytes must live elsewhere, behind authenticated entitlement checks.
for _slug, _name in {
    "pixel-courier": "Pixel Courier",
    "beat-maker": "Beat Maker",
    "visor-explorer": "Visor Explorer",
}.items():
    _pack_id = f"openvegas.{_slug}"
    STORE_CATALOG[_pack_id] = {
        "name": _name,
        "description": f"{_name} companion preview. Native compatibility testing pending.",
        "type": "cosmetic",
        "slot": "companion",
        "cost_v": Decimal("500.00"),
        "price_usd": Decimal("5.00"),
        "approval_status": "concept_approved",
        "artwork_approved": True,
        "native_compatibility_verified": False,
        "sale_enabled": False,
        "asset": {
            "pack_id": _pack_id,
            "version": None,
            "preview_url": f"/ui/assets/emotes/{_slug}/sheet.png",
            "preview_manifest_url": f"/ui/assets/emotes/{_slug}/manifest.json",
            "thumbnail_url": None,
            "compatibility": [],
        },
    }

for _slug, _name, _description in (
    ("skyline-dunk", "Skyline Dunk", "Drive past a human defender, rise for the dunk and celebrate."),
    ("bicycle-finish", "Bicycle Finish", "An overhead goal past a human goalkeeper, then an arms-open celebration."),
    ("three-point-glow", "Three-Point Glow", "A three-point swish against a human opponent and a playful victory dance."),
):
    STORE_CATALOG[f"openvegas.{_slug}"] = {
        "name": _name, "description": _description + " Original 5.4-second completion preview; native compatibility testing pending.",
        "type": "cosmetic", "slot": "completion", "cost_v": Decimal("500.00"),
        "price_usd": Decimal("5.00"),
        "approval_status": "pending", "sale_enabled": False,
        "artwork_approved": True,
        "native_compatibility_verified": False,
        "asset": {
            "pack_id": f"openvegas.{_slug}", "version": None,
            "preview_url": f"/ui/assets/emotes/previews/{_slug}/sheet.png",
            "preview_manifest_url": f"/ui/assets/emotes/previews/{_slug}/manifest.json",
            "thumbnail_url": None, "compatibility": [],
        },
    }

COSMETIC_SLOTS = frozenset({"theme", "victory", "horse_skin", "companion", "completion"})
_PACK_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PREVIEW_PATH = re.compile(r"/ui/assets/emotes/previews/[A-Za-z0-9_./-]+\Z")
_CONCEPT_PREVIEW_PATH = re.compile(
    r"/ui/assets/emotes/(pixel-courier|beat-maker|visor-explorer)/(sheet\.png|manifest\.json)\Z"
)


def valid_pack_id(value: object) -> bool:
    return isinstance(value, str) and bool(_PACK_ID.fullmatch(value)) and ".." not in value


def cosmetic_asset(item: dict) -> dict | None:
    """Validate server-owned pack identity; pack metadata never supplies pricing."""
    asset = item.get("asset")
    if not isinstance(asset, dict) or not valid_pack_id(asset.get("pack_id")):
        return None
    version = asset.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version) or ".." in version:
        return None
    return asset


def cosmetic_price_v(item: dict) -> Decimal | None:
    """Resolve operator-owned USD pricing with the same conversion as top-ups."""
    try:
        if "price_usd" in item:
            usd = Decimal(str(item["price_usd"]))
            rate = v_per_usd()
            if not usd.is_finite() or not rate.is_finite() or usd <= 0 or rate <= 0:
                return None
            price = (usd * rate).quantize(Decimal("0.000001"))
        else:
            price = Decimal(str(item.get("cost_v")))
        if price.is_finite() and 0 <= price <= Decimal("999999999999.999999") and price == price.quantize(Decimal("0.000001")):
            return price
    except (InvalidOperation, ValueError):
        pass
    return None


def cosmetic_purchasable(item: dict) -> bool:
    if not (
        item.get("type") == "cosmetic"
        and item.get("approval_status") == "approved"
        and item.get("sale_enabled") is True
        and item.get("slot") in COSMETIC_SLOTS
        and cosmetic_asset(item) is not None
    ):
        return False
    if item.get("slot") in {"companion", "completion"}:
        release_flags = ("artwork_approved", "native_compatibility_verified")
        # Aggregate approval never substitutes for explicit native release checks.
        if any(item.get(name) is not True for name in release_flags):
            return False
    return cosmetic_price_v(item) is not None


def _safe_preview(value: object) -> str | None:
    if not isinstance(value, str) or not (
        _PREVIEW_PATH.fullmatch(value) or _CONCEPT_PREVIEW_PATH.fullmatch(value)
    ):
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    parsed = urlsplit(value)
    return (
        value if not (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment) else None
    )


def public_cosmetic(item_id: str, item: dict) -> dict:
    """Explicit allowlist: never expose private manifests, storage keys or licenses."""
    asset = item.get("asset")
    if not isinstance(asset, dict) or not valid_pack_id(asset.get("pack_id")):
        asset = {}
    purchasable = cosmetic_purchasable(item)
    price = cosmetic_price_v(item)
    compatibility = asset.get("compatibility", [])
    return {
        "item_id": item_id,
        "name": item.get("name", item_id),
        "description": item.get("description", ""),
        "type": "cosmetic",
        "slot": item.get("slot"),
        "availability": "available" if purchasable else "preview_only",
        "purchasable": purchasable,
        "cost_v": str(price) if purchasable else None,
        "planned_cost_v": str(price) if "price_usd" in item and price is not None else None,
        "planned_cost_usd": str(item["price_usd"]) if "price_usd" in item and price is not None else None,
        "asset": {
            "pack_id": asset.get("pack_id"),
            "version": asset.get("version") if cosmetic_asset(item) else None,
            "preview_url": _safe_preview(asset.get("preview_url")),
            "preview_manifest_url": _safe_preview(asset.get("preview_manifest_url")),
            "thumbnail_url": _safe_preview(asset.get("thumbnail_url")),
            "compatibility": [v for v in compatibility if isinstance(v, str)][:20]
            if isinstance(compatibility, list)
            else [],
        },
    }


def public_catalog(*, cosmetics_only: bool = False) -> dict:
    return {
        sku: public_cosmetic(sku, item) if item.get("type") == "cosmetic" else deepcopy(item)
        for sku, item in STORE_CATALOG.items()
        if not cosmetics_only or item.get("type") == "cosmetic"
    }
