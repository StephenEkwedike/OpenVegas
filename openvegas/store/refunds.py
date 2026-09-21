"""Explicit operator-authorized cosmetic refunds, never a public customer API.

Returns the original $V purchase amount, not the cash funding that preceded it.
Every mutation is in the purchase's account/SKU lock and one ledger transaction.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

from openvegas.store.service import StoreError, StoreService
from openvegas.wallet.ledger import LedgerEntry, WalletService

REASONS = frozenset(
    {"customer_request", "defective_pack", "duplicate_purchase", "operator_correction"}
)


def _uuid(value: str) -> str:
    try:
        parsed = UUID(value)
        if not parsed.int:
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        raise StoreError("REFUND_INVALID_IDENTIFIER") from None


async def inspect_refund(db, *, user_id: str, order_id: str) -> dict:
    user_id, order_id = _uuid(user_id), _uuid(order_id)
    row = await db.fetchrow(
        """SELECT o.id, o.item_id, o.cost_v, o.status, e.status AS entitlement_status
           FROM store_orders o JOIN cosmetic_entitlements e
             ON e.source_order_id=o.id AND e.user_id=o.user_id AND e.item_id=o.item_id
           WHERE o.id=$1 AND o.user_id=$2""",
        order_id,
        user_id,
    )
    if row is None:
        raise StoreError("COSMETIC_ORDER_NOT_FOUND")
    return {
        "order_id": str(row["id"]),
        "item_id": row["item_id"],
        "cost_v": str(row["cost_v"]),
        "status": str(row["status"]),
        "entitlement_status": row["entitlement_status"],
    }


async def refund_cosmetic(
    db, *, user_id: str, order_id: str, operator_id: str, reason: str
) -> dict:
    """Caller must be an authorized operator; no automatic refund eligibility policy."""
    user_id, order_id, operator_id = _uuid(user_id), _uuid(order_id), _uuid(operator_id)
    if reason not in REASONS:
        raise StoreError("REFUND_INVALID_REASON")
    wallet = WalletService(db)
    service = StoreService(db, wallet)
    summary = await inspect_refund(db, user_id=user_id, order_id=order_id)
    item_id = summary["item_id"]
    reference = f"store_refund:{order_id}"
    audit = json.dumps(
        {
            "operation": "cosmetic_v_refund",
            "operator_id": operator_id,
            "reason": reason,
            "reference": reference,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    async with db.transaction() as tx:
        await service._lock_scope(tx, "sku", user_id, item_id)
        row = await tx.fetchrow(
            """SELECT o.status, o.cost_v, o.failure_reason, e.status AS entitlement_status,
                      e.revocation_reason
               FROM store_orders o JOIN cosmetic_entitlements e
                 ON e.source_order_id=o.id AND e.user_id=o.user_id AND e.item_id=o.item_id
               WHERE o.id=$1 AND o.user_id=$2 AND o.item_id=$3 FOR UPDATE OF o,e""",
            order_id,
            user_id,
            item_id,
        )
        if row is None:
            raise StoreError("COSMETIC_ORDER_NOT_FOUND")
        amount = Decimal(str(row["cost_v"]))
        if not amount.is_finite() or amount < 0:
            raise StoreError("REFUND_REQUIRES_RECONCILIATION")
        original = await tx.fetchrow(
            """SELECT amount FROM ledger_entries WHERE reference_id=$1 AND entry_type='redeem'
               AND debit_account=$2 AND credit_account='store'""",
            f"store:{order_id}",
            f"user:{user_id}",
        )
        if amount > 0 and (original is None or original["amount"] != amount):
            raise StoreError("REFUND_REQUIRES_RECONCILIATION")
        if amount == 0 and original is not None:
            raise StoreError("REFUND_REQUIRES_RECONCILIATION")
        refunded = await tx.fetchrow(
            """SELECT amount FROM ledger_entries WHERE reference_id=$1 AND entry_type='store_refund'
               AND debit_account='store' AND credit_account=$2""",
            reference,
            f"user:{user_id}",
        )
        if row["status"] == "reversed":
            try:
                recorded = json.loads(row["revocation_reason"])
            except (ValueError, TypeError):
                recorded = {}
            if (
                not isinstance(recorded, dict)
                or row["entitlement_status"] != "revoked"
                or recorded.get("operation") != "cosmetic_v_refund"
                or recorded.get("reference") != reference
                or row["failure_reason"] != row["revocation_reason"]
                or (amount > 0 and (refunded is None or refunded["amount"] != amount))
                or (amount == 0 and refunded is not None)
            ):
                raise StoreError("REFUND_REQUIRES_RECONCILIATION")
            return {
                **summary,
                "status": "reversed",
                "entitlement_status": "revoked",
                "replayed": True,
            }
        if (
            row["status"] != "fulfilled"
            or row["entitlement_status"] != "active"
            or refunded is not None
        ):
            raise StoreError("REFUND_REQUIRES_RECONCILIATION")
        if amount > 0:
            await wallet._execute(
                LedgerEntry(
                    debit_account="store",
                    credit_account=f"user:{user_id}",
                    amount=amount,
                    entry_type="store_refund",
                    reference_id=reference,
                ),
                tx=tx,
            )
        await tx.execute(
            """UPDATE cosmetic_entitlements SET status='revoked', revoked_at=clock_timestamp(),
               revocation_reason=$3 WHERE source_order_id=$1 AND user_id=$2""",
            order_id,
            user_id,
            audit,
        )
        await tx.execute(
            "DELETE FROM cosmetic_equipment WHERE user_id=$1 AND item_id=$2", user_id, item_id
        )
        await service._transition_order(tx, order_id, "fulfilled", "reversed", audit)
        return {**summary, "status": "reversed", "entitlement_status": "revoked", "replayed": False}
