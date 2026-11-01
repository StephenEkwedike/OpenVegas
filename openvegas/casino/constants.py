"""Shared casino constants/helpers for runtime and UI layers."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

OPENVEGAS_MIN_GAME_WAGER_ENV = "OPENVEGAS_MIN_GAME_WAGER_V"
DEFAULT_MIN_GAME_WAGER_V = Decimal("50")
MIN_WAGER_FLOOR_V = Decimal("0.01")
HIDDEN_CARD_TOKEN = "__HIDDEN_CARD__"


def min_game_wager_v() -> Decimal:
    raw = os.getenv(OPENVEGAS_MIN_GAME_WAGER_ENV, str(DEFAULT_MIN_GAME_WAGER_V))
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        value = DEFAULT_MIN_GAME_WAGER_V
    return max(MIN_WAGER_FLOOR_V, value)
