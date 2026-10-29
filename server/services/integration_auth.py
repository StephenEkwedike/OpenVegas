"""Revocable integration grants. Redis holds hashes, never bearer credentials.

Device approval is PKCE-bound, single use and expires in five minutes. These
Host grants cannot authorize inference, payments, wagers or credit issuance.
Device grants may explicitly approve a separate budgeted native-inference lease.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from fastapi import HTTPException

DEVICE_TTL = 30 * 24 * 3600
LEASE_TTL = 15 * 60
APPROVAL_TTL = 300
HOSTS = frozenset({"claude", "codex"})
PREFIX = "ov:integration:"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class IntegrationAuth:
    def __init__(self, redis):
        self.redis = redis

    async def rate(self, identity: str, maximum=30):
        key = PREFIX + "rate:" + digest(identity)
        n = await self.redis.eval(
            "local n=redis.call('INCR',KEYS[1]); "
            "if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", 1, key,
        )
        if n > maximum:
            raise HTTPException(429, "Too many requests; wait a minute.")

    async def start(self, code_challenge: str, *, purpose='integration'):
        if purpose not in {'integration','game'}:
            raise HTTPException(400,'Unsupported authorization purpose')
        device_code = secrets.token_urlsafe(32)
        user_code = secrets.token_hex(6).upper()
        key = PREFIX + "pending:" + digest(device_code)
        value = {"challenge": code_challenge, "status": "pending", "user_code": user_code, "purpose": purpose}
        await self.redis.set(key, json.dumps(value), ex=APPROVAL_TTL)
        await self.redis.set(PREFIX + "code:" + user_code, key, ex=APPROVAL_TTL)
        return {"device_code": device_code, "user_code": user_code,
                "expires_in": APPROVAL_TTL, "interval": 2}

    async def approve(self, code: str, user_id: str, approved: bool, *, purpose='integration'):
        key = await self.redis.get(PREFIX + "code:" + code)
        if not key:
            raise HTTPException(400, "Authorization expired or invalid.")
        raw = await self.redis.get(key)
        if not raw:
            raise HTTPException(400, "Authorization expired or invalid.")
        value = json.loads(raw)
        if value.get('purpose','integration') != purpose:
            raise HTTPException(400,'Authorization purpose mismatch')
        if value["status"] != "pending":
            raise HTTPException(409, "Authorization already decided.")
        value.update(status="approved" if approved else "denied", user_id=user_id)
        # Compare-and-set preserves expiration and prevents a second approver
        # from replacing the authenticated identity that the user authorized.
        ok = await self.redis.eval(
            "if redis.call('GET',KEYS[1]) == ARGV[1] then "
            "redis.call('SET',KEYS[1],ARGV[2],'KEEPTTL'); return 1 end; return 0",
            1, key, raw, json.dumps(value),
        )
        if not ok:
            raise HTTPException(409, "Authorization changed; try again.")

    async def exchange(self, device_code: str, verifier: str, *, purpose='integration'):
        key = PREFIX + "pending:" + digest(device_code)
        raw = await self.redis.get(key)
        if not raw:
            raise HTTPException(400, "Authorization expired or already used.")
        value = json.loads(raw)
        if value.get('purpose','integration') != purpose:
            raise HTTPException(400,'Authorization purpose mismatch')
        if not secrets.compare_digest(value["challenge"], challenge(verifier)):
            raise HTTPException(400, "Authorization proof is invalid.")
        if value["status"] == "pending":
            return {"status": "pending"}
        consumed = await self.redis.eval(
            "if redis.call('GET',KEYS[1]) == ARGV[1] then "
            "redis.call('DEL',KEYS[1]); return 1 end; return 0",
            1, key, raw,
        )
        if not consumed:
            raise HTTPException(400, "Authorization already used.")
        await self.redis.delete(PREFIX + "code:" + value["user_code"])
        if value["status"] != "approved":
            raise HTTPException(403, "Authorization declined.")
        result = await self.enroll(value["user_id"])
        if purpose == 'game':
            game_token = 'ov_game_' + secrets.token_urlsafe(32)
            await self.redis.set(PREFIX+'token:'+digest(game_token),json.dumps({
                'user_id':value['user_id'],'kind':'game','parent':digest(result['token']),
            }),ex=86400)
            result.update(game_token=game_token,game_expires_in=86400)
        return result

    async def enroll(self, user_id: str):
        token = "ov_device_" + secrets.token_urlsafe(32)
        await self.redis.set(PREFIX + "token:" + digest(token), json.dumps({
            "user_id": user_id, "kind": "device",
        }), ex=DEVICE_TTL)
        return {"status": "approved", "token": token, "expires_in": DEVICE_TTL,
                "user_id": user_id, "scope": "integrations"}

    async def resolve(self, token: str):
        if not token.startswith(("ov_device_", "ov_host_", "ov_game_")) or len(token) > 128:
            raise HTTPException(401, "OpenVegas sign-in required.")
        raw = await self.redis.get(PREFIX + "token:" + digest(token))
        if not raw:
            raise HTTPException(401, "Integration credential expired or revoked.")
        value = json.loads(raw)
        if value.get("parent") and not await self.redis.exists(PREFIX + "token:" + value["parent"]):
            raise HTTPException(401, "Device authorization revoked.")
        return value

    async def lease(self, user_id: str, host: str, parent: str | None = None):
        if host not in HOSTS:
            raise HTTPException(422, "Unsupported host.")
        token = "ov_host_" + secrets.token_urlsafe(32)
        await self.redis.set(PREFIX + "token:" + digest(token), json.dumps({
            "user_id": user_id, "kind": "host", "host": host, "parent": parent,
        }), ex=LEASE_TTL)
        return {"token": token, "expires_in": LEASE_TTL, "host": host,
                "scope": ["wallet:read", "cosmetics:read", "cosmetics:equip", "activity:observe"]}

    async def revoke(self, token: str):
        await self.redis.delete(PREFIX + "token:" + digest(token))

    async def observe(self, user_id: str, host: str, event_id: str, phase: str):
        await self.rate("event:" + user_id, 120)
        key = PREFIX + "event:" + digest(user_id + ":" + host + ":" + event_id)
        fresh = await self.redis.set(key, phase, ex=86400, nx=True)
        return {"accepted": bool(fresh), "duplicate": not bool(fresh), "awarded_v": "0", "awarded_xp": 0}
