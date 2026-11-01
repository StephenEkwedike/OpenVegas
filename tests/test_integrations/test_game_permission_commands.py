from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from openvegas.integrations import commands, game_permissions
from openvegas.integrations.credentials import IntegrationError


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(commands, "detect", lambda _: {"host": "claude", "version": "2.1.301"})
    installer = Mock(return_value={"host": "claude", "version": "2.1.301", "mode": "mod_and_mcp",
        "path": "/fixture", "host_home": "/fixture", "reload": "restart"})
    permission = Mock(return_value={"status": "updated"})
    login_calls = []
    async def login(**kwargs):
        login_calls.append(kwargs)
        return {"balance": "100"}
    monkeypatch.setattr(commands, "install", installer)
    monkeypatch.setattr(commands, "other_installations", lambda _: [])
    monkeypatch.setattr(commands, "browser_login", login)
    monkeypatch.setattr(commands, "_offer_default_terminal", lambda: None)
    monkeypatch.setattr(game_permissions, "configure", permission)
    return installer, permission, login_calls


def test_go_has_one_disclosed_setup_consent(setup):
    installer, permission, login = setup
    result = CliRunner().invoke(commands.go, ["--host", "claude"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "trust only OpenVegas play, not other tools" in result.output
    assert result.output.count("[y/N]") == 1
    permission.assert_called_once_with("claude", apply=True)
    assert len(login) == 1 and installer.call_count == 2


@pytest.mark.parametrize("args,answer", [(["--dry-run"], ""), ([], "n\n")])
def test_dry_run_and_declined_setup_do_not_change_permissions(setup, args, answer):
    installer, permission, login = setup
    result = CliRunner().invoke(commands.go, ["--host", "claude", *args], input=answer)
    assert result.exit_code == (0 if args else 1)
    assert not login
    permission.assert_not_called()
    installer.assert_called_once_with("claude", dry_run=True)


def test_opt_out_keeps_existing_approvals(setup):
    _, permission, login = setup
    result = CliRunner().invoke(commands.go, ["--host", "claude", "--yes", "--keep-game-approvals"])
    assert result.exit_code == 0
    assert len(login) == 1
    permission.assert_not_called()


def test_setup_is_short_with_diagnostics_opt_in(setup, monkeypatch):
    monkeypatch.setattr(commands, 'backend_url', lambda: 'https://app.openvegas.ai')
    result = CliRunner().invoke(commands.go, ['--host', 'claude', '--yes'])
    assert result.exit_code == 0, result.output
    assert 'Hosted / https://app.openvegas.ai' in result.output
    assert 'Wallet: $V 100.00' in result.output
    assert len(result.output.splitlines()) <= 7
    assert 'Plugin:' not in result.output and 'Same action words' not in result.output
    verbose = CliRunner().invoke(commands.go, ['--host', 'claude', '--dry-run'])
    assert 'Plugin: /fixture' in verbose.output


def test_old_connection_retired_only_after_consent_and_success(setup, monkeypatch):
    installer, _, _ = setup
    plan = {'host': 'claude', 'marketplace': 'old-owned'}
    monkeypatch.setattr(commands, 'other_installations', lambda _: [plan])
    retired = Mock()
    monkeypatch.setattr(commands, 'retire_installation', retired)
    result = CliRunner().invoke(commands.go, ['--host', 'claude'], input='n\n')
    assert 'Replace the older OpenVegas connection' in result.output
    retired.assert_not_called()
    result = CliRunner().invoke(commands.go, ['--host', 'claude', '--yes'])
    assert result.exit_code == 0, result.output
    retired.assert_called_once_with(plan)
    retired.reset_mock()
    installer.side_effect = IntegrationError('failed')
    CliRunner().invoke(commands.go, ['--host', 'claude', '--yes'])
    retired.assert_not_called()


def test_permission_failure_keeps_working_plugin(setup):
    _, permission, _ = setup
    permission.side_effect = IntegrationError("preserved")
    result = CliRunner().invoke(commands.go, ["--host", "claude", "--yes"])
    assert result.exit_code == 0
    assert "permission unchanged; use the host's approval prompt" in result.output


def test_explicit_restore_dry_run_and_uninstall(setup, monkeypatch):
    _, permission, _ = setup
    result = CliRunner().invoke(commands.integration, ["game-permissions", "--host", "claude", "--restore", "--dry-run"])
    assert result.exit_code == 0
    permission.assert_called_once_with("claude", apply=False, restore=True)
    permission.reset_mock()
    uninstall = Mock(return_value={"removed": True})
    monkeypatch.setattr(commands, "uninstall", uninstall)
    result = CliRunner().invoke(commands.integration, ["uninstall", "claude"])
    assert result.exit_code == 0
    permission.assert_called_once_with("claude", apply=True, restore=True)
    uninstall.assert_called_once_with("claude")
