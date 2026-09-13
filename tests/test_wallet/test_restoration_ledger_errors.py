"""Unit contract checks; transaction behavior is proven separately on PostgreSQL."""

from decimal import Decimal

import pytest

from openvegas.wallet.ledger import InsufficientBalance, LedgerEntry, WalletService


class _CheckError(Exception):
    sqlstate = "23514"

    def __init__(self, constraint):
        self.constraint_name = constraint


class _Connection:
    def __init__(self, error=None):
        self.error = error
        self.queries = []

    async def execute(self, sql, *args):
        self.queries.append(sql)
        if sql.startswith("UPDATE wallet_accounts SET balance = balance -") and self.error:
            raise self.error
        return "INSERT 0 1" if sql.startswith("INSERT") else "OK"

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return {"balance": Decimal("10")}


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", [
    "ck_wallet_nonnegative_user_agent", "wallet_accounts_balance_check",
])
async def test_known_balance_constraint_rolls_back_savepoint_and_maps_error(constraint):
    error = _CheckError(constraint)
    conn = _Connection(error)
    wallet = WalletService(None)
    with pytest.raises(InsufficientBalance) as caught:
        await wallet._execute(LedgerEntry(
            debit_account="user:local", credit_account="store", amount=Decimal("11"),
            entry_type="redeem", reference_id="local-rejected",
        ), tx=conn)
    assert caught.value.__cause__ is error
    assert conn.queries[-2].startswith("ROLLBACK TO SAVEPOINT wallet_")
    assert conn.queries[-1].startswith("RELEASE SAVEPOINT wallet_")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    _CheckError("some_unrelated_check"), RuntimeError("duplicate provider response"),
    RuntimeError("violates check constraint ck_wallet_nonnegative_user_agent"),
])
async def test_unexpected_errors_are_not_misclassified_or_swallowed(error):
    conn = _Connection(error)
    with pytest.raises(type(error)) as caught:
        await WalletService(None)._execute(LedgerEntry(
            debit_account="user:local", credit_account="store", amount=Decimal("1"),
            entry_type="redeem", reference_id="local-error",
        ), tx=conn)
    assert caught.value is error
    assert conn.queries[-2].startswith("ROLLBACK TO SAVEPOINT wallet_")


@pytest.mark.asyncio
async def test_balance_read_reuses_callers_connection_without_pool_access():
    conn = _Connection()
    assert await WalletService(None).get_balance("user:local", tx=conn) == Decimal("10")
