"""Installer entry contracts: isolated accounts, fake hosts, no native trust writes."""
from unittest.mock import AsyncMock, Mock, call

import click
import pytest
from click.testing import CliRunner

from openvegas.cli import cli
from openvegas.integrations import commands, game_permissions, installer
from openvegas.integrations.credentials import IntegrationError


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENVEGAS_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(commands, "detect", lambda host: {
        "host": host, "version": "0.160.0" if host == "codex" else "2.1.301"})
    install = Mock(side_effect=lambda host, **_: {
        "host": host, "version": "fixture", "mode": "fixture",
        "path": str(tmp_path / host), "host_home": str(tmp_path), "reload": "restart"})
    login = AsyncMock(return_value={"balance": "100"})
    permissions = Mock(return_value={"status": "updated"})
    monkeypatch.setattr(commands, "install", install)
    monkeypatch.setattr(commands, "browser_login", login)
    monkeypatch.setattr(commands, "other_installations", lambda _: [])
    monkeypatch.setattr(commands, "_offer_default_terminal", lambda: None)
    monkeypatch.setattr(game_permissions, "configure", permissions)
    return install, login, permissions


def test_default_setup_connects_both_without_generic_questions_or_tool_trust(setup):
    install, login, permissions = setup
    result = CliRunner().invoke(cli, ["go"], color=True)
    assert result.exit_code == 0, result.output
    assert "Integrate with" not in result.output
    assert "Set up OpenVegas?" not in result.output
    assert "[y/N]" not in result.output
    assert install.call_args_list == [call("claude", dry_run=True), call("codex", dry_run=True),
                                      call("claude", upgrade=True), call("codex", upgrade=True)]
    login.assert_awaited_once()
    assert login.call_args.kwargs["allow_file"] is False
    permissions.assert_not_called()
    assert "\x1b[34mOpenVegas ready." in result.output
    assert "/hooks" in result.output and "does not grant hook trust" in result.output
    assert len(result.output.splitlines()) <= 7


def test_default_uses_only_detected_hosts(setup, monkeypatch):
    install, _, permissions = setup
    monkeypatch.setattr(commands, "detect", lambda host: {"version": "2.1.301"} if host == "claude" else None)
    result = CliRunner().invoke(cli, ["go"])
    assert result.exit_code == 0, result.output
    assert install.call_args_list == [call("claude", dry_run=True), call("claude", upgrade=True)]
    permissions.assert_not_called()


def test_unchanged_setup_stays_concise_and_does_not_grant_approvals(setup):
    install, login, permissions = setup
    original = install.side_effect
    install.side_effect = lambda host, **options: {**original(host, **options), 'unchanged': True}
    for _ in range(2):
        result = CliRunner().invoke(cli, ['go'])
        assert result.exit_code == 0, result.output
        assert result.output.count('OpenVegas ready.') == 1
        assert 'Integrate with' not in result.output and 'Set up OpenVegas?' not in result.output
        assert 'Plugin:' not in result.output and len(result.output.splitlines()) <= 7
    assert login.await_count == 2  # Existing account reuse remains the broker's responsibility.
    permissions.assert_not_called()


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_explicit_host_still_limits_installation(setup, host):
    install, _, permissions = setup
    result = CliRunner().invoke(cli, ["go", "--host", host])
    assert result.exit_code == 0, result.output
    assert install.call_args_list == [call(host, dry_run=True), call(host, upgrade=True)]
    permissions.assert_not_called()


def test_dry_run_does_not_login_install_or_grant_permissions(setup):
    install, login, permissions = setup
    result = CliRunner().invoke(cli, ["go", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert install.call_args_list == [call("claude", dry_run=True), call("codex", dry_run=True)]
    login.assert_not_awaited()
    permissions.assert_not_called()
    assert "Plugin:" in result.output
    assert "OpenVegas ready" not in result.output


def test_browser_consent_is_not_answered_by_install_entry(setup):
    install, login, permissions = setup

    async def authorize(**_):
        click.confirm("Approve browser sign-in?", abort=True)
        return {"balance": "100"}

    login.side_effect = authorize
    result = CliRunner().invoke(cli, ["go"], input="n\n")
    assert result.exit_code == 1
    assert "Approve browser sign-in?" in result.output
    assert all(item.kwargs == {"dry_run": True} for item in install.call_args_list)
    permissions.assert_not_called()
    assert "OpenVegas ready" not in result.output


def test_install_failure_does_not_claim_ready(setup):
    install, _, permissions = setup
    plan = install.side_effect

    def failed_install(host, **kwargs):
        if kwargs.get("dry_run"):
            return plan(host, **kwargs)
        raise IntegrationError("fixture host failure")

    install.side_effect = failed_install
    result = CliRunner().invoke(cli, ["go"])
    assert result.exit_code == 1
    assert "fixture host failure" in result.output
    assert "OpenVegas ready" not in result.output
    permissions.assert_not_called()


@pytest.mark.parametrize("opt_out", [False, True])
def test_only_explicit_yes_can_opt_in_to_existing_game_permission_behavior(setup, opt_out):
    _, _, permissions = setup
    arguments = ["go", "--host", "claude", "--yes"]
    if opt_out:
        arguments.append("--keep-game-approvals")
    result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 0, result.output
    if opt_out:
        permissions.assert_not_called()
    else:
        permissions.assert_called_once_with("claude", apply=True)


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_install_plan_retains_native_review_and_stable_identity(monkeypatch, tmp_path, host):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENVEGAS_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(installer, "detect", lambda _: {
        "host": host, "executable": "/fixture/" + host, "version": "fixture", "mod": False})
    host_command = Mock(side_effect=AssertionError("Dry-run must not modify the native host"))
    monkeypatch.setattr(installer, "_run", host_command)
    first = installer.install(host, dry_run=True)
    assert installer.install(host, dry_run=True) == first
    assert first["commands"] == [
        ["/fixture/" + host, "plugin", "marketplace", "add", first["path"]],
        ["/fixture/" + host, "plugin", "install" if host == "claude" else "add",
         "openvegas@" + first["marketplace"]],
    ]
    host_command.assert_not_called()
    assert not list(tmp_path.iterdir())
