"""Tool heartbeat mutator scaffold."""

from __future__ import annotations

from typing import Any

from openvegas.agent.tool_cas import heartbeat_tx


async def mutate_tool_heartbeat(*, tx: Any, run_id: str, tool_call_id: str, execution_token: str) -> dict[str, Any]:
    heartbeat = await heartbeat_tx(
        tx=tx,
        run_id=run_id,
        tool_call_id=tool_call_id,
        execution_token=execution_token,
    )
    return heartbeat.as_dict()
