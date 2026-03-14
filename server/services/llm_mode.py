"""LLM mode resolution service with policy-aware effective mode selection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openvegas.contracts.enums import ConversationMode, EffectiveReason


@dataclass(frozen=True)
class LLMModeResolution:
    user_pref_mode: str
    effective_mode: str
    effective_reason: EffectiveReason
    conversation_mode: ConversationMode

    def as_dict(self) -> dict[str, str]:
        return {
            "user_pref_mode": self.user_pref_mode,
            "effective_mode": self.effective_mode,
            "effective_reason": self.effective_reason.value,
            "conversation_mode": self.conversation_mode.value,
        }


class LLMModeService:
    """Resolves user preference to an enforced effective mode."""

    def __init__(self, db: Any):
        self.db = db

    async def resolve_for_user(
        self,
        *,
        user_id: str,
        requested_mode: str | None = None,
        requested_conversation_mode: str | None = None,
        org_id: str | None = None,
    ) -> LLMModeResolution:
        row = await self.db.fetchrow(
            """
            SELECT llm_mode, conversation_mode
            FROM user_runtime_prefs
            WHERE user_id = $1
            """,
            user_id,
        )

        stored_mode = str(row["llm_mode"]) if row and row.get("llm_mode") else None
        stored_conv = str(row["conversation_mode"]) if row and row.get("conversation_mode") else None

        pref_missing = requested_mode is None and stored_mode is None
        invalid_mode = False
        if requested_mode is not None:
            pref_mode = requested_mode.strip().lower()
        elif stored_mode is not None:
            pref_mode = stored_mode.strip().lower()
        else:
            pref_mode = "wrapper"
        if pref_mode not in {"wrapper", "byok"}:
            pref_mode = "wrapper"
            invalid_mode = True

        if requested_conversation_mode is not None:
            conv_mode_raw = requested_conversation_mode.strip().lower()
        elif stored_conv is not None:
            conv_mode_raw = stored_conv.strip().lower()
        else:
            conv_mode_raw = ConversationMode.PERSISTENT.value
        if conv_mode_raw not in {ConversationMode.PERSISTENT.value, ConversationMode.EPHEMERAL.value}:
            conv_mode_raw = ConversationMode.PERSISTENT.value
        conversation_mode = ConversationMode(conv_mode_raw)

        if invalid_mode:
            reason = EffectiveReason.INVALID_USER_PREF_FALLBACK
        elif pref_missing:
            reason = EffectiveReason.USER_PREF_MISSING
        else:
            reason = EffectiveReason.USER_PREF_APPLIED
        effective_mode = pref_mode

        if pref_mode == "byok":
            if not self._byok_enabled_globally():
                effective_mode = "wrapper"
                reason = EffectiveReason.GLOBAL_BYOK_DISABLED
            elif await self._org_requires_wrapper(org_id):
                effective_mode = "wrapper"
                reason = EffectiveReason.ORG_POLICY_WRAPPER_REQUIRED

        await self.db.execute(
            """
            INSERT INTO user_runtime_prefs (user_id, llm_mode, conversation_mode, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (user_id)
            DO UPDATE SET
                llm_mode = EXCLUDED.llm_mode,
                conversation_mode = EXCLUDED.conversation_mode,
                updated_at = now()
            """,
            user_id,
            pref_mode,
            conversation_mode.value,
        )

        return LLMModeResolution(
            user_pref_mode=pref_mode,
            effective_mode=effective_mode,
            effective_reason=reason,
            conversation_mode=conversation_mode,
        )

    @staticmethod
    def _byok_enabled_globally() -> bool:
        return os.getenv("OPENVEGAS_BYOK_ENABLED", "0") == "1"

    async def _org_requires_wrapper(self, org_id: str | None) -> bool:
        if not org_id:
            return False
        row = await self.db.fetchrow(
            """
            SELECT wrapper_required, byok_allowed
            FROM org_runtime_policies
            WHERE org_id = $1
            """,
            org_id,
        )
        if not row:
            return False
        if row.get("wrapper_required") is True:
            return True
        return row.get("byok_allowed") is False

