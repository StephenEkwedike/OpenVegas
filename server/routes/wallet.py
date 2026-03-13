"""Wallet routes — balance and history."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends

from server.middleware.auth import get_current_user
from server.services.dependencies import get_wallet, get_db

router = APIRouter()


@router.get("/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    wallet = get_wallet()
    account_id = f"user:{user['user_id']}"
    await wallet.ensure_demo_admin_floor(
        account_id=account_id,
        pending_debit=Decimal("0"),
        reason="read",
    )
    balance = await wallet.get_balance(account_id)
    return {
        "balance": str(balance),
        "tier": "free",
        "lifetime_minted": "0.00",
        "lifetime_won": "0.00",
    }


@router.get("/history")
async def get_history(user: dict = Depends(get_current_user), include_demo: bool = False):
    db = get_db()
    account_id = f"user:{user['user_id']}"
    if include_demo:
        rows = await db.fetch(
            """SELECT * FROM ledger_entries
               WHERE debit_account = $1 OR credit_account = $1
               ORDER BY created_at DESC LIMIT 50""",
            account_id,
        )
    else:
        rows = await db.fetch(
            """SELECT * FROM ledger_entries
               WHERE (debit_account = $1 OR credit_account = $1)
                 AND entry_type NOT IN (
                   'demo_play', 'demo_win', 'demo_loss', 'demo_autofund',
                   'demo_human_casino_play', 'demo_human_casino_win', 'demo_human_casino_loss'
                 )
                 AND debit_account <> 'demo_reserve'
                 AND credit_account <> 'demo_reserve'
               ORDER BY created_at DESC LIMIT 50""",
            account_id,
        )
    entries = [
        {
            "entry_type": r.get("entry_type", ""),
            "amount": str(r.get("amount", "")),
            "reference_id": r.get("reference_id", ""),
            "created_at": str(r.get("created_at", "")),
        }
        for r in rows
    ]
    return {"entries": entries}
