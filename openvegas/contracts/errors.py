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

    STALE_PROJECTION = "stale_projection"
    INVALID_TRANSITION = "invalid_transition"
    APPROVAL_REQUIRED = "approval_required"
    ACTIVE_MUTATION_IN_PROGRESS = "active_mutation_in_progress"
    RUN_NOT_RESUMABLE = "run_not_resumable"
    HANDOFF_BLOCKED = "handoff_blocked"
    MUTATION_UNCERTAIN = "mutation_uncertain"
    LEASE_DELETE_MISMATCH = "lease_delete_mismatch"

    EDITOR_UNAVAILABLE = "editor_unavailable"
    EDITOR_OPEN_FAILED = "editor_open_failed"
    WORKSPACE_PATH_OUT_OF_BOUNDS = "workspace_path_out_of_bounds"
    TOOL_NOT_ALLOWED_IN_PLAN_MODE = "tool_not_allowed_in_plan_mode"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    BINARY_FILE_UNSUPPORTED = "binary_file_unsupported"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


@dataclass
class ContractError(Exception):
    code: APIErrorCode
    detail: str

    def __str__(self) -> str:
        return f"{self.code.value}: {self.detail}"
