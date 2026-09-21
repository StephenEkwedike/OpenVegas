"""Verified cash events -> entitlement restrictions, never another money movement.

No Stripe client, FIFO assumption, price conversion, or automatic refund lives here.
Call only after signature verification, inside the webhook journal transaction.
Mixed-source/partial attribution stays explicit manual review rather than invented
provenance. Store purchases must take lock_user_adjustments before their SKU lock
and call attribute_order in the same transaction after the fulfilled order/debit.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID

EVENTS = frozenset(
    {
        "refund.created",
        "refund.updated",
        "refund.failed",
        "charge.refund.updated",
        "charge.refunded",
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
    }
)
DISPUTE_OPEN = frozenset(
    {"needs_response", "under_review", "warning_needs_response", "warning_under_review"}
)
DISPUTE_CLOSED = frozenset({"won", "lost", "warning_closed"})
REFUND_CLOSED = frozenset({"succeeded", "failed", "canceled"})
MAX_HISTORY = 10000


class AdjustmentError(Exception):
    """Cannot safely process: rollback the journal so delivery can be retried."""


def _id(value, prefix):
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or not len(prefix) < len(value) <= 255
    ):
        raise AdjustmentError("ADJUSTMENT_INVALID_REFERENCE")
    if any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in value):
        raise AdjustmentError("ADJUSTMENT_INVALID_REFERENCE")
    return value


@dataclass(frozen=True)
class Fact:
    kind: str
    object_id: str
    payment_intent: str
    charge_id: str
    amount_minor: int
    currency: str
    state: str
    event_created: int
    livemode: bool


def parse_event(event: dict, *, expected_livemode: bool) -> Fact:
    if event.get("type") not in EVENTS:
        raise AdjustmentError("ADJUSTMENT_UNSUPPORTED_EVENT")
    if type(expected_livemode) is not bool or type(event.get("livemode")) is not bool:
        raise AdjustmentError("ADJUSTMENT_MODE_UNVERIFIED")
    if event["livemode"] != expected_livemode or event.get("account") or event.get("context"):
        raise AdjustmentError("ADJUSTMENT_ACCOUNT_OR_MODE_MISMATCH")
    _id(event.get("id"), "evt_")
    created = event.get("created")
    if type(created) is not int or not 0 < created < 2**63:
        raise AdjustmentError("ADJUSTMENT_INVALID_TIMESTAMP")
    obj = event.get("data", {}).get("object")
    if not isinstance(obj, dict):
        raise AdjustmentError("ADJUSTMENT_INVALID_OBJECT")
    if "livemode" in obj and (
        type(obj["livemode"]) is not bool or obj["livemode"] != expected_livemode
    ):
        raise AdjustmentError("ADJUSTMENT_MODE_MISMATCH")
    kind = "dispute" if event["type"].startswith("charge.dispute.") else "refund"
    if event["type"] == "charge.refunded":
        kind = "charge_refund"
    expected_object = "charge" if kind == "charge_refund" else kind
    if obj.get("object") != expected_object:
        raise AdjustmentError("ADJUSTMENT_INVALID_OBJECT")
    object_id = _id(
        obj.get("id"), {"refund": "re_", "dispute": "dp_", "charge_refund": "ch_"}[kind]
    )
    charge = object_id if kind == "charge_refund" else _id(obj.get("charge"), "ch_")
    amount = obj.get("amount_refunded" if kind == "charge_refund" else "amount")
    if type(amount) is not int or not 0 < amount < 2**63 or obj.get("currency") != "usd":
        raise AdjustmentError("ADJUSTMENT_INVALID_AMOUNT_OR_CURRENCY")
    state = "succeeded" if kind == "charge_refund" else obj.get("status")
    allowed = (
        DISPUTE_OPEN | DISPUTE_CLOSED
        if kind == "dispute"
        else REFUND_CLOSED | {"pending", "requires_action"}
    )
    if state not in allowed:
        raise AdjustmentError("ADJUSTMENT_UNKNOWN_STATE")
    if event["type"] == "charge.dispute.closed" and state not in DISPUTE_CLOSED:
        raise AdjustmentError("ADJUSTMENT_INVALID_CLOSED_DISPUTE")
    if event["type"] == "refund.failed" and state != "failed":
        raise AdjustmentError("ADJUSTMENT_INVALID_FAILED_REFUND")
    return Fact(
        kind,
        object_id,
        _id(obj.get("payment_intent"), "pi_"),
        charge,
        amount,
        "usd",
        state,
        created,
        expected_livemode,
    )


def transition(old: dict | None, fact: Fact) -> tuple[str, bool]:
    """Terminal evidence cannot be erased by a late pending/update delivery."""
    if old is None:
        return "apply", False
    if any(old[key] != getattr(fact, key) for key in ("charge_id", "currency", "livemode")):
        raise AdjustmentError("ADJUSTMENT_IDENTITY_CONFLICT")
    if fact.kind == "charge_refund":
        return ("apply" if fact.amount_minor > old["amount_minor"] else "stale"), bool(
            old["needs_review"]
        )
    if fact.amount_minor != old["amount_minor"]:
        return "conflict", True
    terminal = DISPUTE_CLOSED if fact.kind == "dispute" else REFUND_CLOSED
    if old["state"] in terminal:
        if fact.state in terminal and fact.state != old["state"]:
            return "conflict", True
        return "stale", bool(old["needs_review"])
    if fact.state in terminal or fact.event_created > old["event_created"]:
        return "apply", bool(old["needs_review"])
    return "stale", bool(old["needs_review"])


def policy(rows: list[dict], *, total_minor: int, exclusive: bool) -> tuple[str, str]:
    # The charge aggregate and refund objects are overlapping evidence, NOT additive.
    refunded = max(
        sum(r["amount_minor"] for r in rows if r["kind"] == "refund" and r["state"] == "succeeded"),
        max((r["amount_minor"] for r in rows if r["kind"] == "charge_refund"), default=0),
    )
    if refunded > total_minor:
        return "suspended", "refund_amount_conflict"
    lost = [r for r in rows if r["kind"] == "dispute" and r["state"] == "lost"]
    if refunded == total_minor or any(r["amount_minor"] == total_minor for r in lost):
        return "revoked", "confirmed_full_cash_reversal"
    if exclusive and (refunded > 0 or lost):
        return "revoked", "confirmed_partial_cash_reversal_exclusive_purchase"
    if any(r["needs_review"] for r in rows):
        return "suspended", "conflicting_provider_facts"
    if any(r["kind"] == "dispute" and r["state"] in DISPUTE_OPEN for r in rows):
        return "suspended", "open_dispute"
    if lost:
        return "suspended", "partial_lost_dispute_requires_review"
    if refunded:
        return "review", "partial_refund_allocation_unknown"
    return "active", "no_outstanding_restriction"


async def lock_user_adjustments(tx, user_id: str) -> None:
    await tx.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
        json.dumps(["stripe-emote", str(UUID(str(user_id)))], separators=(",", ":")),
    )


async def _sku(tx, user_id, item_id):
    await tx.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
        json.dumps(["store", "sku", str(user_id), item_id], separators=(",", ":")),
    )


async def attribute_order(tx, *, user_id: str, order_id: str) -> bool:
    """Record a forced minimum contribution under policy/SKU/wallet locks.

    False means unknown, NOT 'not funded by Stripe'. Never allocate ambiguous history.
    This hook does not commit, debit, refund, or accept caller-supplied topup IDs.
    """
    user_id, order_id = str(UUID(str(user_id))), str(UUID(str(order_id)))
    existing = await tx.fetchrow(
        "SELECT order_id FROM stripe_emote_funding WHERE order_id=$1 AND user_id=$2",
        order_id,
        user_id,
    )
    if existing:
        return True
    order = await tx.fetchrow(
        "SELECT * FROM store_orders WHERE id=$1 AND user_id=$2", order_id, user_id
    )
    if not order or order["status"] != "fulfilled" or order["cost_v"] <= 0:
        return False
    account = f"user:{user_id}"
    balance = await tx.fetchval(
        "SELECT balance FROM wallet_accounts WHERE account_id=$1 FOR UPDATE", account
    )
    history = await tx.fetch(
        "SELECT * FROM ledger_entries WHERE debit_account=$1 OR credit_account=$1 LIMIT $2",
        account,
        MAX_HISTORY + 1,
    )
    if len(history) > MAX_HISTORY or balance is None:
        return False
    credits = [r for r in history if r["credit_account"] == account]
    debits = [r for r in history if r["debit_account"] == account]
    total_credit = sum(r["amount"] for r in credits)
    if total_credit - sum(r["amount"] for r in debits) != balance or balance < 0:
        return False
    # Even if every other credit funded this order, this excess must come from
    # this source. Including later credits makes historical proof conservative.
    # Multiple forced sources require a richer allocation contract: don't choose.
    forced = [
        (r, order["cost_v"] - (total_credit - r["amount"]))
        for r in credits
        if r["entry_type"] == "fiat_topup"
        and r["debit_account"] == "fiat_reserve"
        and order["cost_v"] > total_credit - r["amount"]
    ]
    if len(forced) != 1:
        return False
    credit, minimum = forced[0]
    reference = credit["reference_id"] or ""
    try:
        topup_id = str(UUID(reference.removeprefix("fiat_topup:")))
    except ValueError:
        return False
    if reference != f"fiat_topup:{topup_id}":
        return False
    topup = await tx.fetchrow(
        "SELECT * FROM fiat_topups WHERE id=$1 AND user_id=$2", topup_id, user_id
    )
    if (
        not topup
        or topup["mode"] != "stripe"
        or topup["status"] != "paid"
        or credit["amount"] > topup["v_credit"]
    ):
        return False
    original = [r for r in debits if r["reference_id"] == f"store:{order_id}"]
    if (
        len(original) != 1
        or original[0]["entry_type"] != "redeem"
        or original[0]["credit_account"] != "store"
        or original[0]["amount"] != order["cost_v"]
    ):
        return False
    if minimum > credit["amount"] or any(r["amount"] <= 0 for r in history):
        return False
    proof = json.dumps(
        {
            "method": "forced_minimum_ledger_credit_v1",
            "credit_v": str(credit["amount"]),
            "other_credits_v": str(total_credit - credit["amount"]),
            "minimum_v": str(minimum),
            "gross_v": str(topup["v_credit"]),
            "balance_v": str(balance),
            "ledger_ids": sorted(str(r["id"]) for r in history),
        },
        sort_keys=True,
    )
    await tx.execute(
        """INSERT INTO stripe_emote_funding
        (order_id,user_id,item_id,topup_id,credit_entry_id,debit_entry_id,funded_v,evidence)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""",
        order_id,
        user_id,
        order["item_id"],
        topup_id,
        credit["id"],
        original[0]["id"],
        minimum,
        proof,
    )
    return True


async def check_purchase_adjustments(tx, *, user_id: str, order_id: str) -> None:
    """Store hook after fulfillment, before commit; raises to roll back the debit.

    Caller must hold lock_user_adjustments from BEFORE request/SKU/wallet locks.
    Mixed history with an unresolved cash adjustment also blocks NEW purchases;
    it does not invent a link or revoke existing, unproven ownership.
    """
    await attribute_order(tx, user_id=user_id, order_id=order_id)
    rows = await tx.fetch(
        """SELECT a.*,t.amount_usd FROM stripe_emote_adjustments a
        JOIN fiat_topups t ON t.id=a.topup_id AND t.user_id=a.user_id WHERE a.user_id=$1""",
        user_id,
    )
    groups = {}
    for r in rows:
        groups.setdefault(str(r["topup_id"]), []).append(dict(r))
    for group in groups.values():
        target, _ = policy(group, total_minor=int(group[0]["amount_usd"] * 100), exclusive=False)
        if target != "active":
            raise AdjustmentError("COSMETIC_PAYMENT_ADJUSTMENT_REQUIRES_REVIEW")


async def _audit(tx, event_id, topup_id, outcome, facts):
    await tx.execute(
        """INSERT INTO stripe_emote_adjustment_audit(event_id,topup_id,outcome,facts)
                        VALUES ($1,$2,$3,$4::jsonb)""",
        event_id,
        topup_id,
        outcome,
        json.dumps(facts, sort_keys=True),
    )


async def _apply_entitlements(tx, *, topup, event_id):
    user_id, topup_id = str(topup["user_id"]), str(topup["id"])
    # Discover only mathematically unambiguous historical purchases. Cap work rather
    # than silently process part of an account. No ledger timestamp/FIFO inference.
    candidates = await tx.fetch(
        "SELECT source_order_id,item_id FROM cosmetic_entitlements WHERE user_id=$1 ORDER BY item_id LIMIT 1001",
        user_id,
    )
    if len(candidates) > 1000:
        raise AdjustmentError("ADJUSTMENT_ACCOUNT_REQUIRES_BATCH_REVIEW")
    for item in candidates:
        await _sku(tx, user_id, item["item_id"])
    for item in candidates:
        await attribute_order(tx, user_id=user_id, order_id=str(item["source_order_id"]))
    links = await tx.fetch(
        """SELECT f.*,e.id AS entitlement_id,e.status AS entitlement_status,
          o.status AS order_status,o.cost_v,c.amount AS credit_v,d.amount AS debit_v,
          c.reference_id AS credit_ref,c.entry_type AS credit_type,c.debit_account AS credit_from,
          c.credit_account AS credit_to,d.reference_id AS debit_ref,d.entry_type AS debit_type,
          d.debit_account AS debit_from,d.credit_account AS debit_to
        FROM stripe_emote_funding f JOIN cosmetic_entitlements e
          ON e.source_order_id=f.order_id AND e.user_id=f.user_id AND e.item_id=f.item_id
        JOIN store_orders o ON o.id=f.order_id AND o.user_id=f.user_id
        JOIN ledger_entries c ON c.id=f.credit_entry_id JOIN ledger_entries d ON d.id=f.debit_entry_id
        WHERE f.topup_id=$1 AND f.user_id=$2 ORDER BY f.item_id FOR UPDATE OF e,o""",
        topup_id,
        user_id,
    )
    rows = [
        dict(r)
        for r in await tx.fetch(
            "SELECT * FROM stripe_emote_adjustments WHERE topup_id=$1 AND user_id=$2",
            topup_id,
            user_id,
        )
    ]
    for link in links:
        if not (
            link["credit_ref"] == f"fiat_topup:{topup_id}"
            and link["credit_type"] == "fiat_topup"
            and link["credit_from"] == "fiat_reserve"
            and link["credit_to"] == f"user:{user_id}"
            and link["debit_ref"] == f"store:{link['order_id']}"
            and link["debit_type"] == "redeem"
            and link["debit_from"] == f"user:{user_id}"
            and link["debit_to"] == "store"
            and 0 < link["funded_v"] <= link["cost_v"] == link["debit_v"]
            and link["funded_v"] <= link["credit_v"] <= topup["v_credit"]
        ):
            raise AdjustmentError("ADJUSTMENT_FUNDING_EVIDENCE_CHANGED")
        exclusive = (
            len(links) == 1
            and link["cost_v"] == link["funded_v"] == link["credit_v"] == topup["v_credit"]
        )
        target, reason = policy(
            rows, total_minor=int(topup["amount_usd"] * 100), exclusive=exclusive
        )
        previous = link["entitlement_status"]
        owned_hold = await tx.fetchval(
            "SELECT EXISTS(SELECT 1 FROM stripe_emote_suspensions WHERE entitlement_id=$1)",
            link["entitlement_id"],
        )
        if previous == "revoked" or link["order_status"] != "fulfilled":
            target, reason = previous, "independent_or_irreversible_revocation"
        elif target == "revoked":
            await tx.execute(
                """UPDATE cosmetic_entitlements SET status='revoked',revoked_at=now(),
                revocation_reason=$2 WHERE id=$1""",
                link["entitlement_id"],
                json.dumps(
                    {"operation": "stripe_cash_adjustment", "event_id": event_id, "reason": reason}
                ),
            )
        elif target == "suspended" and previous == "active":
            await tx.execute(
                "INSERT INTO stripe_emote_suspensions(entitlement_id,event_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                link["entitlement_id"],
                event_id,
            )
            await tx.execute(
                "UPDATE cosmetic_entitlements SET status='suspended' WHERE id=$1",
                link["entitlement_id"],
            )
        elif target == "active" and previous == "suspended" and owned_hold:
            await tx.execute(
                "UPDATE cosmetic_entitlements SET status='active' WHERE id=$1",
                link["entitlement_id"],
            )
            await tx.execute(
                "DELETE FROM stripe_emote_suspensions WHERE entitlement_id=$1",
                link["entitlement_id"],
            )
        if target in {"suspended", "revoked"}:
            # Do not auto-equip on restoration: the user may have chosen another pack.
            await tx.execute(
                "DELETE FROM cosmetic_equipment WHERE user_id=$1 AND item_id=$2",
                user_id,
                link["item_id"],
            )
        actual = await tx.fetchval(
            "SELECT status FROM cosmetic_entitlements WHERE id=$1", link["entitlement_id"]
        )
        await _audit(
            tx,
            event_id,
            topup_id,
            "manual_review"
            if target == "review" or "review" in reason
            else "entitlement_evaluated",
            {
                "order_id": str(link["order_id"]),
                "previous": previous,
                "status": actual,
                "reason": reason,
            },
        )
    unknown = len(candidates) - len(links)
    if unknown or not links:
        await _audit(
            tx,
            event_id,
            topup_id,
            "manual_review",
            {"reason": "unattributed_or_other_funding", "unlinked_count": unknown},
        )
    return len(links)


async def handle_adjustment(tx, event: dict, *, expected_livemode: bool) -> dict:
    fact = parse_event(event, expected_livemode=expected_livemode)
    topup = await tx.fetchrow(
        "SELECT * FROM fiat_topups WHERE stripe_payment_intent_id=$1", fact.payment_intent
    )
    if not topup or topup["status"] != "paid":
        # Event may precede settlement. No committed dedup entry: retry after mapping.
        raise AdjustmentError("ADJUSTMENT_TOPUP_MAPPING_NOT_READY")
    if topup["mode"] != "stripe" or topup["currency"] != fact.currency:
        raise AdjustmentError("ADJUSTMENT_TOPUP_IDENTITY_MISMATCH")
    total = topup["amount_usd"] * 100
    if total != total.to_integral_value() or fact.amount_minor > total:
        raise AdjustmentError("ADJUSTMENT_AMOUNT_EXCEEDS_TOPUP")
    obj = event["data"]["object"]
    if fact.kind == "charge_refund" and (
        type(obj.get("amount")) is not int
        or obj["amount"] != total
        or obj.get("customer") != topup["stripe_customer_id"]
        or obj.get("paid") is not True
    ):
        raise AdjustmentError("ADJUSTMENT_CHARGE_MISMATCH")
    # Absent provider objects must serialize too, including malicious/corrupt
    # cross-user reuse. ON CONFLICT alone must not overwrite another owner's state.
    for scope in (["stripe-charge", fact.charge_id], ["stripe-object", fact.kind, fact.object_id]):
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            json.dumps(scope, separators=(",", ":")),
        )
    foreign = await tx.fetchval(
        "SELECT EXISTS(SELECT 1 FROM stripe_emote_adjustments WHERE charge_id=$1 AND topup_id<>$2)",
        fact.charge_id,
        topup["id"],
    )
    if foreign:
        raise AdjustmentError("ADJUSTMENT_CHARGE_OWNER_CONFLICT")
    await lock_user_adjustments(tx, str(topup["user_id"]))
    bindings = await tx.fetch(
        "SELECT DISTINCT charge_id,livemode FROM stripe_emote_adjustments WHERE topup_id=$1",
        topup["id"],
    )
    if any(r["charge_id"] != fact.charge_id or r["livemode"] != fact.livemode for r in bindings):
        raise AdjustmentError("ADJUSTMENT_CHARGE_BINDING_CONFLICT")
    old = await tx.fetchrow(
        "SELECT * FROM stripe_emote_adjustments WHERE kind=$1 AND object_id=$2 FOR UPDATE",
        fact.kind,
        fact.object_id,
    )
    if old and (old["topup_id"] != topup["id"] or old["user_id"] != topup["user_id"]):
        raise AdjustmentError("ADJUSTMENT_OWNER_CONFLICT")
    action, needs_review = transition(dict(old) if old else None, fact)
    if action == "apply":
        await tx.execute(
            """INSERT INTO stripe_emote_adjustments
            (kind,object_id,topup_id,user_id,charge_id,livemode,currency,amount_minor,state,event_created,event_id,needs_review)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT(kind,object_id) DO UPDATE SET amount_minor=EXCLUDED.amount_minor,
              state=EXCLUDED.state,event_created=EXCLUDED.event_created,event_id=EXCLUDED.event_id,
              needs_review=EXCLUDED.needs_review""",
            fact.kind,
            fact.object_id,
            topup["id"],
            topup["user_id"],
            fact.charge_id,
            fact.livemode,
            fact.currency,
            fact.amount_minor,
            fact.state,
            fact.event_created,
            event["id"],
            needs_review,
        )
    elif action == "conflict":
        await tx.execute(
            "UPDATE stripe_emote_adjustments SET needs_review=TRUE WHERE kind=$1 AND object_id=$2",
            fact.kind,
            fact.object_id,
        )
    await _audit(tx, event["id"], topup["id"], action, asdict(fact))
    count = await _apply_entitlements(tx, topup=topup, event_id=event["id"])
    return {"status": "adjustment_recorded", "outcome": action, "attributed_orders": count}
