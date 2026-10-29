"""Default-off cosmetics referral grants; no wallet writes or client session proofs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from openvegas.store.catalog import cosmetic_asset
from openvegas.referrals.entitlements import find_entitlement, list_entitlements

THRESHOLD = 5
TRIAL_DAYS = 7
TICKET_SECONDS = 24 * 3600
CODE = re.compile(r"^[a-f0-9]{32}$")


class ReferralError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def enabled():
    return os.getenv("OPENVEGAS_REFERRALS_ENABLED", "0") == "1"


def require_enabled():
    if not enabled():
        raise ReferralError(404, "Referral promotion is not active.")


def identity_key():
    value = os.getenv("OPENVEGAS_REFERRAL_IDENTITY_KEY", "").encode()
    if len(value) < 32:
        raise ReferralError(503, "Referral verification is not configured.")
    return value


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def email_hash(email: str):
    return hmac.new(identity_key(), b"identity:v1:" + email.strip().casefold().encode(), hashlib.sha256).hexdigest()


def make_ticket(code: str, now: datetime):
    body = f"v1.{code}.{int(now.timestamp() * 1_000_000)}.{secrets.token_hex(16)}"
    signature = hmac.new(identity_key(), b"ticket:" + body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def parse_ticket(ticket: str, now: datetime):
    if not isinstance(ticket, str) or len(ticket) > 220:
        raise ReferralError(400, "Invalid invitation ticket.")
    parts = ticket.split(".")
    if (len(parts) != 5 or parts[0] != "v1" or not CODE.fullmatch(parts[1])
            or not re.fullmatch(r"[0-9]{16,17}", parts[2]) or not CODE.fullmatch(parts[3])
            or not re.fullmatch(r"[0-9a-f]{64}", parts[4])):
        raise ReferralError(400, "Invalid invitation ticket.")
    expected = hmac.new(identity_key(), b"ticket:" + ".".join(parts[:4]).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[4]):
        raise ReferralError(400, "Invalid invitation ticket.")
    visited = datetime.fromtimestamp(int(parts[2]) / 1_000_000, timezone.utc)
    if not timedelta(0) <= now - visited <= timedelta(seconds=TICKET_SECONDS):
        raise ReferralError(410, "Invitation ticket expired. Open a fresh invitation before signing up.")
    return parts[1], visited


def approved_item(item):
    return bool(item and item.get("type") == "cosmetic"
                and item.get("slot") in {"companion", "completion"}
                and item.get("approval_status") == "approved"
                and item.get("artwork_approved") is True
                and item.get("native_compatibility_verified") is True
                and cosmetic_asset(item))


def grant_response(row, replayed=False):
    return {"id": str(row["id"]), "kind": row["kind"], "item_id": row["item_id"],
            "status": row["status"], "created_at": row["created_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "replayed": replayed}


class ReferralService:
    def __init__(self, db, store):
        self.db, self.store = db, store

    def ready(self):
        require_enabled()
        identity_key()
        # Reject older store implementations lacking integrated entitlement guards.
        if getattr(self.store, "REFERRAL_ENTITLEMENTS_VERSION", 0) != 1:
            raise ReferralError(503, "Referral entitlement integration is not ready.")

    async def _lock(self, tx, user):
        await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                         json.dumps(["referrals", str(user)], separators=(",", ":")))

    async def _account(self, tx, user):
        row = await tx.fetchrow("""SELECT id, email, created_at, email_confirmed_at
            FROM auth.users WHERE id=$1 AND email_confirmed_at IS NOT NULL
            AND nullif(btrim(email), '') IS NOT NULL AND is_anonymous IS NOT TRUE
            AND deleted_at IS NULL AND (banned_until IS NULL OR banned_until <= clock_timestamp())""", user)
        if not row or row["created_at"] is None:
            raise ReferralError(403, "A verified, active email account is required.")
        return row

    async def _identity(self, tx, user):
        account = await self._account(tx, user)
        identity = email_hash(account["email"])
        await tx.execute("""INSERT INTO public.referral_identities(user_id,identity_hash)
            VALUES ($1,$2) ON CONFLICT DO NOTHING""", user, identity)
        row = await tx.fetchrow("SELECT identity_hash FROM public.referral_identities WHERE user_id=$1", user)
        if not row or not hmac.compare_digest(row["identity_hash"], identity):
            raise ReferralError(409, "This verified identity is not eligible for a new referral account.")
        return account

    async def visit(self, code, previous=None):
        self.ready()
        if not CODE.fullmatch(code):
            raise ReferralError(404, "Invitation unavailable.")
        async with self.db.transaction() as tx:
            now = await tx.fetchval("SELECT clock_timestamp()")
            if previous:
                try:
                    old_code, _ = parse_ticket(previous, now)
                    if old_code == code:
                        return previous
                except ReferralError as exc:
                    if exc.status == 503:
                        raise
            row = await tx.fetchrow("SELECT user_id FROM public.referral_invites WHERE code=$1", code)
            if not row:
                raise ReferralError(404, "Invitation unavailable.")
            await self._account(tx, row["user_id"])
            return make_ticket(code, now)

    async def attribute(self, user_id, ticket):
        self.ready()
        user = UUID(str(user_id))
        async with self.db.transaction() as tx:
            await self._lock(tx, user)
            account = await self._identity(tx, user)
            existing = await tx.fetchrow("SELECT ticket_hash FROM public.referral_attributions WHERE invitee_id=$1", user)
            if existing:
                if isinstance(ticket, str) and hmac.compare_digest(existing["ticket_hash"], digest(ticket)):
                    return {"attributed": True, "replayed": True}
                raise ReferralError(409, "This account already has an invitation attribution.")
            now = await tx.fetchval("SELECT clock_timestamp()")
            code, visited = parse_ticket(ticket, now)
            inviter = await tx.fetchrow("SELECT user_id,created_at FROM public.referral_invites WHERE code=$1", code)
            if not inviter or inviter["user_id"] == user:
                raise ReferralError(409, "Invitation cannot be applied to this account.")
            owner = await self._account(tx, inviter["user_id"])
            if email_hash(owner["email"]) == email_hash(account["email"]):
                raise ReferralError(409, "Invitation cannot be applied to this account.")
            if not inviter["created_at"] <= visited <= account["created_at"] <= now:
                raise ReferralError(409, "Invitations apply only to accounts created after the invitation visit.")
            inserted = await tx.fetchrow("""INSERT INTO public.referral_attributions(invitee_id,inviter_id,ticket_hash,visited_at)
                VALUES ($1,$2,$3,$4) ON CONFLICT(ticket_hash) DO NOTHING RETURNING invitee_id""",
                user, inviter["user_id"], digest(ticket), visited)
            if not inserted:
                raise ReferralError(409, "This invitation visit has already been used by another account.")
            return {"attributed": True, "replayed": False}

    async def _progress(self, tx, user):
        # Only persisted, successfully settled human inference is eligible. Never
        # read host event callbacks, user metadata, or client completion assertions.
        # One bounded reconciliation replaces a fragile settlement callback. An
        # already-used receipt cannot starve later genuine sessions on refresh.
        candidates = await tx.fetch("""SELECT DISTINCT ON (a.invitee_id)
                a.invitee_id, r.id, proof.receipt_hash
            FROM public.referral_attributions a
            JOIN auth.users account ON account.id=a.invitee_id
            JOIN public.inference_requests r ON r.user_id=a.invitee_id
            JOIN public.inference_usage u ON u.request_id=r.id AND u.user_id=r.user_id
            CROSS JOIN LATERAL (SELECT encode(sha256(convert_to(
                u.provider || ':' || r.provider_request_id, 'UTF8')), 'hex') AS receipt_hash) proof
            WHERE (a.inviter_id=$1 OR a.invitee_id=$1)
              AND NOT EXISTS (SELECT 1 FROM public.referral_sessions s
                WHERE s.invitee_id=a.invitee_id OR s.request_id=r.id OR s.receipt_hash=proof.receipt_hash)
              AND r.status='succeeded' AND r.response_status=200
              AND r.created_at >= account.created_at AND r.created_at >= a.visited_at
              AND r.wallet_funding_source <> 'demo' AND u.wallet_funding_source <> 'demo'
              AND r.inference_source IN ('wrapper','byok') AND u.actor_type='human'
              AND u.account_id=('user:' || a.invitee_id::text)
              AND nullif(btrim(r.provider_request_id),'') IS NOT NULL
              AND r.response_body_text::jsonb->>'completion_status'='complete'
              AND r.response_body_text::jsonb->>'provider_request_id'=r.provider_request_id
              AND u.output_tokens > 0 AND u.actual_cost_usd > 0
            ORDER BY a.invitee_id,r.created_at,r.id LIMIT 100""", user)
        for candidate in candidates:
            await tx.execute("""INSERT INTO public.referral_sessions(invitee_id,request_id,receipt_hash)
                VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""", candidate["invitee_id"],
                candidate["id"], candidate["receipt_hash"])
        rows = await tx.fetch("""SELECT a.invitee_id, i.identity_hash FROM public.referral_attributions a
            JOIN public.referral_sessions s ON s.invitee_id=a.invitee_id
            JOIN public.referral_identities i ON i.user_id=a.invitee_id
            WHERE a.inviter_id=$1""", user)
        count = 0
        for row in rows:
            try:
                account = await self._account(tx, row["invitee_id"])
            except ReferralError:
                continue
            if hmac.compare_digest(row["identity_hash"], email_hash(account["email"])):
                count += 1
                if count == THRESHOLD:
                    break
        return count

    async def status(self, user_id):
        self.ready()
        user = UUID(str(user_id))
        async with self.db.transaction() as tx:
            await self._lock(tx, user)
            await self._identity(tx, user)
            await tx.execute("""INSERT INTO public.referral_invites(user_id,code) VALUES ($1,$2)
                ON CONFLICT(user_id) DO NOTHING""", user, secrets.token_hex(16))
            invite = await tx.fetchrow("SELECT code FROM public.referral_invites WHERE user_id=$1", user)
            qualified = await self._progress(tx, user)
            grants = await tx.fetch("SELECT * FROM public.referral_grants WHERE user_id=$1 ORDER BY created_at", user)
            attributed = bool(await tx.fetchrow("SELECT 1 FROM public.referral_attributions WHERE invitee_id=$1", user))
            allowed = await tx.fetch("SELECT item_id FROM public.referral_eligible_items WHERE enabled ORDER BY item_id")
            items = [{"id": row["item_id"], "name": self.store.catalog[row["item_id"]]["name"]}
                     for row in allowed if approved_item(self.store.catalog.get(row["item_id"]))]
            return {"invite_path": "/ui/referrals?invite=" + invite["code"],
                    "required_friends": THRESHOLD, "qualified_friends": qualified,
                    "trial_days": TRIAL_DAYS, "attributed": attributed,
                    "can_claim_trial": attributed and not any(r["kind"] == "trial" for r in grants),
                    "can_claim_reward": qualified == THRESHOLD and not any(r["kind"] == "reward" for r in grants),
                    "eligible_items": items, "grants": [grant_response(r) for r in grants]}

    async def claim(self, user_id, kind, item_id, key):
        self.ready()
        if kind not in {"trial", "reward"}:
            raise ReferralError(422, "Invalid claim kind.")
        user, key = UUID(str(user_id)), UUID(str(key))
        async with self.db.transaction() as tx:
            await self._lock(tx, user)
            await self._identity(tx, user)
            existing = await tx.fetchrow("""SELECT * FROM public.referral_grants
                WHERE user_id=$1 AND (kind=$2 OR idempotency_key=$3)
                ORDER BY (idempotency_key=$3) DESC LIMIT 1""", user, kind, key)
            if existing:
                if existing["kind"] != kind or existing["item_id"] != item_id:
                    raise ReferralError(409, "This claim or request key already chose another emote.")
                return grant_response(existing, True)
            if kind == "trial":
                if not await tx.fetchrow("SELECT 1 FROM public.referral_attributions WHERE invitee_id=$1", user):
                    raise ReferralError(403, "A verified new-account invitation is required.")
            elif await self._progress(tx, user) < THRESHOLD:
                raise ReferralError(409, "Five verified new friends must each complete their first genuine session.")
            allowed = await tx.fetchrow("SELECT item_id FROM public.referral_eligible_items WHERE item_id=$1 AND enabled FOR SHARE", item_id)
            item = self.store.catalog.get(item_id)
            if not allowed or not approved_item(item):
                raise ReferralError(409, "This emote is not eligible for the promotion.")
            await self.store._lock_scope(tx, "sku", str(user), item_id)
            asset = dict(cosmetic_asset(item))
            # Validates the actual private pinned release, not just a catalog label.
            from server.services.emote_delivery import load_delivery_pack, EmoteDeliveryUnavailable
            from openvegas.store.service import _emote_release_configuration
            release = _emote_release_configuration()
            try:
                await asyncio.to_thread(load_delivery_pack, item_id, asset)
            except EmoteDeliveryUnavailable:
                raise ReferralError(503, "The eligible emote is not ready for delivery.") from None
            owned = await tx.fetchrow("""SELECT e.*, g.kind AS grant_kind, g.status AS grant_status
                FROM public.cosmetic_entitlements e LEFT JOIN public.referral_grants g
                  ON g.id=e.source_referral_grant_id
                WHERE e.user_id=$1 AND e.item_id=$2 FOR UPDATE OF e""", user, item_id)
            if owned and not (kind == "reward" and owned["grant_kind"] == "trial"
                    and owned["status"] == "active" and owned["grant_status"] == "active"
                    and owned["pack_id"] == asset["pack_id"] and owned["slot"] == item["slot"]):
                raise ReferralError(409, "Choose an emote you do not already own or contact support for revoked access.")
            grant = await tx.fetchrow("""WITH stamp AS (SELECT clock_timestamp() AS now)
                INSERT INTO public.referral_grants(user_id,kind,item_id,idempotency_key,created_at,expires_at)
                SELECT $1,$2,$3,$4,now,CASE WHEN $2='trial' THEN now + interval '168 hours' ELSE NULL END
                FROM stamp RETURNING *""", user, kind, item_id, key)
            if owned:
                await tx.execute("""UPDATE public.cosmetic_entitlements SET source_referral_grant_id=$2,
                    expires_at=NULL WHERE id=$1""", owned["id"], grant["id"])
            else:
                await tx.execute("""INSERT INTO public.cosmetic_entitlements
                    (user_id,item_id,slot,pack_id,acquired_version,source_referral_grant_id,expires_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)""", user, item_id, item["slot"], asset["pack_id"],
                    asset["version"], grant["id"], grant["expires_at"])
            if release != _emote_release_configuration() or cosmetic_asset(self.store.catalog.get(item_id, {})) != asset:
                raise ReferralError(409, "Emote release changed; retry after availability is restored.")
            return grant_response(grant)
