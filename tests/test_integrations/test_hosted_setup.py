"""Synthetic owned-profile migration; no real accounts or host configuration."""
import hashlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from openvegas.integrations import installer, game_browser
from openvegas.integrations.credentials import IntegrationError


@pytest.fixture
def old_install(tmp_path, monkeypatch):
    name = 'openvegas-local-' + 'a' * 12
    files = {'plugins/openvegas/SKILL.md': 'fixture'}
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    directory = tmp_path / 'old/integrations/hosts/codex'
    root = directory / fingerprint[:16]
    (root / 'plugins/openvegas').mkdir(parents=True)
    directory.chmod(0o700)
    (root / 'plugins/openvegas/SKILL.md').write_text('fixture')
    receipt = {'host': 'codex', 'marketplace': name, 'path': str(root), 'fingerprint': fingerprint}
    (directory / 'installed.json').write_text(json.dumps(receipt))
    (directory / 'installed.json').chmod(0o600)
    actual = {'registered': True, 'installed': True, 'enabled': True,
              'path': str(root), 'plugin_path': str(root / 'plugins/openvegas')}
    monkeypatch.setattr(installer, 'detect', lambda _: {'host': 'codex', 'executable': '/fixture/codex'})
    monkeypatch.setattr(installer, 'identity', lambda _: ('openvegas-local-' + 'b'*12, tmp_path / 'new'))
    monkeypatch.setattr(installer, 'registration', lambda *a, **kw: dict(actual))
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        if args[-2:] == ['list', '--json']:
            return SimpleNamespace(stdout=json.dumps({'marketplaces': [
                {'name': name, 'path': str(root)}, {'name': 'unrelated', 'path': '/untouched'}]}))
        if args[1:3] == ['plugin', 'remove']:
            actual['installed'] = False
        if args[1:4] == ['plugin', 'marketplace', 'remove']:
            actual['registered'] = False
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(installer, '_run', run)
    return directory, root, commands


def test_replaces_only_verified_owned_registration_and_retains_artifacts(old_install):
    directory, root, calls = old_install
    plans = installer.other_installations('codex')
    assert len(plans) == 1
    assert (directory / 'installed.json').exists()
    installer.retire_installation(plans[0])
    assert (directory / 'uninstalled.json').exists()
    assert not (directory / 'installed.json').exists()
    assert (root / 'plugins/openvegas/SKILL.md').read_text() == 'fixture'
    assert all('unrelated' not in args for args in calls)


def test_custom_bundle_cannot_be_retired(old_install):
    directory, root, calls = old_install
    (root / 'plugins/openvegas/SKILL.md').write_text('customized')
    with pytest.raises(IntegrationError, match='modified'):
        installer.other_installations('codex')
    assert all('remove' not in args for args in calls)
    assert (directory / 'installed.json').exists()


@pytest.mark.asyncio
async def test_hosted_game_never_refreshes_or_sends_local_cli_token(monkeypatch):
    from openvegas import client as client_module
    from openvegas.client import OpenVegasClient, APIError
    monkeypatch.setattr(client_module, 'get_session', lambda: {})
    monkeypatch.setattr(client_module, 'get_bearer_token', lambda: None)
    client = OpenVegasClient()
    client.token = 'fixture-local-token'
    monkeypatch.setattr(game_browser, 'game_credentials', lambda _: SimpleNamespace(load=lambda: None))
    refresh = AsyncMock(side_effect=AssertionError('must not refresh local credentials'))
    monkeypatch.setattr(client, '_refresh_single_flight', refresh)
    async def http(*a, **kw):
        assert 'Authorization' not in client._headers()
        return httpx.Response(401, json={'detail': 'sign in'})
    monkeypatch.setattr(client, '_do_http', http)
    game_browser.use_game_login(client, 'https://app.openvegas.ai')
    with pytest.raises(APIError):
        await client._request('GET', '/integrations/account')
    refresh.assert_not_awaited()
    await client._http_client.aclose()


@pytest.mark.asyncio
async def test_hosted_setup_does_not_read_legacy_cli_credentials(monkeypatch):
    from openvegas import config
    from openvegas.integrations.broker import Broker
    monkeypatch.setenv('OPENVEGAS_HOSTED_LAUNCH', '1')
    monkeypatch.setattr(config, 'get_session', lambda: pytest.fail('legacy account was read'))
    broker = Broker(backend='https://app.openvegas.ai')
    monkeypatch.setattr(broker.credentials, 'load', lambda: 'ov-device-fixture')
    monkeypatch.setattr(broker, 'account', AsyncMock(return_value={'user_id': 'fixture'}))
    assert await broker.authorize() == {'user_id': 'fixture'}
    await broker.close()


def test_frozen_billing_hook_skips_heavy_cli(monkeypatch):
    from openvegas.integrations import use
    called = Mock()
    monkeypatch.setattr(use, 'hook_main', called)
    monkeypatch.setattr(sys, 'argv', ['openvegas', 'integration', 'serve', '--host', 'codex',
                                     '--backend', 'https://app.openvegas.ai', '--billing-hook'])
    runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/frozen_entry.py'), run_name='__main__')
    called.assert_called_once_with('codex')


def test_packaged_cli_ignores_project_dotenv(monkeypatch, tmp_path):
    from openvegas import cli
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('OPENVEGAS_BACKEND_URL=http://127.0.0.1:8000\n')
    monkeypatch.setenv('OPENVEGAS_LOAD_PROJECT_ENV', '0')
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'https://app.openvegas.ai')
    monkeypatch.setenv('OPENVEGAS_DOTENV_OVERRIDE', '1')
    monkeypatch.setattr(cli, '_ENV_DEFAULTS_BOOTSTRAPPED', False)
    cli._load_openvegas_env_defaults_from_dotenv()
    assert cli.os.environ['OPENVEGAS_BACKEND_URL'] == 'https://app.openvegas.ai'
