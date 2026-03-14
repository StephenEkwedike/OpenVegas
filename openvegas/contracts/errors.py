"""Canonical error contract codes shared across layers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class APIErrorCode(str, Enum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    PROVIDER_THREAD_MISMATCH = "provider_thread_mismatch"
    MARGIN_FLOOR_VIOLATION = "margin_floor_violation"
    BYOK_NOT_ALLOWED = "byok_not_allowed"
    THREAD_EXPIRED_RESTARTED = "thread_expired_restarted"
    HOLD_CONFLICT = "hold_conflict"


@dataclass
class ContractError(Exception):
    code: APIErrorCode
    detail: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.detail}"

