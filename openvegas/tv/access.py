"""Shared web/terminal TV pricing and authenticated access contract, not a player."""

from decimal import ROUND_UP, Decimal

from openvegas.payments.conversion import V_SCALE, v_per_usd

HOURLY_USD = Decimal("0.75")
MONTHLY_CENTS = 2000
LEASE_SECONDS = 60


def hourly_v() -> Decimal:
    return (HOURLY_USD * v_per_usd()).quantize(V_SCALE, rounding=ROUND_UP)


def segment_cost(seconds: int, rate: Decimal) -> Decimal:
    if type(seconds) is not int or not 0 <= seconds <= LEASE_SECONDS:
        raise ValueError("Delivery must be a bounded server-measured interval")
    if not rate.is_finite() or rate <= 0:
        raise ValueError("Invalid hourly rate")
    return (rate * seconds / 3600).quantize(V_SCALE, rounding=ROUND_UP)


class TVAccessClient:
    """Reuse OpenVegasClient identity; no alternate wallet or automatic spending."""

    def __init__(self, client):
        self.client = client

    async def status(self):
        return await self.client._request("GET", "/tv/billing/status")

    async def lease(
        self, *, episode_id, idempotency_key, mode, expected_hourly_v, max_spend_v, confirmed=False
    ):
        if confirmed is not True:
            raise ValueError("Explicit viewing-price and budget confirmation required")
        return await self.client._request(
            "POST",
            "/tv/playback/leases",
            json={
                "episode_id": episode_id,
                "idempotency_key": idempotency_key,
                "mode": mode,
                "expected_hourly_v": str(expected_hourly_v),
                "max_spend_v": str(max_spend_v),
                "confirmed": True,
            },
        )

    async def stop(self, lease_id):
        return await self.client._request("DELETE", f"/tv/playback/leases/{lease_id}")
