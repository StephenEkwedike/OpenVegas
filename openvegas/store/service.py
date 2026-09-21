"""Store service — transactional purchases and inference grant issuance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from openvegas.payments.adjustments import (
    AdjustmentError,
    check_purchase_adjustments,
    lock_user_adjustments,
)
from openvegas.store.catalog import (
    COSMETIC_SLOTS,
    STORE_CATALOG,
    cosmetic_asset,
    cosmetic_price_v,
    cosmetic_purchasable,
    public_catalog,
    public_cosmetic,
)
from openvegas.wallet.ledger import WalletService


class StoreError(Exception):
    pass


class IdempotencyConflict(StoreError):
    pass


class IllegalTransition(StoreError):
    pass


class CosmeticUnavailable(StoreError):
    pass


class EntitlementDenied(StoreError):
    pass


class EntitlementExpired(EntitlementDenied):
    pass


@dataclass
class StoreOrderResult:
    order_id: str
    status: str
    state: str
    item_id: str
    cost_v: Decimal
    grants: list[dict]
    entitlement: dict | None = None
    already_owned: bool = False
    replayed: bool = False


class StoreService:
    def __init__(self, db: Any, wallet: WalletService):
        self.db = db
        self.wallet = wallet

    @staticmethod
    def canonical_payload_hash(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _provider_for_model(model_id: str) -> str:
        model = model_id.lower()
        if model.startswith("gpt-") or "openai" in model:
            return "openai"
        if model.startswith("claude-"):
            return "anthropic"
        if model.startswith("gemini-"):
            return "gemini"
        raise StoreError(f"Cannot infer provider for model '{model_id}'")

    async def list_catalog(self, *, cosmetics_only: bool = False) -> dict:
        return public_catalog(cosmetics_only=cosmetics_only)

    async def preview(self, item_id: str) -> dict:
        item = STORE_CATALOG.get(item_id)
        if not item or item.get("type") != "cosmetic":
            raise CosmeticUnavailable("COSMETIC_NOT_FOUND")
        return public_cosmetic(item_id, item)

    async def _lock_scope(self, tx: Any, scope: str, user_id: str, value: str) -> None:
        # Transaction-scoped locks also protect an absent row, across workers.
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            json.dumps(["store", scope, user_id, value], separators=(",", ":")),
        )

    @staticmethod
    def _entitlement(row: Any) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["id"] = str(result["id"])
        result["source_order_id"] = str(result["source_order_id"])
        result.pop("user_id", None)
        return result

    async def _find_entitlement(self, conn: Any, user_id: str, item_id: str) -> dict | None:
        row = await conn.fetchrow(
            """
            SELECT e.*, CASE
                WHEN e.status = 'revoked' OR o.status <> 'fulfilled' THEN 'revoked'
                WHEN e.status = 'suspended' THEN 'suspended'
                WHEN e.expires_at IS NOT NULL AND e.expires_at <= clock_timestamp() THEN 'expired'
                ELSE 'active' END AS effective_status
            FROM cosmetic_entitlements e
            JOIN store_orders o ON o.id = e.source_order_id AND o.user_id = e.user_id
            WHERE e.user_id = $1 AND e.item_id = $2
            FOR UPDATE OF e, o
            """,
            user_id,
            item_id,
        )
        return self._entitlement(row)

    @staticmethod
    def _require_active(entitlement: dict | None) -> None:
        if entitlement and entitlement["effective_status"] == "expired":
            raise EntitlementExpired("COSMETIC_ENTITLEMENT_EXPIRED")
        if not entitlement or entitlement["effective_status"] != "active":
            raise EntitlementDenied("COSMETIC_NOT_OWNED_OR_REVOKED")

    @staticmethod
    def _equippable(item: dict, entitlement: dict) -> bool:
        asset = cosmetic_asset(item)
        return bool(
            item.get("type") == "cosmetic"
            and item.get("approval_status") == "approved"
            and asset
            and asset["pack_id"] == entitlement["pack_id"]
            and item.get("slot") == entitlement["slot"]
        )

    async def list_owned(self, user_id: str) -> dict:
        rows = await self.db.fetch(
            """
            SELECT e.*, CASE
                WHEN e.status = 'revoked' OR o.status <> 'fulfilled' THEN 'revoked'
                WHEN e.status = 'suspended' THEN 'suspended'
                WHEN e.expires_at IS NOT NULL AND e.expires_at <= clock_timestamp() THEN 'expired'
                ELSE 'active' END AS effective_status,
                (q.item_id IS NOT NULL AND e.status = 'active' AND o.status = 'fulfilled'
                 AND (e.expires_at IS NULL OR e.expires_at > clock_timestamp())) AS equipped
            FROM cosmetic_entitlements e
            JOIN store_orders o ON o.id = e.source_order_id AND o.user_id = e.user_id
            LEFT JOIN cosmetic_equipment q ON q.user_id = e.user_id AND q.item_id = e.item_id
            WHERE e.user_id = $1
            ORDER BY e.created_at DESC, e.item_id
            """,
            user_id,
        )
        entitlements = [self._entitlement(row) for row in rows]
        # Ownership survives a catalog revision; withdrawn assets cannot activate.
        equipped = {}
        for entitlement in entitlements:
            item = STORE_CATALOG.get(entitlement["item_id"], {})
            available = self._equippable(item, entitlement)
            activatable = available and entitlement["effective_status"] == "active"
            entitlement["activatable"] = activatable
            entitlement["available_version"] = cosmetic_asset(item)["version"] if activatable else None
            entitlement["equipped"] = bool(entitlement["equipped"] and available)
            if entitlement["equipped"]:
                equipped[entitlement["slot"]] = entitlement["item_id"]
        return {"entitlements": entitlements, "equipped": equipped}

    async def equip(self, user_id: str, slot: str | None, item_id: str | None) -> dict:
        if slot is None and item_id is not None:
            slot = STORE_CATALOG.get(item_id, {}).get("slot")
        if slot not in COSMETIC_SLOTS:
            raise StoreError("INVALID_COSMETIC_SLOT")
        async with self.db.transaction() as tx:
            await self._lock_scope(tx, "slot", user_id, slot)
            if item_id is None:
                await tx.execute(
                    "DELETE FROM cosmetic_equipment WHERE user_id = $1 AND slot = $2",
                    user_id,
                    slot,
                )
                return {"slot": slot, "item_id": None}
            await self._lock_scope(tx, "sku", user_id, item_id)
            entitlement = await self._find_entitlement(tx, user_id, item_id)
            self._require_active(entitlement)
            item = STORE_CATALOG.get(item_id, {})
            if not self._equippable(item, entitlement) or entitlement["slot"] != slot:
                raise CosmeticUnavailable("COSMETIC_NOT_EQUIPPABLE")
            await tx.execute(
                """
                INSERT INTO cosmetic_equipment (user_id, slot, item_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, slot) DO UPDATE
                SET item_id = EXCLUDED.item_id, updated_at = now()
                """,
                user_id,
                slot,
                item_id,
            )
            return {"slot": slot, "item_id": item_id, "entitlement": entitlement}

    @asynccontextmanager
    async def delivery_asset(self, user_id: str, item_id: str):
        """Keep ownership/order locks until a private delivery has been validated."""
        user_id = str(user_id)
        async with self.db.transaction() as tx:
            await self._lock_scope(tx, "sku", user_id, item_id)
            entitlement = await self._find_entitlement(tx, user_id, item_id)
            self._require_active(entitlement)
            item = STORE_CATALOG.get(item_id, {})
            if not self._equippable(item, entitlement):
                raise CosmeticUnavailable("COSMETIC_NOT_EQUIPPABLE")
            asset = deepcopy(cosmetic_asset(item))
            yield asset
            # Expiration or an operator catalog withdrawal can occur during IO.
            entitlement = await self._find_entitlement(tx, user_id, item_id)
            self._require_active(entitlement)
            item = STORE_CATALOG.get(item_id, {})
            if not self._equippable(item, entitlement) or cosmetic_asset(item) != asset:
                raise CosmeticUnavailable("COSMETIC_NOT_EQUIPPABLE")

    async def list_grants(self, user_id: str) -> list[dict]:
        rows = await self.db.fetch(
            """
            SELECT id, source_order_id, provider, model_id, tokens_total, tokens_remaining,
                   expires_at, created_at
            FROM inference_token_grants
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )
        return [dict(r) for r in rows]

    async def _transition_order(
        self,
        tx: Any,
        order_id: str,
        from_status: str,
        to_status: str,
        reason: str | None = None,
    ) -> None:
        row = await tx.fetchrow(
            """
            UPDATE store_orders
            SET status = $3,
                failure_reason = COALESCE($4, failure_reason),
                updated_at = now()
            WHERE id = $1 AND status = $2
            RETURNING id
            """,
            order_id,
            from_status,
            to_status,
            reason,
        )
        if not row:
            raise IllegalTransition(f"Illegal transition {from_status}->{to_status} for {order_id}")

    async def _get_or_lock_order(
        self,
        tx: Any,
        user_id: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> dict | None:
        row = await tx.fetchrow(
            "SELECT * FROM store_orders WHERE user_id = $1 AND idempotency_key = $2 FOR UPDATE",
            user_id,
            idempotency_key,
        )
        if not row:
            row = await tx.fetchrow(
                """
                SELECT o.*, r.payload_hash AS idempotency_payload_hash
                FROM store_purchase_requests r
                JOIN store_orders o ON o.id = r.source_order_id AND o.user_id = r.user_id
                WHERE r.user_id = $1 AND r.idempotency_key = $2
                FOR UPDATE OF o
                """,
                user_id,
                idempotency_key,
            )
            if not row:
                return None

        if row["idempotency_payload_hash"] != payload_hash:
            raise IdempotencyConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")

        status = str(row["status"])
        if status in {"created", "settled"}:
            return {"state": "pending", "order": dict(row)}
        alias = row["idempotency_key"] != idempotency_key
        return {
            "state": "already_owned" if alias and status == "fulfilled" else "completed",
            "order": dict(row),
            "already_owned": alias,
        }

    async def buy(self, user_id: str, item_id: str, idempotency_key: str) -> StoreOrderResult:
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise StoreError("INVALID_IDEMPOTENCY_KEY")
        if not idempotency_key.strip() or any(ord(c) < 32 for c in idempotency_key):
            raise StoreError("INVALID_IDEMPOTENCY_KEY")
        if not isinstance(item_id, str) or not 1 <= len(item_id) <= 128:
            raise StoreError("INVALID_ITEM_ID")
        payload_hash = self.canonical_payload_hash({"item_id": item_id})

        async with self.db.transaction() as tx:
            # Cash adjustments must serialize before any request/SKU/wallet lock.
            await lock_user_adjustments(tx, user_id)
            # Always key before SKU, including AI purchases, to avoid absent-row races.
            await self._lock_scope(tx, "request", user_id, idempotency_key)
            await self._lock_scope(tx, "sku", user_id, item_id)
            existing = await self._get_or_lock_order(tx, user_id, idempotency_key, payload_hash)
            if existing:
                order = existing["order"]
                grants = await self._fetch_grants_for_order(tx, str(order["id"]))
                return StoreOrderResult(
                    order_id=str(order["id"]),
                    status=str(order["status"]),
                    state=existing["state"],
                    item_id=order["item_id"],
                    cost_v=Decimal(str(order["cost_v"])),
                    grants=grants,
                    entitlement=await self._find_entitlement(tx, user_id, item_id),
                    already_owned=existing.get("already_owned", False),
                    replayed=True,
                )

            item = STORE_CATALOG.get(item_id)
            if item is None:
                raise StoreError(f"Unknown store item '{item_id}'")
            if item.get("type") == "cosmetic":
                owned = await self._find_entitlement(tx, user_id, item_id)
                if owned:
                    self._require_active(owned)
                    await tx.execute(
                        """
                        INSERT INTO store_purchase_requests
                            (user_id, idempotency_key, payload_hash, source_order_id, item_id)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        user_id,
                        idempotency_key,
                        payload_hash,
                        owned["source_order_id"],
                        item_id,
                    )
                    original = await tx.fetchrow(
                        "SELECT * FROM store_orders WHERE id = $1 AND user_id = $2",
                        owned["source_order_id"],
                        user_id,
                    )
                    return StoreOrderResult(
                        order_id=owned["source_order_id"],
                        status="fulfilled",
                        state="already_owned",
                        item_id=item_id,
                        cost_v=Decimal(str(original["cost_v"])),
                        grants=[],
                        entitlement=owned,
                        already_owned=True,
                    )
                if not cosmetic_purchasable(item):
                    raise CosmeticUnavailable("COSMETIC_PREVIEW_ONLY")
                legacy = await tx.fetchrow(
                    """
                    SELECT id, status FROM store_orders
                    WHERE user_id = $1 AND item_id = $2
                      AND status IN ('created', 'settled', 'fulfilled', 'reversed')
                    ORDER BY created_at DESC LIMIT 1 FOR UPDATE
                    """,
                    user_id,
                    item_id,
                )
                if legacy:
                    raise CosmeticUnavailable("COSMETIC_LEGACY_ORDER_REQUIRES_RECONCILIATION")
                if item.get("slot") in {"companion", "completion"}:
                    from server.services.emote_delivery import (
                        EmoteDeliveryUnavailable,
                        load_delivery_pack,
                    )

                    # New purchases require deliverable bytes, even if the asset
                    # omits delivery_resource. Replays/legacy checks remain above.
                    checked_item = deepcopy(item)
                    try:
                        await asyncio.to_thread(load_delivery_pack, item_id, checked_item["asset"])
                    except EmoteDeliveryUnavailable:
                        raise CosmeticUnavailable("COSMETIC_DELIVERY_UNAVAILABLE") from None
                    if STORE_CATALOG.get(item_id) != checked_item:
                        raise CosmeticUnavailable("COSMETIC_DELIVERY_UNAVAILABLE")
                    item = checked_item
            cost_v = cosmetic_price_v(item) if item.get("type") == "cosmetic" else Decimal(str(item["cost_v"]))
            if cost_v is None:
                raise CosmeticUnavailable("COSMETIC_PRICE_UNAVAILABLE")

            order_id = str(uuid.uuid4())
            await tx.execute(
                """
                INSERT INTO store_orders (id, user_id, item_id, cost_v, status, idempotency_key, idempotency_payload_hash)
                VALUES ($1, $2, $3, $4, 'created', $5, $6)
                """,
                order_id,
                user_id,
                item_id,
                cost_v,
                idempotency_key,
                payload_hash,
            )

            if cost_v > 0:
                await self.wallet.redeem(
                    account_id=f"user:{user_id}",
                    amount=cost_v,
                    reference_id=f"store:{order_id}",
                    tx=tx,
                )
            await self._transition_order(tx, order_id, "created", "settled")

            grants = []
            if item.get("type") == "ai_pack":
                models = list(item.get("models", []))
                if not models:
                    raise StoreError(f"Store item '{item_id}' is missing model mapping")

                total_tokens = int(item.get("tokens", 0))
                base = total_tokens // len(models)
                remainder = total_tokens % len(models)
                for idx, model_id in enumerate(models):
                    tokens = base + (1 if idx < remainder else 0)
                    provider = self._provider_for_model(model_id)
                    await tx.execute(
                        """
                        INSERT INTO inference_token_grants
                          (user_id, source_order_id, provider, model_id, tokens_total, tokens_remaining)
                        VALUES ($1, $2, $3, $4, $5, $5)
                        ON CONFLICT (source_order_id, provider, model_id)
                        DO NOTHING
                        """,
                        user_id,
                        order_id,
                        provider,
                        model_id,
                        tokens,
                    )

                grants = await self._fetch_grants_for_order(tx, order_id)

            if item.get("type") == "cosmetic":
                asset = cosmetic_asset(item)
                await tx.execute(
                    """
                    INSERT INTO cosmetic_entitlements
                        (user_id, item_id, slot, pack_id, acquired_version, source_order_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    user_id,
                    item_id,
                    item["slot"],
                    asset["pack_id"],
                    asset["version"],
                    order_id,
                )
            await self._transition_order(tx, order_id, "settled", "fulfilled")
            if item.get("type") == "cosmetic":
                try:
                    await check_purchase_adjustments(tx, user_id=user_id, order_id=order_id)
                except AdjustmentError as exc:
                    # Raising here rolls back the order, debit and entitlement too.
                    raise StoreError(str(exc)) from None
            entitlement = await self._find_entitlement(tx, user_id, item_id)

        return StoreOrderResult(
            order_id=order_id,
            status="fulfilled",
            state="completed",
            item_id=item_id,
            cost_v=cost_v,
            grants=grants,
            entitlement=entitlement,
        )

    async def _fetch_grants_for_order(self, conn: Any, order_id: str) -> list[dict]:
        rows = await conn.fetch(
            """
            SELECT id, provider, model_id, tokens_total, tokens_remaining, expires_at, created_at
            FROM inference_token_grants
            WHERE source_order_id = $1
            ORDER BY created_at ASC
            """,
            order_id,
        )
        return [dict(r) for r in rows]
