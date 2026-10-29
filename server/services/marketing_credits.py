"""Default-off campaign credits using the existing transactional wallet ledger."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from openvegas.wallet.ledger import LedgerEntry, WalletService

AMOUNT = Decimal("50.000000")
CODE = re.compile(r"^ovp_[a-f0-9]{64}$")


class PromotionError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def enabled():
    return os.getenv("OPENVEGAS_MARKETING_CREDITS_ENABLED", "0") == "1"


def key(name):
    value = os.getenv(name, "").encode()
    if len(value) < 32:
        raise PromotionError(503, "Promotion secrets are not configured.")
    return value


def ready():
    if not enabled():
        raise PromotionError(404, "Promotion is not active.")
    return key("OPENVEGAS_MARKETING_CREDIT_KEY")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_hash(secret, domain, value):
    return hmac.new(secret, (domain + ":" + value).encode(), hashlib.sha256).hexdigest()


def verify_bridge(body, timestamp, signature, *, now=None):
    """Our signed bridge contract, NOT an assertion of native ManyChat signatures."""
    ready()
    if (not re.fullmatch(r"[0-9]{10}", timestamp or "")
            or abs((time.time() if now is None else now) - int(timestamp)) > 300
            or not re.fullmatch(r"[a-f0-9]{64}", signature or "")):
        raise PromotionError(401, "Invalid promotion signature.")
    expected = hmac.new(key("OPENVEGAS_MARKETING_BRIDGE_KEY"),
                        timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PromotionError(401, "Invalid promotion signature.")


class MarketingCreditService:
    def __init__(self, db):
        self.db, self.wallet = db, WalletService(db)

    async def _campaign(self, tx, campaign):
        row = await tx.fetchrow("SELECT * FROM marketing_credit_campaigns WHERE id=$1 FOR UPDATE", campaign)
        now = await tx.fetchval("SELECT clock_timestamp()")
        if not row or not row["enabled"] or row["ends_at"] <= now:
            raise PromotionError(404, "Promotion is not active.")
        return row, now

    async def issue(self, campaign, event_id, recipient_id):
        secret = ready()
        campaign = UUID(str(campaign))
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9:_.-]{1,200}", v)
                   for v in (event_id, recipient_id)):
            raise PromotionError(422, "Invalid promotion event.")
        event = private_hash(secret, "event", event_id)
        recipient = private_hash(secret, "recipient", recipient_id)
        async with self.db.transaction() as tx:
            row, now = await self._campaign(tx, campaign)
            previous = await tx.fetchrow("""SELECT * FROM marketing_credit_codes
                WHERE campaign_id=$1 AND (event_hash=$2 OR recipient_hash=$3)""", campaign, event, recipient)
            if previous:
                if previous["recipient_hash"] != recipient:
                    raise PromotionError(409, "Event recipient mismatch.")
                code = "ovp_" + private_hash(secret, "code", str(previous["id"]))
                if digest(code) != previous["code_hash"]:
                    raise PromotionError(503, "Promotion key changed; operator review required.")
                if previous["expires_at"] <= now:
                    raise PromotionError(410, "Promotion code expired; it cannot be renewed.")
                return {"code": code, "expires_at": previous["expires_at"].isoformat(), "replayed": True}
            if row["reserved_v"] + AMOUNT > row["budget_v"]:
                raise PromotionError(409, "Promotion allocation exhausted.")
            identifier = uuid4()
            code = "ovp_" + private_hash(secret, "code", str(identifier))
            expires = min(now + timedelta(days=7), row["ends_at"])
            await tx.execute("""INSERT INTO marketing_credit_codes
                (id,campaign_id,event_hash,recipient_hash,code_hash,issued_at,expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)""", identifier, campaign, event, recipient, digest(code), now, expires)
            await tx.execute("UPDATE marketing_credit_campaigns SET reserved_v=reserved_v+$2 WHERE id=$1",
                             campaign, AMOUNT)
            return {"code": code, "expires_at": expires.isoformat(), "replayed": False}

    async def redeem(self, user, code):
        secret = ready()
        user = UUID(str(user))
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise PromotionError(400, "Invalid promotion code.")
        async with self.db.transaction() as tx:
            coupon = await tx.fetchrow("SELECT * FROM marketing_credit_codes WHERE code_hash=$1", digest(code))
            if not coupon:
                raise PromotionError(400, "Invalid promotion code.")
            # All campaign mutations share this lock, including issuance and redemption.
            campaign, now = await self._campaign(tx, coupon["campaign_id"])
            account = await tx.fetchrow("""SELECT email FROM auth.users WHERE id=$1
                AND email_confirmed_at IS NOT NULL AND nullif(btrim(email),'') IS NOT NULL
                AND is_anonymous IS NOT TRUE AND deleted_at IS NULL
                AND (banned_until IS NULL OR banned_until <= clock_timestamp()) FOR SHARE""", user)
            if not account:
                raise PromotionError(403, "A verified active account is required.")
            now = await tx.fetchval("SELECT clock_timestamp()")
            if campaign["ends_at"] <= now:
                raise PromotionError(404, "Promotion is not active.")
            identity = private_hash(secret, "email", account["email"].strip().casefold())
            prior = await tx.fetchrow("""SELECT * FROM marketing_credit_redemptions
                WHERE campaign_id=$1 AND (code_id=$2 OR user_id=$3 OR identity_hash=$4)""",
                coupon["campaign_id"], coupon["id"], user, identity)
            if prior:
                if prior["code_id"] == coupon["id"] and prior["user_id"] == user:
                    return {"credited_v": str(AMOUNT), "replayed": True}
                raise PromotionError(409, "Code or verified account has already participated.")
            if coupon["expires_at"] <= now:
                raise PromotionError(410, "Promotion code expired.")
            await tx.execute("""INSERT INTO marketing_credit_redemptions
                (code_id,campaign_id,user_id,identity_hash) VALUES ($1,$2,$3,$4)""",
                coupon["id"], coupon["campaign_id"], user, identity)
            await self.wallet._execute(LedgerEntry(
                debit_account="marketing_reserve", credit_account="user:" + str(user),
                amount=AMOUNT, entry_type="marketing_credit", reference_id="promotion:" + str(coupon["id"])
            ), tx=tx)
            return {"credited_v": str(AMOUNT), "replayed": False}
