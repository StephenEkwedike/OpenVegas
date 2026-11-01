"""Bounded in-process expiry maintenance, owned by the server lifespan."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from server.services.tv_billing import TVBilling

logger = logging.getLogger(__name__)


class TVExpiryWorker:
    def __init__(self, db, wallet, *, batches=3, timeout=5.0, interval=15.0):
        if type(batches) is not int or not 1 <= batches <= 10:
            raise ValueError("TV expiry batches must be 1..10")
        if not 0.01 <= timeout <= 30 or not 0.01 <= interval <= 3600:
            raise ValueError("TV expiry timing is outside its bounds")
        self.db, self.service = db, TVBilling(db, wallet)
        self.batches, self.timeout, self.interval = batches, timeout, interval
        self._lock = asyncio.Lock()

    async def tick(self):
        if self._lock.locked():
            return {"status": "busy", "processed": 0}
        async with self._lock, asyncio.timeout(self.timeout):
            if not await self.db.fetchval("SELECT to_regclass('public.tv_playback_leases')"):
                return {"status": "schema-unavailable", "processed": 0}
            processed = 0
            for _ in range(self.batches):
                count = await self.service.reap_expired()
                processed += count
                if count < 100:
                    return {"status": "drained", "processed": processed}
            return {"status": "batch-limit", "processed": processed}

    async def run(self, stop):
        while not stop.is_set():
            try:
                result = await self.tick()
                if result["processed"]:
                    logger.info(
                        "tv_expiry status=%s processed=%d", result["status"], result["processed"]
                    )
            except Exception:  # noqa: BLE001 - keep maintenance alive without logging secrets
                logger.warning("tv_expiry tick_failed; retrying on next bounded interval")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass


@asynccontextmanager
async def tv_expiry_lifespan(db, wallet):
    """Parent wraps its yield after DB initialization, before dependency shutdown.

    No task starts at import. Runs independently of public/sales flags so closing
    TV cannot strand existing holds. Database transactions protect multiworker use.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(TVExpiryWorker(db, wallet).run(stop), name="tv-expiry")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
