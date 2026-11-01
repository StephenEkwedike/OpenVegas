"""Agent service — accounts, tokens, and session envelopes."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from openvegas.wallet.ledger import WalletService


class AgentService:
    def __init__(self, db: Any, wallet: WalletService):
        self.db = db
        self.wallet = wallet

    async def create_account(self, org_id: str, name: str) -> dict:
        agent_id = str(uuid.uuid4())
        await self.db.execute(
            "INSERT INTO agent_accounts (id, org_id, name) VALUES ($1, $2, $3)",
            agent_id, org_id, name,
        )
        await self.wallet.ensure_account(f"agent:{agent_id}")
        return {"agent_account_id": agent_id, "org_id": org_id, "name": name}

    async def issue_token(
        self,
        agent_account_id: str,
        scopes: list[str],
        ttl_minutes: int = 60,
        created_by_user_id: str | None = None,
    ) -> str:
        """Generate ov_agent_<random> token, store SHA-256 hash, return plaintext ONCE."""
        token = f"ov_agent_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

        try:
            await self.db.execute(
                """INSERT INTO agent_tokens (agent_account_id, scopes, token_hash, expires_at, created_by_user_id)
                   VALUES ($1, $2, $3, $4, $5)""",
                agent_account_id, scopes, token_hash, expires_at, created_by_user_id,
            )
        except Exception:
            # Backward compatibility for DBs without created_by_user_id.
            await self.db.execute(
                """INSERT INTO agent_tokens (agent_account_id, scopes, token_hash, expires_at)
                   VALUES ($1, $2, $3, $4)""",
                agent_account_id, scopes, token_hash, expires_at,
            )
        return token

    async def start_session(
        self,
        agent_account_id: str,
        org_id: str,
        envelope_v: Decimal,
        ttl_seconds: int | None = None,
    ) -> dict:
        session_id = str(uuid.uuid4())
        ttl = int(ttl_seconds if ttl_seconds is not None else os.getenv("CASINO_SESSION_TTL_SECONDS", "1800"))
        ttl = max(60, ttl)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=ttl
        )
        await self.db.execute(
            """INSERT INTO agent_sessions (id, agent_account_id, org_id, envelope_v, expires_at)
               VALUES ($1, $2, $3, $4, $5)""",
            session_id, agent_account_id, org_id, envelope_v, expires_at,
        )
        await self.db.execute(
            """INSERT INTO agent_session_events
               (session_id, event_type, amount_v, metadata)
               VALUES ($1, 'reserve', $2, $3::jsonb)""",
            session_id,
            envelope_v,
            '{"reason":"session_start"}',
        )
        return {
            "session_id": session_id,
            "envelope_v": str(envelope_v),
            "spent_v": "0",
            "remaining_v": str(envelope_v),
            "status": "active",
            "expires_at": expires_at.isoformat(),
        }

    async def check_session_budget(
        self, session_id: str, amount_v: Decimal, agent_account_id: str | None = None
    ) -> bool:
        if agent_account_id:
            row = await self.db.fetchrow(
                """SELECT envelope_v, spent_v, reserved_v, status
                   FROM agent_sessions
                   WHERE id = $1 AND agent_account_id = $2""",
                session_id,
                agent_account_id,
            )
        else:
            row = await self.db.fetchrow(
                "SELECT envelope_v, spent_v, reserved_v, status FROM agent_sessions WHERE id = $1",
                session_id,
            )
        if not row or row["status"] != "active":
            return False
        remaining = (
            Decimal(str(row["envelope_v"]))
            - Decimal(str(row["spent_v"]))
            - Decimal(str(row["reserved_v"]))
        )
        return remaining >= amount_v

    async def record_spend(
        self, session_id: str, amount_v: Decimal, agent_account_id: str | None = None
    ):
        if agent_account_id:
            updated = await self.db.execute(
                "UPDATE agent_sessions SET spent_v = spent_v + $1 WHERE id = $2 AND agent_account_id = $3",
                amount_v,
                session_id,
                agent_account_id,
            )
            if updated.endswith("0"):
                raise ValueError("Session not found or does not belong to this agent")
        else:
            await self.db.execute(
                "UPDATE agent_sessions SET spent_v = spent_v + $1 WHERE id = $2",
                amount_v,
                session_id,
            )
        await self.db.execute(
            """INSERT INTO agent_session_events
               (session_id, event_type, amount_v, metadata)
               VALUES ($1, 'settle', $2, $3::jsonb)""",
            session_id,
            amount_v,
            '{"reason":"inference_or_game_spend"}',
        )
        row = await self.db.fetchrow(
            "SELECT envelope_v, spent_v FROM agent_sessions WHERE id = $1", session_id
        )
        if row and Decimal(str(row["spent_v"])) >= Decimal(str(row["envelope_v"])):
            await self.db.execute(
                "UPDATE agent_sessions SET status = 'exhausted', ended_at = now() WHERE id = $1",
                session_id,
            )

    async def get_budget(self, session_id: str, agent_account_id: str | None = None) -> dict:
        if agent_account_id:
            row = await self.db.fetchrow(
                "SELECT * FROM agent_sessions WHERE id = $1 AND agent_account_id = $2",
                session_id,
                agent_account_id,
            )
        else:
            row = await self.db.fetchrow(
                "SELECT * FROM agent_sessions WHERE id = $1", session_id
            )
        if not row:
            return {"error": "Session not found"}
        envelope = Decimal(str(row["envelope_v"]))
        spent = Decimal(str(row["spent_v"]))
        return {
            "session_id": session_id,
            "envelope_v": str(envelope),
            "spent_v": str(spent),
            "remaining_v": str(envelope - spent),
            "status": row["status"],
        }

    async def close_session(self, session_id: str):
        await self.db.execute(
            "UPDATE agent_sessions SET status = 'closed', ended_at = now() WHERE id = $1",
            session_id,
        )
        await self.db.execute(
            """INSERT INTO agent_session_events
               (session_id, event_type, amount_v, metadata)
               VALUES ($1, 'close', 0, $2::jsonb)""",
            session_id,
            '{"reason":"manual_close"}',
        )

    async def list_accounts(self, *, org_id: str) -> list[dict]:
        rows = await self.db.fetch(
            """
            SELECT id, org_id, name, status, created_at
            FROM agent_accounts
            WHERE org_id = $1
            ORDER BY created_at DESC
            """,
            org_id,
        )
        return [
            {
                "agent_account_id": str(r["id"]),
                "org_id": str(r["org_id"]),
                "name": str(r["name"]),
                "status": str(r["status"]),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ]

    async def list_sessions(self, *, org_id: str, limit: int = 50) -> list[dict]:
        safe_limit = max(1, min(int(limit), 200))
        rows = await self.db.fetch(
            """
            SELECT s.id, s.agent_account_id, a.name AS agent_name, s.envelope_v, s.spent_v, s.reserved_v,
                   s.refunded_v, s.status, s.started_at, s.ended_at, s.expires_at
            FROM agent_sessions s
            JOIN agent_accounts a ON a.id = s.agent_account_id
            WHERE s.org_id = $1
            ORDER BY s.started_at DESC
            LIMIT $2
            """,
            org_id,
            safe_limit,
        )
        out: list[dict] = []
        for r in rows:
            envelope = Decimal(str(r["envelope_v"]))
            spent = Decimal(str(r["spent_v"]))
            reserved = Decimal(str(r.get("reserved_v", "0")))
            refunded = Decimal(str(r.get("refunded_v", "0")))
            remaining = envelope - spent - reserved + refunded
            out.append(
                {
                    "session_id": str(r["id"]),
                    "agent_account_id": str(r["agent_account_id"]),
                    "agent_name": str(r["agent_name"]),
                    "envelope_v": str(envelope),
                    "spent_v": str(spent),
                    "reserved_v": str(reserved),
                    "refunded_v": str(refunded),
                    "remaining_v": str(remaining),
                    "status": str(r["status"]),
                    "started_at": r["started_at"].isoformat() if r.get("started_at") else None,
                    "ended_at": r["ended_at"].isoformat() if r.get("ended_at") else None,
                    "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
                }
            )
        return out
