"""Human-only controls; never route menu navigation through a model."""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from .broker import Broker
from .credentials import IntegrationError
from .native_play import choice_model
from .terminal_handoff import TerminalView, close_writer, connect, play

GAMES = {'blackjack', 'roulette', 'poker', 'baccarat', 'slots', 'horse'}
BILLING_CHECK_SECONDS = 3
CLEANUP_SECONDS = .25


async def controls(mode, *, game=None, menu=False, check_v=None):
    action = 'play'
    if menu:
        reader, writer = await connect()
        view = TerminalView(reader, writer)
        try:
            description = (
                'OPENVEGAS / LOCAL CONTROLS\nNo Claude/Codex model request is needed.\n'
                'Opening this menu needs no wallet balance or host credits.\n'
                'Actual game stakes need your OpenVegas wallet; $V billing needs your spending consent.\n'
                'Esc returns to coding.')
            error = ''
            while True:
                answer = await asyncio.wait_for(view.ask(description + error,
                    choice_model('LocalControls', ['play', 'use_host' if mode == 'v' else 'use_v', 'quit'])), 180)
                action = answer.choice if answer else 'quit'
                if action == 'use_v':
                    try:
                        if check_v is None:
                            raise IntegrationError('Billing availability could not be checked. Billing unchanged.')
                        async with asyncio.timeout(BILLING_CHECK_SECONDS):
                            await check_v()
                    except IntegrationError as exc:
                        error = '\n\n' + str(exc)
                        continue
                    except Exception:  # noqa: BLE001 - Adapter failures must leave the menu usable.
                        error = ('\n\nBilling availability could not be checked. '
                                 'Billing unchanged; no spending authorized. Try again or quit.')
                        continue
                break
            await view.send(kind='done')
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if not isinstance(reply, dict) or reply.get('closed') is not True:
                raise IntegrationError('Terminal did not acknowledge closing. No action started.')
        finally:
            await close_writer(writer)
    if action != 'play':
        return action
    try:
        broker = Broker()
    except Exception:  # noqa: BLE001 - Sanitize initialization errors before any game action.
        raise IntegrationError('OpenVegas game services are unavailable. '
                               'No game request or new wager was sent; no automatic retry.') from None
    try:
        result = await play(None, broker, requested_game=game)
        if result.get('available') is False:
            raise IntegrationError(result.get('message', 'Game unavailable; no automatic retry.'))
        return 'quit'
    finally:
        with suppress(Exception):
            await asyncio.wait_for(broker.close(), CLEANUP_SECONDS)


class LocalKeys:
    """Recognize Ctrl+] then O, leaving host leader/voice keys untouched."""
    MENU = b'\x1do'
    START, END = b'\x1b[200~', b'\x1b[201~'

    def __init__(self):
        self.pending = bytearray()
        self.paste = False

    def feed(self, data, enabled):
        output, opened = bytearray(), False
        for byte in data:
            self.pending.append(byte)
            while self.pending:
                value = bytes(self.pending)
                if value in (self.START, self.END):
                    self.paste = value == self.START
                    output.extend(value)
                    self.pending.clear()
                elif value == self.MENU:
                    if enabled and not self.paste:
                        opened = True
                    else:
                        output.extend(value)
                    self.pending.clear()
                elif any(key.startswith(value) for key in (
                        self.START, self.END, *((self.MENU,) if enabled and not self.paste else ()))):
                    break
                else:
                    output.append(self.pending.pop(0))
        return bytes(output), opened

    @property
    def wait_seconds(self):
        return 1.5 if self.pending == self.MENU[:1] else .15

    def flush(self):
        data = bytes(self.pending)
        self.pending.clear()
        return data
