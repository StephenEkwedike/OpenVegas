"""Wallet routes — balance and history."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server.middleware.auth import get_current_user
from server.services.dependencies import get_wallet, get_db

router = APIRouter()


@router.get("/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    wallet = get_wallet()
    balance = await wallet.get_balance(f"user:{user['user_id']}")
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
                 AND entry_type NOT IN ('demo_play', 'demo_win', 'demo_loss')
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
