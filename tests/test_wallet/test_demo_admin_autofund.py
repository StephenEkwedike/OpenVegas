from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from openvegas.wallet.ledger import InsufficientBalance, LedgerEntry, WalletService


def _d(v: str | int | float | Decimal) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.000001"))


@dataclass
class _Row:
    data: dict[str, Any]

    def __getitem__(self, key: str):
        return self.data[key]


class _Tx:
    def __init__(self, db: "_FakeDB"):
        self.db = db
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for lock in self._locks.values():
            if lock.locked():
                lock.release()
        return False

    async def execute(self, query: str, *args):
        return await self.db._execute(query, *args)

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if "SELECT balance FROM wallet_accounts WHERE account_id = $1 FOR UPDATE" in q:
            acc = str(args[0])
            lock = self.db._locks.setdefault(acc, asyncio.Lock())
            if acc not in self._locks:
                await lock.acquire()
                self._locks[acc] = lock
            bal = self.db.balances.get(acc, _d("0"))
            return _Row({"balance": bal})
        return await self.db._fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        return await self.db._fetch(query, *args)


class _FakeDB:
    def __init__(self):
        self.balances: dict[str, Decimal] = {}
        self.ledger: list[dict[str, Any]] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def transaction(self):
        return _Tx(self)

    async def execute(self, query: str, *args):
        return await self._execute(query, *args)

    async def fetchrow(self, query: str, *args):
        return await self._fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        return await self._fetch(query, *args)

    async def _execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO wallet_accounts"):
            acc = str(args[0])
            self.balances.setdefault(acc, _d("0"))
            return "OK"

        if q.startswith("INSERT INTO ledger_entries"):
            _, debit, credit, amount, entry_type, reference_id = args
            self.ledger.append(
                {
                    "debit_account": debit,
                    "credit_account": credit,
                    "amount": _d(amount),
                    "entry_type": str(entry_type),
                    "reference_id": str(reference_id),
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return "OK"

        if q.startswith("UPDATE wallet_accounts SET balance = balance -"):
            amount, acc = args
            cur = self.balances.get(str(acc), _d("0"))
            nxt = _d(cur - _d(amount))
            if (str(acc).startswith("user:") or str(acc).startswith("agent:")) and nxt < 0:
                raise Exception("violates check constraint ck_wallet_nonnegative_user_agent")
            self.balances[str(acc)] = nxt
            return "OK"

        if q.startswith("UPDATE wallet_accounts SET balance = balance +"):
            amount, acc = args
            cur = self.balances.get(str(acc), _d("0"))
            self.balances[str(acc)] = _d(cur + _d(amount))
            return "OK"

        return "OK"

    async def _fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if "SELECT balance FROM wallet_accounts WHERE account_id = $1" in q:
            acc = str(args[0])
            if acc not in self.balances:
                return None
            return _Row({"balance": self.balances[acc]})

        if "FROM ledger_entries" in q and "entry_type = 'demo_autofund'" in q:
            acc = str(args[0])
            rows = [
                r for r in self.ledger
                if r["credit_account"] == acc and r["entry_type"] == "demo_autofund"
            ]
            if not rows:
                return None
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return _Row({"created_at": rows[0]["created_at"]})

        return None

    async def _fetch(self, query: str, *args):
        q = " ".join(query.split())
        if "FROM ledger_entries" in q:
            acc = str(args[0])
            return [
                _Row(r)
                for r in self.ledger
                if r["debit_account"] == acc or r["credit_account"] == acc
            ]
        return []


def _env_admin(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "admin-user")
    monkeypatch.setenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_MIN", "1000")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_TOPUP", "1500")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_MAX_CYCLES", "20")
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_READ_COOLDOWN_SEC", "0")


@pytest.mark.asyncio
async def test_non_admin_not_autofunded(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:regular"] = _d("10")
    wallet = WalletService(db)

    with pytest.raises(InsufficientBalance):
        await wallet.place_bet("user:regular", Decimal("100"), "g1")

    assert all(r["entry_type"] != "demo_autofund" for r in db.ledger)


@pytest.mark.asyncio
async def test_balance_read_autofunds_admin_to_floor(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    out = await wallet.ensure_demo_admin_floor("user:admin-user", reason="read")
    assert out >= _d("1000")
    assert db.balances["user:admin-user"] >= _d("1000")
    assert any(r["entry_type"] == "demo_autofund" for r in db.ledger)


@pytest.mark.asyncio
async def test_spend_path_autofunds_to_preserve_floor(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    await wallet.place_bet("user:admin-user", Decimal("200"), "g2")
    assert db.balances["user:admin-user"] >= _d("1000")
    assert any(r["entry_type"] == "demo_autofund" for r in db.ledger)
    assert any(r["entry_type"] == "bet" for r in db.ledger)


@pytest.mark.asyncio
async def test_redeem_triggers_autofund_for_admin(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    await wallet.redeem("user:admin-user", Decimal("100"), "store:o1")
    assert db.balances["user:admin-user"] >= _d("1000")
    assert any(r["entry_type"] == "demo_autofund" for r in db.ledger)
    assert any(r["entry_type"] == "redeem" for r in db.ledger)


@pytest.mark.asyncio
async def test_reserve_triggers_autofund_for_admin(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    await wallet.reserve("user:admin-user", Decimal("300"), "rsv-1")
    assert db.balances["user:admin-user"] >= _d("1000")
    assert any(r["entry_type"] == "demo_autofund" for r in db.ledger)
    assert any(r["entry_type"] == "reserve" for r in db.ledger)


@pytest.mark.asyncio
async def test_max_cycles_respected(monkeypatch):
    _env_admin(monkeypatch)
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_MAX_CYCLES", "1")
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    with pytest.raises(InsufficientBalance):
        await wallet.place_bet("user:admin-user", Decimal("5000"), "g3")


@pytest.mark.asyncio
async def test_read_cooldown_suppresses_repeated_read_topups(monkeypatch):
    _env_admin(monkeypatch)
    monkeypatch.setenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_READ_COOLDOWN_SEC", "3600")
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    await wallet.ensure_demo_admin_floor("user:admin-user", reason="read")
    first_count = len([r for r in db.ledger if r["entry_type"] == "demo_autofund"])
    db.balances["user:admin-user"] = _d("900")
    await wallet.ensure_demo_admin_floor("user:admin-user", reason="read")
    second_count = len([r for r in db.ledger if r["entry_type"] == "demo_autofund"])
    assert second_count == first_count


@pytest.mark.asyncio
async def test_execute_has_no_hidden_autofund_side_effect(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    with pytest.raises(InsufficientBalance):
        await wallet._execute(
            LedgerEntry(
                debit_account="user:admin-user",
                credit_account="store",
                amount=Decimal("1"),
                entry_type="redeem",
                reference_id="raw-execute",
            )
        )
    assert all(r["entry_type"] != "demo_autofund" for r in db.ledger)


@pytest.mark.asyncio
async def test_concurrent_spends_keep_balance_valid(monkeypatch):
    _env_admin(monkeypatch)
    db = _FakeDB()
    db.balances["user:admin-user"] = _d("0")
    wallet = WalletService(db)

    await asyncio.gather(
        wallet.place_bet("user:admin-user", Decimal("900"), "ga"),
        wallet.place_bet("user:admin-user", Decimal("900"), "gb"),
    )

    assert db.balances["user:admin-user"] >= _d("0")
    assert db.balances["user:admin-user"] >= _d("1000")
    auto = [r for r in db.ledger if r["entry_type"] == "demo_autofund"]
    assert len(auto) >= 1
