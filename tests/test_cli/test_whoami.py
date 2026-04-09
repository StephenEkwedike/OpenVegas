from __future__ import annotations

import base64
import json

from click.testing import CliRunner

from openvegas.cli import cli


def _jwt(email: str, sub: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"email": email, "sub": sub}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def test_whoami_logged_in(monkeypatch):
    monkeypatch.setattr(
        "openvegas.config.get_session",
        lambda: {
            "access_token": _jwt("user@example.com", "user-123"),
            "refresh_storage": "platform_credential_store",
        },
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["whoami"])
    assert result.exit_code == 0
    assert "email=user@example.com" in result.output
    assert "user_id=user-123" in result.output
    assert "refresh_storage=platform_credential_store" in result.output


def test_whoami_not_logged_in(monkeypatch):
    monkeypatch.setattr("openvegas.config.get_session", lambda: {})
    runner = CliRunner()
    result = runner.invoke(cli, ["whoami"])
    assert result.exit_code == 0
    assert "Not logged in" in result.output
