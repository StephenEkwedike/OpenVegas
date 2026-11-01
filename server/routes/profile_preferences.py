"""Profile preference routes for avatar/dealer/theme settings."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_db

router = APIRouter()

DEFAULT_AVATAR_ID = "ov_user_01"
DEFAULT_AVATAR_PALETTE = "default"
DEFAULT_DEALER_SKIN_ID = "ov_dealer_female_tux_v1"
DEFAULT_THEME = "light"
ALLOWED_THEMES = {"light", "dark"}


class ProfilePrefsPatch(BaseModel):
    avatar_id: str | None = None
    avatar_palette: str | None = None
    dealer_skin_id: str | None = None
    theme: str | None = None

    model_config = ConfigDict(extra="forbid")


def _manifest_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ui" / "assets" / "avatar-manifest.json"


@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, Any]:
    path = _manifest_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _allowed_ids() -> tuple[set[str], set[str], dict[str, set[str]]]:
    manifest = _load_manifest()
    dealer_entries = manifest.get("dealer") if isinstance(manifest, dict) else []
    user_entries = manifest.get("users") if isinstance(manifest, dict) else []

    dealer_ids: set[str] = {DEFAULT_DEALER_SKIN_ID}
    avatar_ids: set[str] = {DEFAULT_AVATAR_ID}
    palette_map: dict[str, set[str]] = {DEFAULT_AVATAR_ID: {DEFAULT_AVATAR_PALETTE}}

    if isinstance(dealer_entries, list):
        for item in dealer_entries:
            if not isinstance(item, dict):
                continue
            token = str(item.get("id") or "").strip()
            if token:
                dealer_ids.add(token)

    if isinstance(user_entries, list):
        for item in user_entries:
            if not isinstance(item, dict):
                continue
            avatar = str(item.get("id") or "").strip()
            if not avatar:
                continue
            avatar_ids.add(avatar)
            raw_palettes = item.get("palettes")
            palettes: set[str] = set()
            if isinstance(raw_palettes, list):
                for p in raw_palettes:
                    token = str(p or "").strip()
                    if token:
                        palettes.add(token)
            if not palettes:
                palettes = {DEFAULT_AVATAR_PALETTE}
            palette_map[avatar] = palettes

    return dealer_ids, avatar_ids, palette_map


def _row_value(row: Any, key: str, default: str) -> str:
    if row is None:
        return default
    if isinstance(row, dict):
        return str(row.get(key) or default)
    try:
        return str(row[key] or default)
    except Exception:
        return default


async def _ensure_profile_row(db: Any, user_id: str) -> None:
    username = f"user_{str(user_id).replace('-', '')[:16]}"
    display_name = f"User {str(user_id)[:8]}"
    await db.execute(
        """
        INSERT INTO profiles (id, username, display_name, avatar_id, avatar_palette, dealer_skin_id, theme)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (id) DO NOTHING
        """,
        user_id,
        username,
        display_name,
        DEFAULT_AVATAR_ID,
        DEFAULT_AVATAR_PALETTE,
        DEFAULT_DEALER_SKIN_ID,
        DEFAULT_THEME,
    )


async def _get_profile_row(db: Any, user_id: str) -> dict[str, str]:
    await _ensure_profile_row(db, user_id)
    row = await db.fetchrow(
        """
        SELECT avatar_id, avatar_palette, dealer_skin_id, theme
        FROM profiles
        WHERE id = $1
        """,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {
        "avatar_id": _row_value(row, "avatar_id", DEFAULT_AVATAR_ID),
        "avatar_palette": _row_value(row, "avatar_palette", DEFAULT_AVATAR_PALETTE),
        "dealer_skin_id": _row_value(row, "dealer_skin_id", DEFAULT_DEALER_SKIN_ID),
        "theme": _row_value(row, "theme", DEFAULT_THEME),
    }


@router.get("/ui/profile/preferences")
async def get_profile_preferences(user: dict = Depends(get_current_user)):
    db = get_db()
    return await _get_profile_row(db, str(user["user_id"]))


@router.patch("/ui/profile/preferences")
async def update_profile_preferences(req: ProfilePrefsPatch, user: dict = Depends(get_current_user)):
    if req.avatar_id is None and req.avatar_palette is None and req.dealer_skin_id is None and req.theme is None:
        emit_metric("avatar_preferences_update_total", {"outcome": "blocked", "reason": "empty_patch"})
        raise HTTPException(status_code=400, detail="No preference fields supplied")

    db = get_db()
    user_id = str(user["user_id"])
    current = await _get_profile_row(db, user_id)
    dealer_ids, avatar_ids, palette_map = _allowed_ids()

    next_avatar = str(req.avatar_id or current["avatar_id"]).strip()
    next_palette = str(req.avatar_palette or current["avatar_palette"]).strip()
    next_dealer = str(req.dealer_skin_id or current["dealer_skin_id"]).strip()
    next_theme = str(req.theme or current["theme"]).strip().lower()

    if next_avatar not in avatar_ids:
        emit_metric("avatar_preferences_update_total", {"outcome": "blocked", "reason": "invalid_avatar_id"})
        raise HTTPException(status_code=422, detail="Unsupported avatar_id")
    if next_dealer not in dealer_ids:
        emit_metric("avatar_preferences_update_total", {"outcome": "blocked", "reason": "invalid_dealer_skin_id"})
        raise HTTPException(status_code=422, detail="Unsupported dealer_skin_id")

    allowed_palettes = palette_map.get(next_avatar, {DEFAULT_AVATAR_PALETTE})
    if next_palette not in allowed_palettes:
        emit_metric("avatar_preferences_update_total", {"outcome": "blocked", "reason": "invalid_avatar_palette"})
        raise HTTPException(status_code=422, detail="Unsupported avatar_palette for avatar_id")
    if next_theme not in ALLOWED_THEMES:
        emit_metric("avatar_preferences_update_total", {"outcome": "blocked", "reason": "invalid_theme"})
        raise HTTPException(status_code=422, detail="Unsupported theme")

    row = await db.fetchrow(
        """
        UPDATE profiles
        SET avatar_id = $2,
            avatar_palette = $3,
            dealer_skin_id = $4,
            theme = $5,
            updated_at = now()
        WHERE id = $1
        RETURNING avatar_id, avatar_palette, dealer_skin_id, theme
        """,
        user_id,
        next_avatar,
        next_palette,
        next_dealer,
        next_theme,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")

    emit_metric("avatar_preferences_update_total", {"outcome": "success"})
    return {
        "avatar_id": _row_value(row, "avatar_id", DEFAULT_AVATAR_ID),
        "avatar_palette": _row_value(row, "avatar_palette", DEFAULT_AVATAR_PALETTE),
        "dealer_skin_id": _row_value(row, "dealer_skin_id", DEFAULT_DEALER_SKIN_ID),
        "theme": _row_value(row, "theme", DEFAULT_THEME),
    }
