from __future__ import annotations

from decimal import Decimal

import pytest

from openvegas.wallet.ledger import InsufficientBalance, WalletService


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query: str, *args):
        if query.startswith("UPDATE wallet_accounts SET balance = balance -") and str(args[1]).startswith("agent:"):
            raise Exception("violates check constraint ck_wallet_nonnegative_user_agent")
        return "OK"


class _FakeDB:
    def transaction(self):
        return _FakeTx()

    async def fetchrow(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_agent_debit_check_violation_maps_to_insufficient_balance():
    wallet = WalletService(_FakeDB())

    with pytest.raises(InsufficientBalance):
        await wallet.place_bet("agent:test-agent", Decimal("5"), "round-1")
