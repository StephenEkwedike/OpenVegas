"""Wallet-first managed launch, using the existing quoted grant protocol."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

import click

from . import native
from .codex_billing_route import _settle
from .credentials import IntegrationError
from .inference_router_wallet import CodexWalletGrant
from .wallet_offer_digest import offer_digest


def initial_billing(env):
    payer = env.get('OPENVEGAS_INITIAL_BILLING', 'v')
    if payer not in {'host', 'v'}:
        raise IntegrationError('Initial billing must be v or host; no session opened.')
    return payer


def _remember_tag(backend, device, quote, options):
    offers = offer_digest(options['models'], quote['max_tokens'])
    scope = ['native-codex-wallet-v1', backend, quote['model'], quote['pricing']['fingerprint'], offers]
    return hmac.new(device.encode(), json.dumps(scope, separators=(',', ':')).encode(),
                    hashlib.sha256).hexdigest()


async def prepare_wallet(broker, model, version, *, emit=click.echo, confirm=None):
    grant = CodexWalletGrant(broker, version=version)

    def display(quote):
        price = quote['pricing']
        _, rate, multiplier = native._pricing(price)
        rates = [native._decimal(price[key], allow_zero=True) * rate * multiplier for key in
                 ('supplier_input_usd_per_million', 'supplier_output_usd_per_million')]
        emit(f"Codex | {model} | $V billing. Input/output per million: "
             f"{rates[0]:f}/{rates[1]:f} $V. No automatic top-ups or host-billing fallback.")

    def review(quote):
        display(quote)
        return confirm('Start this session using $V?', default=False)

    pending = None

    def review_catalog(quote, options, device):
        nonlocal pending
        tag = _remember_tag(broker.backend, device, quote, options)
        path = broker.credentials.path.with_suffix('.codex-wallet-consent')
        try:
            if (not path.is_symlink() and path.is_file() and path.stat().st_size == 64 and
                    hmac.compare_digest(path.read_bytes(), tag.encode())):
                return True
        except OSError:
            pass
        display(quote)
        choice = click.prompt('Start using $V? Choose y for once, always for this device and unchanged rates, or n',
                              type=click.Choice(['y', 'always', 'n'], case_sensitive=False), default='n')
        if choice == 'always':
            pending = (path, tag)
        return choice in ('y', 'always')

    try:
        access = await grant.open(model, **({'confirm_quote': review} if confirm is not None else
                                             {'review_catalog': review_catalog}))
        if pending:
            path, tag = pending
            tmp = None
            try:
                broker.credentials._directory()
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
                    tmp = Path(f.name)
                    f.write(tag)
                os.replace(tmp, path)
            except OSError:
                emit('Could not remember this choice. This session still uses $V.')
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
        return access
    except BaseException:
        if not grant.released:
            await _settle(asyncio.create_task(grant.close()))
        raise
