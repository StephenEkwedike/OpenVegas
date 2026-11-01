"""Dealer panel renderer for CLI tool activity."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from rich.console import Console
from rich.markup import escape

from openvegas.telemetry import emit_metric
from openvegas.tui.avatar_frames import frame_for_state


@dataclass
class DealerPanel:
    console: Console
    enabled: bool = True
    label: str = "Dealer"
    sprite_renderer: Any | None = None
    _tick: int = 0
    _last_state: str = "idle"

    def render(self, state: str, detail: str = "") -> None:
        if not self.enabled:
            return
        token = str(state or "idle").strip().lower() or "idle"
        prev = self._last_state
        sprite_mode = bool(self.sprite_renderer is not None and getattr(self.sprite_renderer, "enabled", lambda: False)())
        if sprite_mode:
            mode = "sprite_truecolor"
        else:
            mode = (
                "ascii_safe"
                if str(os.getenv("OPENVEGAS_CLI_ASCII_SAFE", "0")).strip().lower() in {"1", "true", "yes", "on"}
                else "unicode_fallback"
            )
        emit_metric("avatar_render_mode_total", {"surface": "cli", "mode": mode})
        if token != prev:
            emit_metric("avatar_state_transition_total", {"surface": "cli", "from": prev, "to": token})
        self._last_state = token
        suffix = f" · {detail}" if detail else ""
        if sprite_mode:
            try:
                sprite_text = self.sprite_renderer.render(state=token, tick=self._tick)
                self.console.print(f"[dim]{self.label}[/dim]")
                self.console.print(sprite_text)
                self.console.print(f"[dim]{token}{suffix}[/dim]")
            except Exception:
                emit_metric("avatar_sprite_render_fail_total", {"surface": "cli", "reason": "render_failed"})
                frame = frame_for_state(token, self._tick)
                self.console.print(f"[dim]{self.label}[/dim] {escape(frame)} [dim]{token}{suffix}[/dim]")
        else:
            frame = frame_for_state(token, self._tick)
            self.console.print(f"[dim]{self.label}[/dim] {escape(frame)} [dim]{token}{suffix}[/dim]")
        self._tick += 1

    def reset(self) -> None:
        self.render("idle", "ready")
