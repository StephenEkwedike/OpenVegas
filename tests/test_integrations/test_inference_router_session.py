"""QA-only excerpt of the source fixture and wallet-revocation regression."""
import time
from unittest.mock import AsyncMock

import pytest

from openvegas.integrations.inference_router_adapter import ProviderAdapter
from openvegas.integrations.inference_router_session import RouterSession, WalletAccess

TOKEN = 'ov_native_' + 'synthetic_router_' * 2


def make_session(client=None, **overrides):
    callbacks = {'task_boundary': AsyncMock(return_value=True),
                 'prepare_wallet': AsyncMock(return_value=WalletAccess(TOKEN, time.time() + 60)),
                 'revoke_wallet': AsyncMock(return_value=True)}
    callbacks.update(overrides)
    return RouterSession(ProviderAdapter('claude', 'https://api.anthropic.com',
        'https://wallet.invalid/integrations/native'), client=client, **callbacks), callbacks


@pytest.mark.asyncio
async def test_peer_cleanup_error_cannot_skip_wallet_revocation():
    from types import SimpleNamespace

    session, callbacks = make_session()

    def failed_close():
        raise OSError('test handle cleanup failed')

    session._peer = SimpleNamespace(close=failed_close)
    with pytest.raises(OSError, match='test handle cleanup failed'):
        async with session.launch(version='2.1.322', env={}, args=[], effective_policy_verified=True):
            assert (await session.command('openvegas use v')).changed
    callbacks['revoke_wallet'].assert_awaited_once()
    assert session._wallet is None
