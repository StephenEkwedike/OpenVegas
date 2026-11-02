import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openvegas.integrations import broker as module
from openvegas.integrations import commands
from openvegas.integrations.credentials import IntegrationError


@pytest.mark.asyncio
@pytest.mark.parametrize('consent', [True, False, None])
async def test_approval_retained_only_for_explicit_storage_consent(monkeypatch, consent):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr('openvegas.config.get_session_metadata', lambda: {'access_token': 'fixture'})
    monkeypatch.delenv('OPENVEGAS_HOSTED_LAUNCH', raising=False)
    broker = module.Broker(backend='https://example.invalid')
    broker._existing_session_request = AsyncMock(return_value={'token': 'ov_device_fixture'})
    broker.request = AsyncMock()
    broker.account = AsyncMock(return_value={'balance': '0'})
    save = Mock(side_effect=[IntegrationError('OS store denied'), 'private file'])
    broker.credentials = SimpleNamespace(load=lambda: None, save=save)
    ask = Mock(return_value=consent) if consent is not None else None
    try:
        if consent is True:
            assert await broker.authorize(confirm_file_storage=ask, emit=lambda _: None) == {'balance': '0'}
            assert save.call_args.kwargs == {'allow_file': True}
            broker.request.assert_not_awaited()
        else:
            with pytest.raises(IntegrationError):
                await broker.authorize(confirm_file_storage=ask, emit=lambda _: None)
            assert save.call_count == 1
            broker.request.assert_awaited_once_with('POST', '/revoke', token='ov_device_fixture')
        if ask:
            ask.assert_called_once_with()
        broker._existing_session_request.assert_awaited_once()
    finally:
        await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interactive', [False, True])
async def test_cli_noninteractive_never_implies_storage_consent(monkeypatch, interactive):
    async def authorize(**kwargs):
        return kwargs['confirm_file_storage']()
    broker = SimpleNamespace(authorize=authorize, close=AsyncMock())
    monkeypatch.setattr(commands, 'Broker', lambda: broker)
    monkeypatch.setattr(commands, '_interactive_terminal', lambda: interactive)
    ask = Mock(return_value=True)
    monkeypatch.setattr(commands.click, 'confirm', ask)
    emitted = []
    assert await commands.browser_login(emit=emitted.append) is interactive
    assert ask.call_count == int(interactive)
    if interactive:
        assert 'not encrypted' in emitted[0]
        assert ask.call_args.kwargs['default'] is False
    broker.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_mac_storage_failure_revokes_without_consent_retry(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr('openvegas.config.get_session_metadata', lambda: {'access_token': 'fixture'})
    monkeypatch.delenv('OPENVEGAS_HOSTED_LAUNCH', raising=False)
    broker = module.Broker(backend='https://example.invalid')
    broker._existing_session_request = AsyncMock(return_value={'token': 'ov_device_fixture'})
    broker.request = AsyncMock()
    broker.credentials = SimpleNamespace(load=lambda: None, save=Mock(side_effect=IntegrationError('disk denied')))
    ask = Mock(return_value=True)
    try:
        with pytest.raises(IntegrationError, match='disk denied'):
            await broker.authorize(confirm_file_storage=ask, emit=lambda _: None)
        assert broker.credentials.save.call_count == 1
        ask.assert_not_called()
        broker.request.assert_awaited_once_with('POST', '/revoke', token='ov_device_fixture')
    finally:
        await broker.close()
