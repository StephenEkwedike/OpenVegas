"""Voice button wrapper for chat composer voice capture/transcription."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable

from rich.console import Console

from openvegas.telemetry import emit_metric
from openvegas.tui.voice_input import VoiceCaptureSession, VoiceState, voice_status_label


def _voice_stop_timeout_sec() -> float:
    raw = str(os.getenv("OPENVEGAS_CHAT_VOICE_STOP_TIMEOUT_SEC", "20")).strip()
    try:
        return max(3.0, min(60.0, float(raw)))
    except Exception:
        return 20.0


class VoiceButton:
    """Stateful mic wrapper so chat can call only `voice_button.toggle(...)`."""

    def __init__(self, console: Console):
        self.console = console
        self.state: VoiceState = VoiceState.IDLE
        self._capture: VoiceCaptureSession | None = None
        self._last_error: str | None = None
        self._last_transcript_chars: int = 0
        self._toggle_lock = asyncio.Lock()
        self._show_errors = str(os.getenv("OPENVEGAS_CHAT_VOICE_SHOW_ERRORS", "1")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @property
    def level(self) -> float:
        try:
            return float(self._capture.last_level if self._capture else 0.0)
        except Exception:
            return 0.0

    def label(self, *, include_hint: bool = True) -> str:
        return voice_status_label(self.state, amplitude=self.level, include_hint=include_hint)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_transcript_chars(self) -> int:
        return int(self._last_transcript_chars or 0)

    @property
    def is_recording(self) -> bool:
        return str(getattr(self.state, "value", "")) == "listening"

    def _emit_error(self, msg: str) -> None:
        self._last_error = str(msg or "").strip() or "voice unavailable"
        if self._show_errors:
            self.console.print(f"[yellow]Voice error: {self._last_error}[/yellow]")

    async def toggle(
        self,
        *,
        insert_text: Callable[[str], None],
        transcribe_wav: Callable[[str, float], Awaitable[str]],
    ) -> None:
        """Start/stop capture, transcribe, then insert text back into composer."""
        if self._toggle_lock.locked():
            self.console.print("[dim]Voice is processing...[/dim]")
            return
        async with self._toggle_lock:
            if self.state in {VoiceState.IDLE, VoiceState.ERROR}:
                self._start_recording()
                return
            if self.state == VoiceState.LISTENING:
                await self._stop_and_insert(insert_text=insert_text, transcribe_wav=transcribe_wav)
                return
            # PROCESSING -> no-op (prevent re-entry)
            self.console.print("[dim]Voice is processing...[/dim]")

    def _start_recording(self) -> None:
        self.state = VoiceState.LISTENING
        self._last_error = None
        self._last_transcript_chars = 0
        emit_metric("voice_capture_phase_total", {"phase": "start"})
        session = VoiceCaptureSession(sample_rate=16000, channels=1)
        err = session.start()
        if err:
            self.state = VoiceState.ERROR
            self._emit_error(err)
            emit_metric("voice_capture_phase_total", {"phase": "start_failed"})
            self.state = VoiceState.IDLE
            return
        self._capture = session

    async def _stop_and_insert(
        self,
        *,
        insert_text: Callable[[str], None],
        transcribe_wav: Callable[[str, float], Awaitable[str]],
    ) -> None:
        capture = self._capture
        if capture is None:
            self.state = VoiceState.IDLE
            return

        self.state = VoiceState.PROCESSING
        emit_metric("voice_capture_phase_total", {"phase": "stop_requested"})
        self.console.print("[dim]Stopping microphone...[/dim]")

        try:
            wav_path, duration_sec, err = await asyncio.wait_for(
                asyncio.to_thread(capture.stop_to_wav),
                timeout=_voice_stop_timeout_sec(),
            )
        except asyncio.TimeoutError:
            emit_metric("voice_capture_phase_total", {"phase": "stop_timeout"})
            try:
                capture.abort()
            except Exception:
                pass
            self._capture = None
            self.state = VoiceState.ERROR
            self._emit_error("voice stop timeout; audio device busy")
            self.state = VoiceState.IDLE
            return

        self._capture = None
        if err or not wav_path:
            emit_metric("voice_capture_phase_total", {"phase": "stop_failed"})
            self.state = VoiceState.ERROR
            self._emit_error(err or "no audio captured")
            self.state = VoiceState.IDLE
            return

        try:
            emit_metric("voice_capture_phase_total", {"phase": "transcribe_started"})
            transcript = str(await transcribe_wav(wav_path, float(duration_sec or 0.0)) or "").strip()
            self._last_transcript_chars = len(transcript)
            if transcript:
                insert_text(transcript)
            else:
                emit_metric("voice_capture_phase_total", {"phase": "transcript_empty"})
            emit_metric("voice_capture_phase_total", {"phase": "transcribe_succeeded"})
        except Exception as exc:
            self._last_transcript_chars = 0
            emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
            self.state = VoiceState.ERROR
            self._emit_error(str(exc or "transcription failed"))
        finally:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except Exception:
                pass
            self.state = VoiceState.IDLE

    def stop_if_recording(self) -> None:
        """Best-effort stop during shutdown."""
        capture = self._capture
        if capture is None:
            self.state = VoiceState.IDLE
            return
        try:
            capture.abort()
        except Exception:
            pass
        self._capture = None
        self.state = VoiceState.IDLE
