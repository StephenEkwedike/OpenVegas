"""All automated macOS credential operations fail without opening system UI."""
import sys
from types import SimpleNamespace

import keyring
import pytest

from openvegas import config
from openvegas.integrations.credentials import Credentials, IntegrationError


@pytest.fixture
def protected(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path))
    allowed, stored, calls = [True], {}, []
    def set_allowed(value):
        allowed[0] = value
        return 0
    monkeypatch.setitem(sys.modules, "Security", SimpleNamespace(
        SecKeychainGetUserInteractionAllowed=lambda _: (0, allowed[0]),
        SecKeychainSetUserInteractionAllowed=set_allowed))
    def op(kind, service, account, *value):
        assert not allowed[0], "Automatic operation requested Keychain UI"
        calls.append(kind)
        if kind == "set":
            stored[service, account] = value[0]
        if kind == "get":
            return stored.get((service, account))
        if kind == "delete":
            stored.pop((service, account), None)
    monkeypatch.setattr(keyring, "get_password", lambda *args: op("get", *args))
    monkeypatch.setattr(keyring, "set_password", lambda *args: op("set", *args))
    monkeypatch.setattr(keyring, "delete_password", lambda *args: op("delete", *args))
    return allowed, stored, calls


def test_game_and_cosmetic_credentials_never_request_os_ui(protected):
    allowed, _, _ = protected
    for domain in ("https://app.openvegas.ai", "https://app.openvegas.ai\nhuman-game-login-v1"):
        credential = Credentials(domain)
        assert credential.load() is None
        credential.save("test-credential")
        assert credential.load() == credential.load_noninteractive() == "test-credential"
        credential.clear()
        assert credential.load() is None
        assert allowed[0]


def test_legacy_refresh_read_write_clear_never_request_os_ui(protected):
    config.save_refresh_to_platform_store("test-refresh")
    assert config.load_refresh_from_platform_store() == "test-refresh"
    config._clear_platform_refresh_token_only()
    assert config.load_refresh_from_platform_store() == ""
    assert protected[0][0]


def test_denied_read_no_retry_and_no_file(monkeypatch, protected):
    def denied(*args):
        assert not protected[0][0]
        raise RuntimeError("macOS interaction not allowed")
    monkeypatch.setattr(keyring, "get_password", denied)
    credential = Credentials("https://app.openvegas.ai")
    assert credential.load() is None
    assert not credential.path.exists()
    assert protected[0][0]


def test_denied_write_requires_explicit_file_consent(monkeypatch, protected):
    def denied(*args):
        assert not protected[0][0]
        raise RuntimeError("macOS interaction not allowed")
    monkeypatch.setattr(keyring, "set_password", denied)
    credential = Credentials("https://app.openvegas.ai")
    with pytest.raises(IntegrationError):
        credential.save("test-credential")
    assert not credential.path.exists()
    credential.save("test-credential", allow_file=True)
    assert credential.path.stat().st_mode & 0o777 == 0o600
    assert credential.load() == "test-credential"
    monkeypatch.setattr(keyring, "set_password", lambda *args: pytest.fail("Existing consented store changed"))
    credential.save("replacement-fixture")
    assert credential.load() == "replacement-fixture"


def test_unsafe_existing_file_not_overwritten_or_migrated(protected):
    credential = Credentials("https://app.openvegas.ai")
    credential._directory()
    credential.path.write_text('{"token":"test-credential"}')
    credential.path.chmod(0o644)
    with pytest.raises(IntegrationError, match="unsafe"):
        credential.save("replacement-fixture", allow_file=True)
    assert "replacement" not in credential.path.read_text()
    assert protected[2] == []
