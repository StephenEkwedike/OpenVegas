"""Shared enum contracts used by API + CLI + telemetry."""

from __future__ import annotations

from enum import Enum


class EffectiveReason(str, Enum):
    GLOBAL_BYOK_DISABLED = "global_byok_disabled"
    ORG_POLICY_WRAPPER_REQUIRED = "org_policy_wrapper_required"
    USER_PREF_APPLIED = "user_pref_applied"
    USER_PREF_MISSING = "user_pref_missing"
    INVALID_USER_PREF_FALLBACK = "invalid_user_pref_fallback"


class ConversationMode(str, Enum):
    PERSISTENT = "persistent"
    EPHEMERAL = "ephemeral"

