"""Human casino service — session management, round lifecycle, and settlement."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from openvegas.casino.blackjack import BlackjackGame, hand_value
from openvegas.casino.poker import PokerGame
from openvegas.casino.roulette import RouletteGame
from openvegas.casino.slots import SlotsGame
from openvegas.casino.baccarat import BaccaratGame
from openvegas.casino.constants import HIDDEN_CARD_TOKEN, min_game_wager_v
from openvegas.rng.provably_fair import ProvablyFairRNG
from openvegas.wallet.ledger import InsufficientBalance, WalletService

HUMAN_CASINO_GAMES = {
    "blackjack": BlackjackGame(),
    "roulette": RouletteGame(),
    "slots": SlotsGame(),
    "poker": PokerGame(),
    "baccarat": BaccaratGame(),
}


@dataclass
class SerializedHTTPResponse:
    status_code: int
    body_text: str


@dataclass
class _IdemState:
    row_id: str
    replay: SerializedHTTPResponse | None


def _json_text(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _canonical_hash(payload: dict) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _state_error(error: str, current_state: str, valid_actions: list[str] | None = None) -> SerializedHTTPResponse:
    return SerializedHTTPResponse(
        status_code=409,
        body_text=_json_text(
            {
                "error": error,
                "current_state": current_state,
                "valid_actions": valid_actions or [],
            }
        ),
    )


def _public_state_for_game(game_code: str, state: dict, current_state: str) -> dict:
    public_state = {k: v for k, v in state.items() if not str(k).startswith("_")}
    # Never expose future-card information.
    public_state.pop("deck", None)
    public_state.pop("shoe", None)
    if game_code == "blackjack" and current_state != "resolved":
        dealer = public_state.get("dealer")
        if isinstance(dealer, list) and len(dealer) >= 2:
            public_state["dealer"] = [dealer[0], HIDDEN_CARD_TOKEN]
    if game_code == "poker" and current_state != "resolved":
        dealer = public_state.get("dealer")
        if isinstance(dealer, list) and dealer:
            public_state["dealer"] = [HIDDEN_CARD_TOKEN for _ in dealer]
    return public_state


def _parse_state(raw: Any) -> dict:
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _session_ttl_seconds() -> int:
    return max(60, int(os.getenv("CASINO_HUMAN_SESSION_TTL_SECONDS", "1800")))


def _round_ttl_seconds() -> int:
    return max(30, int(os.getenv("CASINO_HUMAN_ROUND_TTL_SECONDS", "600")))


def _demo_attempt_cap(game_code: str) -> int:
    default_cap = int(os.getenv("OPENVEGAS_DEMO_MAX_ATTEMPTS", "120"))
    game_cap = int(
        os.getenv(f"OPENVEGAS_DEMO_MAX_ATTEMPTS_{game_code.upper()}", str(default_cap))
    )
    return max(1, min(game_cap, 500))


class HumanCasinoService:
    def __init__(self, db: Any, wallet: WalletService):
        self.db = db
        self.wallet = wallet

    async def _expire_unfinished_round(self, tx: Any, row: Any) -> bool:
        """Caller holds the round lock. A decided outcome is never an expiry loss."""
        status = str(row["status"])
        if (status not in {"awaiting_action", "expired"} or not row["expires_at"]
                or row["expires_at"] > _now_utc()):
            return False
        state = _parse_state(row["state_json"])
        game = HUMAN_CASINO_GAMES.get(str(row["game_code"]))
        if game is None or game.is_resolved(state):
            return False
        await self._settle_unfinished_loss(tx, row, terminal_status="expired")
        return True

    async def _settle_unfinished_loss(self, tx: Any, row: Any, *, terminal_status: str) -> None:
        """Round lock held; explicit forfeit and expiry share one ledger settlement."""
        state = _parse_state(row["state_json"])
        round_id = str(row["id"])
        if await tx.fetchrow("SELECT 1 FROM human_casino_payouts WHERE round_id = $1", round_id):
            return
        wager = Decimal(str(row["wager_v"]))
        await self.wallet.settle_loss(round_id, wager, tx=tx,
                                      entry_type="human_casino_loss", reference_id=round_id)
        await tx.execute(
            """INSERT INTO human_casino_payouts (round_id, wager_v, payout_v, net_v, ledger_ref)
               VALUES ($1, $2, $3, $4, $5)""",
            round_id, wager, Decimal("0"), -wager,
            f"human_casino_{'expiry' if terminal_status == 'expired' else 'forfeit'}:{round_id}")
        seed = str(state.pop("_server_seed", ""))
        await tx.execute(
            """INSERT INTO human_casino_verifications (round_id, commit_hash, reveal_seed, client_seed, nonce)
               VALUES ($1, $2, $3, $4, $5)""",
            round_id, row["rng_commit"], seed, row["client_seed"], row["nonce"])
        await tx.execute(
            """UPDATE human_casino_rounds SET status = $4, rng_reveal = $1,
               resolved_at = now(), state_json = $2 WHERE id = $3""",
            seed, json.dumps(state), round_id, terminal_status)
        await tx.execute("UPDATE human_casino_sessions SET net_pnl_v = net_pnl_v + $1 WHERE id = $2",
                         -wager, row["session_id"])

    async def _idem_begin(
        self,
        tx: Any,
        *,
        user_id: str,
        scope: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> _IdemState:
        await tx.execute(
            """
            INSERT INTO human_casino_idempotency
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
            SELECT id, payload_hash, response_status, response_body
            FROM human_casino_idempotency
            WHERE user_id = $1 AND scope = $2 AND idempotency_key = $3
            FOR UPDATE
            """,
            user_id,
            scope,
            idempotency_key,
        )
        if not row:
            raise ValueError("idempotency_row_not_found")

        if str(row["payload_hash"]) != payload_hash:
            raise ValueError("idempotency_conflict")

        if row["response_status"] is not None and row["response_body"] is not None:
            return _IdemState(
                row_id=str(row["id"]),
                replay=SerializedHTTPResponse(
                    status_code=int(row["response_status"]),
                    body_text=str(row["response_body"]),
                ),
            )
        return _IdemState(row_id=str(row["id"]), replay=None)

    async def _idem_persist(self, tx: Any, *, row_id: str, response: SerializedHTTPResponse) -> None:
        await tx.execute(
            """
            UPDATE human_casino_idempotency
            SET response_status = $1, response_body = $2, updated_at = now()
            WHERE id = $3
            """,
            response.status_code,
            response.body_text,
            row_id,
        )

    async def start_session(
        self,
        *,
        user_id: str,
        max_loss_v: Decimal,
        max_rounds: int,
        idempotency_key: str,
    ) -> SerializedHTTPResponse:
        payload = {
            "max_loss_v": str(max_loss_v),
            "max_rounds": int(max_rounds),
        }
        payload_hash = _canonical_hash(payload)
        scope = "human_session_start"

        async with self.db.transaction() as tx:
            idem = await self._idem_begin(
                tx,
                user_id=user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if idem.replay:
                return idem.replay

            # Bounded, account-scoped cleanup on the next explicit game session.
            # No new plays, refunds or random outcomes; settled rows are excluded.
            overdue = await tx.fetch(
                """SELECT r.* FROM human_casino_rounds r
                   WHERE r.user_id = $1 AND r.expires_at <= now()
                     AND r.status IN ('awaiting_action', 'expired')
                     AND NOT EXISTS (SELECT 1 FROM human_casino_payouts p WHERE p.round_id = r.id)
                   ORDER BY r.expires_at LIMIT 20 FOR UPDATE OF r SKIP LOCKED""", user_id)
            for expired in overdue:
                await self._expire_unfinished_round(tx, expired)

            session_id = str(uuid.uuid4())
            expires_at = _now_utc() + timedelta(seconds=_session_ttl_seconds())
            await tx.execute(
                """
                INSERT INTO human_casino_sessions
                  (id, user_id, max_loss_v, max_rounds, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                session_id,
                user_id,
                Decimal(str(max_loss_v)),
                int(max_rounds),
                expires_at,
            )
            out = SerializedHTTPResponse(
                status_code=200,
                body_text=_json_text(
                    {
                        "casino_session_id": session_id,
                        "max_loss_v": str(Decimal(str(max_loss_v))),
                        "max_rounds": int(max_rounds),
                        "rounds_played": 0,
                        "net_pnl_v": "0.000000",
                        "status": "active",
                        "expires_at": expires_at.isoformat(),
                    }
                ),
            )
            await self._idem_persist(tx, row_id=idem.row_id, response=out)
            return out

    async def list_games(self) -> dict:
        rows = await self.db.fetch(
            "SELECT game_code, rtp, rules_json, payout_table_json FROM casino_game_catalog WHERE enabled = TRUE"
        )
        return {"games": [dict(r) for r in rows], "min_wager_v": str(min_game_wager_v())}

    async def start_round(
        self,
        *,
        user_id: str,
        session_id: str,
        game_code: str,
        wager_v: Decimal,
        idempotency_key: str,
    ) -> SerializedHTTPResponse:
        payload = {
            "session_id": session_id,
            "game_code": game_code,
            "wager_v": str(Decimal(str(wager_v))),
        }
        payload_hash = _canonical_hash(payload)
        scope = f"human_round_start:{session_id}"
        game = HUMAN_CASINO_GAMES.get(game_code)
        if not game:
            raise ValueError(f"Unknown game: {game_code}")

        async with self.db.transaction() as tx:
            idem = await self._idem_begin(
                tx,
                user_id=user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if idem.replay:
                return idem.replay

            session = await tx.fetchrow(
                """
                SELECT *
                FROM human_casino_sessions
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                session_id,
                user_id,
            )
            if not session:
                raise ValueError("Session not found")
            if session["status"] != "active":
                out = _state_error("invalid_transition", str(session["status"]), [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if session["expires_at"] and session["expires_at"] <= _now_utc():
                await tx.execute(
                    "UPDATE human_casino_sessions SET status = 'closed', ended_at = now() WHERE id = $1",
                    session_id,
                )
                out = _state_error("session_expired", "closed", [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if int(session["rounds_played"]) >= int(session["max_rounds"]):
                await tx.execute(
                    "UPDATE human_casino_sessions SET status = 'round_capped', ended_at = now() WHERE id = $1",
                    session_id,
                )
                out = _state_error("session_round_capped", "round_capped", [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            current_loss = Decimal(str(session["net_pnl_v"]))
            max_loss = Decimal(str(session["max_loss_v"]))
            if current_loss <= -max_loss:
                await tx.execute(
                    "UPDATE human_casino_sessions SET status = 'loss_capped', ended_at = now() WHERE id = $1",
                    session_id,
                )
                out = _state_error("session_loss_capped", "loss_capped", [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            rng = ProvablyFairRNG()
            commitment = rng.new_round()
            client_seed = secrets.token_hex(16)
            nonce = 0
            state = game.initial_state(rng, client_seed, nonce)
            if game_code == "poker":
                state["_human_streets"] = 1
            if game_code == "blackjack":
                game.enable_extended(state)
                state['base_wager_v'] = str(wager_v)
                state['total_wager_v'] = str(wager_v)
            state["_server_seed"] = rng.server_seed
            current_state = "resolvable" if game.is_resolved(state) else "awaiting_action"

            round_id = str(uuid.uuid4())
            wager_v = Decimal(str(wager_v))
            if wager_v < min_game_wager_v():
                raise ValueError(f"Wager must be at least {min_game_wager_v()} $V")
            account_id = f"user:{user_id}"
            await self.wallet.ensure_escrow_account(round_id)
            await self.wallet.place_bet(
                account_id,
                wager_v,
                round_id,
                tx=tx,
                entry_type="human_casino_play",
                reference_id=round_id,
            )

            round_expires = _now_utc() + timedelta(seconds=_round_ttl_seconds())
            await tx.execute(
                """
                INSERT INTO human_casino_rounds
                  (id, session_id, user_id, game_code, wager_v, state_json, rng_commit, client_seed, nonce, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                round_id,
                session_id,
                user_id,
                game_code,
                wager_v,
                json.dumps(state),
                commitment,
                client_seed,
                nonce,
                current_state,
                round_expires,
            )
            await tx.execute(
                "UPDATE human_casino_sessions SET rounds_played = rounds_played + 1 WHERE id = $1",
                session_id,
            )

            valid_actions = [] if current_state != "awaiting_action" else game.valid_actions(state)
            out = SerializedHTTPResponse(
                status_code=200,
                body_text=_json_text(
                    {
                        "round_id": round_id,
                        "casino_session_id": session_id,
                        "rng_commit": commitment,
                        "state": _public_state_for_game(game_code, state, current_state),
                        "current_state": current_state,
                        "valid_actions": valid_actions,
                    }
                ),
            )
            await self._idem_persist(tx, row_id=idem.row_id, response=out)
            return out

    async def apply_action(
        self,
        *,
        user_id: str,
        round_id: str,
        action: str,
        payload: dict,
        idempotency_key: str,
    ) -> SerializedHTTPResponse:
        payload_in = {
            "round_id": round_id,
            "action": action,
            "payload": payload or {},
        }
        payload_hash = _canonical_hash(payload_in)
        scope = f"human_round_action:{round_id}"

        async with self.db.transaction() as tx:
            idem = await self._idem_begin(
                tx,
                user_id=user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if idem.replay:
                return idem.replay

            row = await tx.fetchrow(
                """
                SELECT *
                FROM human_casino_rounds
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                round_id,
                user_id,
            )
            if not row:
                raise ValueError("Round not found")

            status = str(row["status"])
            state = _parse_state(row["state_json"])
            game = HUMAN_CASINO_GAMES.get(str(row["game_code"]))
            if game is None:
                out = _state_error("invalid_transition", status, [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if await self._expire_unfinished_round(tx, row):
                status = "expired"

            if status in {"resolved", "canceled", "expired"}:
                err = "round_already_resolved" if status == "resolved" else "round_expired" if status == "expired" else "invalid_transition"
                out = _state_error(err, status, [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if action == "forfeit":
                # A concurrent spin/stand may already have decided a win. Return
                # that state for normal resolve, never overwrite it with a loss.
                if status == "awaiting_action" and not game.is_resolved(state):
                    await self._settle_unfinished_loss(tx, row, terminal_status="resolved")
                    wager = Decimal(str(row["wager_v"]))
                    response = {"round_id": round_id, "current_state": "resolved",
                                "valid_actions": [], "wager_v": str(wager),
                                "payout_v": "0", "net_v": str(-wager),
                                "outcome": {"result": "forfeit"}}
                elif status == "resolvable":
                    response = {"round_id": round_id, "current_state": status,
                                "valid_actions": [],
                                "state": _public_state_for_game(str(row["game_code"]), state, status)}
                else:
                    out = _state_error("invalid_transition", status, [])
                    await self._idem_persist(tx, row_id=idem.row_id, response=out)
                    return out
                out = SerializedHTTPResponse(status_code=200, body_text=_json_text(response))
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if status != "awaiting_action":
                out = _state_error("not_resolvable", status, [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            valid_actions = game.valid_actions(state)
            if action not in valid_actions:
                out = _state_error("invalid_transition", status, valid_actions)
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            move_count_row = await tx.fetchrow(
                "SELECT COUNT(*) AS cnt FROM human_casino_moves WHERE round_id = $1",
                round_id,
            )
            move_count = int(move_count_row["cnt"] if move_count_row else 0)

            if str(row['game_code']) == 'blackjack':
                extra_units = game.extra_stake_units(state, action)
                if extra_units:
                    extra = Decimal(state['base_wager_v']) * extra_units
                    total = Decimal(str(row['wager_v'])) + extra
                    session = await tx.fetchrow(
                        'SELECT * FROM human_casino_sessions WHERE id = $1 FOR UPDATE', row['session_id'])
                    if not session or session['status'] != 'active' or (
                        session['expires_at'] and session['expires_at'] <= _now_utc()
                    ):
                        raise ValueError('Session is no longer active; hit or stand without adding a stake')
                    if total > Decimal(str(session['max_loss_v'])) + Decimal(str(session['net_pnl_v'])):
                        out = _state_error('extra_stake_limit', status, valid_actions)
                        await self._idem_persist(tx, row_id=idem.row_id, response=out)
                        return out
                    # Round lock + existing idempotency transaction cover the debit,
                    # move and total exposure together. A retry cannot charge twice.
                    await self.wallet.place_bet(f'user:{user_id}', extra, round_id, tx=tx,
                        entry_type='human_casino_play', reference_id=f'{round_id}:extra:{move_count}')
                    await tx.execute('UPDATE human_casino_rounds SET wager_v = $1 WHERE id = $2', total, round_id)
                    state['total_wager_v'] = str(total)

            rng = ProvablyFairRNG()
            rng.server_seed = str(state.pop("_server_seed", ""))
            rng.server_seed_hash = str(row["rng_commit"])
            action_nonce = int(row["nonce"]) + 100 + move_count

            state = game.apply_action(
                state,
                action,
                payload or {},
                rng,
                str(row["client_seed"]),
                action_nonce,
            )
            state["_server_seed"] = rng.server_seed
            new_status = "resolvable" if game.is_resolved(state) else "awaiting_action"
            new_valid_actions = [] if new_status != "awaiting_action" else game.valid_actions(state)

            await tx.execute(
                """
                INSERT INTO human_casino_moves
                  (round_id, move_index, action, payload_json, idempotency_key)
                VALUES ($1, $2, $3, $4, $5)
                """,
                round_id,
                move_count,
                action,
                json.dumps(payload or {}),
                idempotency_key,
            )
            await tx.execute(
                """
                UPDATE human_casino_rounds
                SET state_json = $1, status = $2
                WHERE id = $3
                """,
                json.dumps(state),
                new_status,
                round_id,
            )

            out = SerializedHTTPResponse(
                status_code=200,
                body_text=_json_text(
                    {
                        "round_id": round_id,
                        "state": _public_state_for_game(str(row["game_code"]), state, new_status),
                        "current_state": new_status,
                        "valid_actions": new_valid_actions,
                    }
                ),
            )
            await self._idem_persist(tx, row_id=idem.row_id, response=out)
            return out

    async def resolve_round(
        self,
        *,
        user_id: str,
        round_id: str,
        idempotency_key: str,
    ) -> SerializedHTTPResponse:
        payload_hash = _canonical_hash({"round_id": round_id})
        scope = f"human_round_resolve:{round_id}"

        async with self.db.transaction() as tx:
            idem = await self._idem_begin(
                tx,
                user_id=user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if idem.replay:
                return idem.replay

            row = await tx.fetchrow(
                """
                SELECT *
                FROM human_casino_rounds
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                round_id,
                user_id,
            )
            if not row:
                raise ValueError("Round not found")

            status = str(row["status"])
            state = _parse_state(row["state_json"])
            game = HUMAN_CASINO_GAMES.get(str(row["game_code"]))
            if game is None:
                out = _state_error("invalid_transition", status, [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if await self._expire_unfinished_round(tx, row):
                status = "expired"

            if status in {"resolved", "canceled", "expired"}:
                err = "round_already_resolved" if status == "resolved" else "round_expired" if status == "expired" else "invalid_transition"
                out = _state_error(err, status, [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            if status != "resolvable":
                out = _state_error("not_resolvable", status, game.valid_actions(state) if status == "awaiting_action" else [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            payout_exists = await tx.fetchrow(
                "SELECT 1 FROM human_casino_payouts WHERE round_id = $1",
                round_id,
            )
            if payout_exists:
                out = _state_error("round_already_resolved", "resolved", [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            server_seed = str(state.pop("_server_seed", ""))
            multiplier, outcome_data = game.resolve(state)
            wager_v = Decimal(str(row["wager_v"]))
            payout_v = (wager_v * multiplier).quantize(Decimal("0.000001"))
            if state.get('_blackjack_actions_v2'):
                # Calculate in original stake units, avoiding a repeating-decimal
                # average across mixed split/doubled hands.
                payout_v = (Decimal(state['base_wager_v']) * Decimal(outcome_data['payout_units'])).quantize(Decimal('0.000001'))
            net_v = payout_v - wager_v

            account_id = f"user:{user_id}"
            if payout_v > 0:
                await self.wallet.settle_win(
                    account_id,
                    payout_v,
                    round_id,
                    tx=tx,
                    entry_type="human_casino_win",
                    reference_id=round_id,
                )
                leftover = wager_v - payout_v
                if leftover > 0:
                    await self.wallet.settle_loss(
                        round_id,
                        leftover,
                        tx=tx,
                        entry_type="human_casino_loss",
                        reference_id=round_id,
                    )
            else:
                await self.wallet.settle_loss(
                    round_id,
                    wager_v,
                    tx=tx,
                    entry_type="human_casino_loss",
                    reference_id=round_id,
                )

            await tx.execute(
                """
                INSERT INTO human_casino_payouts
                  (round_id, wager_v, payout_v, net_v, ledger_ref)
                VALUES ($1, $2, $3, $4, $5)
                """,
                round_id,
                wager_v,
                payout_v,
                net_v,
                f"human_casino_payout:{round_id}",
            )
            await tx.execute(
                """
                INSERT INTO human_casino_verifications
                  (round_id, commit_hash, reveal_seed, client_seed, nonce)
                VALUES ($1, $2, $3, $4, $5)
                """,
                round_id,
                row["rng_commit"],
                server_seed,
                row["client_seed"],
                row["nonce"],
            )
            await tx.execute(
                """
                UPDATE human_casino_rounds
                SET status = 'resolved', rng_reveal = $1, resolved_at = now(), state_json = $2
                WHERE id = $3
                """,
                server_seed,
                json.dumps(state),
                round_id,
            )
            await tx.execute(
                """
                UPDATE human_casino_sessions
                SET net_pnl_v = net_pnl_v + $1
                WHERE id = $2
                """,
                net_v,
                row["session_id"],
            )

            out = SerializedHTTPResponse(
                status_code=200,
                body_text=_json_text(
                    {
                        "round_id": round_id,
                        "wager_v": str(wager_v),
                        "payout_v": str(payout_v),
                        "net_v": str(net_v),
                        "outcome": outcome_data,
                        "rng_reveal": server_seed,
                        "rng_commit": row["rng_commit"],
                        "current_state": "resolved",
                        "valid_actions": [],
                    }
                ),
            )
            await self._idem_persist(tx, row_id=idem.row_id, response=out)
            return out

    async def verify_round(self, *, user_id: str, round_id: str) -> dict:
        row = await self.db.fetchrow(
            """
            SELECT hv.*, hr.game_code
            FROM human_casino_verifications hv
            JOIN human_casino_rounds hr ON hv.round_id = hr.id
            WHERE hv.round_id = $1 AND hr.user_id = $2
            """,
            round_id,
            user_id,
        )
        if not row:
            raise ValueError("Verification data not found")
        return {
            "round_id": round_id,
            "rng_commit": row["commit_hash"],
            "rng_reveal": row["reveal_seed"],
            "client_seed": row["client_seed"],
            "nonce": row["nonce"],
            "game_code": row["game_code"],
        }

    async def get_session(self, *, user_id: str, session_id: str) -> dict:
        row = await self.db.fetchrow(
            "SELECT * FROM human_casino_sessions WHERE id = $1 AND user_id = $2",
            session_id,
            user_id,
        )
        if not row:
            raise ValueError("Session not found")
        return {
            "casino_session_id": str(row["id"]),
            "max_loss_v": str(row["max_loss_v"]),
            "max_rounds": row["max_rounds"],
            "rounds_played": row["rounds_played"],
            "net_pnl_v": str(row["net_pnl_v"]),
            "status": row["status"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        }

    def _autoplay_actions(
        self,
        game_code: str,
        state: dict,
        *,
        preferred_action: str | None = None,
        preferred_payload: dict | None = None,
    ) -> list[tuple[str, dict]]:
        if game_code == "roulette":
            allowed = {"bet_red", "bet_black", "bet_odd", "bet_even", "bet_number"}
            action = "bet_red"
            payload: dict = {}
            if preferred_action in allowed:
                action = str(preferred_action)
                if action == "bet_number":
                    raw_number = ((preferred_payload or {}).get("number", 7))
                    try:
                        payload["number"] = max(0, min(36, int(raw_number)))
                    except Exception:
                        payload["number"] = 7
            return [(action, payload), ("spin", {})]
        if game_code == "slots":
            return [("spin", {})]
        if game_code == "baccarat":
            return [("bet_banker", {})]
        if game_code == "blackjack":
            actions: list[tuple[str, dict]] = []
            while not HUMAN_CASINO_GAMES["blackjack"].is_resolved(state):
                valid = HUMAN_CASINO_GAMES["blackjack"].valid_actions(state)
                if "hit" in valid and hand_value(state["player"]) < 17:
                    actions.append(("hit", {}))
                    state = HUMAN_CASINO_GAMES["blackjack"].apply_action(
                        state, "hit", {}, ProvablyFairRNG(), "", 0
                    )
                    continue
                if "stand" in valid:
                    actions.append(("stand", {}))
                    state = HUMAN_CASINO_GAMES["blackjack"].apply_action(
                        state, "stand", {}, ProvablyFairRNG(), "", 0
                    )
                    continue
                break
            return actions
        if game_code == "poker":
            return [("call", {})]
        return []

    async def demo_autoplay(
        self,
        *,
        user_id: str,
        session_id: str,
        game_code: str,
        wager_v: Decimal,
        idempotency_key: str,
        preferred_action: str | None = None,
        preferred_payload: dict | None = None,
    ) -> SerializedHTTPResponse:
        if game_code not in HUMAN_CASINO_GAMES:
            raise ValueError(f"Unknown game: {game_code}")

        payload = {
            "session_id": session_id,
            "game_code": game_code,
            "wager_v": str(Decimal(str(wager_v))),
            "preferred_action": preferred_action or "",
            "preferred_payload": preferred_payload or {},
        }
        payload_hash = _canonical_hash(payload)
        scope = f"human_demo_autoplay:{session_id}:{game_code}"

        async with self.db.transaction() as tx:
            idem = await self._idem_begin(
                tx,
                user_id=user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if idem.replay:
                return idem.replay

            session = await tx.fetchrow(
                """
                SELECT *
                FROM human_casino_sessions
                WHERE id = $1 AND user_id = $2
                FOR UPDATE
                """,
                session_id,
                user_id,
            )
            if not session:
                raise ValueError("Session not found")
            if session["status"] != "active":
                out = _state_error("invalid_transition", str(session["status"]), [])
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            game = HUMAN_CASINO_GAMES[game_code]
            cap = _demo_attempt_cap(game_code)
            chosen = None
            chosen_round = ""
            chosen_moves: list[tuple[str, dict]] = []
            chosen_rng = None
            chosen_state: dict = {}
            chosen_client_seed = ""

            for attempt in range(cap):
                rng = ProvablyFairRNG()
                commitment = rng.new_round()
                client_seed = secrets.token_hex(16)
                nonce = attempt * 1000
                state = game.initial_state(rng, client_seed, nonce)
                state["_server_seed"] = rng.server_seed
                moves = self._autoplay_actions(
                    game_code,
                    _parse_state(state),
                    preferred_action=preferred_action,
                    preferred_payload=preferred_payload or {},
                )
                working = _parse_state(state)
                move_nonce = nonce + 100
                for action, move_payload in moves:
                    working = game.apply_action(
                        working,
                        action,
                        move_payload,
                        rng,
                        client_seed,
                        move_nonce,
                    )
                    move_nonce += 1
                if not game.is_resolved(working):
                    # Some games need explicit final action sequence.
                    valid = game.valid_actions(working)
                    for fallback_action in valid:
                        working = game.apply_action(
                            working,
                            fallback_action,
                            {},
                            rng,
                            client_seed,
                            move_nonce,
                        )
                        moves.append((fallback_action, {}))
                        move_nonce += 1
                        if game.is_resolved(working):
                            break

                multiplier, outcome = game.resolve(_parse_state(working))
                if multiplier > Decimal("1"):
                    chosen = (commitment, nonce, multiplier, outcome)
                    chosen_rng = rng
                    chosen_state = _parse_state(working)
                    chosen_client_seed = client_seed
                    chosen_moves = moves
                    chosen_round = str(uuid.uuid4())
                    break

            if chosen is None or chosen_rng is None:
                out = SerializedHTTPResponse(
                    status_code=409,
                    body_text=_json_text(
                        {
                            "error": "demo_autoplay_cap_exhausted",
                            "game_code": game_code,
                            "attempt_cap": cap,
                            "valid_actions": [],
                        }
                    ),
                )
                await self._idem_persist(tx, row_id=idem.row_id, response=out)
                return out

            commitment, nonce, multiplier, outcome = chosen
            wager_v = Decimal(str(wager_v))
            payout_v = (wager_v * multiplier).quantize(Decimal("0.000001"))
            net_v = payout_v - wager_v
            round_id = chosen_round
            account_id = f"user:{user_id}"
            round_expires = _now_utc() + timedelta(seconds=_round_ttl_seconds())

            await self.wallet.ensure_escrow_account(round_id)
            await self.wallet.place_bet(
                account_id,
                wager_v,
                round_id,
                tx=tx,
                entry_type="demo_human_casino_play",
                reference_id=f"demo:{round_id}",
            )
            await self.wallet.settle_win(
                account_id,
                payout_v,
                round_id,
                tx=tx,
                entry_type="demo_human_casino_win",
                reference_id=f"demo:{round_id}",
            )
            leftover = wager_v - payout_v
            if leftover > 0:
                await self.wallet.settle_loss(
                    round_id,
                    leftover,
                    tx=tx,
                    entry_type="demo_human_casino_loss",
                    reference_id=f"demo:{round_id}",
                )

            await tx.execute(
                """
                INSERT INTO human_casino_rounds
                  (id, session_id, user_id, game_code, wager_v, state_json, rng_commit, rng_reveal, client_seed, nonce, status, started_at, resolved_at, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'resolved', now(), now(), $11)
                """,
                round_id,
                session_id,
                user_id,
                game_code,
                wager_v,
                json.dumps(chosen_state),
                commitment,
                chosen_rng.server_seed,
                chosen_client_seed,
                nonce,
                round_expires,
            )
            for idx, (action, move_payload) in enumerate(chosen_moves):
                await tx.execute(
                    """
                    INSERT INTO human_casino_moves
                      (round_id, move_index, action, payload_json, idempotency_key)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    round_id,
                    idx,
                    action,
                    json.dumps(move_payload),
                    f"demo-autoplay:{round_id}:{idx}",
                )
            await tx.execute(
                """
                INSERT INTO human_casino_payouts
                  (round_id, wager_v, payout_v, net_v, ledger_ref)
                VALUES ($1, $2, $3, $4, $5)
                """,
                round_id,
                wager_v,
                payout_v,
                net_v,
                f"demo_human_casino_payout:{round_id}",
            )
            await tx.execute(
                """
                INSERT INTO human_casino_verifications
                  (round_id, commit_hash, reveal_seed, client_seed, nonce)
                VALUES ($1, $2, $3, $4, $5)
                """,
                round_id,
                commitment,
                chosen_rng.server_seed,
                chosen_client_seed,
                nonce,
            )
            await tx.execute(
                """
                UPDATE human_casino_sessions
                SET rounds_played = rounds_played + 1, net_pnl_v = net_pnl_v + $1
                WHERE id = $2
                """,
                net_v,
                session_id,
            )

            out = SerializedHTTPResponse(
                status_code=200,
                body_text=_json_text(
                    {
                        "round_id": round_id,
                        "game_code": game_code,
                        "wager_v": str(wager_v),
                        "payout_v": str(payout_v),
                        "net_v": str(net_v),
                        "outcome": outcome,
                        "demo_mode": True,
                        "canonical": False,
                        "current_state": "resolved",
                        "valid_actions": [],
                    }
                ),
            )
            await self._idem_persist(tx, row_id=idem.row_id, response=out)
            return out
