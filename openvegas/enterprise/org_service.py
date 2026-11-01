"""Enterprise org service — CRUD, policies, sponsorships."""

from __future__ import annotations

import uuid
import os
from decimal import Decimal
from typing import Any


class OrgService:
    def __init__(self, db: Any):
        self.db = db

    async def create_org(self, name: str, owner_user_id: str) -> dict:
        org_id = str(uuid.uuid4())
        await self.db.execute(
            "INSERT INTO organizations (id, name) VALUES ($1, $2)",
            org_id, name,
        )
        await self.db.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, 'owner')",
            org_id, owner_user_id,
        )
        # Create default policy
        await self.db.execute(
            "INSERT INTO org_policies (org_id) VALUES ($1)",
            org_id,
        )
        return {"org_id": org_id, "name": name}

    async def create_sponsorship(self, org_id: str, monthly_usd: float) -> dict:
        sp_id = str(uuid.uuid4())
        await self.db.execute(
            """INSERT INTO org_sponsorships (id, org_id, monthly_budget_usd)
               VALUES ($1, $2, $3)""",
            sp_id, org_id, monthly_usd,
        )
        await self.db.execute(
            """INSERT INTO org_budget_ledger (org_id, source, delta_usd, reference_id)
               VALUES ($1, 'sponsor_refill', $2, $3)""",
            org_id, monthly_usd, sp_id,
        )
        return {"sponsorship_id": sp_id, "monthly_budget_usd": monthly_usd}

    async def set_policy(self, org_id: str, **fields) -> dict:
        allowed = {
            "allowed_providers", "allowed_models", "user_daily_cap_usd",
            "byok_fallback_enabled", "boost_enabled", "casino_enabled",
            "casino_agent_max_loss_v", "casino_round_max_wager_v", "casino_round_cooldown_ms",
            "agent_default_envelope_v", "agent_max_envelope_v", "agent_session_ttl_sec", "agent_infer_enabled",
        }
        existing = await self._policy_columns()
        sets = []
        params = []
        for key, val in fields.items():
            if key in allowed and key in existing:
                params.append(val)
                sets.append(f"{key} = ${len(params)}")
        if not sets:
            return {"updated": False}
        params.append(org_id)
        await self.db.execute(
            f"UPDATE org_policies SET {', '.join(sets)} WHERE org_id = ${len(params)}",
            *params,
        )
        return {"updated": True}

    async def get_policy(self, org_id: str) -> dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM org_policies WHERE org_id = $1", org_id
        )
        if not row:
            return None
        out = dict(row)
        out.setdefault("agent_default_envelope_v", Decimal(os.getenv("OPENVEGAS_AGENT_DEFAULT_ENVELOPE_V", "25.0")))
        out.setdefault("agent_max_envelope_v", Decimal(os.getenv("OPENVEGAS_AGENT_MAX_ENVELOPE_V", "250.0")))
        out.setdefault("agent_session_ttl_sec", int(os.getenv("OPENVEGAS_AGENT_SESSION_TTL_SECONDS", "1800")))
        out.setdefault("agent_infer_enabled", True)
        return out

    async def invite_member(self, org_id: str, user_id: str, role: str = "member") -> dict:
        await self.db.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, $3) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role = $3",
            org_id, user_id, role,
        )
        return {"org_id": org_id, "user_id": user_id, "role": role}

    async def check_policy(self, org_id: str, provider: str, model: str) -> bool:
        """Returns True if provider/model is allowed by org policy."""
        policy = await self.get_policy(org_id)
        if not policy:
            return True  # no policy = no restrictions
        allowed_providers = policy.get("allowed_providers", [])
        if allowed_providers and provider not in allowed_providers:
            return False
        allowed_models = policy.get("allowed_models", [])
        if allowed_models and model not in allowed_models:
            return False
        return True

    async def _policy_columns(self) -> set[str]:
        try:
            rows = await self.db.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'org_policies'
                """
            )
            return {str(r["column_name"]) for r in rows}
        except Exception:
            return {
                "allowed_providers", "allowed_models", "user_daily_cap_usd",
                "byok_fallback_enabled", "boost_enabled", "casino_enabled",
                "casino_agent_max_loss_v", "casino_round_max_wager_v", "casino_round_cooldown_ms",
            }
