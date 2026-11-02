from __future__ import annotations

import sys
from click.testing import CliRunner

from openvegas.cli import cli


def test_doctor_auth_ready(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("openvegas.config.platform_keychain_available", lambda: True)
    monkeypatch.setattr("openvegas.config.load_refresh_from_platform_store", lambda: "rtok")
    monkeypatch.setattr("openvegas.config.get_session_metadata", lambda: {"refresh_storage": "platform_credential_store"})

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor-auth"])
    assert result.exit_code == 0
    assert "doctor-auth:" in result.output
    assert "ready=1" in result.output
    assert "keychain_token=1" in result.output
    assert "refresh_storage=platform_credential_store" in result.output


def test_doctor_auth_not_ready_without_keychain_token(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("openvegas.config.platform_keychain_available", lambda: True)
    monkeypatch.setattr("openvegas.config.load_refresh_from_platform_store", lambda: "")
    monkeypatch.setattr("openvegas.config.get_session_metadata", lambda: {"refresh_storage": "platform_credential_store"})

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor-auth"])
    assert result.exit_code == 0
    assert "ready=0" in result.output
    assert "keychain_token=0" in result.output


def test_mac_doctor_reports_browser_login_without_keychain(monkeypatch, tmp_path):
    from openvegas import config
    from openvegas.integrations.credentials import Credentials
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setenv('OPENVEGAS_INTEGRATION_PROFILE', str(tmp_path))
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'https://example.invalid')
    Credentials('https://example.invalid').save('synthetic-only')
    result = CliRunner().invoke(cli, ['doctor-auth'])
    assert result.exit_code == 0, result.output
    assert 'ready=1' in result.output and 'keychain=not_used' in result.output
    assert 'synthetic-only' not in result.output
