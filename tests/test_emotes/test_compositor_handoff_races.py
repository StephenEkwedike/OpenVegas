"""Real application handoffs with offline pipes, not native UX certification."""

import asyncio
from types import SimpleNamespace

import pytest

from tests.test_emotes.test_compositor import running


@pytest.mark.asyncio
async def test_concurrent_external_handoffs_have_one_owner(pack, clock):
    async with running(pack, clock) as (owner, _, _, transcript, _):
        callbacks = []

        def external(name):
            callbacks.append((name, owner._suspended))
            assert owner.console.file is transcript
            return name

        results = await asyncio.gather(
            owner.run_external(lambda: external("first")),
            owner.run_external(lambda: external("second")),
            return_exceptions=True,
        )
        assert results[0] == "first"
        assert isinstance(results[1], RuntimeError)
        assert "handoff is already active" in str(results[1])
        assert callbacks == [("first", True)]
        assert not owner._suspended
        assert owner.console.file is not transcript
        assert await owner.run_external(lambda: external("next")) == "next"
        assert callbacks[-1] == ("next", True)


@pytest.mark.asyncio
async def test_handoff_cancelled_before_child_runs_releases_owner(pack, clock):
    async with running(pack, clock) as (owner, _, _, transcript, _):
        task = asyncio.create_task(owner.run_external(lambda: pytest.fail("Cancelled handoff ran")))
        asyncio.get_running_loop().call_soon(task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._suspended
        assert owner.console.file is not transcript
        assert await owner.run_external(lambda: "next") == "next"


@pytest.mark.asyncio
async def test_handoff_task_setup_failure_releases_owner(pack, clock, monkeypatch):
    async with running(pack, clock) as (owner, _, _, transcript, _):
        def broken(*args):
            raise RuntimeError("synthetic task setup failure")

        with monkeypatch.context() as patch:
            patch.setattr(owner.app, "context", SimpleNamespace(run=broken))
            with pytest.raises(RuntimeError, match="synthetic task setup failure"):
                await owner.run_external(lambda: pytest.fail("Unscheduled handoff ran"))
        assert not owner._suspended
        assert owner.console.file is not transcript
        assert await owner.run_external(lambda: "next") == "next"
