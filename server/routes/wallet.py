"""Wallet routes — balance and history."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends

from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_wallet, get_db

router = APIRouter()
V_SCALE = Decimal("0.000001")
USD_SCALE = Decimal("0.01")


def _money(value: Decimal | str | float) -> Decimal:
    return Decimal(str(value)).quantize(V_SCALE)


def _usd(value: Decimal | str | float) -> Decimal:
    return Decimal(str(value)).quantize(USD_SCALE)


def _v_per_usd() -> Decimal:
    return _money(os.getenv("V_PER_USD", "100"))


def _warning_floor_usd() -> Decimal:
    return _usd(os.getenv("TOPUP_LOW_BALANCE_FLOOR_USD", "5.00"))


def _critical_floor_v() -> Decimal:
    return _money(os.getenv("TOPUP_CRITICAL_BALANCE_FLOOR_V", "90"))


def _starter_grant_v() -> Decimal:
    return _money(os.getenv("STARTER_GRANT_V", "100"))


def _balance_state(balance_v: Decimal, v_per_usd: Decimal) -> str:
    warning_floor = _warning_floor_usd()
    critical_floor = _critical_floor_v()
    usd_equiv = (balance_v / v_per_usd).quantize(USD_SCALE) if v_per_usd > 0 else Decimal("0.00")
    if balance_v <= critical_floor:
        return "critical"
    if usd_equiv <= warning_floor:
        return "warning"
    return "ok"


def _render_balance_state(balance_v: Decimal) -> dict[str, Any]:
    v_per_usd = _v_per_usd()
    state = _balance_state(balance_v, v_per_usd)
    return {
        "balance_state": state,
        "warning_floor_usd": format(_warning_floor_usd(), "f"),
        "critical_floor_v": format(_critical_floor_v(), "f"),
        "topup_recommended": state in {"warning", "critical"},
    }


async def _resolve_user_tier(db: Any, user_id: str) -> str:
    # Precedence follows product request:
    # 1) personal active subscription -> subscribed
    # 2) member of actively sponsored org -> team
    # 3) otherwise -> free
    personal_row = await db.fetchrow(
        """
        SELECT EXISTS (
          SELECT 1
          FROM user_subscriptions
          WHERE user_id = $1
            AND has_active_subscription = TRUE
        ) AS has_personal
        """,
        user_id,
    )
    if bool(personal_row and personal_row.get("has_personal")):
        return "subscribed"

    team_row = await db.fetchrow(
        """
        SELECT EXISTS (
          SELECT 1
          FROM org_members m
          JOIN org_sponsorships s ON s.org_id = m.org_id
          WHERE m.user_id = $1
            AND COALESCE(m.status, 'active') = 'active'
            AND (
              COALESCE(s.has_active_subscription, FALSE) = TRUE
              OR COALESCE(s.stripe_subscription_status, '') IN ('active', 'trialing')
            )
        ) AS has_team
        """,
        user_id,
    )
    if bool(team_row and team_row.get("has_team")):
        return "team"

    return "free"


async def _starter_grant_received(db: Any, user_id: str) -> bool:
    row = await db.fetchrow(
        """
        SELECT user_id
        FROM user_starter_grants
        WHERE user_id = $1
        """,
        user_id,
    )
    return bool(row)


async def _apply_starter_grant_once(db: Any, wallet: Any, user_id: str) -> dict[str, Any]:
    account_id = f"user:{user_id}"
    amount_v = _starter_grant_v()
    grant_version = "v1"
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) ON CONFLICT DO NOTHING",
            account_id,
        )
        existing = await tx.fetchrow(
            """
            SELECT user_id, granted_amount_v
            FROM user_starter_grants
            WHERE user_id = $1
            FOR UPDATE
            """,
            user_id,
        )
        if existing:
            emit_metric("starter_grant_attempt_total", {"outcome": "already_applied", "reason": "exists"})
            existing_amount = existing["granted_amount_v"] if "granted_amount_v" in existing else amount_v
            return {
                "applied": False,
                "amount_v": _money(existing_amount or amount_v),
                "grant_version": grant_version,
            }

        await tx.execute(
            """
            INSERT INTO user_starter_grants (user_id, granted_amount_v, grant_version)
            VALUES ($1, $2, $3)
            """,
            user_id,
            amount_v,
            grant_version,
        )
        await wallet.fund_from_card(
            account_id=account_id,
            amount_v=amount_v,
            reference_id=f"starter_grant:{user_id}:{grant_version}",
            entry_type="starter_grant",
            tx=tx,
        )
        emit_metric("starter_grant_attempt_total", {"outcome": "applied", "reason": "first_bootstrap"})
        return {"applied": True, "amount_v": amount_v, "grant_version": grant_version}


@router.get("/exchange-rate")
async def get_exchange_rate():
    v_per_usd = Decimal(str(os.getenv("V_PER_USD", "100"))).quantize(Decimal("0.000001"))
    usd_per_v = (Decimal("1") / v_per_usd).quantize(Decimal("0.01")) if v_per_usd > 0 else Decimal("0.00")
    return {
        "v_per_usd": format(v_per_usd, "f"),
        "usd_per_v": format(usd_per_v, "f"),
    }


@router.get("/balance")
async def get_balance(user: dict = Depends(get_current_user)):
    wallet = get_wallet()
    db = get_db()
    account_id = f"user:{user['user_id']}"
    balance = await wallet.get_balance(account_id)
    tier = await _resolve_user_tier(db, str(user["user_id"]))
    state = _render_balance_state(_money(balance))
    emit_metric("wallet_balance_state_total", {"state": state["balance_state"]})
    starter_received = await _starter_grant_received(db, str(user["user_id"]))
    return {
        "balance": format(_money(balance), "f"),
        "tier": tier,
        "starter_grant_received": bool(starter_received),
        **state,
        "lifetime_minted": "0.00",
        "lifetime_won": "0.00",
    }


@router.post("/bootstrap")
async def wallet_bootstrap(user: dict = Depends(get_current_user)):
    db = get_db()
    wallet = get_wallet()
    result = await _apply_starter_grant_once(db, wallet, str(user["user_id"]))
    account_id = f"user:{user['user_id']}"
    balance = _money(await wallet.get_balance(account_id))
    tier = await _resolve_user_tier(db, str(user["user_id"]))
    state = _render_balance_state(balance)
    emit_metric("wallet_balance_state_total", {"state": state["balance_state"]})
    return {
        "balance": format(balance, "f"),
        "tier": tier,
        "starter_grant_applied": bool(result["applied"]),
        **state,
    }


@router.get("/history")
async def get_history(user: dict = Depends(get_current_user), include_demo: bool = False):
    db = get_db()
    account_id = f"user:{user['user_id']}"
    rows = []
    if not include_demo:
        try:
            projection_rows = await db.fetch(
                """
                SELECT event_type, display_amount_v, occurred_at, request_id, display_status, metadata_json
                FROM wallet_history_projection
                WHERE user_id = $1
                ORDER BY occurred_at DESC
                LIMIT 50
                """,
                user["user_id"],
            )
            if projection_rows:
                entries = [
                    {
                        "entry_type": r.get("event_type", ""),
                        "amount": str(r.get("display_amount_v", "")),
                        "reference_id": str(r.get("request_id", "") or ""),
                        "created_at": str(r.get("occurred_at", "")),
                        "status": r.get("display_status", ""),
                        "metadata": r.get("metadata_json", {}),
                    }
                    for r in projection_rows
                ]
                return {"entries": entries}
        except Exception:
            # Fallback to raw ledger history where projection is unavailable.
            pass

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
