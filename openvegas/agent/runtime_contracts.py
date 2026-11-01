"""Runtime contracts for agent orchestration + local tool callbacks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import re
from typing import Any

from openvegas.agent.orchestration_contracts import (
    MutatingResponseEnvelope,
    canonical_json,
    canonicalize_valid_actions,
    sha256_hex_utf8,
    valid_actions_signature,
)


class ToolStatus(str, Enum):
    PROPOSED = "proposed"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ToolName(str, Enum):
    FS_LIST = "fs_list"
    FS_READ = "fs_read"
    FS_SEARCH = "fs_search"
    FS_APPLY_PATCH = "fs_apply_patch"
    SHELL_RUN = "shell_run"
    EDITOR_OPEN = "editor_open"
    MCP_CALL = "mcp_call"


class ToolReasonCode(str, Enum):
    EDITOR_UNAVAILABLE = "editor_unavailable"
    EDITOR_OPEN_FAILED = "editor_open_failed"
    WORKSPACE_PATH_OUT_OF_BOUNDS = "workspace_path_out_of_bounds"
    TOOL_NOT_ALLOWED_IN_PLAN_MODE = "tool_not_allowed_in_plan_mode"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    BINARY_FILE_UNSUPPORTED = "binary_file_unsupported"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INTERRUPTED_LEASE_EXPIRED = "interrupted_lease_expired"


class ShellMode(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"


class ToolPolicyDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    EXCLUDE = "exclude"


class HandoffBlockReason(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    ACTIVE_TOOL_EXECUTION = "active_tool_execution"
    LEASED_WORKER_ACTIVE = "leased_worker_active"
    MUTATION_UNCERTAIN = "mutation_uncertain"


TOOL_REGISTRY: tuple[str, ...] = tuple(t.value for t in ToolName)
MUTATING_TOOLS: frozenset[str] = frozenset({ToolName.FS_APPLY_PATCH.value})
TERMINAL_TOOL_STATUSES: frozenset[str] = frozenset(
    {
        ToolStatus.SUCCEEDED.value,
        ToolStatus.FAILED.value,
        ToolStatus.TIMED_OUT.value,
        ToolStatus.BLOCKED.value,
        ToolStatus.CANCELLED.value,
    }
)
RESULT_TOOL_STATUSES: frozenset[str] = frozenset(
    {
        ToolStatus.SUCCEEDED.value,
        ToolStatus.FAILED.value,
        ToolStatus.TIMED_OUT.value,
        ToolStatus.BLOCKED.value,
    }
)

_RAW_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def is_raw_sha256_hex(value: str) -> bool:
    return bool(_RAW_SHA256_HEX_RE.fullmatch(str(value)))


def require_raw_sha256_hex(value: str, field_name: str) -> str:
    if not is_raw_sha256_hex(value):
        raise ValueError(f"{field_name} must be raw lowercase sha256 hex (64 chars).")
    return value


def is_mutating_tool(tool_name: str, shell_mode: str | None = None) -> bool:
    if tool_name == ToolName.SHELL_RUN.value:
        return shell_mode == ShellMode.MUTATING.value
    return tool_name in MUTATING_TOOLS


def evaluate_tool_policy(*, tool_name: str, shell_mode: str | None, approval_mode: str) -> ToolPolicyDecision:
    mode = str(approval_mode or "ask").strip().lower()
    if mode not in {
        ToolPolicyDecision.ALLOW.value,
        ToolPolicyDecision.ASK.value,
        ToolPolicyDecision.EXCLUDE.value,
    }:
        mode = ToolPolicyDecision.ASK.value
    if not is_mutating_tool(tool_name=tool_name, shell_mode=shell_mode):
        return ToolPolicyDecision.ALLOW
    if mode == ToolPolicyDecision.ALLOW.value:
        return ToolPolicyDecision.ALLOW
    if mode == ToolPolicyDecision.EXCLUDE.value:
        return ToolPolicyDecision.EXCLUDE
    return ToolPolicyDecision.ASK


def tool_payload_hash(tool_name: str, normalized_arguments: dict[str, Any], shell_mode: str | None) -> str:
    preimage = canonical_json(
        {
            "tool_name": tool_name,
            "normalized_arguments": normalized_arguments,
            "shell_mode": shell_mode,
        }
    )
    return sha256_hex_utf8(preimage)


def result_submission_hash(
    result_status: str,
    result_payload: dict[str, Any],
    stdout_sha256: str,
    stderr_sha256: str,
) -> str:
    require_raw_sha256_hex(stdout_sha256, "stdout_sha256")
    require_raw_sha256_hex(stderr_sha256, "stderr_sha256")
    preimage = canonical_json(
        {
            "result_status": result_status,
            "result_payload": result_payload,
            "stdout_sha256": stdout_sha256,
            "stderr_sha256": stderr_sha256,
        }
    )
    return sha256_hex_utf8(preimage)


def canonical_sha256_prefixed(text: str) -> str:
    return f"sha256:{sha256_hex_utf8(text)}"


@dataclass(frozen=True)
class ToolRequest:
    tool_call_id: str
    execution_token: str
    tool_name: str
    arguments: dict[str, Any]
    payload_hash: str
    requires_approval: bool
    shell_mode: str | None
    timeout_sec: int


@dataclass(frozen=True)
class ToolHeartbeatResponse:
    active: bool
    status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"active": bool(self.active)}
        if not self.active and self.status:
            payload["status"] = str(self.status)
        return payload


def normalize_decimal(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    return v
