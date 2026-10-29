"""Default-off TV access. Wallet holds settle only on trusted delivery receipts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from openvegas.tv.access import LEASE_SECONDS, hourly_v, segment_cost
from openvegas.wallet.ledger import InsufficientBalance
from server.services.tv_studio import IntakeError

# The concept/animatic is not paid inventory. Only a reviewed delivery integration
# may populate this registry with final approved programs and immutable revisions.
PAID_PROGRAMS: dict[str, str] = {}


class TVError(IntakeError):
    pass


def billing_enabled():
    return (
        os.getenv("OPENVEGAS_TV_ENABLED", "0") == "1"
        and os.getenv("OPENVEGAS_TV_BILLING_ENABLED", "0") == "1"
    )


def require_billing():
    if not billing_enabled():
        raise TVError(503, "TV billing is closed. The animatic remains free.")


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class LeaseRequest(Consent):
    episode_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    mode: Literal["payg", "monthly"]
    expected_hourly_v: Decimal = Field(gt=0, max_digits=24, decimal_places=6)
    max_spend_v: Decimal = Field(ge=0, max_digits=24, decimal_places=6)


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def user_lock(tx, user_id):
    await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "tv:" + str(user_id))


async def active_pass(tx, user_id, now):
    return await tx.fetchrow(
        "SELECT s.stripe_subscription_id, s.cancel_at_period_end, max(t.period_end) AS expires_at "
        "FROM tv_subscriptions s JOIN tv_paid_terms t ON t.stripe_subscription_id=s.stripe_subscription_id "
        "WHERE s.user_id=$1 AND s.status NOT IN ('canceled','blocked','unpaid','paused') "
        "AND t.revoked=false AND t.period_start <= $2 AND t.period_end > $2 "
        "GROUP BY s.stripe_subscription_id, s.cancel_at_period_end",
        UUID(str(user_id)),
        now,
    )


def lease_view(row):
    return {
        "id": str(row["id"]),
        "episode_id": row["episode_id"],
        "mode": row["mode"],
        "state": row["state"],
        "expires_at": row["expires_at"].isoformat(),
        "reserved_v": str(row["reserved_v"]),
        "charged_v": str(row["charged_v"]),
        "seconds": LEASE_SECONDS,
        "media_available": False,
    }


class TVBilling:
    def __init__(self, db, wallet, *, programs=None, clock=None):
        self.db, self.wallet = db, wallet
        self.programs = PAID_PROGRAMS if programs is None else programs
        self.clock = clock or (lambda: datetime.now(UTC))

    async def status(self, user_id):
        require_billing()
        await self.reap_expired(user_id=user_id)
        row = await active_pass(self.db, user_id, self.clock())
        return {
            "monthly_active": bool(row),
            "unlimited": bool(row),
            "expires_at": row["expires_at"].isoformat() if row else None,
            "cancel_at_period_end": bool(row and row["cancel_at_period_end"]),
            "hourly_usd": "0.75",
            "hourly_v": str(hourly_v()),
            "monthly_usd": "20.00",
            "paid_programs_available": bool(self.programs),
            "payg_automatic_fallback": False,
            "wallet_v": str(await self.wallet.get_balance("user:" + str(user_id))),
        }

    async def create_lease(self, user_id, request: LeaseRequest):
        require_billing()
        # Revalidate even callers that used model_construct/model_copy locally.
        request = LeaseRequest.model_validate(request.model_dump())
        revision = self.programs.get(request.episode_id)
        if not revision:
            raise TVError(409, "No final approved paid program is available. No credits reserved.")
        user = UUID(str(user_id))
        digest = fingerprint(request.model_dump(mode="json"))
        await self.reap_expired(user_id=user)
        async with self.db.transaction() as tx:
            await user_lock(tx, user)
            old = await tx.fetchrow(
                "SELECT * FROM tv_playback_leases WHERE user_id=$1 AND idempotency_key=$2",
                user,
                request.idempotency_key,
            )
            if old:
                if old["payload_hash"] != digest:
                    raise TVError(409, "Viewing request key already used with different consent.")
                return lease_view(old)
            now = self.clock()
            membership = await active_pass(tx, user, now)
            if request.mode == "monthly" and not membership:
                raise TVError(402, "No verified paid TV pass. PAYG was not enabled automatically.")
            rate = hourly_v()
            mode = "monthly" if membership else "payg"
            amount = Decimal(0) if membership else segment_cost(LEASE_SECONDS, rate)
            if not membership and request.expected_hourly_v != rate:
                raise TVError(409, "The conversion rate changed. Review the current price again.")
            if amount > request.max_spend_v:
                raise TVError(402, "Viewing budget is below this bounded reservation.")
            lease_id = uuid4()
            expiry = (
                min(now + timedelta(seconds=LEASE_SECONDS), membership["expires_at"])
                if membership
                else now + timedelta(seconds=LEASE_SECONDS)
            )
            if amount:
                try:
                    await self.wallet.reserve(
                        "user:" + str(user), amount, "tv:" + str(lease_id), tx=tx
                    )
                except InsufficientBalance:
                    raise TVError(
                        402, "Insufficient $V. No automatic top-up or card charge."
                    ) from None
            row = await tx.fetchrow(
                "INSERT INTO tv_playback_leases (id,user_id,idempotency_key,payload_hash,episode_id,revision,mode,hourly_v,reserved_v,expires_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",
                lease_id,
                user,
                request.idempotency_key,
                digest,
                request.episode_id,
                revision,
                mode,
                rate,
                amount,
                expiry,
            )
            if not row:
                raise TVError(503, "Viewing reservation was not persisted.")
            return lease_view(row)

    async def authorize(self, user_id, lease_id):
        require_billing()
        row = await self.db.fetchrow(
            "SELECT * FROM tv_playback_leases WHERE id=$1 AND user_id=$2",
            UUID(str(lease_id)),
            UUID(str(user_id)),
        )
        if not row or row["state"] != "reserved" or row["expires_at"] <= self.clock():
            raise TVError(404, "Viewing lease is absent or expired.")
        if self.programs.get(row["episode_id"]) != row["revision"]:
            raise TVError(409, "Approved program revision is unavailable.")
        if row["mode"] == "monthly" and not await active_pass(self.db, user_id, self.clock()):
            raise TVError(402, "TV pass no longer grants access.")
        return lease_view(row)

    async def settle_verified_delivery(self, user_id, lease_id, *, receipt_id, delivered_seconds):
        """Trusted adapter only. Never expose client-reported seconds as an API."""
        if not isinstance(receipt_id, str) or not 1 <= len(receipt_id) <= 128:
            raise TVError(422, "Invalid verified delivery receipt.")
        # Validate even a monthly/zero-charge delivery receipt.
        segment_cost(delivered_seconds, Decimal(1))
        async with self.db.transaction() as tx:
            await user_lock(tx, user_id)
            row = await tx.fetchrow(
                "SELECT * FROM tv_playback_leases WHERE id=$1 AND user_id=$2 FOR UPDATE",
                UUID(str(lease_id)),
                UUID(str(user_id)),
            )
            if not row:
                raise TVError(404, "Viewing lease is unavailable.")
            if row["state"] == "settled":
                if row["receipt_id"] != receipt_id or row["delivered_seconds"] != delivered_seconds:
                    raise TVError(409, "Delivery receipt conflicts with settled usage.")
                return lease_view(row)
            if row["state"] != "reserved" or row["expires_at"] <= self.clock():
                raise TVError(409, "Viewing lease already closed; no late debit is allowed.")
            cost = (
                segment_cost(delivered_seconds, row["hourly_v"])
                if row["mode"] == "payg"
                else Decimal(0)
            )
            if row["reserved_v"]:
                await self.wallet.settle_reservation(
                    "user:" + str(user_id), "tv:" + str(row["id"]), cost, tx=tx
                )
            result = await tx.fetchrow(
                "UPDATE tv_playback_leases SET state='settled', charged_v=$2, receipt_id=$3, delivered_seconds=$4 WHERE id=$1 RETURNING *",
                row["id"],
                cost,
                receipt_id,
                delivered_seconds,
            )
            return lease_view(result)

    async def stop(self, user_id, lease_id):
        # Release stays available when billing is turned off; it never adds access.
        async with self.db.transaction() as tx:
            await user_lock(tx, user_id)
            row = await tx.fetchrow(
                "SELECT * FROM tv_playback_leases WHERE id=$1 AND user_id=$2 FOR UPDATE",
                UUID(str(lease_id)),
                UUID(str(user_id)),
            )
            if not row:
                raise TVError(404, "Viewing lease is unavailable.")
            if row["state"] != "reserved":
                return lease_view(row)
            if row["reserved_v"]:
                await self.wallet.settle_reservation(
                    "user:" + str(user_id), "tv:" + str(row["id"]), Decimal(0), tx=tx
                )
            result = await tx.fetchrow(
                "UPDATE tv_playback_leases SET state='released' WHERE id=$1 RETURNING *", row["id"]
            )
            return lease_view(result)

    async def reap_expired(self, *, user_id=None):
        rows = await self.db.fetch(
            "SELECT id,user_id FROM tv_playback_leases WHERE state='reserved' AND expires_at <= $1 "
            "AND ($2::uuid IS NULL OR user_id=$2) ORDER BY expires_at LIMIT 100",
            self.clock(),
            UUID(str(user_id)) if user_id else None,
        )
        for row in rows:
            await self.stop(row["user_id"], row["id"])
        return len(rows)
