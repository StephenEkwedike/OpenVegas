"""Tool result mutator scaffold."""

from __future__ import annotations

from typing import Any

from openvegas.agent.tool_cas import terminalize_tx


async def mutate_tool_result(
    *,
    tx: Any,
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
) -> dict[str, Any]:
    outcome = await terminalize_tx(
        tx=tx,
        run_id=run_id,
        tool_call_id=tool_call_id,
        execution_token=execution_token,
        result_status=result_status,
        result_payload=result_payload,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        terminal_response_status=terminal_response_status,
        terminal_response_body_text=terminal_response_body_text,
        terminal_response_truncated=terminal_response_truncated,
        terminal_response_hash=terminal_response_hash,
    )
    return {
        "outcome": outcome.state,
        "result_submission_hash": outcome.result_submission_hash,
        "response_status": outcome.response_status,
        "response_body_text": outcome.response_body_text,
    }
