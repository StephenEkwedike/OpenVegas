import asyncio
from types import SimpleNamespace

import pytest

from openvegas.tui.voice_refresh import voice_refresh_during_prompt


@pytest.mark.asyncio
async def test_idle_does_not_repaint_and_prompt_exit_retires_timer():
    invalidations = []
    recording = False
    app = SimpleNamespace(invalidate=lambda: invalidations.append(True))
    async with voice_refresh_during_prompt(app, lambda: recording, interval=0.002):
        await asyncio.sleep(0.012)
        assert invalidations == []
        recording = True
        await asyncio.sleep(0.012)
        assert invalidations
        recording = False
        count = len(invalidations)
        await asyncio.sleep(0.012)
        assert len(invalidations) == count
    recording = True
    await asyncio.sleep(0.012)
    assert len(invalidations) == count
