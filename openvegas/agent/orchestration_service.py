"""Agent orchestration runtime service (v26 contracts)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openvegas.agent.orchestration_contracts import (
    MutatingResponseEnvelope,
    TERMINAL_RUN_STATES,
    UI_HANDOFF_BLOCK_REASONS,
    canonical_json,
    canonicalize_valid_actions,
    valid_actions_signature,
)
from openvegas.agent.runtime_contracts import (
    RESULT_TOOL_STATUSES,
    ShellMode,
    ToolName,
    is_mutating_tool,
    require_raw_sha256_hex,
    result_submission_hash,
    tool_payload_hash,
)
from openvegas.agent.tool_cas import (
    cancel_started_tx,
    claim_started_tx,
    heartbeat_tx,
    load_tool_for_result_tx,
    redact_hash_truncate,
    terminalize_tx,
)
from openvegas.agent.tool_stream import publish_tool_event
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.telemetry import emit_metric

logger = logging.getLogger(__name__)


def _rows_affected(exec_status: str) -> int:
    try:
        return int(str(exec_status).rsplit(" ", 1)[-1])
    except Exception:
        return 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _row_optional(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


@dataclass(frozen=True)
class MutationHTTPResult:
    status_code: int
    payload: dict[str, Any]


class AgentOrchestrationService:
    """Mutator-authoritative orchestration service."""

    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def _log_tool_lifecycle(
        *,
        event: str,
        run_id: str,
        tool_call_id: str | None,
        runtime_session_id: str | None,
        status: str | None = None,
    ) -> None:
        logger.info(
            "tool_lifecycle event=%s run_id=%s tool_call_id=%s runtime_session_id=%s status=%s",
            event,
            run_id,
            tool_call_id or "",
            runtime_session_id or "",
            status or "",
        )

    async def create_run(
        self,
        *,
        user_id: str,
        state: str = "created",
        is_resumable: bool = False,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        await self.db.execute(
            """
            INSERT INTO agent_runs
              (id, user_id, state, version, run_event_seq, is_resumable, state_entered_at, expires_at, updated_at)
            VALUES
              ($1::uuid, $2::uuid, $3, 0, 0, $4, now(), $5, now())
            """,
            run_id,
            user_id,
            state,
            bool(is_resumable),
            expires_at,
        )
        row = await self._fetch_run(user_id=user_id, run_id=run_id)
        payload = await self._success_envelope_for_row(row)
        payload["run_id"] = run_id
        return payload

    async def get_run(self, *, user_id: str, run_id: str) -> dict[str, Any]:
        row = await self._fetch_run(user_id=user_id, run_id=run_id)
        payload = await self._success_envelope_for_row(row)
        payload["run_id"] = run_id
        return payload

    async def transition_run(
        self,
        *,
        user_id: str,
        actor_role: str,
        run_id: str,
        idempotency_key: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> MutationHTTPResult:
        scope = "run_transition"
        role_class = self._actor_role_class(actor_role)
        normalized_payload = {
            "action": action,
            "payload": payload or {},
            "expected_run_version": int(expected_run_version),
            "expected_valid_actions_signature": str(expected_valid_actions_signature),
        }
        payload_hash = self._payload_hash(normalized_payload)

        replay = await self._replay_preread(
            run_id=run_id,
            tool_call_id=None,
            actor_id=user_id,
            actor_role_class=role_class,
            scope=scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if replay is not None:
            return replay

        lease_token = str(uuid.uuid4())
        lease_holder = f"{role_class}:{user_id}"
        expiry_sec = max(5, int(os.getenv("OPENVEGAS_AGENT_LEASE_TTL_SEC", "30")))

        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")

            await self._acquire_lease_tx(
                tx=tx,
                run_id=run_id,
                lease_holder=lease_holder,
                lease_token=lease_token,
                ttl_seconds=expiry_sec,
            )
            replay_id = await self._claim_replay_processing_tx(
                tx=tx,
                run_id=run_id,
                tool_call_id=None,
                actor_id=user_id,
                actor_role_class=role_class,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )

            run = await self._maybe_expire_run_tx(tx=tx, run=run)

            valid_actions = await self._derive_valid_actions_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            signature = valid_actions_signature(int(run["version"]), valid_actions)
            if int(run["version"]) != int(expected_run_version) or signature != expected_valid_actions_signature:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.STALE_PROJECTION.value,
                    detail="Client projection is stale.",
                    retryable=False,
                )
                await self._complete_replay_tx(
                    tx=tx,
                    replay_id=replay_id,
                    status_code=409,
                    payload=env,
                )
                await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            names = {str(a.get("action", "")) for a in valid_actions}
            if action not in names:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.INVALID_TRANSITION.value,
                    detail="Action is not valid for current run state.",
                    retryable=False,
                )
                await self._complete_replay_tx(
                    tx=tx,
                    replay_id=replay_id,
                    status_code=409,
                    payload=env,
                )
                await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            run = await self._apply_transition_action_tx(
                tx=tx,
                run=run,
                action=action,
            )

            success = await self._success_envelope_tx(
                tx=tx,
                run=run,
                actor_id=user_id,
                actor_role_class=role_class,
            )
            await self._insert_durable_event_tx(
                tx=tx,
                run_id=run_id,
                run_version=int(run["version"]),
                actor_id=user_id,
                event_type=f"run_transition:{action}",
                payload={"action": action, "resulting_state": str(run["state"])},
            )
            await self._complete_replay_tx(
                tx=tx,
                replay_id=replay_id,
                status_code=200,
                payload=success,
            )
            await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
            success["run_id"] = run_id
            return MutationHTTPResult(status_code=200, payload=success)

    async def consume_approval(
        self,
        *,
        user_id: str,
        actor_role: str,
        run_id: str,
        tool_call_id: str,
        approval_id: str,
        idempotency_key: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
    ) -> MutationHTTPResult:
        scope = "approval_decision"
        role_class = self._actor_role_class(actor_role)
        payload_hash = self._payload_hash(
            {
                "tool_call_id": tool_call_id,
                "approval_id": approval_id,
                "expected_run_version": int(expected_run_version),
                "expected_valid_actions_signature": str(expected_valid_actions_signature),
            }
        )
        replay = await self._replay_preread(
            run_id=run_id,
            tool_call_id=tool_call_id,
            actor_id=user_id,
            actor_role_class=role_class,
            scope=scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if replay is not None:
            return replay

        lease_token = str(uuid.uuid4())
        lease_holder = f"{role_class}:{user_id}"
        expiry_sec = max(5, int(os.getenv("OPENVEGAS_AGENT_LEASE_TTL_SEC", "30")))

        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")

            tool_call = await tx.fetchrow(
                """
                SELECT *
                FROM agent_run_tool_calls
                WHERE id = $1::uuid AND run_id = $2::uuid
                FOR UPDATE
                """,
                tool_call_id,
                run_id,
            )
            if not tool_call:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool call not found.")

            approval = await tx.fetchrow(
                """
                SELECT *
                FROM agent_tool_approvals
                WHERE id = $1::uuid AND run_id = $2::uuid AND tool_call_id = $3::uuid
                FOR UPDATE
                """,
                approval_id,
                run_id,
                tool_call_id,
            )
            if not approval:
                raise ContractError(APIErrorCode.APPROVAL_REQUIRED, "Approval not found.")

            await self._acquire_lease_tx(
                tx=tx,
                run_id=run_id,
                lease_holder=lease_holder,
                lease_token=lease_token,
                ttl_seconds=expiry_sec,
            )
            replay_id = await self._claim_replay_processing_tx(
                tx=tx,
                run_id=run_id,
                tool_call_id=tool_call_id,
                actor_id=user_id,
                actor_role_class=role_class,
                scope=scope,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )

            run = await self._maybe_expire_run_tx(tx=tx, run=run)
            valid_actions = await self._derive_valid_actions_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            signature = valid_actions_signature(int(run["version"]), valid_actions)
            if int(run["version"]) != int(expected_run_version) or signature != expected_valid_actions_signature:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.STALE_PROJECTION.value,
                    detail="Client projection is stale.",
                    retryable=False,
                )
                await self._complete_replay_tx(tx=tx, replay_id=replay_id, status_code=409, payload=env)
                await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            if str(approval["decision_state"]) != "approved" or approval["consumed_at"] is not None:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.APPROVAL_REQUIRED.value,
                    detail="Approval is not currently consumable.",
                    retryable=False,
                )
                await self._complete_replay_tx(tx=tx, replay_id=replay_id, status_code=409, payload=env)
                await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            consumed = await tx.execute(
                """
                UPDATE agent_tool_approvals
                SET decision_state = 'consumed',
                    consumed_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid
                  AND decision_state = 'approved'
                  AND consumed_at IS NULL
                  AND run_version_approved = $2
                """,
                approval_id,
                int(run["version"]),
            )
            if _rows_affected(consumed) != 1:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.APPROVAL_REQUIRED.value,
                    detail="Approval consumption precondition failed.",
                    retryable=False,
                )
                await self._complete_replay_tx(tx=tx, replay_id=replay_id, status_code=409, payload=env)
                await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            run = await self._increment_run_version_tx(
                tx=tx,
                run_id=run_id,
                next_state="running" if str(run["state"]) == "awaiting_approval" else str(run["state"]),
                state_reason_code=None if str(run["state"]) == "awaiting_approval" else run["state_reason_code"],
                is_resumable=bool(run["is_resumable"]),
                cancel_requested_at=run["cancel_requested_at"],
                cancel_disposition=run["cancel_disposition"],
            )
            success = await self._success_envelope_tx(
                tx=tx,
                run=run,
                actor_id=user_id,
                actor_role_class=role_class,
            )
            await self._insert_durable_event_tx(
                tx=tx,
                run_id=run_id,
                run_version=int(run["version"]),
                actor_id=user_id,
                event_type="approval_consumed",
                payload={"approval_id": approval_id, "tool_call_id": tool_call_id},
            )
            await self._complete_replay_tx(tx=tx, replay_id=replay_id, status_code=200, payload=success)
            await self._delete_lease_tx(tx=tx, run_id=run_id, lease_token=lease_token)
            success["run_id"] = run_id
            return MutationHTTPResult(status_code=200, payload=success)

    async def check_handoff_block(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")

            reason: str | None = None
            if str(run["state_reason_code"] or "") == APIErrorCode.MUTATION_UNCERTAIN.value:
                reason = UI_HANDOFF_BLOCK_REASONS[3]
            else:
                pending_approval = await tx.fetchrow(
                    """
                    SELECT 1
                    FROM agent_tool_approvals
                    WHERE run_id = $1::uuid
                      AND decision_state IN ('pending','approved')
                    LIMIT 1
                    """,
                    run_id,
                )
                if pending_approval:
                    reason = UI_HANDOFF_BLOCK_REASONS[0]
                else:
                    started_tool = await tx.fetchrow(
                        """
                        SELECT 1
                        FROM agent_run_tool_calls
                        WHERE run_id = $1::uuid
                          AND status = 'started'
                        LIMIT 1
                        """,
                        run_id,
                    )
                    if started_tool:
                        reason = UI_HANDOFF_BLOCK_REASONS[1]
                    else:
                        lease = await tx.fetchrow(
                            """
                            SELECT 1
                            FROM agent_run_mutation_leases
                            WHERE run_id = $1::uuid
                              AND expires_at > now()
                            LIMIT 1
                            """,
                            run_id,
                        )
                        if lease:
                            reason = UI_HANDOFF_BLOCK_REASONS[2]

            if reason:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class="user",
                    error=APIErrorCode.HANDOFF_BLOCKED.value,
                    detail="",
                    retryable=False,
                )
                env["handoff_block_reason"] = reason
                env["run_id"] = run_id
                return env

            env = await self._success_envelope_tx(
                tx=tx,
                run=run,
                actor_id=user_id,
                actor_role_class="user",
            )
            env["run_id"] = run_id
            return env

    async def register_workspace(
        self,
        *,
        user_id: str,
        run_id: str,
        runtime_session_id: str,
        workspace_root: str,
        workspace_fingerprint: str,
        git_root: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")

            has_tool = await tx.fetchrow(
                "SELECT 1 FROM agent_run_tool_calls WHERE run_id = $1::uuid LIMIT 1",
                run_id,
            )
            existing_session_raw = _row_optional(run, "runtime_session_id")
            existing_root_raw = _row_optional(run, "workspace_root")
            existing_fp_raw = _row_optional(run, "workspace_fingerprint")
            existing_git_raw = _row_optional(run, "git_root")
            existing_session = str(existing_session_raw) if existing_session_raw else None
            existing_root = str(existing_root_raw) if existing_root_raw else None
            existing_fp = str(existing_fp_raw) if existing_fp_raw else None
            existing_git = str(existing_git_raw) if existing_git_raw else None

            if has_tool and any(
                [
                    existing_session and existing_session != runtime_session_id,
                    existing_root and existing_root != workspace_root,
                    existing_fp and existing_fp != workspace_fingerprint,
                    existing_git and existing_git != (git_root or ""),
                ]
            ):
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION,
                    "Workspace registration is immutable after first proposed tool call.",
                )

            await tx.execute(
                """
                UPDATE agent_runs
                SET runtime_session_id = $2::uuid,
                    workspace_root = $3,
                    workspace_fingerprint = $4,
                    git_root = $5,
                    updated_at = now()
                WHERE id = $1::uuid
                """,
                run_id,
                runtime_session_id,
                workspace_root,
                workspace_fingerprint,
                git_root,
            )
            run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id = $1::uuid", run_id)
            payload = await self._success_envelope_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class="user"
            )
            payload["run_id"] = run_id
            payload["runtime_session_id"] = runtime_session_id
            return payload

    async def propose_tool_call(
        self,
        *,
        user_id: str,
        actor_role: str,
        run_id: str,
        runtime_session_id: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        idempotency_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        shell_mode: str | None,
        timeout_sec: int | None,
        plan_mode: bool,
    ) -> MutationHTTPResult:
        del idempotency_key
        role_class = self._actor_role_class(actor_role)
        tool_name = str(tool_name).strip()
        if tool_name not in {t.value for t in ToolName}:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, f"Unknown tool name: {tool_name}")

        normalized_args = self._normalize_tool_arguments(
            tool_name=tool_name,
            arguments=json.loads(canonical_json(arguments or {})),
        )
        self._validate_tool_arguments(tool_name=tool_name, arguments=normalized_args)
        shell_mode_norm = (shell_mode or ShellMode.READ_ONLY.value).strip()
        timeout_value = int(timeout_sec or self._default_timeout_sec(tool_name))
        timeout_value = max(1, min(timeout_value, self._default_timeout_sec(tool_name)))

        is_mutating = is_mutating_tool(tool_name, shell_mode_norm)
        requires_approval = is_mutating
        execution_token = uuid.uuid4().hex
        payload_hash = tool_payload_hash(tool_name, normalized_args, shell_mode_norm)

        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")
            await self._assert_runtime_session_tx(
                tx=tx,
                run=run,
                runtime_session_id=runtime_session_id,
            )

            valid_actions = await self._derive_valid_actions_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            signature = valid_actions_signature(int(run["version"]), valid_actions)
            if int(run["version"]) != int(expected_run_version) or signature != expected_valid_actions_signature:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.STALE_PROJECTION.value,
                    detail="Client projection is stale.",
                    retryable=False,
                )
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            status = "proposed"
            reason_code = None
            started_at = None
            finished_at = None
            terminal_status = 200
            if plan_mode and is_mutating:
                status = "blocked"
                reason_code = "tool_not_allowed_in_plan_mode"
                started_at = _utc_now()
                finished_at = _utc_now()
                terminal_status = 409

            tool_call_id = str(uuid.uuid4())
            await tx.execute(
                """
                INSERT INTO agent_run_tool_calls (
                  id, run_id, run_version, tool_name, tool_class, payload_hash, request_payload_json,
                  execution_token, status, commit_state, recovery_policy, approval_required, state_reason_code,
                  started_at, finished_at, created_at, updated_at
                ) VALUES (
                  $1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb,
                  $8, $9, 'not_applicable', 'not_resumable', $10, $11,
                  $12, $13, now(), now()
                )
                """,
                tool_call_id,
                run_id,
                int(run["version"]),
                tool_name,
                "mutating" if is_mutating else "read_only",
                payload_hash,
                canonical_json(
                    {
                        "tool_name": tool_name,
                        "arguments": normalized_args,
                        "shell_mode": shell_mode_norm,
                        "timeout_sec": timeout_value,
                    }
                ),
                execution_token,
                status,
                bool(requires_approval),
                reason_code,
                started_at,
                finished_at,
            )

            if status == "blocked":
                await self._insert_durable_event_tx(
                    tx=tx,
                    run_id=run_id,
                    run_version=int(run["version"]),
                    actor_id=user_id,
                    event_type="tool_finished_blocked",
                    payload={"tool_call_id": tool_call_id, "reason": reason_code},
                )
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.TOOL_NOT_ALLOWED_IN_PLAN_MODE.value,
                    detail="Mutating tool is blocked in plan mode.",
                    retryable=False,
                )
                env["run_id"] = run_id
                env["tool_call_id"] = tool_call_id
                return MutationHTTPResult(status_code=terminal_status, payload=env)

            env = await self._success_envelope_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            env["run_id"] = run_id
            env["tool_request"] = {
                "tool_call_id": tool_call_id,
                "execution_token": execution_token,
                "tool_name": tool_name,
                "arguments": normalized_args,
                "payload_hash": payload_hash,
                "requires_approval": requires_approval,
                "shell_mode": shell_mode_norm,
                "timeout_sec": timeout_value,
            }
            self._log_tool_lifecycle(
                event="tool_proposed",
                run_id=run_id,
                tool_call_id=tool_call_id,
                runtime_session_id=runtime_session_id,
                status="proposed",
            )
            return MutationHTTPResult(status_code=200, payload=env)

    async def start_tool_call(
        self,
        *,
        user_id: str,
        actor_role: str,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
        expected_run_version: int,
        expected_valid_actions_signature: str,
        idempotency_key: str,
    ) -> MutationHTTPResult:
        del idempotency_key
        role_class = self._actor_role_class(actor_role)
        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")
            await self._assert_runtime_session_tx(tx=tx, run=run, runtime_session_id=runtime_session_id)

            valid_actions = await self._derive_valid_actions_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            signature = valid_actions_signature(int(run["version"]), valid_actions)
            if int(run["version"]) != int(expected_run_version) or signature != expected_valid_actions_signature:
                env = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.STALE_PROJECTION.value,
                    detail="Client projection is stale.",
                    retryable=False,
                )
                env["run_id"] = run_id
                return MutationHTTPResult(status_code=409, payload=env)

            start_outcome = await claim_started_tx(
                tx,
                run_id=run_id,
                tool_call_id=tool_call_id,
                execution_token=execution_token,
            )
            if start_outcome == "claimed":
                await self._insert_durable_event_tx(
                    tx=tx,
                    run_id=run_id,
                    run_version=int(run["version"]),
                    actor_id=user_id,
                    event_type="tool_claimed_started",
                    payload={"tool_call_id": tool_call_id},
                )
            run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id = $1::uuid", run_id)
            env = await self._success_envelope_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            env["run_id"] = run_id
            env["tool_call_id"] = tool_call_id
            publish_tool_event(
                run_id=run_id,
                tool_call_id=tool_call_id,
                event={"run_id": run_id, "tool_call_id": tool_call_id, "status": "started"},
            )
            self._log_tool_lifecycle(
                event="tool_started",
                run_id=run_id,
                tool_call_id=tool_call_id,
                runtime_session_id=runtime_session_id,
                status="started",
            )
            return MutationHTTPResult(status_code=200, payload=env)

    async def heartbeat_tool_call(
        self,
        *,
        user_id: str,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
    ) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")
            await self._assert_runtime_session_tx(tx=tx, run=run, runtime_session_id=runtime_session_id)
            hb = await heartbeat_tx(
                tx,
                run_id=run_id,
                tool_call_id=tool_call_id,
                execution_token=execution_token,
            )
            payload = hb.as_dict()
            payload["run_id"] = run_id
            payload["tool_call_id"] = tool_call_id
            publish_tool_event(
                run_id=run_id,
                tool_call_id=tool_call_id,
                event={
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "status": str(payload.get("status") or ("started" if payload.get("active") else "unknown")),
                    "active": bool(payload.get("active", False)),
                },
            )
            self._log_tool_lifecycle(
                event="tool_heartbeat",
                run_id=run_id,
                tool_call_id=tool_call_id,
                runtime_session_id=runtime_session_id,
                status=str(payload.get("status") or ("started" if payload.get("active") else "inactive")),
            )
            return payload

    async def cancel_tool_call(
        self,
        *,
        user_id: str,
        actor_role: str,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
    ) -> MutationHTTPResult:
        role_class = self._actor_role_class(actor_role)
        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")
            await self._assert_runtime_session_tx(tx=tx, run=run, runtime_session_id=runtime_session_id)

            cancel_outcome = await cancel_started_tx(
                tx,
                run_id=run_id,
                tool_call_id=tool_call_id,
                execution_token=execution_token,
            )
            if cancel_outcome == "cancelled":
                await self._insert_durable_event_tx(
                    tx=tx,
                    run_id=run_id,
                    run_version=int(run["version"]),
                    actor_id=user_id,
                    event_type="tool_finished_cancelled",
                    payload={"tool_call_id": tool_call_id, "status": "cancelled"},
                )

            env = await self._success_envelope_tx(
                tx=tx, run=run, actor_id=user_id, actor_role_class=role_class
            )
            env["run_id"] = run_id
            env["tool_call_id"] = tool_call_id
            env["tool_status"] = "cancelled"
            publish_tool_event(
                run_id=run_id,
                tool_call_id=tool_call_id,
                event={"run_id": run_id, "tool_call_id": tool_call_id, "status": "cancelled"},
            )
            self._log_tool_lifecycle(
                event="tool_finished_cancelled",
                run_id=run_id,
                tool_call_id=tool_call_id,
                runtime_session_id=runtime_session_id,
                status="cancelled",
            )
            return MutationHTTPResult(status_code=200, payload=env)

    async def result_tool_call(
        self,
        *,
        user_id: str,
        actor_role: str,
        run_id: str,
        runtime_session_id: str,
        tool_call_id: str,
        execution_token: str,
        result_status: str,
        result_payload: dict[str, Any],
        stdout: str,
        stderr: str,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        stdout_sha256: str | None = None,
        stderr_sha256: str | None = None,
        result_submission_hash_value: str | None = None,
    ) -> MutationHTTPResult:
        # tool-result is token/session-bound CAS once execution was started.
        role_class = self._actor_role_class(actor_role)
        result_status = str(result_status).strip()
        if result_status not in RESULT_TOOL_STATUSES:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Invalid terminal tool status.")

        stdout_cap = max(1024, int(os.getenv("OPENVEGAS_TOOL_STDOUT_MAX_BYTES", "131072")))
        stderr_cap = max(1024, int(os.getenv("OPENVEGAS_TOOL_STDERR_MAX_BYTES", "131072")))
        payload_cap = max(256, int(os.getenv("OPENVEGAS_TOOL_RESULT_PAYLOAD_MAX_BYTES", "65536")))
        payload_text = canonical_json(result_payload)
        if len(payload_text.encode("utf-8")) > payload_cap:
            raise ContractError(
                APIErrorCode.TOOL_EXECUTION_FAILED,
                "Result payload exceeds max serialized JSON size.",
            )

        stdout_env = redact_hash_truncate(stdout or "", stdout_cap)
        stderr_env = redact_hash_truncate(stderr or "", stderr_cap)
        # Optional client-submitted hash fields are format-validated and compared as
        # hints; authoritative persistence remains server-computed.
        if stdout_sha256 is not None:
            require_raw_sha256_hex(str(stdout_sha256), "stdout_sha256")
        if stderr_sha256 is not None:
            require_raw_sha256_hex(str(stderr_sha256), "stderr_sha256")
        if result_submission_hash_value is not None:
            require_raw_sha256_hex(str(result_submission_hash_value), "result_submission_hash")
        if stdout_sha256 is not None and str(stdout_sha256) != stdout_env.sha256:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "stdout_sha256 mismatch.")
        if stderr_sha256 is not None and str(stderr_sha256) != stderr_env.sha256:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "stderr_sha256 mismatch.")
        if bool(stdout_truncated) and not stdout_env.truncated:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "stdout_truncated mismatch.")
        if bool(stderr_truncated) and not stderr_env.truncated:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "stderr_truncated mismatch.")

        incoming_hash = result_submission_hash_value or result_submission_hash(
            result_status=result_status,
            result_payload=result_payload,
            stdout_sha256=stdout_env.sha256,
            stderr_sha256=stderr_env.sha256,
        )

        async with self.db.transaction() as tx:
            run = await tx.fetchrow(
                """
                SELECT *
                FROM agent_runs
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                run_id,
                user_id,
            )
            if not run:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")
            await self._assert_runtime_session_tx(tx=tx, run=run, runtime_session_id=runtime_session_id)

            tool = await load_tool_for_result_tx(tx, run_id=run_id, tool_call_id=tool_call_id)
            tool_status = str(tool["status"] or "")
            if tool_status == "cancelled":
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool was cancelled.")
            if tool_status in RESULT_TOOL_STATUSES:
                if str(tool["result_submission_hash"] or "") == incoming_hash:
                    body_text = str(tool["terminal_response_body_text"] or "{}")
                    try:
                        payload = json.loads(body_text)
                    except Exception:
                        payload = {}
                    return MutationHTTPResult(
                        status_code=int(tool["terminal_response_status"] or 200),
                        payload=payload,
                    )
                raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Tool result hash mismatch.")

            if tool_status != "started" or str(tool["execution_token"] or "") != execution_token:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Tool result cannot be accepted in current state.")

            if result_status == "succeeded":
                response_payload = await self._success_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    ignore_started_tool_call_id=tool_call_id,
                )
                response_status = 200
                event_type = "tool_finished_succeeded"
            elif result_status == "timed_out":
                response_payload = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.TOOL_TIMEOUT.value,
                    detail="Tool execution timed out.",
                    retryable=False,
                    ignore_started_tool_call_id=tool_call_id,
                )
                response_status = 409
                event_type = "tool_finished_timed_out"
            elif result_status == "blocked":
                reason = str(result_payload.get("reason_code") or APIErrorCode.INVALID_TRANSITION.value)
                response_payload = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=reason,
                    detail="Tool execution blocked by policy.",
                    retryable=False,
                    ignore_started_tool_call_id=tool_call_id,
                )
                response_status = 409
                event_type = "tool_finished_blocked"
            else:
                response_payload = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=user_id,
                    actor_role_class=role_class,
                    error=APIErrorCode.TOOL_EXECUTION_FAILED.value,
                    detail="Tool execution failed.",
                    retryable=False,
                    ignore_started_tool_call_id=tool_call_id,
                )
                response_status = 409
                event_type = "tool_finished_failed"

            response_payload["run_id"] = run_id
            response_payload["tool_call_id"] = tool_call_id
            response_text = canonical_json(response_payload)
            response_bytes = response_text.encode("utf-8")
            response_cap = max(1024, int(os.getenv("OPENVEGAS_TOOL_RESPONSE_MAX_BYTES", "131072")))
            if len(response_bytes) > response_cap:
                stored_response = response_bytes[:response_cap].decode("utf-8", errors="ignore")
                response_truncated = True
                response_hash = hashlib.sha256(response_bytes).hexdigest()
            else:
                stored_response = response_text
                response_truncated = False
                response_hash = None

            term_outcome = await terminalize_tx(
                tx,
                run_id=run_id,
                tool_call_id=tool_call_id,
                execution_token=execution_token,
                result_status=result_status,
                result_payload=result_payload,
                stdout_text=stdout_env.text,
                stderr_text=stderr_env.text,
                stdout_truncated=stdout_env.truncated,
                stderr_truncated=stderr_env.truncated,
                stdout_sha256=stdout_env.sha256,
                stderr_sha256=stderr_env.sha256,
                terminal_response_status=response_status,
                terminal_response_body_text=stored_response,
                terminal_response_truncated=response_truncated,
                terminal_response_hash=response_hash,
            )
            if term_outcome.state == "replayed":
                replay_text = str(term_outcome.response_body_text or "{}")
                try:
                    replay_payload = json.loads(replay_text)
                except Exception:
                    replay_payload = {}
                return MutationHTTPResult(
                    status_code=int(term_outcome.response_status or 200),
                    payload=replay_payload,
                )
            await self._insert_durable_event_tx(
                tx=tx,
                run_id=run_id,
                run_version=int(run["version"]),
                actor_id=user_id,
                event_type=event_type,
                payload={"tool_call_id": tool_call_id, "status": result_status},
            )
            publish_tool_event(
                run_id=run_id,
                tool_call_id=tool_call_id,
                event={"run_id": run_id, "tool_call_id": tool_call_id, "status": result_status},
            )
            self._log_tool_lifecycle(
                event=event_type,
                run_id=run_id,
                tool_call_id=tool_call_id,
                runtime_session_id=runtime_session_id,
                status=result_status,
            )
            return MutationHTTPResult(status_code=response_status, payload=response_payload)

    async def reconcile_stale_started_tools(self, *, timeout_seconds: int = 90) -> int:
        """Timeout started tool rows when heartbeat is stale (NULL-safe fallback)."""
        timeout_seconds = max(5, int(timeout_seconds))
        empty_hash = hashlib.sha256(b"").hexdigest()
        touched = 0
        async with self.db.transaction() as tx:
            stale_rows = await tx.fetch(
                """
                SELECT id, run_id
                FROM agent_run_tool_calls
                WHERE status='started'
                  AND (
                    (last_heartbeat_at IS NULL AND started_at < now() - make_interval(secs => $1))
                    OR
                    (last_heartbeat_at IS NOT NULL AND last_heartbeat_at < now() - make_interval(secs => $1))
                  )
                FOR UPDATE SKIP LOCKED
                """,
                timeout_seconds,
            )
            for row in stale_rows:
                run_id = str(row["run_id"])
                tool_call_id = str(row["id"])
                emit_metric("tool_heartbeat_miss_total", {"source": "reconciler"})
                run = await tx.fetchrow("SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE", run_id)
                if not run:
                    continue

                result_payload = {
                    "ok": False,
                    "reason_code": APIErrorCode.TOOL_TIMEOUT.value,
                    "detail": "Heartbeat expired.",
                }
                envelope = await self._error_envelope_tx(
                    tx=tx,
                    run=run,
                    actor_id=str(run["user_id"]),
                    actor_role_class="reconciler",
                    error=APIErrorCode.TOOL_TIMEOUT.value,
                    detail="Tool heartbeat expired.",
                    retryable=False,
                )
                envelope["run_id"] = run_id
                envelope["tool_call_id"] = tool_call_id
                envelope_text = canonical_json(envelope)
                result_hash = result_submission_hash(
                    result_status="timed_out",
                    result_payload=result_payload,
                    stdout_sha256=empty_hash,
                    stderr_sha256=empty_hash,
                )
                updated = await tx.execute(
                    """
                    UPDATE agent_run_tool_calls
                    SET status='timed_out',
                        finished_at=now(),
                        result_payload=$3::jsonb,
                        stdout='',
                        stderr='',
                        stdout_truncated=FALSE,
                        stderr_truncated=FALSE,
                        stdout_sha256=$4,
                        stderr_sha256=$5,
                        result_submission_hash=$6,
                        terminal_response_status=409,
                        terminal_response_content_type='application/json',
                        terminal_response_body_text=$7,
                        terminal_response_truncated=FALSE,
                        terminal_response_hash=NULL,
                        updated_at=now()
                    WHERE id=$1::uuid
                      AND run_id=$2::uuid
                      AND status='started'
                    """,
                    tool_call_id,
                    run_id,
                    canonical_json(result_payload),
                    empty_hash,
                    empty_hash,
                    result_hash,
                    envelope_text,
                )
                if _rows_affected(updated) != 1:
                    continue
                touched += 1
                await self._insert_durable_event_tx(
                    tx=tx,
                    run_id=run_id,
                    run_version=int(run["version"]),
                    actor_id=str(run["user_id"]),
                    event_type="tool_finished_timed_out",
                    payload={"tool_call_id": tool_call_id, "status": "timed_out", "source": "reconciler"},
                )
                publish_tool_event(
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    event={"run_id": run_id, "tool_call_id": tool_call_id, "status": "timed_out"},
                )
        return touched

    async def _assert_runtime_session_tx(self, *, tx: Any, run: Any, runtime_session_id: str) -> None:
        if not runtime_session_id:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "runtime_session_id is required.")
        bound = str(run["runtime_session_id"]) if run["runtime_session_id"] else None
        if bound is None:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run workspace/session is not registered.")
        if bound != runtime_session_id:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "runtime_session_id mismatch.")

    @staticmethod
    def _normalize_tool_arguments(*, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments or {})

        def _coerce_nonempty_str(v: Any) -> str | None:
            if isinstance(v, str):
                s = v.strip()
                return s if s else None
            if isinstance(v, (int, float, bool)):
                return str(v)
            return None

        def _deep_find_keyed_string(v: Any, keys: tuple[str, ...], depth: int = 0) -> str | None:
            if depth > 3:
                return None
            if isinstance(v, dict):
                for k in keys:
                    if k in v:
                        hit = _coerce_nonempty_str(v.get(k))
                        if hit:
                            return hit
                        hit = _deep_find_keyed_string(v.get(k), keys, depth + 1)
                        if hit:
                            return hit
                for child in v.values():
                    hit = _deep_find_keyed_string(child, keys, depth + 1)
                    if hit:
                        return hit
            if isinstance(v, list):
                for child in v:
                    hit = _deep_find_keyed_string(child, keys, depth + 1)
                    if hit:
                        return hit
            return None

        # Some providers emit nested argument objects (e.g. {"file":{"path":"..."}}).
        # Lift known scalar fields from one-level nested dicts before alias mapping.
        for key, value in list(args.items()):
            if isinstance(value, dict):
                if "path" not in args and isinstance(value.get("path"), str):
                    args["path"] = value.get("path")
                if "pattern" not in args and isinstance(value.get("pattern"), str):
                    args["pattern"] = value.get("pattern")
                if "query" not in args and isinstance(value.get("query"), str):
                    args["query"] = value.get("query")
                if "keyword" not in args and isinstance(value.get("keyword"), str):
                    args["keyword"] = value.get("keyword")
                if "command" not in args and isinstance(value.get("command"), str):
                    args["command"] = value.get("command")
                if "patch" not in args and isinstance(value.get("patch"), str):
                    args["patch"] = value.get("patch")
                if "line" not in args and value.get("line") is not None:
                    args["line"] = value.get("line")
                if "col" not in args and value.get("col") is not None:
                    args["col"] = value.get("col")

        def _alias(dest: str, keys: tuple[str, ...], default: Any = None) -> None:
            value = _coerce_nonempty_str(args.get(dest, None))
            if value is None:
                for k in keys:
                    alias_value = _coerce_nonempty_str(args.get(k))
                    if alias_value is not None:
                        args[dest] = alias_value
                        return
                if dest not in args and default is not None:
                    args[dest] = default

        if tool_name in {ToolName.FS_READ.value, ToolName.EDITOR_OPEN.value}:
            _alias("path", ("file_path", "filepath", "file", "target_path"))
            if "line" not in args and "line_number" in args:
                args["line"] = args.get("line_number")
            if "col" not in args and "column" in args:
                args["col"] = args.get("column")
            return args

        if tool_name == ToolName.FS_LIST.value:
            _alias("path", ("dir", "directory", "target_path"), default=".")
            return args

        if tool_name == ToolName.FS_SEARCH.value:
            _alias("pattern", ("query", "term", "text", "regex", "keyword", "needle", "search_term"))
            _alias("path", ("dir", "directory", "target_path"), default=".")
            if not isinstance(args.get("pattern"), str) or not str(args.get("pattern")).strip():
                inferred = _deep_find_keyed_string(
                    args,
                    ("pattern", "query", "text", "term", "value", "keyword", "needle", "search"),
                )
                if inferred:
                    args["pattern"] = inferred
            return args

        if tool_name == ToolName.FS_APPLY_PATCH.value:
            _alias("patch", ("diff", "patch_text", "unified_diff", "changes", "edit", "edits", "text", "value"))
            if not isinstance(args.get("patch"), str) or not str(args.get("patch")).strip():
                inferred = _deep_find_keyed_string(
                    args,
                    ("patch", "diff", "patch_text", "unified_diff", "changes", "edit", "text", "value"),
                )
                if inferred:
                    args["patch"] = inferred
            return args

        if tool_name == ToolName.SHELL_RUN.value:
            _alias("command", ("cmd", "shell_command", "script"))
            if not isinstance(args.get("command"), str) or not str(args.get("command")).strip():
                inferred = _deep_find_keyed_string(
                    args,
                    ("command", "cmd", "shell_command", "script", "text", "value"),
                )
                if inferred:
                    args["command"] = inferred
            return args
        if tool_name == ToolName.MCP_CALL.value:
            _alias("server_id", ("server", "mcp_server_id"))
            _alias("tool", ("tool_name", "name"))
            if not isinstance(args.get("arguments"), dict):
                nested = args.get("args")
                if isinstance(nested, dict):
                    args["arguments"] = nested
                else:
                    args["arguments"] = {}
            return args

        return args

    @staticmethod
    def _default_timeout_sec(tool_name: str) -> int:
        if tool_name in {ToolName.FS_LIST.value, ToolName.FS_READ.value, ToolName.FS_SEARCH.value, ToolName.EDITOR_OPEN.value}:
            return 5
        if tool_name == ToolName.FS_APPLY_PATCH.value:
            return 30
        if tool_name == ToolName.SHELL_RUN.value:
            return int(os.getenv("OPENVEGAS_TOOL_SHELL_TIMEOUT_SEC", "30"))
        if tool_name == ToolName.MCP_CALL.value:
            return int(os.getenv("OPENVEGAS_TOOL_MCP_TIMEOUT_SEC", "20"))
        return 30

    @staticmethod
    def _validate_tool_arguments(*, tool_name: str, arguments: dict[str, Any]) -> None:
        def _require_string(name: str) -> None:
            if not isinstance(arguments.get(name), str) or not str(arguments.get(name)).strip():
                raise ContractError(APIErrorCode.INVALID_TRANSITION, f"{tool_name} requires string field: {name}")

        if tool_name in {ToolName.FS_READ.value, ToolName.EDITOR_OPEN.value}:
            _require_string("path")
            return
        if tool_name == ToolName.FS_LIST.value:
            if "path" in arguments and arguments.get("path") is not None:
                _require_string("path")
            return
        if tool_name == ToolName.FS_SEARCH.value:
            _require_string("pattern")
            return
        if tool_name == ToolName.FS_APPLY_PATCH.value:
            _require_string("patch")
            return
        if tool_name == ToolName.SHELL_RUN.value:
            _require_string("command")
            return
        if tool_name == ToolName.MCP_CALL.value:
            _require_string("server_id")
            _require_string("tool")
            if "arguments" in arguments and not isinstance(arguments.get("arguments"), dict):
                raise ContractError(
                    APIErrorCode.INVALID_TRANSITION,
                    f"{tool_name} requires object field: arguments",
                )

    async def _fetch_run(self, *, user_id: str, run_id: str) -> Any:
        row = await self.db.fetchrow(
            """
            SELECT *
            FROM agent_runs
            WHERE id = $1::uuid AND user_id = $2::uuid
            """,
            run_id,
            user_id,
        )
        if not row:
            raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run not found.")
        return row

    async def _success_envelope_for_row(self, row: Any) -> dict[str, Any]:
        valid_actions = await self._derive_valid_actions_db(
            run_id=str(row["id"]),
            run=row,
            actor_id=str(row["user_id"]),
            actor_role_class="user",
        )
        projection_version = await self._projection_version(str(row["id"]))
        env = MutatingResponseEnvelope(
            error=None,
            detail="",
            retryable=False,
            current_state=str(row["state"]),
            run_version=int(row["version"]),
            projection_version=projection_version,
            valid_actions=valid_actions,
            valid_actions_signature=valid_actions_signature(int(row["version"]), valid_actions),
        )
        return env.as_dict()

    async def _success_envelope_tx(
        self,
        *,
        tx: Any,
        run: Any,
        actor_id: str,
        actor_role_class: str,
        ignore_started_tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        valid_actions = await self._derive_valid_actions_tx(
            tx=tx,
            run=run,
            actor_id=actor_id,
            actor_role_class=actor_role_class,
            ignore_started_tool_call_id=ignore_started_tool_call_id,
        )
        projection_version = await self._projection_version_tx(tx, str(run["id"]))
        env = MutatingResponseEnvelope(
            error=None,
            detail="",
            retryable=False,
            current_state=str(run["state"]),
            run_version=int(run["version"]),
            projection_version=projection_version,
            valid_actions=valid_actions,
            valid_actions_signature=valid_actions_signature(int(run["version"]), valid_actions),
        )
        return env.as_dict()

    async def _error_envelope_tx(
        self,
        *,
        tx: Any,
        run: Any,
        actor_id: str,
        actor_role_class: str,
        error: str,
        detail: str,
        retryable: bool,
        ignore_started_tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        valid_actions = await self._derive_valid_actions_tx(
            tx=tx,
            run=run,
            actor_id=actor_id,
            actor_role_class=actor_role_class,
            ignore_started_tool_call_id=ignore_started_tool_call_id,
        )
        projection_version = await self._projection_version_tx(tx, str(run["id"]))
        env = MutatingResponseEnvelope(
            error=error,
            detail=detail,
            retryable=retryable,
            current_state=str(run["state"]),
            run_version=int(run["version"]),
            projection_version=projection_version,
            valid_actions=valid_actions,
            valid_actions_signature=valid_actions_signature(int(run["version"]), valid_actions),
        )
        return env.as_dict()

    async def _projection_version(self, run_id: str) -> int:
        row = await self.db.fetchrow(
            "SELECT projection_version FROM run_status_projection WHERE run_id = $1::uuid",
            run_id,
        )
        if not row:
            return 0
        return int(row["projection_version"])

    async def _projection_version_tx(self, tx: Any, run_id: str) -> int:
        row = await tx.fetchrow(
            "SELECT projection_version FROM run_status_projection WHERE run_id = $1::uuid",
            run_id,
        )
        if not row:
            return 0
        return int(row["projection_version"])

    async def _derive_valid_actions_db(
        self, *, run_id: str, run: Any, actor_id: str, actor_role_class: str
    ) -> list[dict[str, Any]]:
        async with self.db.transaction() as tx:
            return await self._derive_valid_actions_tx(
                tx=tx, run=run, actor_id=actor_id, actor_role_class=actor_role_class
            )

    async def _derive_valid_actions_tx(
        self,
        *,
        tx: Any,
        run: Any,
        actor_id: str,
        actor_role_class: str,
        ignore_started_tool_call_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del actor_id, actor_role_class
        run_id = str(run["id"])
        state = str(run["state"])
        if state in TERMINAL_RUN_STATES:
            return []

        if ignore_started_tool_call_id:
            started_row = await tx.fetchrow(
                """
                SELECT 1
                FROM agent_run_tool_calls
                WHERE run_id = $1::uuid
                  AND status = 'started'
                  AND id <> $2::uuid
                LIMIT 1
                """,
                run_id,
                ignore_started_tool_call_id,
            )
        else:
            started_row = await tx.fetchrow(
                """
                SELECT 1
                FROM agent_run_tool_calls
                WHERE run_id = $1::uuid AND status = 'started'
                LIMIT 1
                """,
                run_id,
            )
        has_started_tool = bool(started_row)

        approved = await tx.fetch(
            """
            SELECT id, tool_call_id
            FROM agent_tool_approvals
            WHERE run_id = $1::uuid
              AND decision_state = 'approved'
              AND consumed_at IS NULL
              AND run_version_approved = $2
            ORDER BY tool_call_id ASC
            """,
            run_id,
            int(run["version"]),
        )

        actions: list[dict[str, Any]] = []
        for row in approved:
            actions.append(
                {
                    "action": "approve",
                    "approval_id": str(row["id"]),
                    "tool_call_id": str(row["tool_call_id"]),
                }
            )

        if state == "awaiting_approval":
            actions.append({"action": "cancel"})
            return canonicalize_valid_actions(actions)

        if state == "created":
            actions.extend([{"action": "resume"}, {"action": "cancel"}])
            return canonicalize_valid_actions(actions)

        if state == "interrupted":
            if bool(run["is_resumable"]):
                actions.append({"action": "resume"})
            actions.append({"action": "cancel"})
            return canonicalize_valid_actions(actions)

        if state == "running":
            actions.append({"action": "cancel"})
            if not has_started_tool:
                actions.append({"action": "handoff"})
            return canonicalize_valid_actions(actions)

        return canonicalize_valid_actions(actions)

    @staticmethod
    def _actor_role_class(actor_role: str) -> str:
        role = str(actor_role or "").strip().lower()
        if role in {"service_role", "admin"}:
            return "admin"
        if role in {"system", "worker", "reconciler"}:
            return role
        return "user"

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    async def _replay_preread(
        self,
        *,
        run_id: str,
        tool_call_id: str | None,
        actor_id: str,
        actor_role_class: str,
        scope: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> MutationHTTPResult | None:
        row = await self.db.fetchrow(
            """
            SELECT id, payload_hash, status, response_status, content_type, response_body_text
            FROM agent_mutation_replays
            WHERE run_id = $1::uuid
              AND actor_id = $2::uuid
              AND actor_role_class = $3
              AND scope = $4
              AND idempotency_key = $5
              AND (
                ($6::uuid IS NULL AND tool_call_id IS NULL)
                OR
                ($6::uuid IS NOT NULL AND tool_call_id = $6::uuid)
              )
            """,
            run_id,
            actor_id,
            actor_role_class,
            scope,
            idempotency_key,
            tool_call_id,
        )
        if not row:
            return None
        if str(row["payload_hash"]) != payload_hash:
            raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency payload mismatch.")
        status = str(row["status"])
        if status == "completed":
            body = json.loads(str(row["response_body_text"])) if row["response_body_text"] else {}
            return MutationHTTPResult(status_code=int(row["response_status"]), payload=body)
        if status == "processing":
            raise ContractError(
                APIErrorCode.ACTIVE_MUTATION_IN_PROGRESS,
                "Mutation is already in progress.",
            )
        return None

    async def _claim_replay_processing_tx(
        self,
        *,
        tx: Any,
        run_id: str,
        tool_call_id: str | None,
        actor_id: str,
        actor_role_class: str,
        scope: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> str:
        replay_id = str(uuid.uuid4())
        inserted = await tx.fetchrow(
            """
            INSERT INTO agent_mutation_replays
              (id, run_id, tool_call_id, actor_id, actor_role_class, scope, idempotency_key, payload_hash, status)
            VALUES
              ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8, 'processing')
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            replay_id,
            run_id,
            tool_call_id,
            actor_id,
            actor_role_class,
            scope,
            idempotency_key,
            payload_hash,
        )
        if inserted:
            return str(inserted["id"])

        row = await tx.fetchrow(
            """
            SELECT id, payload_hash, status
            FROM agent_mutation_replays
            WHERE run_id = $1::uuid
              AND actor_id = $2::uuid
              AND actor_role_class = $3
              AND scope = $4
              AND idempotency_key = $5
              AND (
                ($6::uuid IS NULL AND tool_call_id IS NULL)
                OR
                ($6::uuid IS NOT NULL AND tool_call_id = $6::uuid)
              )
            FOR UPDATE
            """,
            run_id,
            actor_id,
            actor_role_class,
            scope,
            idempotency_key,
            tool_call_id,
        )
        if not row:
            raise ContractError(APIErrorCode.HOLD_CONFLICT, "Replay row resolution failed.")
        if str(row["payload_hash"]) != payload_hash:
            raise ContractError(APIErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency payload mismatch.")

        rid = str(row["id"])
        status = str(row["status"])
        if status == "processing":
            first = await tx.execute(
                """
                UPDATE agent_mutation_replays
                SET status = 'failed', updated_at = now()
                WHERE id = $1::uuid AND status = 'processing'
                """,
                rid,
            )
            if _rows_affected(first) != 1:
                raise ContractError(
                    APIErrorCode.ACTIVE_MUTATION_IN_PROGRESS,
                    "Mutation replay row is owned by another active worker.",
                )
            second = await tx.execute(
                """
                UPDATE agent_mutation_replays
                SET status = 'processing',
                    payload_hash = $2,
                    response_status = NULL,
                    content_type = NULL,
                    response_body_text = NULL,
                    response_truncated = FALSE,
                    response_hash = NULL,
                    updated_at = now()
                WHERE id = $1::uuid AND status = 'failed'
                """,
                rid,
                payload_hash,
            )
            if _rows_affected(second) != 1:
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Replay CAS reclaim failed.")
            return rid

        if status == "failed":
            reopened = await tx.execute(
                """
                UPDATE agent_mutation_replays
                SET status = 'processing',
                    payload_hash = $2,
                    response_status = NULL,
                    content_type = NULL,
                    response_body_text = NULL,
                    response_truncated = FALSE,
                    response_hash = NULL,
                    updated_at = now()
                WHERE id = $1::uuid AND status = 'failed'
                """,
                rid,
                payload_hash,
            )
            if _rows_affected(reopened) != 1:
                raise ContractError(APIErrorCode.HOLD_CONFLICT, "Replay reopen failed.")
            return rid

        raise ContractError(APIErrorCode.HOLD_CONFLICT, "Unexpected replay status.")

    async def _acquire_lease_tx(
        self,
        *,
        tx: Any,
        run_id: str,
        lease_holder: str,
        lease_token: str,
        ttl_seconds: int,
    ) -> None:
        row = await tx.fetchrow(
            """
            SELECT run_id, lease_token, expires_at
            FROM agent_run_mutation_leases
            WHERE run_id = $1::uuid
            FOR UPDATE
            """,
            run_id,
        )
        if not row:
            await tx.execute(
                """
                INSERT INTO agent_run_mutation_leases (run_id, lease_holder, lease_token, acquired_at, expires_at)
                VALUES ($1::uuid, $2, $3::uuid, now(), now() + ($4 || ' seconds')::interval)
                """,
                run_id,
                lease_holder,
                lease_token,
                int(ttl_seconds),
            )
            return

        expires_at = row["expires_at"]
        if expires_at is not None and expires_at > _utc_now():
            raise ContractError(
                APIErrorCode.ACTIVE_MUTATION_IN_PROGRESS,
                "Another mutation lease is currently active.",
            )

        await tx.execute(
            """
            UPDATE agent_run_mutation_leases
            SET lease_holder = $2,
                lease_token = $3::uuid,
                acquired_at = now(),
                expires_at = now() + ($4 || ' seconds')::interval
            WHERE run_id = $1::uuid
            """,
            run_id,
            lease_holder,
            lease_token,
            int(ttl_seconds),
        )

    async def _delete_lease_tx(self, *, tx: Any, run_id: str, lease_token: str) -> None:
        status = await tx.execute(
            """
            DELETE FROM agent_run_mutation_leases
            WHERE run_id = $1::uuid
              AND lease_token = $2::uuid
            """,
            run_id,
            lease_token,
        )
        if _rows_affected(status) != 1:
            raise ContractError(
                APIErrorCode.LEASE_DELETE_MISMATCH,
                "Mutation lease deletion mismatch.",
            )

    async def _maybe_expire_run_tx(self, *, tx: Any, run: Any) -> Any:
        if str(run["state"]) in TERMINAL_RUN_STATES:
            return run
        expires_at = run["expires_at"]
        if expires_at is None or expires_at > _utc_now():
            return run
        return await self._increment_run_version_tx(
            tx=tx,
            run_id=str(run["id"]),
            next_state="expired",
            state_reason_code="expired_timeout",
            is_resumable=False,
            cancel_requested_at=run["cancel_requested_at"],
            cancel_disposition=run["cancel_disposition"],
        )

    async def _apply_transition_action_tx(self, *, tx: Any, run: Any, action: str) -> Any:
        run_id = str(run["id"])
        state = str(run["state"])
        if action == "resume":
            if state == "interrupted" and not bool(run["is_resumable"]):
                raise ContractError(APIErrorCode.RUN_NOT_RESUMABLE, "Run is not resumable.")
            if state not in {"created", "interrupted"}:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run cannot be resumed from current state.")
            return await self._increment_run_version_tx(
                tx=tx,
                run_id=run_id,
                next_state="running",
                state_reason_code=None,
                is_resumable=bool(run["is_resumable"]),
                cancel_requested_at=run["cancel_requested_at"],
                cancel_disposition=run["cancel_disposition"],
            )

        if action == "cancel":
            if state in TERMINAL_RUN_STATES:
                raise ContractError(APIErrorCode.INVALID_TRANSITION, "Run is already terminal.")
            started_row = await tx.fetchrow(
                "SELECT 1 FROM agent_run_tool_calls WHERE run_id = $1::uuid AND status = 'started' LIMIT 1",
                run_id,
            )
            if started_row:
                return await self._increment_run_version_tx(
                    tx=tx,
                    run_id=run_id,
                    next_state=state,
                    state_reason_code=run["state_reason_code"],
                    is_resumable=bool(run["is_resumable"]),
                    cancel_requested_at=_utc_now(),
                    cancel_disposition=run["cancel_disposition"],
                )
            return await self._increment_run_version_tx(
                tx=tx,
                run_id=run_id,
                next_state="canceled",
                state_reason_code="canceled_user",
                is_resumable=False,
                cancel_requested_at=run["cancel_requested_at"] or _utc_now(),
                cancel_disposition="canceled_by_user",
            )

        if action == "handoff":
            return run

        raise ContractError(APIErrorCode.INVALID_TRANSITION, "Unknown run transition action.")

    async def _increment_run_version_tx(
        self,
        *,
        tx: Any,
        run_id: str,
        next_state: str,
        state_reason_code: str | None,
        is_resumable: bool,
        cancel_requested_at: datetime | None,
        cancel_disposition: str | None,
    ) -> Any:
        row = await tx.fetchrow(
            """
            UPDATE agent_runs
            SET state = $2,
                state_reason_code = $3,
                is_resumable = $4,
                cancel_requested_at = $5,
                cancel_disposition = $6,
                version = version + 1,
                state_entered_at = now(),
                updated_at = now()
            WHERE id = $1::uuid
            RETURNING *
            """,
            run_id,
            next_state,
            state_reason_code,
            bool(is_resumable),
            cancel_requested_at,
            cancel_disposition,
        )
        if not row:
            raise ContractError(APIErrorCode.HOLD_CONFLICT, "Run version update failed.")
        return row

    async def _insert_durable_event_tx(
        self,
        *,
        tx: Any,
        run_id: str,
        run_version: int,
        actor_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        seq = await tx.fetchrow(
            """
            UPDATE agent_runs
            SET run_event_seq = run_event_seq + 1,
                updated_at = now()
            WHERE id = $1::uuid
            RETURNING run_event_seq
            """,
            run_id,
        )
        if not seq:
            raise ContractError(APIErrorCode.HOLD_CONFLICT, "Event sequence allocation failed.")
        await tx.execute(
            """
            INSERT INTO agent_run_events (run_id, run_version, event_seq, event_type, replay_class, actor_id, payload)
            VALUES ($1::uuid, $2, $3, $4, 'durable', $5::uuid, $6::jsonb)
            """,
            run_id,
            int(run_version),
            int(seq["run_event_seq"]),
            event_type,
            actor_id,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        )

    async def _complete_replay_tx(
        self,
        *,
        tx: Any,
        replay_id: str,
        status_code: int,
        payload: dict[str, Any],
    ) -> None:
        body_full = canonical_json(payload)
        full_hash = hashlib.sha256(body_full.encode("utf-8")).hexdigest()
        cap_bytes = max(1024, int(os.getenv("OPENVEGAS_REPLAY_BODY_MAX_BYTES", "131072")))
        body_bytes = body_full.encode("utf-8")
        if len(body_bytes) > cap_bytes:
            stored_bytes = body_bytes[:cap_bytes]
            body_text = stored_bytes.decode("utf-8", errors="ignore")
            truncated = True
        else:
            body_text = body_full
            truncated = False

        updated = await tx.execute(
            """
            UPDATE agent_mutation_replays
            SET status = 'completed',
                response_status = $2,
                content_type = 'application/json',
                response_body_text = $3,
                response_truncated = $4,
                response_hash = $5,
                updated_at = now()
            WHERE id = $1::uuid
              AND status = 'processing'
            """,
            replay_id,
            int(status_code),
            body_text,
            bool(truncated),
            full_hash if truncated else None,
        )
        if _rows_affected(updated) != 1:
            raise ContractError(APIErrorCode.HOLD_CONFLICT, "Replay completion failed.")
