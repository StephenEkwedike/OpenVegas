"""Game routes — play games, horse quote pricing, and verify outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from openvegas.casino.constants import min_game_wager_v
from server.middleware.auth import get_current_user
from server.services.dependencies import get_wallet, get_fraud_engine, get_db
from server.services.demo_admin import demo_mode_enabled, is_demo_admin_user
from openvegas.games.horse_racing import HorseRacing
from openvegas.games.skill_shot import SkillShotGame
from openvegas.rng.provably_fair import ProvablyFairRNG
from openvegas.wallet.ledger import InsufficientBalance

router = APIRouter()

GAMES = {
    "horse": HorseRacing,
    "skillshot": SkillShotGame,
}

MONEY_QUANT = Decimal("0.000001")


@dataclass
class SerializedResponse:
    status_code: int
    body_text: str
    content_type: str = "application/json"

    def to_response(self) -> Response:
        return Response(
            content=self.body_text,
            status_code=self.status_code,
            media_type=self.content_type,
        )


@dataclass
class _IdemState:
    row_id: str
    replay: SerializedResponse | None


class PlayRequest(BaseModel):
    amount: float | None = None
    type: str = "win"
    horse: int | None = None
    stop_position: int | None = None
    quote_id: str | None = None
    idempotency_key: str | None = None


class DemoPlayRequest(BaseModel):
    amount: float | None = None
    type: str = "win"
    horse: int | None = None
    stop_position: int | None = None
    quote_id: str | None = None
    idempotency_key: str | None = None


class HorseQuoteRequest(BaseModel):
    bet_type: str = "win"
    budget_v: str
    idempotency_key: str


def _q6_down(value: Decimal | str | float) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_DOWN)


def _money_text(value: Decimal | str | float) -> str:
    return f"{_q6_down(value):.6f}"


def _canonical_json(value: Any) -> str:
    def norm(v: Any):
        if isinstance(v, Decimal):
            return format(v, "f")
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v.keys())}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v

    return json.dumps(norm(value), sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _json_response(status_code: int, payload: dict) -> SerializedResponse:
    return SerializedResponse(
        status_code=status_code,
        body_text=json.dumps(payload, separators=(",", ":")),
    )


def _error_response(status_code: int, code: str, detail: str) -> SerializedResponse:
    return _json_response(
        status_code,
        {
            "error": code,
            "detail": detail,
            "valid_actions": [],
        },
    )


def _is_demo_admin(user_id: str) -> bool:
    if not demo_mode_enabled():
        return False
    return is_demo_admin_user(user_id)


def _demo_attempt_cap(game_name: str) -> int:
    default_cap = int(os.getenv("OPENVEGAS_DEMO_MAX_ATTEMPTS", "120"))
    game_cap = int(
        os.getenv(f"OPENVEGAS_DEMO_MAX_ATTEMPTS_{game_name.upper()}", str(default_cap))
    )
    return max(1, min(game_cap, 500))


def _horse_quote_ttl_sec() -> int:
    return max(5, min(int(os.getenv("OPENVEGAS_HORSE_QUOTE_TTL_SEC", "60")), 3600))


def _horse_quote_max_active_per_user() -> int:
    return max(1, int(os.getenv("OPENVEGAS_HORSE_QUOTE_MAX_ACTIVE_PER_USER", "5")))


def _horse_quote_max_create_per_min() -> int:
    return max(1, int(os.getenv("OPENVEGAS_HORSE_QUOTE_MAX_CREATE_PER_MIN", "30")))


def _quote_bet_type(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"win", "place", "show"}:
        raise HTTPException(400, "Invalid bet_type (must be win/place/show)")
    return normalized


def _quote_budget(value: str) -> Decimal:
    try:
        amount = _q6_down(Decimal(str(value)))
    except InvalidOperation as e:
        raise HTTPException(400, "Invalid budget_v") from e
    if amount <= 0:
        raise HTTPException(400, "budget_v must be > 0")
    return amount


async def _idem_begin(
    tx,
    *,
    user_id: str,
    scope: str,
    idempotency_key: str,
    payload_hash: str,
) -> _IdemState:
    await tx.execute(
        """
        INSERT INTO horse_quote_idempotency
          (user_id, scope, idempotency_key, payload_hash)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, scope, idempotency_key) DO NOTHING
        """,
        user_id,
        scope,
        idempotency_key,
        payload_hash,
    )
    row = await tx.fetchrow(
        """
        SELECT id, payload_hash, response_status, response_body_text, response_content_type
        FROM horse_quote_idempotency
        WHERE user_id = $1 AND scope = $2 AND idempotency_key = $3
        FOR UPDATE
        """,
        user_id,
        scope,
        idempotency_key,
    )
    if not row:
        raise HTTPException(500, "Idempotency row not found")

    if str(row["payload_hash"]) != payload_hash:
        raise HTTPException(409, "idempotency_conflict")

    if row["response_status"] is not None and row["response_body_text"] is not None:
        return _IdemState(
            row_id=str(row["id"]),
            replay=SerializedResponse(
                status_code=int(row["response_status"]),
                body_text=str(row["response_body_text"]),
                content_type=str(row["response_content_type"] or "application/json"),
            ),
        )

    return _IdemState(row_id=str(row["id"]), replay=None)


async def _idem_persist(
    tx,
    *,
    row_id: str,
    response: SerializedResponse,
    resource_id: str | None = None,
) -> None:
    await tx.execute(
        """
        UPDATE horse_quote_idempotency
        SET response_status = $1,
            response_body_text = $2,
            response_content_type = $3,
            resource_id = COALESCE($4::uuid, resource_id),
            updated_at = now()
        WHERE id = $5
        """,
        response.status_code,
        response.body_text,
        response.content_type,
        resource_id,
        row_id,
    )


def _parse_horses_json(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return [dict(x) for x in raw if isinstance(x, dict)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [dict(x) for x in parsed if isinstance(x, dict)]
        except Exception:
            return []
    return []


def _build_bet(game_id: str, user_id: str, req: PlayRequest | DemoPlayRequest) -> dict:
    if req.amount is None:
        raise HTTPException(400, "amount is required for this game mode")

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


def _horse_hit(outcome_data: dict, bet_type: str, horse_number: int) -> bool:
    finish = [int(x) for x in (outcome_data.get("finish_order_nums") or [])]
    if not finish:
        return False
    if bet_type == "win":
        return horse_number == finish[0]
    if bet_type == "place":
        return horse_number in finish[:2]
    if bet_type == "show":
        return horse_number in finish[:3]
    return False


def _horse_effective_multiplier(odds: Decimal, bet_type: str) -> Decimal:
    if bet_type == "win":
        return _q6_down(odds)
    if bet_type == "place":
        return _q6_down(odds / Decimal("2"))
    return _q6_down(odds / Decimal("3"))


def _build_horse_pricing_board(*, game: HorseRacing, budget_v: Decimal, bet_type: str) -> list[dict]:
    rows: list[dict] = []
    for horse in game.horses:
        odds = _q6_down(Decimal(str(horse.odds)))
        effective_multiplier = _horse_effective_multiplier(odds, bet_type)

        selectable = False
        unit_price = Decimal("0")
        max_units = 0
        debit_v = Decimal("0")
        payout_if_hit_v = Decimal("0")

        if effective_multiplier > 0:
            unit_price = _q6_down(Decimal("1") / effective_multiplier)
            if unit_price > 0:
                max_units = int((budget_v / unit_price).to_integral_value(rounding=ROUND_DOWN))
                selectable = max_units > 0
                if selectable:
                    debit_v = _q6_down(unit_price * Decimal(max_units))
                    payout_if_hit_v = _q6_down(Decimal(max_units))

        rows.append(
            {
                "number": int(horse.number),
                "name": str(horse.name),
                "odds": _money_text(odds),
                "effective_multiplier": _money_text(effective_multiplier),
                "unit_price_v": _money_text(unit_price),
                "max_units": int(max_units),
                "debit_v": _money_text(debit_v),
                "payout_if_hit_v": _money_text(payout_if_hit_v),
                "selectable": bool(selectable),
            }
        )

    rows.sort(key=lambda x: x["number"])
    return rows


async def _create_horse_quote(req: HorseQuoteRequest, user: dict) -> SerializedResponse:
    user_id = str(user["user_id"])
    bet_type = _quote_bet_type(req.bet_type)
    budget_v = _quote_budget(req.budget_v)
    idempotency_key = str(req.idempotency_key or "").strip()
    if not idempotency_key:
        raise HTTPException(400, "idempotency_key is required")

    payload_hash = _sha256_text(
        _canonical_json(
            {
                "bet_type": bet_type,
                "budget_v": _money_text(budget_v),
            }
        )
    )

    db = get_db()
    async with db.transaction() as tx:
        idem = await _idem_begin(
            tx,
            user_id=user_id,
            scope="quote_create",
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if idem.replay is not None:
            return idem.replay

        active_cap = _horse_quote_max_active_per_user()
        active = await tx.fetchrow(
            """
            SELECT COUNT(*) AS c
            FROM horse_quotes
            WHERE user_id = $1
              AND consumed_at IS NULL
              AND expires_at > now()
            """,
            user_id,
        )
        if int(active["c"] or 0) >= active_cap:
            out = _error_response(429, "rate_limited", "Too many active horse quotes")
            await _idem_persist(tx, row_id=idem.row_id, response=out)
            return out

        minute_cap = _horse_quote_max_create_per_min()
        per_min = await tx.fetchrow(
            """
            SELECT COUNT(*) AS c
            FROM horse_quotes
            WHERE user_id = $1
              AND created_at >= now() - interval '1 minute'
            """,
            user_id,
        )
        if int(per_min["c"] or 0) >= minute_cap:
            out = _error_response(429, "rate_limited", "Horse quote creation rate limit exceeded")
            await _idem_persist(tx, row_id=idem.row_id, response=out)
            return out

        rng = ProvablyFairRNG()
        rng.new_round()
        game = HorseRacing()
        game.setup_race(rng, secrets.token_hex(16), 0)

        horses = _build_horse_pricing_board(game=game, budget_v=budget_v, bet_type=bet_type)
        if all(not bool(h.get("selectable")) for h in horses):
            out = _error_response(400, "budget_too_low_for_any_position", "Budget too low for any horse position")
            await _idem_persist(tx, row_id=idem.row_id, response=out)
            return out

        board_hash = _sha256_text(
            _canonical_json(
                {
                    "bet_type": bet_type,
                    "budget_v": _money_text(budget_v),
                    "horses": horses,
                }
            )
        )

        ttl_sec = _horse_quote_ttl_sec()
        row = await tx.fetchrow(
            """
            INSERT INTO horse_quotes
              (user_id, bet_type, budget_v, horses_json, board_hash, expires_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, now() + make_interval(secs => $6::int))
            RETURNING id, expires_at
            """,
            user_id,
            bet_type,
            budget_v,
            json.dumps(horses),
            board_hash,
            ttl_sec,
        )

        out = _json_response(
            200,
            {
                "quote_id": str(row["id"]),
                "expires_at": row["expires_at"].isoformat(),
                "bet_type": bet_type,
                "budget_v": _money_text(budget_v),
                "board_hash": board_hash,
                "horses": horses,
            },
        )
        await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=str(row["id"]))
        return out


async def _play_horse_quote(
    *,
    req: PlayRequest | DemoPlayRequest,
    user: dict,
    is_demo: bool,
) -> SerializedResponse:
    user_id = str(user["user_id"])
    quote_id = str(req.quote_id or "").strip()
    idempotency_key = str(req.idempotency_key or "").strip()

    if not quote_id:
        raise HTTPException(400, "quote_id is required")
    if req.horse is None:
        raise HTTPException(400, "horse is required")
    if not idempotency_key:
        raise HTTPException(400, "idempotency_key is required")

    payload_hash = _sha256_text(
        _canonical_json(
            {
                "quote_id": quote_id,
                "horse": int(req.horse),
            }
        )
    )

    db = get_db()
    wallet = get_wallet()
    account_id = f"user:{user_id}"

    async with db.transaction() as tx:
        idem = await _idem_begin(
            tx,
            user_id=user_id,
            scope="quote_play",
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if idem.replay is not None:
            return idem.replay

        quote = await tx.fetchrow(
            """
            SELECT *, (expires_at <= now()) AS is_expired
            FROM horse_quotes
            WHERE id = $1 AND user_id = $2
            FOR UPDATE
            """,
            quote_id,
            user_id,
        )
        if not quote:
            out = _error_response(404, "quote_not_found", "Horse quote not found")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        if quote["consumed_at"] is not None:
            prior = await tx.fetchrow(
                """
                SELECT response_status, response_body_text, response_content_type
                FROM horse_quote_idempotency
                WHERE user_id = $1
                  AND scope = 'quote_play'
                  AND resource_id = $2::uuid
                  AND payload_hash = $3
                  AND response_status IS NOT NULL
                  AND response_body_text IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id,
                quote_id,
                payload_hash,
            )
            if prior:
                out = SerializedResponse(
                    status_code=int(prior["response_status"]),
                    body_text=str(prior["response_body_text"]),
                    content_type=str(prior["response_content_type"] or "application/json"),
                )
                await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
                return out

            out = _error_response(409, "quote_already_consumed", "Quote already consumed")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        if bool(quote["is_expired"]):
            out = _error_response(409, "quote_expired", "Horse quote expired")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        horses = _parse_horses_json(quote["horses_json"])
        selected = next((h for h in horses if int(h.get("number", -1)) == int(req.horse)), None)
        if selected is None:
            out = _error_response(400, "invalid_horse_selection", "Selected horse not in quote board")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        if not bool(selected.get("selectable", False)):
            out = _error_response(409, "quote_position_unselectable", "Selected horse position is not selectable")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        bet_type = str(quote["bet_type"])
        debit_v = _q6_down(Decimal(str(selected.get("debit_v", "0"))))
        payout_if_hit_v = _q6_down(Decimal(str(selected.get("payout_if_hit_v", "0"))))
        if debit_v <= 0:
            out = _error_response(409, "quote_position_unselectable", "Selected horse position has zero debit")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        game = HorseRacing()
        game_id = str(uuid.uuid4())
        bet = {
            "game_id": game_id,
            "player_id": user_id,
            "amount": float(debit_v),
            "type": bet_type,
            "horse": int(req.horse),
        }
        if not await game.validate_bet(bet):
            out = _error_response(400, "invalid_bet", "Invalid horse bet")
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        demo_ref = f"demo:{game_id}" if is_demo else game_id

        try:
            await tx.execute(
                "INSERT INTO wallet_accounts (account_id, balance) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                f"escrow:{game_id}",
            )
            await wallet.place_bet(
                account_id,
                debit_v,
                game_id,
                tx=tx,
                entry_type="demo_play" if is_demo else "bet",
                reference_id=demo_ref,
            )
        except InsufficientBalance as e:
            out = _error_response(400, "insufficient_balance", str(e))
            await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
            return out

        rng = ProvablyFairRNG()
        rng.new_round()
        client_seed = secrets.token_hex(16)
        nonce = 0
        result = await _resolve_result(
            game=game,
            bet=bet,
            rng=rng,
            client_seed=client_seed,
            nonce=nonce,
            is_demo=is_demo,
            game_name="horse",
        )

        hit = _horse_hit(result.outcome_data or {}, bet_type, int(req.horse))
        payout_v = payout_if_hit_v if hit else Decimal("0")
        net_v = _q6_down(payout_v - debit_v)

        result.bet_amount = _q6_down(debit_v)
        result.payout = _q6_down(payout_v)
        result.net = net_v
        result.outcome_data = {
            **(result.outcome_data or {}),
            "quote_id": quote_id,
            "board_hash": str(quote["board_hash"]),
            "bet_type": bet_type,
            "selected_horse": int(req.horse),
            "selected_horse_odds": str(selected.get("odds")),
            "effective_multiplier": str(selected.get("effective_multiplier")),
            "unit_price_v": str(selected.get("unit_price_v")),
            "max_units": int(selected.get("max_units", 0)),
            "debit_v": _money_text(debit_v),
            "payout_if_hit_v": _money_text(payout_if_hit_v),
            "quote_mode": True,
        }

        if result.payout > 0:
            await wallet.settle_win(
                account_id,
                result.payout,
                game_id,
                tx=tx,
                entry_type="demo_win" if is_demo else "win",
                reference_id=demo_ref,
            )
            remaining = _q6_down(debit_v - result.payout)
            if remaining > 0:
                await wallet.settle_loss(
                    game_id,
                    remaining,
                    tx=tx,
                    entry_type="demo_loss" if is_demo else "loss",
                    reference_id=demo_ref,
                )
        else:
            await wallet.settle_loss(
                game_id,
                debit_v,
                tx=tx,
                entry_type="demo_loss" if is_demo else "loss",
                reference_id=demo_ref,
            )

        await _record_game_history(
            db=tx,
            game_id=game_id,
            user_id=user_id,
            game_name="horse",
            result=result,
            is_demo=is_demo,
        )

        await tx.execute(
            """
            UPDATE horse_quotes
            SET consumed_at = now(), consumed_game_id = $2::uuid, updated_at = now()
            WHERE id = $1::uuid
            """,
            quote_id,
            game_id,
        )

        payload = {
            "game_id": game_id,
            "quote_id": quote_id,
            "bet_amount": _money_text(result.bet_amount),
            "payout": _money_text(result.payout),
            "net": _money_text(result.net),
            "outcome_data": result.outcome_data,
            "server_seed_hash": result.server_seed_hash,
            "provably_fair": (False if is_demo else result.provably_fair),
        }
        if is_demo:
            payload["demo_mode"] = True
            payload["canonical"] = False

        out = _json_response(200, payload)
        await _idem_persist(tx, row_id=idem.row_id, response=out, resource_id=quote_id)
        return out


async def _play_round_legacy(
    *,
    game_name: str,
    req: PlayRequest | DemoPlayRequest,
    user: dict,
    is_demo: bool,
):
    if game_name not in GAMES:
        raise HTTPException(400, f"Unknown game: {game_name}")

    if req.amount is None:
        raise HTTPException(400, "amount is required")

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
    if not is_demo and bet_amount < min_game_wager_v():
        raise HTTPException(400, f"Wager must be at least {min_game_wager_v()} $V")
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


@router.post("/horse/quotes")
async def create_horse_quote(
    req: HorseQuoteRequest,
    user: dict = Depends(get_current_user),
):
    out = await _create_horse_quote(req, user)
    return out.to_response()


@router.post("/{game_name}/play")
async def play_game(
    game_name: str,
    req: PlayRequest,
    user: dict = Depends(get_current_user),
):
    if req.quote_id is not None:
        if game_name != "horse":
            raise HTTPException(400, "quote_id mode is only supported for horse")
        out = await _play_horse_quote(req=req, user=user, is_demo=False)
        return out.to_response()

    return await _play_round_legacy(game_name=game_name, req=req, user=user, is_demo=False)


@router.post("/{game_name}/play-demo")
async def play_game_demo(
    game_name: str,
    req: DemoPlayRequest,
    user: dict = Depends(get_current_user),
):
    if not _is_demo_admin(user["user_id"]):
        raise HTTPException(403, "Demo mode not allowed")

    if req.quote_id is not None:
        if game_name != "horse":
            raise HTTPException(400, "quote_id mode is only supported for horse")
        out = await _play_horse_quote(req=req, user=user, is_demo=True)
        return out.to_response()

    return await _play_round_legacy(game_name=game_name, req=req, user=user, is_demo=True)


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
