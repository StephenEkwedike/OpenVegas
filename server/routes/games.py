"""Game routes — play games and verify outcomes."""

from __future__ import annotations

import os
import secrets
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.middleware.auth import get_current_user
from server.services.dependencies import get_wallet, get_fraud_engine, get_db
from openvegas.games.horse_racing import HorseRacing
from openvegas.games.skill_shot import SkillShotGame
from openvegas.rng.provably_fair import ProvablyFairRNG
from openvegas.wallet.ledger import InsufficientBalance

router = APIRouter()

GAMES = {
    "horse": HorseRacing,
    "skillshot": SkillShotGame,
}


class PlayRequest(BaseModel):
    amount: float
    type: str = "win"
    horse: int | None = None
    stop_position: int | None = None


class DemoPlayRequest(BaseModel):
    amount: float
    type: str = "win"
    horse: int | None = None
    stop_position: int | None = None


def _is_demo_admin(user_id: str) -> bool:
    if os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0") != "1":
        return False
    allow = {
        x.strip()
        for x in os.getenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "").split(",")
        if x.strip()
    }
    return user_id in allow


def _demo_attempt_cap(game_name: str) -> int:
    default_cap = int(os.getenv("OPENVEGAS_DEMO_MAX_ATTEMPTS", "120"))
    game_cap = int(
        os.getenv(f"OPENVEGAS_DEMO_MAX_ATTEMPTS_{game_name.upper()}", str(default_cap))
    )
    return max(1, min(game_cap, 500))


def _build_bet(game_id: str, user_id: str, req: PlayRequest | DemoPlayRequest) -> dict:
    bet = {
        "game_id": game_id,
        "player_id": user_id,
        "amount": req.amount,
        "type": req.type,
    }
    if req.horse is not None:
        bet["horse"] = req.horse
    if req.stop_position is not None:
        bet["stop_position"] = req.stop_position
    return bet


async def _resolve_result(
    *,
    game: HorseRacing | SkillShotGame,
    bet: dict,
    rng: ProvablyFairRNG,
    client_seed: str,
    nonce: int,
    is_demo: bool,
    game_name: str,
):
    if not is_demo:
        return await game.resolve(bet, rng, client_seed, nonce)

    max_attempts = _demo_attempt_cap(game_name)
    for idx in range(max_attempts):
        candidate_nonce = nonce + idx
        candidate = await game.resolve(bet, rng, client_seed, candidate_nonce)
        if candidate.net > 0:
            candidate.outcome_data = {
                **(candidate.outcome_data or {}),
                "demo_mode": True,
                "demo_forced_win": True,
                "demo_attempts": idx + 1,
                "canonical_fairness": False,
            }
            return candidate
    raise HTTPException(500, "Unable to force demo win within cap")


async def _record_game_history(
    *,
    db,
    game_id: str,
    user_id: str,
    game_name: str,
    result,
    is_demo: bool,
) -> None:
    import json

    await db.execute(
        """INSERT INTO game_history
           (id, user_id, game_type, bet_amount, payout, outcome_data,
            server_seed, server_seed_hash, client_seed, nonce, provably_fair, is_demo)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
        game_id,
        user_id,
        game_name,
        result.bet_amount,
        result.payout,
        json.dumps(result.outcome_data),
        result.server_seed,
        result.server_seed_hash,
        result.client_seed,
        result.nonce,
        False if is_demo else result.provably_fair,
        is_demo,
    )


async def _play_round(
    *,
    game_name: str,
    req: PlayRequest | DemoPlayRequest,
    user: dict,
    is_demo: bool,
):
    if game_name not in GAMES:
        raise HTTPException(400, f"Unknown game: {game_name}")

    if not is_demo:
        fraud = get_fraud_engine()
        try:
            await fraud.check_bet(user["user_id"])
        except Exception as e:
            raise HTTPException(429, str(e))

    wallet = get_wallet()
    db = get_db()
    game_cls = GAMES[game_name]
    game = game_cls()

    game_id = str(uuid.uuid4())
    client_seed = secrets.token_hex(16)
    nonce = 0

    bet = _build_bet(game_id, user["user_id"], req)

    if not await game.validate_bet(bet):
        raise HTTPException(400, "Invalid bet")

    # Escrow the bet
    bet_amount = Decimal(str(req.amount))
    account_id = f"user:{user['user_id']}"
    demo_ref = f"demo:{game_id}" if is_demo else game_id
    try:
        await wallet.ensure_escrow_account(game_id)
        await wallet.place_bet(
            account_id,
            bet_amount,
            game_id,
            entry_type="demo_play" if is_demo else "bet",
            reference_id=demo_ref,
        )
    except InsufficientBalance as e:
        raise HTTPException(400, str(e))

    # Resolve
    rng = ProvablyFairRNG()
    rng.new_round()
    result = await _resolve_result(
        game=game,
        bet=bet,
        rng=rng,
        client_seed=client_seed,
        nonce=nonce,
        is_demo=is_demo,
        game_name=game_name,
    )

    # Settle
    if result.payout > 0:
        await wallet.settle_win(
            account_id,
            result.payout,
            game_id,
            entry_type="demo_win" if is_demo else "win",
            reference_id=demo_ref,
        )
        remaining = bet_amount - result.payout
        if remaining > 0:
            await wallet.settle_loss(
                game_id,
                remaining,
                entry_type="demo_loss" if is_demo else "loss",
                reference_id=demo_ref,
            )
    else:
        await wallet.settle_loss(
            game_id,
            bet_amount,
            entry_type="demo_loss" if is_demo else "loss",
            reference_id=demo_ref,
        )

    await _record_game_history(
        db=db,
        game_id=game_id,
        user_id=user["user_id"],
        game_name=game_name,
        result=result,
        is_demo=is_demo,
    )

    response = {
        "game_id": game_id,
        "bet_amount": str(result.bet_amount),
        "payout": str(result.payout),
        "net": str(result.net),
        "outcome_data": result.outcome_data,
        "server_seed_hash": result.server_seed_hash,
        "provably_fair": (False if is_demo else result.provably_fair),
    }
    if is_demo:
        response["demo_mode"] = True
        response["canonical"] = False
    return response


@router.post("/{game_name}/play")
async def play_game(
    game_name: str,
    req: PlayRequest,
    user: dict = Depends(get_current_user),
):
    return await _play_round(game_name=game_name, req=req, user=user, is_demo=False)


@router.post("/{game_name}/play-demo")
async def play_game_demo(
    game_name: str,
    req: DemoPlayRequest,
    user: dict = Depends(get_current_user),
):
    if not _is_demo_admin(user["user_id"]):
        raise HTTPException(403, "Demo mode not allowed")
    return await _play_round(game_name=game_name, req=req, user=user, is_demo=True)


@router.get("/verify/{game_id}")
async def verify_game(
    game_id: str,
    user: dict = Depends(get_current_user),
):
    db = get_db()
    row = await db.fetchrow(
        "SELECT * FROM game_history WHERE id = $1 AND user_id = $2",
        game_id, user["user_id"],
    )
    if not row:
        raise HTTPException(404, "Game not found")
    if row["is_demo"]:
        raise HTTPException(400, "Demo round: use /games/demo/verify/{game_id}")

    return {
        "game_id": str(row["id"]),
        "server_seed": row["server_seed"],
        "server_seed_hash": row["server_seed_hash"],
        "client_seed": row["client_seed"],
        "nonce": row["nonce"],
        "provably_fair": row["provably_fair"],
    }


@router.get("/demo/verify/{game_id}")
async def verify_demo_game(
    game_id: str,
    user: dict = Depends(get_current_user),
):
    if not _is_demo_admin(user["user_id"]):
        raise HTTPException(403, "Demo verification not allowed")

    db = get_db()
    row = await db.fetchrow(
        """SELECT id, user_id, is_demo, outcome_data, server_seed_hash, nonce
           FROM game_history
           WHERE id = $1 AND user_id = $2 AND is_demo = TRUE""",
        game_id,
        user["user_id"],
    )
    if not row:
        raise HTTPException(404, "Demo round not found")

    return {
        "game_id": str(row["id"]),
        "demo_mode": True,
        "canonical": False,
        "server_seed_hash": row["server_seed_hash"],
        "nonce": row["nonce"],
        "note": "Demo verification surface (non-canonical; do not use for real fairness stats)",
    }
