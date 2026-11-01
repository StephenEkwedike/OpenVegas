"""Canonical contracts for agent orchestration runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


RUN_STATES: tuple[str, ...] = (
    "created",
    "running",
    "awaiting_approval",
    "completed",
    "failed",
    "canceled",
    "expired",
    "interrupted",
)

APPROVAL_STATES: tuple[str, ...] = (
    "pending",
    "approved",
    "consumed",
    "expired",
    "revoked",
    "superseded",
)

TOOL_EXECUTION_STATES: tuple[str, ...] = (
    "proposed",
    "started",
    "succeeded",
    "failed",
    "timed_out",
    "blocked",
    "cancelled",
)

TERMINAL_RUN_STATES: frozenset[str] = frozenset({"completed", "failed", "canceled", "expired"})

UI_HANDOFF_BLOCK_REASONS: tuple[str, ...] = (
    "pending_approval",
    "active_tool_execution",
    "leased_worker_active",
    "mutation_uncertain",
)

_RUN_LEVEL_PRIORITY = {
    "resume": 10,
    "approve": 20,
    "cancel": 30,
    "handoff": 40,
}


def _norm(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, dict):
        return {k: _norm(v[k]) for k in sorted(v.keys())}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def canonical_json(v: Any) -> str:
    """Canonical JSON serializer used for hashing and replay storage."""
    return json.dumps(_norm(v), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex_utf8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _stable_action_identifier(action: dict[str, Any]) -> str:
    for key in ("tool_call_id", "id", "target_id", "resource_id", "reference_id"):
        val = action.get(key)
        if val is not None:
            return str(val)
    return ""


def action_sort_key(action: dict[str, Any]) -> tuple[int, Any, Any]:
    """Deterministic action ordering for signature stability."""
    name = str(action.get("action", "")).strip()
    if name == "approve" and action.get("tool_call_id") is not None:
        return (0, str(action["tool_call_id"]), "")
    if name in _RUN_LEVEL_PRIORITY:
        return (1, _RUN_LEVEL_PRIORITY[name], _stable_action_identifier(action))
    return (2, name, _stable_action_identifier(action))


def canonicalize_valid_actions(valid_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministically ordered + normalized valid_actions."""
    normalized: list[dict[str, Any]] = []
    for action in valid_actions:
        normalized.append(_norm(action))
    normalized.sort(key=action_sort_key)
    return normalized


def valid_actions_signature(run_version: int, valid_actions: list[dict[str, Any]]) -> str:
    ordered = canonicalize_valid_actions(valid_actions)
    preimage = f"{run_version}:{canonical_json(ordered)}"
    return f"sha256:{sha256_hex_utf8(preimage)}"


@dataclass(frozen=True)
class MutatingResponseEnvelope:
    error: str | None
    detail: str
    retryable: bool
    current_state: str
    run_version: int
    projection_version: int
    valid_actions: list[dict[str, Any]]
    valid_actions_signature: str
    response_truncated: bool = False
    response_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # "error" must always be present, including on success (null).
        return {
            "error": self.error,
            "detail": self.detail,
            "retryable": self.retryable,
            "current_state": self.current_state,
            "run_version": self.run_version,
            "projection_version": self.projection_version,
            "valid_actions": self.valid_actions,
            "valid_actions_signature": self.valid_actions_signature,
            "response_truncated": self.response_truncated,
            "response_hash": self.response_hash,
        }
