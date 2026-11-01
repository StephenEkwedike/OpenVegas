"""Bounded owner-consented refill worker. Orders/locks survive process restarts."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from openvegas.payments.agent_funding import AgentFunding, require_enabled

log = logging.getLogger(__name__)


async def run(stop):
    from server.services.dependencies import get_billing_service, get_db, get_wallet

    cursor = None
    while not stop.is_set():
        try:
            if os.getenv("OPENVEGAS_AGENT_FUNDING_ENABLED", "0") == "1":
                gateway = get_billing_service().stripe_gateway
                require_enabled(gateway)
                db = get_db()
                rows = await db.fetch(
                    "SELECT agent_id FROM agent_auto_topups WHERE enabled AND ($1::uuid IS NULL OR agent_id>$1) ORDER BY agent_id LIMIT 25",
                    cursor,
                )
                cursor = rows[-1]["agent_id"] if rows else None
                svc = AgentFunding(db, get_wallet(), gateway)
                for row in rows:
                    if stop.is_set():
                        break
                    async with asyncio.timeout(30):
                        await svc.auto_tick(row["agent_id"])
        except Exception:  # noqa: BLE001 - maintenance must preserve pending orders and redact secrets.
            log.warning(
                "Agent funding maintenance incomplete; pending orders retained, no automatic retry."
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass


@asynccontextmanager
async def agent_funding_lifespan():
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop), name="agent-funding")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
