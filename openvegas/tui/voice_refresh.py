"""Invalidate only active voice previews, not an idle composer's scrollback."""

import asyncio
from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def voice_refresh_during_prompt(app, is_recording, *, interval=0.15):
    async def refresh():
        while True:
            await asyncio.sleep(interval)
            if is_recording():
                app.invalidate()

    task = asyncio.create_task(refresh())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
