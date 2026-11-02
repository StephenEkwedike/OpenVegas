"""Browser approval is sufficient on macOS; even a Keychain probe is a failure."""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import keyring
import pytest

from openvegas import config
from openvegas.integrations.credentials import Credentials, IntegrationError
from openvegas.integrations import game_browser


@pytest.fixture
def protected(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    def forbidden(*args, **kwargs):
        pytest.fail("macOS credential operation attempted Keychain access")
    for name in ("get_password", "set_password", "delete_password", "get_keyring"):
        monkeypatch.setattr(keyring, name, forbidden)
    monkeypatch.setitem(sys.modules, "Security", SimpleNamespace(
        SecKeychainGetUserInteractionAllowed=forbidden, SecKeychainSetUserInteractionAllowed=forbidden))
    return tmp_path


def test_game_and_cosmetic_credentials_are_private_and_separate(protected):
    first = Credentials("https://app.openvegas.ai")
    game = game_browser.game_credentials("https://app.openvegas.ai")
    other = Credentials("http://localhost:8000")
    assert len({first.path, game.path, other.path}) == 3
    for store in (first, game):
        assert store.load() is None
        store.save("synthetic-first")
        assert store.load_noninteractive() == "synthetic-first"
        store.save("synthetic-next")
        assert store.load() == "synthetic-next"
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700
        store.clear()
        assert store.load() is None
    assert other.load() is None


def test_session_save_refresh_logout_never_probe_keychain(protected):
    config.save_session("synthetic-access", "synthetic-refresh")
    assert config.get_session()["refresh_token"] == "synthetic-refresh"
    config.save_session("synthetic-new", "synthetic-rotated", preserve_refresh_storage=True)
    assert config.get_session()["refresh_token"] == "synthetic-rotated"
    assert config.CONFIG_FILE.stat().st_mode & 0o777 == 0o600
    config.clear_session()
    assert not config.get_session().get("access_token")
    assert not config.platform_keychain_available()
    with pytest.raises(RuntimeError, match="keyring_unavailable"):
        config.save_refresh_to_platform_store("synthetic-refresh")


def test_legacy_platform_refresh_is_not_migrated_or_unlocked(protected):
    config.save_config({"session": {"refresh_storage": "platform_credential_store"}})
    assert config.get_session()["refresh_token"] == ""
    assert config.load_refresh_from_platform_store() == ""
    config.clear_persisted_refresh_token()


def test_unsafe_file_rejected_without_overwrite(protected):
    store = Credentials("https://app.openvegas.ai")
    store._directory()
    store.path.write_text('{"token":"synthetic"}')
    store.path.chmod(0o644)
    with pytest.raises(IntegrationError, match="unsafe"):
        store.save("replacement")
    assert "replacement" not in store.path.read_text()


def test_config_symlink_rejected(protected):
    target = protected / "outside.json"
    target.write_text("{}")
    config.CONFIG_FILE.symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        config.load_config()
    with pytest.raises(OSError, match="symlink"):
        config.save_session("synthetic", "synthetic")
    assert target.read_text() == "{}"


def make_login():
    broker = SimpleNamespace(backend="https://app.openvegas.ai", leases={},
        credentials=Credentials("https://app.openvegas.ai"), request=AsyncMock(side_effect=[
            {"user_code":"SYNTHETIC", "device_code":"fixture"},
            {"status":"approved", "token":"synthetic-device", "game_token":"synthetic-game"}, {}]))
    return broker, SimpleNamespace(token=None), AsyncMock(return_value=SimpleNamespace(choice="continue"))


@pytest.mark.asyncio
async def test_browser_approval_persists_without_storage_question(protected, monkeypatch):
    monkeypatch.setattr(game_browser, "open_account_browser", lambda _: True)
    broker, client, ask = make_login()
    assert await game_browser.browser_action("sign_in", ask, client, broker)
    assert ask.await_count == 1
    assert client.token == "synthetic-game"
    client.token = None
    game_browser.use_game_login(client, broker.backend)
    assert client.token == "synthetic-game"
    assert broker.credentials.load() == "synthetic-device"


@pytest.mark.asyncio
async def test_storage_failure_revokes_without_second_prompt(protected, monkeypatch):
    monkeypatch.setattr(game_browser, "open_account_browser", lambda _: True)
    broker, client, ask = make_login()
    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(broker.credentials, "save", fail)
    with pytest.raises(IntegrationError, match="Could not save"):
        await game_browser.browser_action("sign_in", ask, client, broker)
    assert ask.await_count == 1
    assert client.token is None
    assert game_browser.game_credentials(broker.backend).load() is None
    assert broker.request.call_args.args == ("POST", "/revoke")
