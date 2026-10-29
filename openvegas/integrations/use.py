"""Exact, pre-model billing commands. No model call or provider key on the client."""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
from pathlib import Path

import click

from .credentials import IntegrationError

SOCKET = 'OPENVEGAS_SWITCH_SOCKET'
TOKEN = 'OPENVEGAS_SWITCH_TOKEN'
COMMAND = re.compile(r'(?:/|\$)?openvegas(?::openvegas)? use (v|host)\Z', re.IGNORECASE)
MISSING = ('Billing switch is not connected to this host session. Restart the native host in '
           'the same OpenVegas-enabled terminal, then enter openvegas use v. No billing changed.')


async def request(host, action, *, session_id=None, model=None):
    path, token = os.getenv(SOCKET), os.getenv(TOKEN)
    if not path or not token:
        raise IntegrationError(MISSING)
    try:
        info = Path(path).lstat()
        parent = Path(path).parent.stat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
            raise ValueError
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection(path, limit=8192)
            try:
                writer.write(json.dumps({'token': token, 'host': host, 'action': action,
                    'session_id': session_id, 'model': model}).encode() + b'\n')
                await writer.drain()
                reply = json.loads(await reader.readline())
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
    except (OSError, ValueError, TimeoutError):
        raise IntegrationError(MISSING) from None


async def hook(host, data):
    if type(data) is not dict or data.get('agent_id'):
        return {}
    event = data.get('hook_event_name')
    match = COMMAND.fullmatch(str(data.get('prompt', '')).strip()) if event == 'UserPromptSubmit' else None
    if event != 'SessionStart' and match is None:
        return {}
    try:
        message = await request(host, match[1].lower() if match else 'register',
                                session_id=data.get('session_id'), model=data.get('model'))
    except IntegrationError as exc:
        message = str(exc)
    # Never let the model reinterpret a billing command or charge for handling it.
    return {'decision': 'block', 'reason': message} if match else {}


def hook_main(host):
    try:
        data = json.loads(sys.stdin.buffer.read(131073))
        result = asyncio.run(hook(host, data))
    except (ValueError, OSError):
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
