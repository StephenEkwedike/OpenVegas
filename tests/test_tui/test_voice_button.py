from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console

from openvegas.telemetry import get_metrics_snapshot, reset_metrics
from openvegas.tui.voice_button import VoiceButton
from openvegas.tui.voice_input import VoiceState


class _CaptureTimeout:
    def __init__(self) -> None:
        self.abort_called = False

    def stop_to_wav(self):
        time.sleep(0.2)
        return None, 0.0, "timeout"

    def abort(self) -> None:
        self.abort_called = True


class _CaptureSuccess:
    def __init__(self, wav_path: Path, duration: float = 1.7) -> None:
        self.wav_path = wav_path
        self.duration = duration
        self.calls = 0
        self.abort_called = False

    def stop_to_wav(self):
        self.calls += 1
        return str(self.wav_path), self.duration, None

    def abort(self) -> None:
        self.abort_called = True


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, color_system=None)


def _has_phase(phase: str) -> bool:
    snap = get_metrics_snapshot()
    token = f"voice_capture_phase_total|phase={phase}"
    return any(key.startswith(token) for key in snap)


@pytest.mark.asyncio
async def test_voice_stop_timeout_recovers_to_idle(monkeypatch: pytest.MonkeyPatch):
    reset_metrics()
    button = VoiceButton(_console())
    capture = _CaptureTimeout()
    button.state = VoiceState.LISTENING
    button._capture = capture

    monkeypatch.setattr("openvegas.tui.voice_button._voice_stop_timeout_sec", lambda: 0.01)

    inserted: list[str] = []

    async def _transcribe(_wav: str, _duration: float) -> str:
        inserted.append("called")
        return ""

    await button.toggle(insert_text=inserted.append, transcribe_wav=_transcribe)

    assert capture.abort_called is True
    assert button.state == VoiceState.IDLE
    assert button._capture is None
    assert inserted == []
    assert _has_phase("stop_requested")
    assert _has_phase("stop_timeout")


@pytest.mark.asyncio
async def test_voice_stop_and_transcribe_inserts_text(tmp_path: Path):
    reset_metrics()
    wav_path = tmp_path / "voice.wav"
    wav_path.write_bytes(b"RIFF")

    button = VoiceButton(_console())
    button.state = VoiceState.LISTENING
    button._capture = _CaptureSuccess(wav_path)

    inserted: list[str] = []

    async def _transcribe(_wav: str, _duration: float) -> str:
        return "hello from mic"

    await button.toggle(insert_text=inserted.append, transcribe_wav=_transcribe)

    assert inserted == ["hello from mic"]
    assert button.state == VoiceState.IDLE
    assert button._capture is None
    assert not wav_path.exists()
    assert _has_phase("transcribe_started")
    assert _has_phase("transcribe_succeeded")


@pytest.mark.asyncio
async def test_voice_toggle_lock_prevents_race(tmp_path: Path):
    reset_metrics()
    wav_path = tmp_path / "voice.wav"
    wav_path.write_bytes(b"RIFF")

    button = VoiceButton(_console())
    capture = _CaptureSuccess(wav_path)
    button.state = VoiceState.LISTENING
    button._capture = capture

    calls = {"transcribe": 0}

    async def _transcribe(_wav: str, _duration: float) -> str:
        calls["transcribe"] += 1
        await asyncio.sleep(0.05)
        return "race-safe"

    inserted: list[str] = []
    await asyncio.gather(
        button.toggle(insert_text=inserted.append, transcribe_wav=_transcribe),
        button.toggle(insert_text=inserted.append, transcribe_wav=_transcribe),
    )

    assert calls["transcribe"] == 1
    assert capture.calls == 1
    assert inserted == ["race-safe"]
    assert button.state == VoiceState.IDLE


@pytest.mark.asyncio
async def test_voice_empty_transcript_emits_metric(tmp_path: Path):
    reset_metrics()
    wav_path = tmp_path / "voice.wav"
    wav_path.write_bytes(b"RIFF")

    button = VoiceButton(_console())
    button.state = VoiceState.LISTENING
    button._capture = _CaptureSuccess(wav_path)

    inserted: list[str] = []

    async def _transcribe(_wav: str, _duration: float) -> str:
        return ""

    await button.toggle(insert_text=inserted.append, transcribe_wav=_transcribe)

    assert inserted == []
    assert button.last_transcript_chars == 0
    assert _has_phase("transcript_empty")
    assert button.state == VoiceState.IDLE


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_shutdown_during_transcription_discards_late_result(tmp_path, fail):
    wav = tmp_path / "retired.wav"
    wav.write_bytes(b"RIFF")
    button = VoiceButton(_console())
    button.state = VoiceState.LISTENING
    button._capture = _CaptureSuccess(wav)
    entered, release = asyncio.Event(), asyncio.Event()
    inserted = []

    async def transcribe(*_):
        entered.set()
        await release.wait()
        if fail:
            raise RuntimeError("late failure")
        return "must not reach the next prompt"

    task = asyncio.create_task(button.toggle(insert_text=inserted.append, transcribe_wav=transcribe))
    await asyncio.wait_for(entered.wait(), 1)
    button.stop_if_recording()
    release.set()
    await task
    assert inserted == []
    assert button.last_transcript_chars == 0
    assert button.last_error is None
    assert button.state == VoiceState.IDLE
    assert not wav.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["cancel", "timeout", "shutdown"])
async def test_retired_stop_cleans_late_wav_without_transcribing(tmp_path, monkeypatch, retirement):
    wav = tmp_path / "late.wav"
    entered, release = threading.Event(), threading.Event()

    class Capture(_CaptureSuccess):
        def stop_to_wav(self):
            entered.set()
            assert release.wait(2)
            wav.write_bytes(b"RIFF")
            return str(wav), 1.0, None

    button = VoiceButton(_console())
    capture = Capture(wav)
    button.state = VoiceState.LISTENING
    button._capture = capture
    calls = []

    async def transcribe(*_):
        calls.append("transcribe")
        return "late text"

    if retirement == "timeout":
        monkeypatch.setattr("openvegas.tui.voice_button._voice_stop_timeout_sec", lambda: 0.01)
    task = asyncio.create_task(button.toggle(insert_text=calls.append, transcribe_wav=transcribe))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        if retirement == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif retirement == "timeout":
            await task
        else:
            button.stop_if_recording()
        release.set()
        if retirement == "shutdown":
            await task
        # Drain this test's stop worker and its result-cleanup callback.
        async with asyncio.timeout(1):
            while any(t is not asyncio.current_task() and not t.done()
                      and "to_thread" in str(t.get_coro()) for t in asyncio.all_tasks()):
                await asyncio.sleep(0.001)
        await asyncio.sleep(0)
        assert not wav.exists()
        assert calls == []
        assert capture.abort_called
        assert button._capture is None
        assert button.state == VoiceState.IDLE
        assert not button._toggle_lock.locked()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_transcription_cleans_wav_and_propagates(tmp_path):
    wav = tmp_path / "cancel.wav"
    wav.write_bytes(b"RIFF")
    button = VoiceButton(_console())
    button.state = VoiceState.LISTENING
    button._capture = _CaptureSuccess(wav)
    entered = asyncio.Event()
    inserted = []

    async def transcribe(*_):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(button.toggle(insert_text=inserted.append, transcribe_wav=transcribe))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not wav.exists()
    assert inserted == []
    assert button.state == VoiceState.IDLE
    assert not button._toggle_lock.locked()


def test_asyncio_run_shutdown_cleans_wav_after_stop_task_cancelled(tmp_path):
    wav = tmp_path / "shutdown.wav"
    entered, release = threading.Event(), threading.Event()
    button = VoiceButton(_console())
    inserted = []

    class Capture(_CaptureSuccess):
        def stop_to_wav(self):
            entered.set()
            assert release.wait(2)
            wav.write_bytes(b"RIFF")
            return str(wav), 1.0, None

        def abort(self):
            super().abort()
            release.set()

    capture = Capture(wav)
    button._capture = capture
    button.state = VoiceState.LISTENING

    async def transcribe(*_):
        pytest.fail("Retired capture must not transcribe")

    async def main():
        task = asyncio.create_task(button.toggle(insert_text=inserted.append, transcribe_wav=transcribe))
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        return task

    try:
        task = asyncio.run(main())
    finally:
        release.set()
    assert task.cancelled()
    assert capture.abort_called
    assert not wav.exists()
    assert inserted == []
    assert button.state == VoiceState.IDLE
    assert not button._toggle_lock.locked()


@pytest.mark.asyncio
async def test_completed_stop_result_is_cleaned_when_await_is_cancelled(tmp_path, monkeypatch):
    wav = tmp_path / "completed.wav"
    wav.write_bytes(b"RIFF")
    button = VoiceButton(_console())
    button._capture = _CaptureSuccess(wav)
    button.state = VoiceState.LISTENING

    async def cancel_after_result(awaitable, **_):
        await awaitable
        raise asyncio.CancelledError()

    async def transcribe(*_):
        pytest.fail("Cancelled stop must not transcribe")

    monkeypatch.setattr("openvegas.tui.voice_button.asyncio.wait_for", cancel_after_result)
    with pytest.raises(asyncio.CancelledError):
        await button.toggle(insert_text=lambda _: pytest.fail("Late insertion"), transcribe_wav=transcribe)
    await asyncio.sleep(0)
    assert not wav.exists()
    assert button.state == VoiceState.IDLE


@pytest.mark.asyncio
async def test_unexpected_stop_exception_is_redacted_and_recovers(tmp_path):
    class Capture(_CaptureSuccess):
        def stop_to_wav(self):
            raise RuntimeError("SECRET device detail")

    console = _console()
    button = VoiceButton(console)
    capture = Capture(tmp_path / "unused.wav")
    button._capture = capture
    button.state = VoiceState.LISTENING

    async def transcribe(*_):
        pytest.fail("Failed capture must not transcribe")

    await button.toggle(insert_text=lambda _: pytest.fail("Late insertion"), transcribe_wav=transcribe)
    assert button.state == VoiceState.IDLE
    assert button._capture is None
    assert capture.abort_called
    assert not button._toggle_lock.locked()
    assert button.last_error == "voice stop failed; audio device unavailable"
    assert "SECRET" not in console.file.getvalue()
