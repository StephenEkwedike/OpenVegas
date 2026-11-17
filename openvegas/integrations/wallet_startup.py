"""Wallet-first managed launch, using the existing quoted grant protocol."""
from __future__ import annotations

import asyncio

import click

from . import native
from .codex_billing_route import _settle
from .credentials import IntegrationError
from .inference_router_wallet import CodexWalletGrant


def initial_billing(env):
    payer = env.get('OPENVEGAS_INITIAL_BILLING', 'v')
    if payer not in {'host', 'v'}:
        raise IntegrationError('Initial billing must be v or host; no session opened.')
    return payer


async def prepare_wallet(broker, model, version, *, emit=click.echo, confirm=click.confirm):
    grant = CodexWalletGrant(broker, version=version)

    def review(quote):
        price = quote['pricing']
        _, rate, multiplier = native._pricing(price)
        rates = [native._decimal(price[key], allow_zero=True) * rate * multiplier for key in
                 ('supplier_input_usd_per_million', 'supplier_output_usd_per_million')]
        emit(f"Codex | {model} | $V billing. Input/output per million: "
             f"{rates[0]:f}/{rates[1]:f} $V. No automatic top-ups or host-billing fallback.")
        return confirm('Start this session using $V?', default=False)

    try:
        return await grant.open(model, confirm_quote=review)
    except BaseException:
        if not grant.released:
            await _settle(asyncio.create_task(grant.close()))
        raise
