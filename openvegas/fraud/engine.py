"""Fraud / anti-abuse engine — velocity checks via Redis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any


class AbuseBlocked(Exception):
    pass


@dataclass
class AbuseThresholds:
    max_mints_per_hour: int = 10
    max_mints_per_day: int = 50
    max_mint_usd_per_day: float = 100.00
    max_bets_per_minute: int = 20
    max_bets_per_hour: int = 200
    max_infer_requests_per_minute: int = 30
    max_infer_v_per_hour: float = 500.00
    max_accounts_per_ip: int = 3
    suspicious_ip_cooldown: timedelta = timedelta(hours=1)


class FraudEngine:
    """Velocity checks + anomaly scoring. Phase 1 baseline."""

    def __init__(self, redis: Any, db: Any, thresholds: AbuseThresholds | None = None):
        self.redis = redis
        self.db = db
        self.thresholds = thresholds or AbuseThresholds()

    async def check_mint(self, user_id: str, amount_usd: float, ip: str) -> bool:
        t = self.thresholds

        # Mints per hour
        key_hour = f"fraud:mint:hour:{user_id}"
        count = await self.redis.incr(key_hour)
        if count == 1:
            await self.redis.expire(key_hour, 3600)
        if count > t.max_mints_per_hour:
            await self._flag(user_id, "velocity_breach", {"type": "mint_hourly", "count": count})
            raise AbuseBlocked("Mint rate limit exceeded (hourly)")

        # Mints per day
        key_day = f"fraud:mint:day:{user_id}"
        count_day = await self.redis.incr(key_day)
        if count_day == 1:
            await self.redis.expire(key_day, 86400)
        if count_day > t.max_mints_per_day:
            await self._flag(user_id, "velocity_breach", {"type": "mint_daily", "count": count_day})
            raise AbuseBlocked("Mint rate limit exceeded (daily)")

        # Daily USD cap
        key_usd = f"fraud:mint:usd:{user_id}"
        total = await self.redis.incrbyfloat(key_usd, amount_usd)
        if float(total) == amount_usd:
            await self.redis.expire(key_usd, 86400)
        if float(total) > t.max_mint_usd_per_day:
            await self._flag(user_id, "velocity_breach", {"type": "mint_usd_daily", "total": total})
            raise AbuseBlocked("Daily mint USD cap exceeded")

        # Multi-account per IP
        key_ip = f"fraud:ip:accounts:{ip}"
        await self.redis.sadd(key_ip, user_id)
        await self.redis.expire(key_ip, 86400)
        ip_accounts = await self.redis.scard(key_ip)
        if ip_accounts > t.max_accounts_per_ip:
            await self._flag(user_id, "anomaly_hold", {"type": "multi_account_ip", "ip": ip})
            raise AbuseBlocked("Suspicious multi-account activity")

        return True

    async def check_bet(self, user_id: str) -> bool:
        key = f"fraud:bet:min:{user_id}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, 60)
        if count > self.thresholds.max_bets_per_minute:
            raise AbuseBlocked("Bet rate limit exceeded")
        return True

    async def check_inference(self, user_id: str) -> bool:
        key = f"fraud:infer:min:{user_id}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, 60)
        if count > self.thresholds.max_infer_requests_per_minute:
            raise AbuseBlocked("Inference rate limit exceeded")
        return True

    async def _flag(self, user_id: str, event_type: str, details: dict):
        await self.db.execute(
            "INSERT INTO fraud_events (user_id, event_type, details) VALUES ($1, $2, $3)",
            user_id, event_type, json.dumps(details),
        )
