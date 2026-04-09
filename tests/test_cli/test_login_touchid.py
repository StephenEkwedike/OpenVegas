from __future__ import annotations

from click.testing import CliRunner

from openvegas.cli import cli


class _AuthTouchIdSuccess:
    def refresh_token(self) -> str:
        return "fresh-token"

    def login_with_email(self, _email: str, _password: str) -> dict:
        raise AssertionError("email/password should not be used when touchid succeeds")

    def login_with_otp(self, _email: str) -> None:
        raise AssertionError("otp should not run in this test")


class _AuthFallback:
    def __init__(self):
        self.email_login_calls: list[tuple[str, str]] = []

    def refresh_token(self) -> str:
        raise RuntimeError("should not refresh when touchid declined")

    def login_with_email(self, email: str, password: str) -> dict:
        self.email_login_calls.append((email, password))
        return {"email": email, "user_id": "user-1"}

    def login_with_otp(self, _email: str) -> None:
        raise AssertionError("otp should not run in this test")


def test_login_biometric_first_success(monkeypatch):
    monkeypatch.setattr("openvegas.auth.SupabaseAuth", _AuthTouchIdSuccess)
    monkeypatch.setattr(
        "openvegas.config.get_session",
        lambda: {"refresh_storage": "platform_credential_store"},
    )
    monkeypatch.setattr("openvegas.config.require_touchid_unlock_for_refresh_storage", lambda _s: True)
    monkeypatch.setattr("openvegas.config.request_touchid_unlock", lambda: True)
    monkeypatch.setattr("openvegas.config.touchid_enabled", lambda: True)
    monkeypatch.setattr("openvegas.config.touchid_supported", lambda: True)

    def _boom(*_args, **_kwargs):
        raise AssertionError("Prompt.ask should not be called when biometric unlock succeeds")

    monkeypatch.setattr("openvegas.cli.Prompt.ask", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 0
    assert "Attempting Touch ID unlock" in result.output
    assert "Unlocked with Touch ID" in result.output


def test_login_biometric_fallback_to_email_password(monkeypatch):
    auth = _AuthFallback()
    monkeypatch.setattr("openvegas.auth.SupabaseAuth", lambda: auth)
    monkeypatch.setattr(
        "openvegas.config.get_session",
        lambda: {"refresh_storage": "platform_credential_store"},
    )
    monkeypatch.setattr("openvegas.config.require_touchid_unlock_for_refresh_storage", lambda _s: True)
    monkeypatch.setattr("openvegas.config.request_touchid_unlock", lambda: False)
    monkeypatch.setattr("openvegas.config.touchid_enabled", lambda: True)
    monkeypatch.setattr("openvegas.config.touchid_supported", lambda: True)

    answers = iter(["user@example.com", "secret"])

    def _ask(_prompt: str, password: bool = False):
        return next(answers)

    monkeypatch.setattr("openvegas.cli.Prompt.ask", _ask)

    runner = CliRunner()
    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 0
    assert "Touch ID was unavailable or declined" in result.output
    assert "Logged in as user@example.com" in result.output
    assert auth.email_login_calls == [("user@example.com", "secret")]
