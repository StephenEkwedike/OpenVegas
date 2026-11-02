from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from openvegas.integrations import game_browser as module


@pytest.mark.asyncio
async def test_native_disabled_is_not_reported_as_game_backend_disabled():
    from openvegas.integrations.broker import Broker
    from openvegas.integrations.credentials import IntegrationError
    broker = Broker(backend='https://example.invalid', transport=httpx.MockTransport(
        lambda _: httpx.Response(503)))
    try:
        with pytest.raises(IntegrationError, match='Host billing is unchanged'):
            await broker.request('GET', '/native/options?host=codex')
        with pytest.raises(IntegrationError, match='Native integrations'):
            await broker.request('GET', '/account')
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_purchase_opens_browser_without_charge_or_wager(monkeypatch):
    opened = Mock()
    monkeypatch.setattr(module, "open_account_browser", opened)
    ask = AsyncMock(return_value=SimpleNamespace(choice="continue"))
    client = SimpleNamespace(_request=AsyncMock())
    broker = SimpleNamespace(backend="http://127.0.0.1:8000", request=AsyncMock())
    assert await module.browser_action("purchase_v", ask, client, broker)
    opened.assert_called_once_with("http://127.0.0.1:8000/ui/balance")
    client._request.assert_not_awaited()
    broker.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_login_pending_then_same_client_and_wallet_grant(monkeypatch):
    opened = Mock()
    monkeypatch.setattr(module, "open_account_browser", opened)
    credentials = Mock()
    monkeypatch.setattr(module, "game_credentials", lambda backend: credentials)
    broker = SimpleNamespace(
        backend="http://127.0.0.1:8000",
        credentials=Mock(),
        leases={"codex": "old"},
        request=AsyncMock(
            side_effect=[
                {"user_code": "LOCALCODE", "device_code": "device_fixture"},
                {"status": "pending"},
                {
                    "status": "approved",
                    "token": "ov_device_fixture",
                    "game_token": "ov_game_fixture",
                },
            ]
        ),
    )
    ask = AsyncMock(return_value=SimpleNamespace(choice="continue"))
    client = SimpleNamespace(token=None)
    assert await module.browser_action("sign_in", ask, client, broker)
    assert client.token == "ov_game_fixture" and not broker.leases
    credentials.save.assert_called_once_with("ov_game_fixture")
    broker.credentials.save.assert_called_once_with("ov_device_fixture")
    calls = broker.request.call_args_list
    assert calls[0].args == ("POST", "/game-login/start")
    assert calls[1].kwargs["data"] == calls[2].kwargs["data"]
    assert "mode=game" in opened.call_args.args[0]
    assert "fixture" not in opened.call_args.args[0]


@pytest.mark.asyncio
async def test_signed_out_quit_never_calls_game(monkeypatch):
    action = AsyncMock()
    monkeypatch.setattr(module, "browser_action", action)
    ask = AsyncMock(return_value=SimpleNamespace(choice="quit"))
    client = SimpleNamespace(_request=AsyncMock())
    assert await module.signed_out_menu(ask, client, SimpleNamespace()) is None
    assert "Sign in" in ask.call_args.args[0]
    action.assert_not_awaited()
    client._request.assert_not_awaited()


def test_mac_browser_uses_launch_services_not_automation(monkeypatch):
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setattr(module.webbrowser, 'open', lambda _: pytest.fail('Apple Events not needed'))
    url = 'http://127.0.0.1:8000/ui/balance'
    assert module.open_account_browser(url)
    assert run.call_args.args == (['/usr/bin/open', url],)
    assert 'shell' not in run.call_args.kwargs
    assert run.call_args.kwargs['timeout'] == 5


@pytest.mark.parametrize('platform', ['linux', 'win32'])
def test_other_platforms_keep_system_browser(monkeypatch, platform):
    monkeypatch.setattr(module.sys, 'platform', platform)
    opened = Mock(return_value=True)
    monkeypatch.setattr(module.webbrowser, 'open', opened)
    assert module.open_account_browser('https://openvegas.ai/ui/balance')
    opened.assert_called_once_with('https://openvegas.ai/ui/balance')


@pytest.mark.asyncio
async def test_failed_browser_launch_is_explicit_and_cancellable(monkeypatch):
    monkeypatch.setattr(module, 'open_account_browser', lambda _: False)
    ask = AsyncMock(return_value=SimpleNamespace(choice='quit'))
    client = SimpleNamespace(_request=AsyncMock())
    broker = SimpleNamespace(backend='http://127.0.0.1:8000', request=AsyncMock())
    assert not await module.browser_action('purchase_v', ask, client, broker)
    assert 'Could not open your browser' in ask.call_args.args[0]
    assert '/ui/balance' in ask.call_args.args[0]
    client._request.assert_not_awaited()
    broker.request.assert_not_awaited()
