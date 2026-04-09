from __future__ import annotations

from click.testing import CliRunner

from openvegas.cli import cli


def test_doctor_auth_ready(monkeypatch):
    monkeypatch.setattr("openvegas.config.touchid_enabled", lambda: True)
    monkeypatch.setattr("openvegas.config.touchid_supported", lambda: True)
    monkeypatch.setattr("openvegas.config.platform_keychain_available", lambda: True)
    monkeypatch.setattr("openvegas.config.load_refresh_from_platform_store", lambda: "rtok")
    monkeypatch.setattr("openvegas.config.get_session", lambda: {"refresh_storage": "platform_credential_store"})

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor-auth"])
    assert result.exit_code == 0
    assert "doctor-auth:" in result.output
    assert "ready=1" in result.output
    assert "touchid_enabled=1" in result.output
    assert "touchid_supported=1" in result.output
    assert "keychain_token=1" in result.output
    assert "refresh_storage=platform_credential_store" in result.output


def test_doctor_auth_not_ready_without_keychain_token(monkeypatch):
    monkeypatch.setattr("openvegas.config.touchid_enabled", lambda: True)
    monkeypatch.setattr("openvegas.config.touchid_supported", lambda: True)
    monkeypatch.setattr("openvegas.config.platform_keychain_available", lambda: True)
    monkeypatch.setattr("openvegas.config.load_refresh_from_platform_store", lambda: "")
    monkeypatch.setattr("openvegas.config.get_session", lambda: {"refresh_storage": "platform_credential_store"})

    runner = CliRunner()
    result = runner.invoke(cli, ["doctor-auth"])
    assert result.exit_code == 0
    assert "ready=0" in result.output
    assert "keychain_token=0" in result.output
