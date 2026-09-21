"""Store-specific extension of the existing billing fake DB contract.

Scope locks are modeled, not PostgreSQL isolation/RLS. Rollback tests run alone.
"""

import asyncio
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

from openvegas.wallet.ledger import InsufficientBalance
from tests.test_billing.test_billing_service import _FakeDB


def normalized(query):
    return " ".join(query.split())


class StoreFakeDB(_FakeDB):
    def __init__(self):
        self.state = {
            "orders": {},
            "requests": {},
            "entitlements": {},
            "equipment": {},
            "grants": [],
            "balances": {},
            "debits": [],
        }
        self.locks = {}
        self.calls = []
        self.fail = None

    def transaction(self):
        return StoreFakeTx(self)

    async def fetch(self, query, *args):
        return await StoreFakeTx(self).fetch(query, *args)


class StoreFakeTx:
    def __init__(self, db):
        self.db = db
        self.held = []
        self.snapshot = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type and self.snapshot is not None:
            self.db.state = self.snapshot
        for lock in reversed(self.held):
            lock.release()

    def write(self):
        if self.snapshot is None:
            self.snapshot = deepcopy(self.db.state)

    def record(self, query, args):
        query = normalized(query)
        self.db.calls.append((query, args))
        if self.db.fail and self.db.fail in query:
            raise RuntimeError("injected database failure")
        if (
            self.db.fail == "final_transition"
            and query.startswith("UPDATE store_orders")
            and args[2] == "fulfilled"
        ):
            raise RuntimeError("injected database failure")
        return query

    def entitlement(self, row):
        if row is None:
            return None
        result = deepcopy(row)
        order = self.db.state["orders"][row["source_order_id"]]
        if row["status"] == "revoked" or order["status"] != "fulfilled":
            result["effective_status"] = "revoked"
        elif row["status"] == "suspended":
            result["effective_status"] = "suspended"
        elif row["expires_at"] and row["expires_at"] <= datetime.now(UTC):
            result["effective_status"] = "expired"
        else:
            result["effective_status"] = "active"
        return result

    async def execute(self, query, *args):
        query = self.record(query, args)
        state = self.db.state
        if "pg_advisory_xact_lock" in query:
            lock = self.db.locks.setdefault(args[0], asyncio.Lock())
            await lock.acquire()
            self.held.append(lock)
            await asyncio.sleep(0)
            return "SELECT 1"
        self.write()
        if query.startswith("INSERT INTO store_orders"):
            order_id, user, sku, price, key, payload = args
            assert not any(
                o["user_id"] == user and o["idempotency_key"] == key
                for o in state["orders"].values()
            )
            state["orders"][order_id] = {
                "id": order_id,
                "user_id": user,
                "item_id": sku,
                "cost_v": price,
                "idempotency_key": key,
                "idempotency_payload_hash": payload,
                "status": "created",
            }
        elif query.startswith("INSERT INTO store_purchase_requests"):
            user, key, payload, order_id, sku = args
            assert (user, key) not in state["requests"]
            state["requests"][user, key] = {"source_order_id": order_id, "payload_hash": payload}
        elif query.startswith("INSERT INTO cosmetic_entitlements"):
            user, sku, slot, pack, version, order_id = args
            assert (user, sku) not in state["entitlements"]
            state["entitlements"][user, sku] = {
                "id": str(uuid.uuid4()),
                "user_id": user,
                "item_id": sku,
                "slot": slot,
                "pack_id": pack,
                "acquired_version": version,
                "source_order_id": order_id,
                "status": "active",
                "expires_at": None,
                "created_at": datetime.now(UTC),
                "revoked_at": None,
                "revocation_reason": None,
            }
        elif query.startswith("INSERT INTO cosmetic_equipment"):
            user, slot, sku = args
            state["equipment"][user, slot] = sku
        elif query.startswith("DELETE FROM cosmetic_equipment"):
            state["equipment"].pop(tuple(args), None)
        elif query.startswith("INSERT INTO inference_token_grants"):
            user, order_id, provider, model, tokens = args
            state["grants"].append(
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user,
                    "source_order_id": order_id,
                    "provider": provider,
                    "model_id": model,
                    "tokens_total": tokens,
                    "tokens_remaining": tokens,
                    "expires_at": None,
                }
            )
        else:
            raise AssertionError(query)
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        query = self.record(query, args)
        state = self.db.state
        if query.startswith("UPDATE store_orders"):
            self.write()
            order_id, before, after, _reason = args
            row = state["orders"].get(order_id)
            if row and row["status"] == before:
                row["status"] = after
                return {"id": order_id}
            return None
        if "FROM store_purchase_requests r" in query:
            request = state["requests"].get(tuple(args))
            if request:
                row = deepcopy(state["orders"][request["source_order_id"]])
                row["idempotency_payload_hash"] = request["payload_hash"]
                return row
            return None
        if "FROM cosmetic_entitlements e" in query:
            assert "e.user_id = $1 AND e.item_id = $2" in query
            return self.entitlement(state["entitlements"].get(tuple(args)))
        if "FROM store_orders WHERE user_id" in query and "item_id = $2" in query:
            return next(
                (
                    deepcopy(o)
                    for o in state["orders"].values()
                    if (o["user_id"], o["item_id"]) == tuple(args)
                    and o["status"] in {"created", "settled", "fulfilled", "reversed"}
                ),
                None,
            )
        if "FROM store_orders WHERE user_id" in query:
            return next(
                (
                    deepcopy(o)
                    for o in state["orders"].values()
                    if (o["user_id"], o["idempotency_key"]) == tuple(args)
                ),
                None,
            )
        if "FROM store_orders WHERE id" in query:
            row = state["orders"].get(args[0])
            return deepcopy(row) if row and row["user_id"] == args[1] else None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        query = self.record(query, args)
        state = self.db.state
        if "FROM inference_token_grants" in query:
            field = "source_order_id" if "WHERE source_order_id" in query else "user_id"
            return [deepcopy(g) for g in state["grants"] if g[field] == args[0]]
        if "FROM cosmetic_entitlements e" in query:
            assert "WHERE e.user_id = $1" in query
            rows = []
            for (user, sku), row in state["entitlements"].items():
                if user == args[0]:
                    result = self.entitlement(row)
                    result["equipped"] = (
                        state["equipment"].get((user, row["slot"])) == sku
                        and result["effective_status"] == "active"
                    )
                    rows.append(result)
            return rows
        raise AssertionError(query)


class StoreFakeWallet:
    def __init__(self, db):
        self.db = db

    async def redeem(self, account_id, amount, reference_id, *, tx):
        assert isinstance(tx, StoreFakeTx) and tx.db is self.db
        tx.write()
        state = self.db.state
        balance = state["balances"].get(account_id, Decimal(100))
        if balance < amount:
            raise InsufficientBalance("insufficient test funds")
        state["balances"][account_id] = balance - amount
        state["debits"].append((account_id, amount, reference_id))
