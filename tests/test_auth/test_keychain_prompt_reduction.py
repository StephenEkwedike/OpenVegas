import time

import pytest

from openvegas import config
from openvegas.client import OpenVegasClient
from openvegas.integrations.credentials import Credentials, IntegrationError


@pytest.fixture
def stored_session(monkeypatch):
    session = {"access_token": "fixture-access", "refresh_token": "",
               "refresh_storage": "platform_credential_store",
               "access_expires_at": int(time.time()) + 3600}
    monkeypatch.setattr(config, "load_config", lambda: {"session": dict(session)})
    return session


def test_metadata_and_bearer_do_not_unlock(monkeypatch, stored_session):
    monkeypatch.setattr(config, "load_refresh_from_platform_store",
                        lambda: pytest.fail("Metadata attempted Keychain access"))
    assert config.get_bearer_token() == "fixture-access"
    assert not config.token_expires_soon()
    assert config.token_expires_soon({})
    assert config.get_session_metadata()["refresh_token"] == ""


@pytest.mark.asyncio
async def test_valid_client_does_not_unlock(monkeypatch, stored_session):
    monkeypatch.setattr(config, "load_refresh_from_platform_store",
                        lambda: pytest.fail("Valid access token attempted Keychain access"))
    async with OpenVegasClient() as client:
        await client.auth_preflight()
        assert client.token == "fixture-access"


@pytest.mark.asyncio
async def test_refresh_reads_keychain_once(monkeypatch, stored_session):
    from openvegas import client as client_module
    reads = []
    stored_session["access_expires_at"] = 1
    monkeypatch.setattr(config, "load_refresh_from_platform_store",
                        lambda: reads.append(True) or "protected-refresh")

    class Auth:
        def refresh_token(self):
            assert config.get_session()["refresh_token"] == "protected-refresh"
            return "new-access"

    monkeypatch.setattr(client_module, "SupabaseAuth", Auth)
    async with OpenVegasClient() as client:
        await client.auth_preflight()
        assert client.token == "new-access"
    assert reads == [True]


def test_explicit_file_store_never_probes_keychain(monkeypatch, tmp_path):
    import keyring
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path))
    credentials = Credentials("https://openvegas.ai")
    credentials._directory()
    credentials.path.write_text('{"token":"file-fixture"}')
    credentials.path.chmod(0o600)
    monkeypatch.setattr(keyring, "get_password", lambda *args: pytest.fail("File store probed Keychain"))
    assert credentials.load() == "file-fixture"
    credentials.path.chmod(0o644)
    with pytest.raises(IntegrationError, match="unsafe"):
        credentials.load()


def test_missing_file_uses_protected_store(monkeypatch, tmp_path):
    import keyring
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path))
    monkeypatch.setattr(keyring, "get_password", lambda *args: "keychain-fixture")
    assert Credentials("https://openvegas.ai").load() == "keychain-fixture"


def test_symlink_file_is_rejected_without_keychain(monkeypatch, tmp_path):
    import keyring
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path))
    credentials = Credentials("https://openvegas.ai")
    credentials._directory()
    credentials.path.symlink_to(tmp_path / "missing")
    monkeypatch.setattr(keyring, "get_password", lambda *args: pytest.fail("Symlink probed Keychain"))
    with pytest.raises(OSError):
        credentials.load()


@pytest.mark.parametrize('denied', [False, True])
def test_background_read_suppresses_and_restores_ui(monkeypatch, tmp_path, denied):
    import sys, keyring
    from types import SimpleNamespace
    monkeypatch.setenv('OPENVEGAS_INTEGRATION_PROFILE', str(tmp_path))
    state = [True]
    transitions = []
    def set_allowed(value):
        transitions.append(value)
        state[0] = value
        return 0
    api = SimpleNamespace(SecKeychainGetUserInteractionAllowed=lambda _: (0, state[0]),
                          SecKeychainSetUserInteractionAllowed=set_allowed)
    monkeypatch.setitem(sys.modules, 'Security', api)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    def read(*args):
        assert state[0] is False
        if denied: raise RuntimeError('Denied')
        return 'fixture'
    monkeypatch.setattr(keyring, 'get_password', read)
    c = Credentials('https://openvegas.ai')
    assert c.load_noninteractive() == (None if denied else 'fixture')
    assert state[0] is True
    assert transitions == [False, True]
    assert not c.path.exists()


@pytest.mark.parametrize('prior', [True, False])
def test_keychain_policy_restored_after_exception(monkeypatch, prior):
    import sys
    from types import SimpleNamespace
    from openvegas.keychain_access import keychain_call
    calls = []
    api = SimpleNamespace(SecKeychainGetUserInteractionAllowed=lambda _: (0, prior),
                          SecKeychainSetUserInteractionAllowed=lambda value: calls.append(value) or 0)
    monkeypatch.setitem(sys.modules, 'Security', api)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    def fail():
        raise ValueError('fixture failure')
    with pytest.raises(ValueError):
        keychain_call(fail, interactive=False)
    assert calls == [False, prior]


def test_failed_ui_suppression_never_runs_keychain_read(monkeypatch):
    import sys
    from types import SimpleNamespace
    from openvegas.keychain_access import keychain_call
    api = SimpleNamespace(SecKeychainGetUserInteractionAllowed=lambda _: (0, True),
                          SecKeychainSetUserInteractionAllowed=lambda _: -1)
    monkeypatch.setitem(sys.modules, 'Security', api)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    with pytest.raises(RuntimeError, match='suppress'):
        keychain_call(lambda: pytest.fail('Interactive read executed'), interactive=False)
