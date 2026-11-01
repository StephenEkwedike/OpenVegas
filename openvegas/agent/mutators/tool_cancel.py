"""Tool cancel mutator scaffold."""

from __future__ import annotations

from typing import Any

from openvegas.agent.tool_cas import cancel_started_tx


async def mutate_tool_cancel(*, tx: Any, run_id: str, tool_call_id: str, execution_token: str) -> dict[str, Any]:
    outcome = await cancel_started_tx(
        tx=tx,
        run_id=run_id,
        tool_call_id=tool_call_id,
        execution_token=execution_token,
    )
    return {"outcome": outcome}
