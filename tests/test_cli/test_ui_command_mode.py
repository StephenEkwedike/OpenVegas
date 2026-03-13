from __future__ import annotations

from click.testing import CliRunner

from openvegas.cli import cli


def test_ui_defaults_to_inline(monkeypatch):
    called: dict = {}

    def _fake_inline(*, no_render: bool, render_timeout_sec: float) -> None:
        called["no_render"] = no_render
        called["render_timeout_sec"] = render_timeout_sec

    monkeypatch.setattr("openvegas.tui.prompt_ui.run_prompt_ui", _fake_inline)

    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "--no-render", "--render-timeout-sec", "3"])

    assert result.exit_code == 0
    assert called == {"no_render": True, "render_timeout_sec": 3.0}


def test_ui_full_uses_legacy_wizard(monkeypatch):
    called = {"full": False}

    def _fake_full() -> None:
        called["full"] = True

    monkeypatch.setattr("openvegas.tui.wizard.run_wizard", _fake_full)

    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "--full"])

    assert result.exit_code == 0
    assert called["full"] is True
