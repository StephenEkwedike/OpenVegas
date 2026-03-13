"""Demo admin gating and autofund configuration helpers."""

from __future__ import annotations

import os
from decimal import Decimal


def demo_mode_enabled() -> bool:
    return os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0") == "1"


def is_demo_admin_user(user_id: str) -> bool:
    if not demo_mode_enabled():
        return False
    raw = os.getenv("OPENVEGAS_DEMO_ADMIN_USER_IDS", "").strip()
    if not raw:
        return os.getenv("OPENVEGAS_DEMO_ALLOW_LOCAL_OPEN", "0") == "1"
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
