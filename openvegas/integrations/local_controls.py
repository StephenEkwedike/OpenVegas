"""Human-only controls; never route menu navigation through a model."""
from __future__ import annotations

import asyncio
import json

from .broker import Broker
from .credentials import IntegrationError
from .terminal_handoff import TerminalView, close_writer, connect, play
from .native_play import choice_model

GAMES = {'blackjack', 'roulette', 'poker', 'baccarat', 'slots', 'horse'}


async def controls(mode, *, game=None, menu=False):
    action = 'play'
    if menu:
        reader, writer = await connect()
        view = TerminalView(reader, writer)
        try:
            answer = await asyncio.wait_for(view.ask(
                'OPENVEGAS / LOCAL CONTROLS\nNo Claude/Codex model request is needed.\n'
                'Games use your OpenVegas wallet. Switching billing requires your spending limit.\n'
                'Esc returns to coding.',
                choice_model('LocalControls', ['play', 'use_host' if mode == 'v' else 'use_v', 'quit'])), 180)
            action = answer.choice if answer else 'quit'
            await view.send(kind='done')
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if reply.get('closed') is not True:
                raise IntegrationError('Terminal did not acknowledge closing. No action started.')
        finally:
            await close_writer(writer)
    if action != 'play':
        return action
    broker = Broker()
    try:
        result = await play(None, broker, requested_game=game)
        if result.get('available') is False:
            raise IntegrationError(result.get('message', 'Game unavailable; no automatic retry.'))
        return 'quit'
    finally:
        await broker.close()


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
