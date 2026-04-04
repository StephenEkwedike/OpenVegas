"""Demo admin gating and autofund configuration helpers."""

from __future__ import annotations

import os
from decimal import Decimal


def _truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def demo_mode_enabled() -> bool:
    # Preferred alias for QA/content workflows; falls back to legacy flag.
    win_always_raw = str(os.getenv("OPENVEGAS_WIN_ALWAYS", "")).strip()
    if win_always_raw:
        return _truthy(win_always_raw)
    return _truthy(os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0"))


def is_demo_admin_user(user_id: str) -> bool:
    if not demo_mode_enabled():
        return False
    # Prefer new allowlist alias, fall back to legacy var for compatibility.
    raw = str(os.getenv("OPENVEGAS_WIN_ALWAYS_USER_IDS", "")).strip()
    if not raw:
        raw = str(os.getenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "")).strip()
    if not raw:
        # Single-switch behavior: OPENVEGAS_WIN_ALWAYS=Y with no allowlist
        # enables QA win mode for all accounts.
        if _truthy(os.getenv("OPENVEGAS_WIN_ALWAYS", "")):
            return True
        return _truthy(os.getenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0"))
    allow = {x.strip() for x in raw.split(",") if x.strip()}
    return user_id in allow


def is_demo_admin_account(account_id: str) -> bool:
    if not account_id.startswith("user:"):
        return False
    return is_demo_admin_user(account_id.removeprefix("user:"))


def demo_admin_autofund_enabled() -> bool:
    return os.getenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_ENABLED", "1") == "1"


def demo_admin_autofund_min() -> Decimal:
    return Decimal(os.getenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_MIN", "1000"))


def demo_admin_autofund_topup() -> Decimal:
    return Decimal(os.getenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_TOPUP", "1500"))


def demo_admin_autofund_max_cycles() -> int:
    raw = int(os.getenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_MAX_CYCLES", "20"))
    return max(1, raw)


def demo_admin_autofund_read_cooldown_sec() -> int:
    raw = int(os.getenv("OPENVEGAS_DEMO_ADMIN_AUTOFUND_READ_COOLDOWN_SEC", "0"))
    return max(0, raw)
