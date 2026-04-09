from __future__ import annotations

import asyncio
import io
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
