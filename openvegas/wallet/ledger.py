"""Double-entry bookkeeping ledger for $V transactions.

Account ID convention:
  - "user:<uuid>"   — human user wallet
  - "agent:<uuid>"  — agent service account wallet
  - "escrow:<id>"   — game/round escrow
  - "house"         — house bankroll
  - "mint_reserve"  — mint issuance source
  - "rake_revenue"  — PvP rake revenue
  - "store"         — redemption store

All public methods accept full prefixed account_id strings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

MONEY_SCALE = Decimal("0.000001")


class AccountType(Enum):
    USER = "user"
    AGENT = "agent"
    HOUSE = "house"
    MINT_RESERVE = "mint_reserve"
    RAKE_REVENUE = "rake_revenue"
    STORE = "store"


@dataclass
class LedgerEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    debit_account: str = ""
    credit_account: str = ""
    amount: Decimal = Decimal("0")
    entry_type: str = ""
    reference_id: str = ""
    # Idempotency enforced by UNIQUE(reference_id, entry_type, debit_account, credit_account)
    created_at: datetime = field(default_factory=datetime.utcnow)


class InsufficientBalance(Exception):
    pass


class WalletService:
    """Double-entry ledger for $V transactions with strict invariants.

    Invariants:
    1. No negative user/agent balances (Postgres CHECK constraint).
    2. Atomic transactions (single BEGIN...COMMIT per entry).
    3. Idempotency via UNIQUE(reference_id, entry_type, debit_account, credit_account).
    4. Double-entry balance: sum(debits) == sum(credits).

    All methods accept full prefixed account_id strings (e.g., "user:abc", "agent:xyz").
    """

    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def _money(value: Decimal | float | str) -> Decimal:
        return Decimal(str(value)).quantize(MONEY_SCALE)

    async def ensure_demo_admin_floor(
        self,
        account_id: str,
        *,
        pending_debit: Decimal = Decimal("0"),
        reason: str = "spend",
        tx=None,
    ) -> Decimal:
        """Demo-admin-only autofund to keep a testing floor.

        Read path: if below floor, top up toward floor.
        Spend path: top up toward floor + pending_debit so post-debit stays above floor.
        """
        from server.services.demo_admin import (
            demo_admin_autofund_enabled,
            demo_admin_autofund_max_cycles,
            demo_admin_autofund_min,
            demo_admin_autofund_read_cooldown_sec,
            demo_admin_autofund_topup,
            is_demo_admin_account,
        )

        if not demo_admin_autofund_enabled() or not is_demo_admin_account(account_id):
            return await self.get_balance(account_id)

        min_floor = self._money(demo_admin_autofund_min())
        topup = self._money(demo_admin_autofund_topup())
        max_cycles = demo_admin_autofund_max_cycles()
        cooldown_sec = demo_admin_autofund_read_cooldown_sec()
        pending_debit = self._money(max(Decimal("0"), Decimal(str(pending_debit))))
        target = self._money(min_floor + pending_debit)

        if topup <= 0:
            return await self.get_balance(account_id)

        async def _run(conn):
            await conn.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                account_id,
            )
            await conn.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                "demo_reserve",
            )

            row = await conn.fetchrow(
                "SELECT balance FROM wallet_accounts WHERE account_id = $1 FOR UPDATE",
                account_id,
            )
            balance = self._money(row["balance"] if row else Decimal("0"))
            if balance >= target:
                return balance

            if reason == "read" and cooldown_sec > 0:
                last = await conn.fetchrow(
                    """
                    SELECT created_at
                    FROM ledger_entries
                    WHERE credit_account = $1
                      AND entry_type = 'demo_autofund'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    account_id,
                )
                if last and last["created_at"] is not None:
                    last_ts = last["created_at"]
                    if getattr(last_ts, "tzinfo", None) is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if now - last_ts <= timedelta(seconds=cooldown_sec):
                        return balance

            cycles = 0
            while balance < target and cycles < max_cycles:
                cycles += 1
                await self._execute(
                    LedgerEntry(
                        debit_account="demo_reserve",
                        credit_account=account_id,
                        amount=topup,
                        entry_type="demo_autofund",
                        reference_id=f"demo_autofund:{reason}:{account_id}:{uuid.uuid4()}",
                    ),
                    tx=conn,
                )
                balance = self._money(balance + topup)

            return balance

        if tx is not None:
            return await _run(tx)
        async with self.db.transaction() as own_tx:
            return await _run(own_tx)

    async def mint(self, account_id: str, amount: Decimal, mint_id: str, *, tx=None):
        """Credit wallet from mint (new $V enters system).
        account_id: full prefixed ID (e.g., 'user:abc' or 'agent:xyz').
        Pass tx= to run within an existing transaction."""
        entry = LedgerEntry(
            debit_account="mint_reserve",
            credit_account=account_id,
            amount=self._money(amount),
            entry_type="mint",
            reference_id=mint_id,
        )
        await self._execute(entry, tx=tx)

    async def place_bet(
        self,
        account_id: str,
        amount: Decimal,
        game_id: str,
        *,
        tx=None,
        entry_type: str = "bet",
        reference_id: str | None = None,
    ):
        """Move $V from account to escrow for a game/round.
        account_id: full prefixed ID (e.g., 'user:abc' or 'agent:xyz')."""
        amount = self._money(amount)
        entry = LedgerEntry(
            debit_account=account_id,
            credit_account=f"escrow:{game_id}",
            amount=amount,
            entry_type=entry_type,
            reference_id=reference_id or game_id,
        )
        if tx is not None:
            await self.ensure_demo_admin_floor(
                account_id,
                pending_debit=amount,
                reason="spend",
                tx=tx,
            )
            await self._execute(entry, tx=tx)
            return

        async with self.db.transaction() as own_tx:
            await self.ensure_demo_admin_floor(
                account_id,
                pending_debit=amount,
                reason="spend",
                tx=own_tx,
            )
            await self._execute(entry, tx=own_tx)

    async def settle_win(
        self,
        account_id: str,
        payout: Decimal,
        game_id: str,
        *,
        tx=None,
        entry_type: str = "win",
        reference_id: str | None = None,
    ):
        """Pay out winnings from escrow to account.
        account_id: full prefixed ID (e.g., 'user:abc' or 'agent:xyz')."""
        entry = LedgerEntry(
            debit_account=f"escrow:{game_id}",
            credit_account=account_id,
            amount=self._money(payout),
            entry_type=entry_type,
            reference_id=reference_id or game_id,
        )
        await self._execute(entry, tx=tx)

    async def settle_loss(
        self,
        game_id: str,
        amount: Decimal,
        *,
        tx=None,
        entry_type: str = "loss",
        reference_id: str | None = None,
    ):
        """Move lost bet from escrow to house."""
        entry = LedgerEntry(
            debit_account=f"escrow:{game_id}",
            credit_account="house",
            amount=self._money(amount),
            entry_type=entry_type,
            reference_id=reference_id or game_id,
        )
        await self._execute(entry, tx=tx)

    async def pvp_rake(self, pot: Decimal, game_id: str) -> Decimal:
        """Take platform rake from PvP pot."""
        rake = (pot * Decimal("0.03")).quantize(MONEY_SCALE)
        entry = LedgerEntry(
            debit_account=f"escrow:{game_id}",
            credit_account="rake_revenue",
            amount=rake,
            entry_type="rake",
            reference_id=game_id,
        )
        await self._execute(entry)
        return rake

    async def redeem(self, account_id: str, amount: Decimal, reference_id: str, *, tx=None):
        """Deduct $V for AI inference or store purchase.
        account_id: full prefixed ID."""
        amount = self._money(amount)
        entry = LedgerEntry(
            debit_account=account_id,
            credit_account="store",
            amount=amount,
            entry_type="redeem",
            reference_id=reference_id,
        )
        if tx is not None:
            await self.ensure_demo_admin_floor(
                account_id,
                pending_debit=amount,
                reason="spend",
                tx=tx,
            )
            await self._execute(entry, tx=tx)
            return

        async with self.db.transaction() as own_tx:
            await self.ensure_demo_admin_floor(
                account_id,
                pending_debit=amount,
                reason="spend",
                tx=own_tx,
            )
            await self._execute(entry, tx=own_tx)

    async def fund_from_card(self, account_id: str, amount_v: Decimal, reference_id: str, *, tx=None):
        """Credit wallet from card purchase settlement.

        Uses the fiat_reserve system account as balancing source.
        """
        entry = LedgerEntry(
            debit_account="fiat_reserve",
            credit_account=account_id,
            amount=self._money(amount_v),
            entry_type="fiat_topup",
            reference_id=reference_id,
        )
        await self._execute(entry, tx=tx)

    async def reserve(self, account_id: str, amount: Decimal, reference_id: str, *, tx=None):
        """Reserve funds in escrow for post-settlement charging.
        reference_id should be stable/idempotent (e.g. infer-preauth:<id>)."""
        amount = self._money(amount)
        escrow_account = f"escrow:{reference_id}"
        entry = LedgerEntry(
            debit_account=account_id,
            credit_account=escrow_account,
            amount=amount,
            entry_type="reserve",
            reference_id=reference_id,
        )
        if tx is not None:
            await tx.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                escrow_account,
            )
            await self.ensure_demo_admin_floor(
                account_id,
                pending_debit=amount,
                reason="spend",
                tx=tx,
            )
            await self._execute(entry, tx=tx)
            return

        async with self.db.transaction() as own_tx:
            await own_tx.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                escrow_account,
            )
            await self.ensure_demo_admin_floor(
                account_id,
                pending_debit=amount,
                reason="spend",
                tx=own_tx,
            )
            await self._execute(entry, tx=own_tx)

    async def settle_reservation(
        self,
        account_id: str,
        reservation_ref: str,
        settle_amount: Decimal,
        *,
        tx=None,
    ) -> None:
        """Settle a reservation to store and refund remainder.

        This uses append-only compensating ledger entries:
        - reserve_settle: escrow -> store
        - reserve_refund: escrow -> user/agent account
        """
        settle_amount = self._money(settle_amount)
        escrow_account = f"escrow:{reservation_ref}"
        conn = tx or self.db
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount), 0) AS reserved
            FROM ledger_entries
            WHERE reference_id = $1
              AND entry_type = 'reserve'
              AND credit_account = $2
            """,
            reservation_ref,
            escrow_account,
        )
        reserved = self._money(row["reserved"] if row else Decimal("0"))
        if settle_amount > reserved:
            raise InsufficientBalance(
                f"Settle amount {settle_amount} exceeds reserved amount {reserved}"
            )

        if settle_amount > 0:
            await self._execute(
                LedgerEntry(
                    debit_account=escrow_account,
                    credit_account="store",
                    amount=settle_amount,
                    entry_type="reserve_settle",
                    reference_id=reservation_ref,
                ),
                tx=tx,
            )

        refund = self._money(reserved - settle_amount)
        if refund > 0:
            await self._execute(
                LedgerEntry(
                    debit_account=escrow_account,
                    credit_account=account_id,
                    amount=refund,
                    entry_type="reserve_refund",
                    reference_id=reservation_ref,
                ),
                tx=tx,
            )

    async def get_balance(self, account_id: str) -> Decimal:
        """Get current $V balance for an account.
        account_id: full prefixed ID (e.g., 'user:abc' or 'agent:xyz')."""
        row = await self.db.fetchrow(
            "SELECT balance FROM wallet_accounts WHERE account_id = $1",
            account_id,
        )
        if row is None:
            return Decimal("0")
        return Decimal(str(row["balance"]))

    async def ensure_account(self, account_id: str) -> None:
        """Create wallet account if it doesn't exist.
        account_id: full prefixed ID (e.g., 'user:abc', 'agent:xyz', 'escrow:game1')."""
        await self.db.execute(
            "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) "
            "ON CONFLICT DO NOTHING",
            account_id,
        )

    # Backward-compat aliases (will be removed after all consumers migrate)
    async def ensure_user_account(self, user_id: str) -> None:
        await self.ensure_account(f"user:{user_id}")

    async def ensure_escrow_account(self, game_id: str) -> None:
        await self.ensure_account(f"escrow:{game_id}")

    async def _execute(self, entry: LedgerEntry, *, tx=None):
        """Atomically execute a ledger entry.

        If tx is provided, runs within that existing transaction (caller owns the tx).
        If tx is None, opens its own transaction (self-contained mode).
        """
        async def _do(conn):
            amount = self._money(entry.amount)
            if amount <= 0:
                raise ValueError("Ledger amount must be > 0")

            await conn.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) "
                "ON CONFLICT DO NOTHING",
                entry.debit_account,
            )
            await conn.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) "
                "ON CONFLICT DO NOTHING",
                entry.credit_account,
            )
            await conn.execute(
                "INSERT INTO ledger_entries "
                "(id, debit_account, credit_account, amount, entry_type, reference_id) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                entry.id, entry.debit_account, entry.credit_account,
                amount, entry.entry_type, entry.reference_id,
            )
            await conn.execute(
                "UPDATE wallet_accounts SET balance = balance - $1, updated_at = now() "
                "WHERE account_id = $2",
                amount, entry.debit_account,
            )
            await conn.execute(
                "UPDATE wallet_accounts SET balance = balance + $1, updated_at = now() "
                "WHERE account_id = $2",
                amount, entry.credit_account,
            )

        try:
            if tx is not None:
                await _do(tx)
            else:
                async with self.db.transaction() as own_tx:
                    await _do(own_tx)
        except Exception as e:
            err_str = str(e).lower()
            if "check" in err_str or "violates check" in err_str:
                raise InsufficientBalance(
                    f"Account {entry.debit_account} has insufficient balance "
                    f"for {entry.amount} $V"
                ) from e
            if "unique" in err_str or "duplicate" in err_str:
                # Idempotent — already processed, silently succeed
                return
            raise
