"""Status-only legacy profile identity; no rendering, persistence or entitlements."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

PROFILE_TIMEOUT_SECONDS = 2.0

# Reviewed legacy registry mirrored from avatar-manifest.json. Keep it packaged:
# installed/frozen clients must not discover a manifest in an arbitrary cwd.
AVATARS = {
    "ov_user_01": ("Classic Player", frozenset({"default", "warm", "cool"})),
    "ov_user_02": ("High Roller", frozenset({"default", "neon", "mono"})),
}
DEALERS = {
    "ov_dealer_female_tux_v1": "Victoria - Classic Tuxedo",
    "ov_dealer_female_tux_blonde_v1": "Victoria - Blonde",
}
PALETTES = {
    "default": "House Standard", "warm": "Golden Hour", "cool": "Midnight Blue",
    "neon": "Neon Pulse", "mono": "Monochrome",
}


@dataclass(frozen=True)
class ProfileIdentity:
    avatar_id: str
    avatar_palette: str
    dealer_skin_id: str

    @classmethod
    def parse(cls, payload: object) -> ProfileIdentity:
        if type(payload) is not dict:
            raise ValueError("Invalid profile identity")
        values = [payload.get(key) for key in ("avatar_id", "avatar_palette", "dealer_skin_id")]
        if not all(type(value) is str and len(value) <= 64 for value in values):
            raise ValueError("Invalid profile identity")
        avatar, palette, dealer = values
        if avatar not in AVATARS or dealer not in DEALERS or palette not in AVATARS[avatar][1]:
            raise ValueError("Unsupported profile identity")
        return cls(avatar, palette, dealer)

    def status(self) -> str:
        return (
            "Saved profile identity (status only): "
            f"Avatar: {AVATARS[self.avatar_id][0]} ({self.avatar_id}); "
            f"Palette: {PALETTES[self.avatar_palette]}; "
            f"Dealer: {DEALERS[self.dealer_skin_id]} ({self.dealer_skin_id}). "
            "Rendering and emote equipment unchanged."
        )


async def profile_status(client: object) -> str:
    """Fresh call-local data only; never display a prior account's saved choice."""
    getter = getattr(client, "get_profile_preferences", None)
    if not callable(getter):
        return "Profile identity: unavailable (not supported by this client)."
    try:
        payload = await asyncio.wait_for(getter(), timeout=PROFILE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - Cosmetic failures stay neutral; cancellation propagates.
        status = getattr(exc, "status", None)
        if type(status) is int and status in {401, 403}:
            return "Profile identity: unavailable (sign in to read saved preferences)."
        if type(status) is int and status in {404, 405, 501}:
            return "Profile identity: unavailable (not supported by this backend)."
        return "Profile identity: unavailable (saved preferences could not be read)."
    try:
        return ProfileIdentity.parse(payload).status()
    except ValueError:
        return "Profile identity: unavailable (invalid or unsupported saved preferences)."
