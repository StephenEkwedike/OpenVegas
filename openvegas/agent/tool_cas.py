"""Authoritative-row CAS helpers for tool callback endpoints.

Callbacks intentionally avoid agent_mutation_replays in v1.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from openvegas.agent.orchestration_contracts import canonical_json
from openvegas.agent.runtime_contracts import (
    RESULT_TOOL_STATUSES,
    TERMINAL_TOOL_STATUSES,
    ToolHeartbeatResponse,
    result_submission_hash,
)
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.telemetry import emit_metric


def rows_affected(exec_status: str) -> int:
    try:
        return int(str(exec_status).rsplit(" ", 1)[-1])
    except Exception:
        return 0


def _compiled_redaction_patterns() -> list[re.Pattern[str]]:
    raw = os.getenv("OPENVEGAS_TOOL_REDACT_PATTERNS", "")
    patterns: list[re.Pattern[str]] = []
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            patterns.append(re.compile(item))
        except re.error:
            continue
    return patterns


def redact_text(text: str) -> str:
    out = text
    for pat in _compiled_redaction_patterns():
        out = pat.sub("[REDACTED]", out)
    return out


def redaction_required(value: Any) -> bool:
    """Inspect nested output without changing callback hashes or stored payloads."""
    patterns = _compiled_redaction_patterns()
    pending = [value]
    visited = 0
    while pending:
        visited += 1
        if visited > 10000:
            return True
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and any(pat.search(item) for pat in patterns):
            return True
    return False


@dataclass(frozen=True)
class OutputEnvelope:
    text: str
    truncated: bool
    sha256: str


@dataclass(frozen=True)
class TerminalizeOutcome:
    state: str  # terminalized | replayed
    result_submission_hash: str
    response_status: int | None = None
    response_body_text: str | None = None


def redact_hash_truncate(text: str, cap_bytes: int) -> OutputEnvelope:
    redacted = redact_text(text or "")
    redacted_bytes = redacted.encode("utf-8")
    full_hash = hashlib.sha256(redacted_bytes).hexdigest()
    if len(redacted_bytes) <= cap_bytes:
        return OutputEnvelope(text=redacted, truncated=False, sha256=full_hash)
    trunc = redacted_bytes[:cap_bytes].decode("utf-8", errors="ignore")
    return OutputEnvelope(text=trunc, truncated=True, sha256=full_hash)


async def claim_started_tx(tx: Any, *, run_id: str, tool_call_id: str, execution_token: str) -> str:
    try:
        status = await tx.execute(
            """
            UPDATE agent_run_tool_calls
            SET status='started', claimed_at=now(), started_at=now(), last_heartbeat_at=now(), updated_at=now()
            WHERE id=$1::uuid
              AND run_id=$2::uuid
              AND status='proposed'
              AND execution_token=$3
            """,
            tool_call_id,
            run_id,
            execution_token,
        )
    except Exception as e:
        # Partial unique index ux_one_started_tool_per_run can raise here when another
        # started tool already exists on the run. Convert to contract error (409 path)
        # instead of leaking a raw DB exception as 500.
        text = str(e)
        if "ux_one_started_tool_per_run" in text or "duplicate key value violates unique constraint" in text:
            emit_metric("tool_cas_conflict_total", {"endpoint": "tool_start", "error": "one_started_unique"})
            raise ContractError(
                APIErrorCode.ACTIVE_MUTATION_IN_PROGRESS,
                "Another tool execution is already started for this run.",
            ) from e
        raise
    if rows_affected(status) == 1:
        return "claimed"
    emit_metric("tool_cas_conflict_total", {"endpoint": "tool_start"})

    row = await tx.fetchrow(
        """
        SELECT status, execution_token
        FROM agent_run_tool_calls
        WHERE id=$1::uuid AND run_id=$2::uuid
        FOR UPDATE
        """,
        tool_call_id,
        run_id,
    )
    if not row:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool start claim rejected.")
    if str(row["status"]) == "started" and str(row["execution_token"] or "") == execution_token:
        return "idempotent"
    if str(row["status"]) == "started" and str(row["execution_token"] or "") != execution_token:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool start ownership mismatch.")
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool start claim rejected.")


async def heartbeat_tx(
    tx: Any, *, run_id: str, tool_call_id: str, execution_token: str
) -> ToolHeartbeatResponse:
    status = await tx.execute(
        """
        UPDATE agent_run_tool_calls
        SET last_heartbeat_at=now(), updated_at=now()
        WHERE id=$1::uuid
          AND run_id=$2::uuid
          AND status='started'
          AND execution_token=$3
        """,
        tool_call_id,
        run_id,
        execution_token,
    )
    if rows_affected(status) == 1:
        return ToolHeartbeatResponse(active=True)
    emit_metric("tool_cas_conflict_total", {"endpoint": "tool_heartbeat"})

    row = await tx.fetchrow(
        """
        SELECT status, execution_token
        FROM agent_run_tool_calls
        WHERE id=$1::uuid AND run_id=$2::uuid
        FOR UPDATE
        """,
        tool_call_id,
        run_id,
    )
    if not row:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool heartbeat rejected.")

    row_status = str(row["status"] or "")
    row_token = str(row["execution_token"] or "")
    if row_status == "started" and row_token == execution_token:
        return ToolHeartbeatResponse(active=True)
    if row_status in TERMINAL_TOOL_STATUSES and row_token == execution_token:
        return ToolHeartbeatResponse(active=False, status=row_status)
    if row_status == "started" and row_token != execution_token:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool heartbeat ownership mismatch.")
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool heartbeat rejected.")


async def cancel_started_tx(tx: Any, *, run_id: str, tool_call_id: str, execution_token: str) -> str:
    status = await tx.execute(
        """
        UPDATE agent_run_tool_calls
        SET status='cancelled', finished_at=now(), updated_at=now()
        WHERE id=$1::uuid
          AND run_id=$2::uuid
          AND status='started'
          AND execution_token=$3
        """,
        tool_call_id,
        run_id,
        execution_token,
    )
    if rows_affected(status) == 1:
        return "cancelled"
    emit_metric("tool_cas_conflict_total", {"endpoint": "tool_cancel"})

    row = await tx.fetchrow(
        """
        SELECT status, execution_token
        FROM agent_run_tool_calls
        WHERE id=$1::uuid AND run_id=$2::uuid
        FOR UPDATE
        """,
        tool_call_id,
        run_id,
    )
    if not row:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool cancel rejected.")
    row_status = str(row["status"] or "")
    row_token = str(row["execution_token"] or "")
    if row_status == "cancelled" and row_token == execution_token:
        return "idempotent"
    if row_status == "cancelled" and row_token != execution_token:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool cancel ownership mismatch.")
    if row_status == "started" and row_token != execution_token:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool cancel ownership mismatch.")
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool cancel rejected.")


def is_started_tool_timed_out(
    *,
    started_at: datetime | None,
    last_heartbeat_at: datetime | None,
    now: datetime | None = None,
    timeout_seconds: int = 90,
) -> bool:
    if started_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    threshold = now - timedelta(seconds=max(1, int(timeout_seconds)))
    if last_heartbeat_at is None:
        return started_at < threshold
    return last_heartbeat_at < threshold


async def load_tool_for_result_tx(tx: Any, *, run_id: str, tool_call_id: str) -> Any:
    row = await tx.fetchrow(
        """
        SELECT *
        FROM agent_run_tool_calls
        WHERE id=$1::uuid AND run_id=$2::uuid
        FOR UPDATE
        """,
        tool_call_id,
        run_id,
    )
    if not row:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool call not found.")
    return row


async def terminalize_tx(
    tx: Any,
    *,
    run_id: str,
    tool_call_id: str,
    execution_token: str,
    result_status: str,
    result_payload: dict[str, Any],
    stdout_text: str,
    stderr_text: str,
    stdout_truncated: bool,
    stderr_truncated: bool,
    stdout_sha256: str,
    stderr_sha256: str,
    terminal_response_status: int,
    terminal_response_body_text: str,
    terminal_response_truncated: bool,
    terminal_response_hash: str | None,
) -> TerminalizeOutcome:
    r_hash = result_submission_hash(
        result_status=result_status,
        result_payload=result_payload,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
    )
    status = await tx.execute(
        """
        UPDATE agent_run_tool_calls
        SET status=$4,
            finished_at=now(),
            result_payload=$5::jsonb,
            stdout=$6,
            stderr=$7,
            stdout_truncated=$8,
            stderr_truncated=$9,
            stdout_sha256=$10,
            stderr_sha256=$11,
            result_submission_hash=$12,
            terminal_response_status=$13,
            terminal_response_content_type='application/json',
            terminal_response_body_text=$14,
            terminal_response_truncated=$15,
            terminal_response_hash=$16,
            updated_at=now()
        WHERE id=$1::uuid
          AND run_id=$2::uuid
          AND status='started'
          AND execution_token=$3
        """,
        tool_call_id,
        run_id,
        execution_token,
        result_status,
        canonical_json(result_payload),
        stdout_text,
        stderr_text,
        bool(stdout_truncated),
        bool(stderr_truncated),
        stdout_sha256,
        stderr_sha256,
        r_hash,
        int(terminal_response_status),
        terminal_response_body_text,
        bool(terminal_response_truncated),
        terminal_response_hash,
    )
    if rows_affected(status) == 1:
        return TerminalizeOutcome(state="terminalized", result_submission_hash=r_hash)
    emit_metric("tool_cas_conflict_total", {"endpoint": "tool_result"})

    row = await tx.fetchrow(
        """
        SELECT status, execution_token, result_submission_hash, terminal_response_status, terminal_response_body_text
        FROM agent_run_tool_calls
        WHERE id=$1::uuid AND run_id=$2::uuid
        FOR UPDATE
        """,
        tool_call_id,
        run_id,
    )
    if not row:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool result CAS rejected.")

    row_status = str(row["status"] or "")
    row_token = str(row["execution_token"] or "")
    if row_status == "cancelled":
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool was cancelled.")
    if row_status in RESULT_TOOL_STATUSES:
        existing_hash = str(row["result_submission_hash"] or "")
        if existing_hash == r_hash:
            return TerminalizeOutcome(
                state="replayed",
                result_submission_hash=r_hash,
                response_status=int(row["terminal_response_status"] or 200),
                response_body_text=str(row["terminal_response_body_text"] or "{}"),
            )
        raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Tool result hash mismatch.")
    if row_status == "started" and row_token != execution_token:
        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool result ownership mismatch.")
    raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool result CAS rejected.")
