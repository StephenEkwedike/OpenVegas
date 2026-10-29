"""Shared server-owned exchange rate for billing and USD-priced store packs."""

import os
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

V_SCALE = Decimal("0.000001")


def v_per_usd() -> Decimal:
    """Normalize the rate once, before multiplication, on every money path."""
    try:
        rate = Decimal(os.getenv("V_PER_USD", "100")).quantize(
            V_SCALE, rounding=ROUND_HALF_EVEN
        )
        if rate.is_finite() and 0 < rate <= Decimal("999999999999.999999"):
            return rate
    except (InvalidOperation, ValueError):
        pass
    raise ValueError("Invalid V_PER_USD configuration")
