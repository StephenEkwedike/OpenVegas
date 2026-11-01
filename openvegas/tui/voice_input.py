"""Terminal voice capture helpers for OpenVegas chat."""

from __future__ import annotations

import tempfile
import time
import wave
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from openvegas.tui.icons import mic_icon


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    ERROR = "error"


@dataclass
class VoiceInputResult:
    text: str
    duration_sec: float
    state: VoiceState
    error: str | None = None


WAVEFORM_CHARS = "▁▂▃▄▅▆▇█"


def amplitude_to_bar(amplitude: float, width: int = 20) -> str:
    """Convert 0.0-1.0 amplitude into a center-weighted waveform bar."""
    try:
        clamped = max(0.0, min(1.0, float(amplitude)))
    except Exception:
        clamped = 0.0
    # Make peaks/troughs ~40% more visible for live mic feedback.
    clamped = min(1.0, clamped * 1.4)
    if width <= 1:
        width = 1
    half = max(1, width // 2)
    phase = int(time.time() * 12)
    bars: list[str] = []
    for i in range(width):
        center_dist = abs(i - half) / float(half)
        height = clamped * (1.0 - center_dist * 0.60)
        jitter = ((phase + i * 3) % 5 - 2) / 40.0
        level = max(0.0, min(1.0, height + jitter))
        idx = int(round(level * (len(WAVEFORM_CHARS) - 1)))
        idx = max(0, min(idx, len(WAVEFORM_CHARS) - 1))
        bars.append(WAVEFORM_CHARS[idx])
    return "".join(bars)


def voice_status_label(state: VoiceState | str, *, amplitude: float = 0.0, include_hint: bool = True) -> str:
    """Terminal-safe voice status label for prompt-toolkit surfaces."""
    token = str(getattr(state, "value", state) or "").strip().lower()
    icon = mic_icon()
    if token == VoiceState.LISTENING.value:
        return f"{icon} Listening {amplitude_to_bar(amplitude)}"
    if token == VoiceState.PROCESSING.value:
        return f"{icon} Transcribing..."
    if token == VoiceState.ERROR.value:
        return f"{icon} Error"
    return f"{icon} Voice" if include_hint else icon


@dataclass
class VoiceCaptureSession:
    """Local microphone capture session; writes to temporary wav on stop."""

    sample_rate: int = 16000
    channels: int = 1
    chunks: list[Any] = field(default_factory=list)
    last_level: float = 0.0
    started_at: float = 0.0
    stream: Any = None
    _np: Any = None
    _sd: Any = None

    def start(self) -> str | None:
        try:
            import numpy as _np
            import sounddevice as _sd
        except Exception as exc:
            return f"audio runtime unavailable: {exc}"
        self._np = _np
        self._sd = _sd
        self.chunks = []
        self.last_level = 0.0

        def _cb(indata, _frames, _time_info, _status):
            try:
                arr = _np.asarray(indata, dtype="float32")
                self.chunks.append(arr.copy())
                self.last_level = float(_np.sqrt(_np.mean(arr * arr))) if arr.size else 0.0
            except Exception:
                return

        try:
            self.stream = _sd.InputStream(
                samplerate=int(self.sample_rate),
                channels=int(self.channels),
                dtype="float32",
                callback=_cb,
            )
            self.stream.start()
            self.started_at = time.time()
            return None
        except Exception as exc:
            return str(exc)

    def abort(self) -> None:
        stream = self.stream
        self.stream = None
        try:
            if stream is not None and hasattr(stream, "abort"):
                stream.abort()
        except Exception:
            pass
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            pass

    def stop_to_wav(self) -> tuple[str | None, float, str | None]:
        stream = self.stream
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            pass
        self.stream = None

        if not self.chunks:
            return None, 0.0, "no audio captured"
        if self._np is None:
            return None, 0.0, "numpy unavailable"

        try:
            merged = self._np.concatenate(self.chunks, axis=0).reshape(-1)
        except Exception as exc:
            return None, 0.0, f"capture merge failed: {exc}"

        duration_sec = float(len(merged)) / float(max(1, int(self.sample_rate)))
        pcm = (self._np.clip(merged, -1.0, 1.0) * 32767.0).astype("<i2")
        out = Path(tempfile.gettempdir()) / f"openvegas-voice-{int(time.time() * 1000)}.wav"
        try:
            with wave.open(str(out), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(self.sample_rate))
                wf.writeframes(pcm.tobytes())
        except Exception as exc:
            return None, duration_sec, f"wav write failed: {exc}"
        return str(out), duration_sec, None
