from __future__ import annotations

from click.testing import CliRunner

from openvegas.cli import cli


def test_quit_command_locks_session(monkeypatch):
    called = {"n": 0}

    def _lock() -> None:
        called["n"] += 1

    monkeypatch.setattr("openvegas.config.clear_access_token_keep_refresh", _lock)

    runner = CliRunner()
    result = runner.invoke(cli, ["quit"])
    assert result.exit_code == 0
    assert "Session locked" in result.output
    assert called["n"] == 1
