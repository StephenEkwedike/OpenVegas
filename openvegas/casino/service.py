"""Casino service — session management, round lifecycle, and ledger settlement."""

from __future__ import annotations

import json
import secrets
import uuid
from decimal import Decimal
from typing import Any

from openvegas.casino.blackjack import BlackjackGame
from openvegas.casino.roulette import RouletteGame
from openvegas.casino.slots import SlotsGame
from openvegas.casino.poker import PokerGame
from openvegas.casino.baccarat import BaccaratGame
from openvegas.rng.provably_fair import ProvablyFairRNG
from openvegas.wallet.ledger import WalletService

CASINO_GAMES = {
    "blackjack": BlackjackGame(),
    "roulette": RouletteGame(),
    "slots": SlotsGame(),
    "poker": PokerGame(),
    "baccarat": BaccaratGame(),
}


class CasinoService:
    def __init__(self, db: Any, wallet: WalletService):
        self.db = db
        self.wallet = wallet

    async def start_session(
        self, org_id: str, agent_account_id: str,
        agent_session_id: str, max_loss_v: Decimal, max_rounds: int = 100,
    ) -> dict:
        # Validate parent agent session ownership and status
        parent = await self.db.fetchrow(
            """SELECT id, status FROM agent_sessions
               WHERE id = $1 AND agent_account_id = $2 AND org_id = $3""",
            agent_session_id, agent_account_id, org_id,
        )
        if not parent:
            raise ValueError("Parent agent session not found or does not belong to this agent/org")
        if parent["status"] != "active":
            raise ValueError(f"Parent agent session is '{parent['status']}', must be 'active'")

        # Check org policy
        policy = await self.db.fetchrow(
            "SELECT * FROM org_policies WHERE org_id = $1", org_id
        )
        if policy and not policy["casino_enabled"]:
            raise ValueError("Casino mode is disabled for this org")
        if policy:
            cap = Decimal(str(policy["casino_agent_max_loss_v"]))
            max_loss_v = min(max_loss_v, cap)

        session_id = str(uuid.uuid4())
        await self.db.execute(
            """INSERT INTO casino_sessions
               (id, org_id, agent_account_id, agent_session_id, max_loss_v, max_rounds)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            session_id, org_id, agent_account_id, agent_session_id,
            max_loss_v, max_rounds,
        )
        return {
            "casino_session_id": session_id,
            "agent_session_id": agent_session_id,
            "max_loss_v": str(max_loss_v),
            "max_rounds": max_rounds,
            "status": "active",
        }

    async def start_round(
        self, session_id: str, game_code: str,
        wager_v: Decimal, agent_account_id: str,
    ) -> dict:
        # Ownership check
        session = await self.db.fetchrow(
            "SELECT * FROM casino_sessions WHERE id = $1 AND agent_account_id = $2",
            session_id, agent_account_id,
        )
        if not session or session["status"] != "active":
            raise ValueError("Casino session is not active or does not belong to this agent")
        if session["rounds_played"] >= session["max_rounds"]:
            raise ValueError("Max rounds reached for this session")

        current_loss = Decimal(str(session["net_pnl_v"]))
        max_loss = Decimal(str(session["max_loss_v"]))
        if current_loss <= -max_loss:
            await self.db.execute(
                "UPDATE casino_sessions SET status = 'loss_capped', ended_at = now() WHERE id = $1",
                session_id,
            )
            raise ValueError("Session loss cap reached")

        # Org policy wager cap
        policy = await self.db.fetchrow(
            "SELECT casino_round_max_wager_v FROM org_policies WHERE org_id = $1",
            str(session["org_id"]),
        )
        if policy:
            round_cap = Decimal(str(policy["casino_round_max_wager_v"]))
            if wager_v > round_cap:
                raise ValueError(f"Wager {wager_v} exceeds round cap {round_cap}")

        game = CASINO_GAMES.get(game_code)
        if not game:
            raise ValueError(f"Unknown game: {game_code}")

        rng = ProvablyFairRNG()
        commitment = rng.new_round()
        client_seed = secrets.token_hex(16)
        nonce = 0
        state = game.initial_state(rng, client_seed, nonce)

        round_id = str(uuid.uuid4())
        agent_wallet_id = f"agent:{agent_account_id}"

        # Atomic: escrow + round insert + session update
        await self.wallet.ensure_account(f"escrow:{round_id}")
        async with self.db.transaction() as tx:
            await self.wallet.place_bet(agent_wallet_id, wager_v, round_id, tx=tx)

            await tx.execute(
                """INSERT INTO casino_rounds
                   (id, session_id, game_code, wager_v, state_json, rng_commit, client_seed, nonce)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                round_id, session_id, game_code, wager_v,
                json.dumps({**state, "_server_seed": rng.server_seed}),
                commitment, client_seed, nonce,
            )

            await tx.execute(
                "UPDATE casino_sessions SET rounds_played = rounds_played + 1 WHERE id = $1",
                session_id,
            )

        visible_state = {k: v for k, v in state.items() if not k.startswith("_")}
        return {
            "round_id": round_id,
            "rng_commit": commitment,
            "state": visible_state,
            "valid_actions": game.valid_actions(state),
        }

    async def apply_action(
        self, round_id: str, action: str, payload: dict,
        idempotency_key: str, agent_account_id: str,
    ) -> dict:
        # Ownership check via session join
        row = await self.db.fetchrow(
            """SELECT cr.* FROM casino_rounds cr
               JOIN casino_sessions cs ON cr.session_id = cs.id
               WHERE cr.id = $1 AND cs.agent_account_id = $2""",
            round_id, agent_account_id,
        )
        if not row or row["status"] != "active":
            raise ValueError("Round is not active or does not belong to this agent")

        state = json.loads(row["state_json"]) if isinstance(row["state_json"], str) else row["state_json"]
        game = CASINO_GAMES[row["game_code"]]

        if action not in game.valid_actions(state):
            raise ValueError(f"Invalid action '{action}'. Valid: {game.valid_actions(state)}")

        rng = ProvablyFairRNG()
        rng.server_seed = state.pop("_server_seed", "")
        rng.server_seed_hash = row["rng_commit"]

        # Nonce from persisted move count
        move_count_row = await self.db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM casino_moves WHERE round_id = $1", round_id
        )
        move_count = move_count_row["cnt"] if move_count_row else 0
        action_nonce = row["nonce"] + 100 + move_count

        state = game.apply_action(state, action, payload, rng, row["client_seed"], action_nonce)

        # Atomic: move insert + state update
        state["_server_seed"] = rng.server_seed
        async with self.db.transaction() as tx:
            await tx.execute(
                """INSERT INTO casino_moves (round_id, move_index, action, payload_json, idempotency_key)
                   VALUES ($1, $2, $3, $4, $5)""",
                round_id, move_count, action, json.dumps(payload), idempotency_key,
            )
            await tx.execute(
                "UPDATE casino_rounds SET state_json = $1 WHERE id = $2",
                json.dumps(state), round_id,
            )

        visible_state = {k: v for k, v in state.items() if not k.startswith("_")}
        return {
            "state": visible_state,
            "valid_actions": game.valid_actions(state),
            "is_resolved": game.is_resolved(state),
        }

    async def resolve_round(self, round_id: str, agent_account_id: str) -> dict:
        # Ownership check — payout goes to DB-verified owner
        row = await self.db.fetchrow(
            """SELECT cr.*, cs.agent_account_id AS owner_agent_id, cs.id AS session_id_val
               FROM casino_rounds cr
               JOIN casino_sessions cs ON cr.session_id = cs.id
               WHERE cr.id = $1 AND cs.agent_account_id = $2""",
            round_id, agent_account_id,
        )
        if not row or row["status"] != "active":
            raise ValueError("Round is not active, already resolved, or does not belong to this agent")

        verified_owner = f"agent:{row['owner_agent_id']}"

        state = json.loads(row["state_json"]) if isinstance(row["state_json"], str) else row["state_json"]
        game = CASINO_GAMES[row["game_code"]]
        server_seed = state.pop("_server_seed", "")

        if not game.is_resolved(state):
            raise ValueError("Round is not in a resolved state — submit remaining actions first")

        payout_multiplier, outcome_data = game.resolve(state)
        wager_v = Decimal(str(row["wager_v"]))
        payout_v = (wager_v * payout_multiplier).quantize(Decimal("0.01"))
        net_v = payout_v - wager_v

        # Atomic settlement
        async with self.db.transaction() as tx:
            if payout_v > 0:
                await self.wallet.settle_win(verified_owner, payout_v, round_id, tx=tx)
                leftover = wager_v - payout_v
                if leftover > 0:
                    await self.wallet.settle_loss(round_id, leftover, tx=tx)
            else:
                await self.wallet.settle_loss(round_id, wager_v, tx=tx)

            ledger_ref = f"casino_payout:{round_id}"
            await tx.execute(
                """INSERT INTO casino_payouts (round_id, wager_v, payout_v, net_v, ledger_ref)
                   VALUES ($1, $2, $3, $4, $5)""",
                round_id, wager_v, payout_v, net_v, ledger_ref,
            )

            await tx.execute(
                "UPDATE casino_rounds SET status = 'resolved', rng_reveal = $1, resolved_at = now() WHERE id = $2",
                server_seed, round_id,
            )

            await tx.execute(
                """INSERT INTO casino_verifications
                   (round_id, commit_hash, reveal_seed, client_seed, nonce)
                   VALUES ($1, $2, $3, $4, $5)""",
                round_id, row["rng_commit"], server_seed, row["client_seed"], row["nonce"],
            )

            session_id = str(row["session_id_val"])
            await tx.execute(
                "UPDATE casino_sessions SET net_pnl_v = net_pnl_v + $1 WHERE id = $2",
                net_v, session_id,
            )

        return {
            "round_id": round_id,
            "wager_v": str(wager_v),
            "payout_v": str(payout_v),
            "net_v": str(net_v),
            "outcome": outcome_data,
            "rng_reveal": server_seed,
            "rng_commit": row["rng_commit"],
        }

    async def get_session(self, session_id: str, agent_account_id: str) -> dict:
        row = await self.db.fetchrow(
            "SELECT * FROM casino_sessions WHERE id = $1 AND agent_account_id = $2",
            session_id, agent_account_id,
        )
        if not row:
            raise ValueError("Session not found or does not belong to this agent")
        return {
            "casino_session_id": str(row["id"]),
            "max_loss_v": str(row["max_loss_v"]),
            "max_rounds": row["max_rounds"],
            "rounds_played": row["rounds_played"],
            "net_pnl_v": str(row["net_pnl_v"]),
            "status": row["status"],
        }
