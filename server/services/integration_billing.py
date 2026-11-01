"""Short-lived, model-bound spending consent. Wallet remains the ledger authority."""
from __future__ import annotations

import json
import secrets
from decimal import Decimal

from fastapi import HTTPException

from openvegas.gateway.native_pricing import amount
from server.services import integration_auth as auth

PREFIX = "ov:native-budget:"
TTL = 8 * 3600


def micros(value):
    value = amount(value) * 1_000_000
    if value != value.to_integral_value():
        raise ValueError("Use at most six decimal places")
    return int(value)


class NativeBudget:
    def __init__(self, redis):
        self.redis = redis

    async def create(self, device, *, host, model, max_tokens, limit_v, pricing, max_requests=1000, provider="openrouter"):
        if pricing.get("provider", "openrouter") != provider:
            raise ValueError("Native provider quote mismatch")
        parent = await auth.IntegrationAuth(self.redis).resolve(device)
        if parent["kind"] != "device":
            raise HTTPException(403, "Only an explicitly authorized device may start $V billing.")
        limit = micros(limit_v)
        if not 0 < limit <= 100_000_000_000 or host not in auth.HOSTS:
            raise HTTPException(422, "Invalid session spending limit or host.")
        if provider not in {"openrouter", "anthropic" if host == "claude" else "openai"}:
            raise HTTPException(422, "Provider does not match the native host.")
        token = "ov_native_" + secrets.token_urlsafe(32)
        sid = secrets.token_hex(16)
        value = {"user_id": parent["user_id"], "parent": auth.digest(device), "session_id": sid,
                 "host": host, "model": model, "max_tokens": max_tokens, "pricing": pricing}
        if provider != "openrouter":
            value["provider"] = provider
        key = PREFIX + auth.digest(token)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"identity": json.dumps(value), "limit": limit, "used": 0,
                                    "closed": 0, "count": 0, "max_requests": max_requests})
            pipe.expire(key, TTL)
            await pipe.execute()
        return {"token": token, "session_id": sid, "expires_in": TTL,
                "budget_v": str(Decimal(limit) / 1_000_000), "pricing": pricing}

    def key(self, token):
        if not token.startswith("ov_native_") or not 40 <= len(token) <= 128:
            raise HTTPException(401, "An approved $V session is required.")
        return PREFIX + auth.digest(token)

    async def resolve(self, token):
        data = await self.redis.hgetall(self.key(token))
        data = {(k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v) for k, v in data.items()}
        if not data or data["closed"] != "0":
            raise HTTPException(401, "$V session expired or closed. Start a new session with explicit consent.")
        identity = json.loads(data["identity"])
        if not await self.redis.exists(auth.PREFIX + "token:" + identity["parent"]):
            raise HTTPException(401, "Device access was revoked.")
        return {**identity, "budget_v": str(Decimal(data["limit"]) / 1_000_000),
                "used_or_reserved_v": str(Decimal(data["used"]) / 1_000_000), "requests": int(data["count"])}

    async def reserve(self, token, request_key, value):
        identity = await self.resolve(token)
        n = micros(value)
        # One atomic check/reservation across concurrent host subagents. Keep
        # failed/ambiguous attempts reserved; neither retry nor assume upstream
        # cancellation was free. A settled replay does not consume another slot.
        result = await self.redis.eval("""
            if redis.call('EXISTS',KEYS[2]) == 0 or redis.call('HGET',KEYS[1],'closed') ~= '0' then return -3 end
            local old=redis.call('HGET',KEYS[1],ARGV[1]); if old then return 0 end
            local used=tonumber(redis.call('HGET',KEYS[1],'used'))
            if used+tonumber(ARGV[2]) > tonumber(redis.call('HGET',KEYS[1],'limit')) then return -1 end
            if tonumber(redis.call('HGET',KEYS[1],'count')) >= tonumber(redis.call('HGET',KEYS[1],'max_requests')) then return -2 end
            redis.call('HINCRBY',KEYS[1],'used',ARGV[2]); redis.call('HINCRBY',KEYS[1],'count',1)
            redis.call('HSET',KEYS[1],ARGV[1],'r:'..ARGV[2]); return 1
        """, 2, self.key(token), auth.PREFIX + "token:" + identity["parent"], "req:" + request_key, n)
        if result < 0:
            raise HTTPException(402 if result in {-1, -2} else 401,
                                "$V session limit reached or access revoked. No provider request was sent.")
        return bool(result)

    async def settle(self, token, request_key, value):
        actual = micros(value)
        result = await self.redis.eval("""
            local old=redis.call('HGET',KEYS[1],ARGV[1]); if not old then return -1 end
            if string.sub(old,1,2)=='s:' then if old=='s:'..ARGV[2] then return 0 else return -2 end end
            local held=tonumber(string.sub(old,3)); local actual=tonumber(ARGV[2])
            if actual>held then return -2 end
            redis.call('HINCRBY',KEYS[1],'used',actual-held)
            redis.call('HSET',KEYS[1],ARGV[1],'s:'..ARGV[2]); return 1
        """, 1, self.key(token), "req:" + request_key, actual)
        if result < 0:
            raise HTTPException(409, "Wallet settled but session receipt requires reconciliation. Do not retry.")

    async def close(self, token):
        key = self.key(token)
        await self.redis.eval("if redis.call('EXISTS',KEYS[1])==1 then redis.call('HSET',KEYS[1],'closed',1) end; return 1", 1, key)
        return {"closed": True}
