"""Exact, pre-model billing commands. No model call or provider key on the client."""
from __future__ import annotations

import asyncio
import json
import os
import re
import select
import stat
import sys
import time
from pathlib import Path

import click

from .credentials import IntegrationError

SOCKET = 'OPENVEGAS_SWITCH_SOCKET'
TOKEN = 'OPENVEGAS_SWITCH_TOKEN'
PREFIX = r'(?:/|\$)?openvegas(?::openvegas)?'
COMMAND = re.compile(PREFIX + r'[ \t]+use[ \t]+(v|host)\Z', re.IGNORECASE | re.ASCII)
PLAY_COMMAND = re.compile(PREFIX + r'[ \t]+play(?:[ \t]+(blackjack|roulette|poker|baccarat|slots|horse))?\Z', re.IGNORECASE | re.ASCII)
LOBBY_COMMAND = re.compile(PREFIX + r'\Z', re.IGNORECASE | re.ASCII)
MAX_INPUT = 131072
MAX_FRAME = 8192
MISSING = ('Local OpenVegas controls are not connected to this session. Restart the native host '
           'in the same OpenVegas-enabled terminal and retry your command. No billing changed.')


def parse_command(prompt):
    """Only whole control commands; newlines separate prose, never command tokens."""
    if type(prompt) is not str:
        return None
    prompt = prompt.strip(' \t\r\n')
    match = COMMAND.fullmatch(prompt)
    if match:
        return match[1].lower(), None
    play = PLAY_COMMAND.fullmatch(prompt)
    if play or LOBBY_COMMAND.fullmatch(prompt):
        return 'play', play[1].lower() if play and play[1] else None
    return None


def decode_json(raw):
    def reject_constant(_):
        raise ValueError('Non-JSON numeric constant')
    return json.loads(raw.decode('utf-8'), parse_constant=reject_constant)


async def request(host, action, *, session_id=None, model=None, game=None, transcript_path=None, rate_limits=None):
    path, token = os.getenv(SOCKET), os.getenv(TOKEN)
    if not path or not token or not hasattr(os, 'getuid'):
        raise IntegrationError(MISSING)
    try:
        payload = json.dumps({'token': token, 'host': host, 'action': action,
            'session_id': session_id, 'model': model, 'game': game,
            'transcript_path': transcript_path, 'rate_limits': rate_limits},
            ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
        if len(payload) > MAX_FRAME:
            raise ValueError
        info = Path(path).lstat()
        parent = Path(path).parent.stat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
            raise ValueError
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection(path, limit=MAX_FRAME)
            try:
                writer.write(payload)
                await writer.drain()
                frame = await reader.readline()
                if len(frame) > MAX_FRAME or not frame.endswith(b'\n'):
                    raise ValueError
                reply = decode_json(frame)
                if type(reply) is not dict or type(reply.get('message')) is not str:
                    raise ValueError
                if reply.get('ok') is not True:
                    raise IntegrationError(reply['message'])
                return reply['message']
            finally:
                writer.close()
                await writer.wait_closed()
    except IntegrationError:
        raise
    except (OSError, ValueError, TypeError, TimeoutError, RecursionError):
        raise IntegrationError(MISSING) from None


async def hook(host, data, *, input_failed=False):
    if (type(data) is not dict or host not in ('codex', 'claude')
            or (data.get('agent_id') is not None and data['agent_id'] != '')):
        return {}
    event = data.get('hook_event_name')
    command = parse_command(data.get('prompt')) if event == 'UserPromptSubmit' else None
    if event != 'SessionStart' and command is None:
        return {}
    if input_failed:
        return {'decision': 'block', 'reason': MISSING} if command else {}
    try:
        message = await request(host, command[0] if command else 'register',
                                session_id=data.get('session_id'), model=data.get('model'),
                                game=command[1] if command else None,
                                transcript_path=data.get('transcript_path'))
    except IntegrationError as exc:
        return {'decision': 'block', 'reason': str(exc)} if command else {}
    # A successful command was handled, not rejected by policy. Stop before the
    # model without manufacturing a blocking error; real failures stay visible.
    return {'continue': False, 'stopReason': message, 'suppressOutput': True} if command else {}


def read_input():
    """Bound pipe reads without letting a recognized, complete envelope fall through."""
    data = bytearray()
    deadline = time.monotonic() + 4
    fd = sys.stdin.fileno()
    while len(data) <= MAX_INPUT:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            return bytes(data), True
        chunk = os.read(fd, min(65536, MAX_INPUT + 1 - len(data)))
        if not chunk:
            return bytes(data), False
        data.extend(chunk)
    return bytes(data), True


def hook_main(host):
    try:
        raw, failed = read_input()
        data = decode_json(raw)
        result = asyncio.run(hook(host, data, input_failed=failed))
    except (ValueError, OSError, RecursionError):
        result = {}
    print(json.dumps(result))


@click.command('use')
@click.argument('mode', type=click.Choice(['v', 'host']))
def use(mode):
    """Switch billing in the connected native host; quote and consent come first."""
    try:
        click.echo(asyncio.run(request(None, mode)))
    except IntegrationError as exc:
        raise click.ClickException(str(exc)) from None
