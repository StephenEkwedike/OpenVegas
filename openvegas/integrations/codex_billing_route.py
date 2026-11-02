"""Own a native Codex app-server with process-only proxy authentication.

Codex binds saved chats to their provider ID. Its supported openai_base_url
override keeps that ID; ephemeral auth keeps the wallet grant out of the user's
auth.json and Keychain. The ordinary native TUI attaches over a private socket.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from websockets.asyncio.client import unix_connect

from .credentials import IntegrationError


async def rpc(connection, identifier, method, params):
    await connection.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
    async with asyncio.timeout(10):
        while True:
            value = json.loads(await connection.recv())
            if value.get('id') == identifier:
                if 'error' in value:
                    # Native diagnostics can contain paths, credentials or prompts.
                    raise IntegrationError('Codex could not prepare the billing connection. No prompt submitted.')
                return value.get('result', {})


_STOP_GRACE_SECONDS = 5
_STOP_KILL_SECONDS = 5


async def _settle(task):
    """Finish an owned operation despite repeated caller cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _stop(process, process_group):
    if process_group is None:
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), _STOP_GRACE_SECONDS)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), _STOP_KILL_SECONDS)
            except ProcessLookupError:
                await asyncio.wait_for(process.wait(), _STOP_KILL_SECONDS)
        return

    # Only a group created by our start_new_session spawn may be targeted.
    if (type(process_group) is not int or process_group <= 1
            or process_group != process.pid or process_group == os.getpgrp()):
        raise IntegrationError('Refusing to stop an unowned Codex process group.')

    def signal_group(sig):
        try:
            os.killpg(process_group, sig)
            return True
        except ProcessLookupError:
            return False

    if signal_group(signal.SIGTERM):
        deadline = asyncio.get_running_loop().time() + _STOP_GRACE_SECONDS
        # The leader may already be reaped while its tools are still running.
        while signal_group(0):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                signal_group(signal.SIGKILL)
                break
            await asyncio.sleep(min(.05, remaining))
    await asyncio.wait_for(process.wait(), _STOP_KILL_SECONDS)


async def stop(process, *, process_group=None):
    _, cancelled = await _settle(asyncio.create_task(_stop(process, process_group)))
    if cancelled:
        raise asyncio.CancelledError


@asynccontextmanager
async def route(executable, backend, model, env, *, local_tools_only=True):
    """Yield a private endpoint; never log in to or change the host's account."""
    token = env.get('OPENVEGAS_NATIVE_TOKEN', '')
    if not token.startswith('ov_native_'):
        raise IntegrationError('Missing OpenVegas spending authorization; no Codex route opened.')
    with tempfile.TemporaryDirectory(prefix='ov-codex-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        socket = str(Path(directory) / 'native.sock')
        controls = [
            '-c', 'model_provider="openai"',
            '-c', 'openai_base_url=' + json.dumps(backend + '/integrations/native/v1'),
            '-c', 'cli_auth_credentials_store="ephemeral"',
            '-c', 'model=' + json.dumps(model),
            '-c', 'review_model=' + json.dumps(model),
        ]
        if local_tools_only:
            controls += ['-c', 'web_search="disabled"']
        process = None
        process_group = None
        try:
            process, cancelled = await _settle(asyncio.create_task(asyncio.create_subprocess_exec(
                executable, *controls, 'app-server', '--listen', 'unix://' + socket,
                env=env, stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)))
            process_group = getattr(process, 'pid', None)
            if cancelled:
                raise asyncio.CancelledError
            async with asyncio.timeout(12):
                while not Path(socket).exists():
                    if process.returncode is not None:
                        raise IntegrationError('Codex could not open its local billing connection. No prompt submitted.')
                    await asyncio.sleep(.05)
                os.chmod(socket, 0o600)
                async with unix_connect(socket, open_timeout=3, max_size=1024 * 1024) as connection:
                    await rpc(connection, 1, 'initialize', {
                        'clientInfo': {'name': 'openvegas-billing', 'version': '1'}})
                    await connection.send(json.dumps({'method': 'initialized', 'params': {}}))
                    configuration = await rpc(connection, 4, 'config/read', {'includeLayers': False})
                    effective = configuration.get('config', {})
                    if (effective.get('cli_auth_credentials_store') != 'ephemeral'
                            or effective.get('model_provider') != 'openai'
                            or effective.get('openai_base_url') != backend + '/integrations/native/v1'):
                        raise IntegrationError('Codex configuration cannot isolate OpenVegas billing. '
                                               'Saved login unchanged; no prompt submitted.')
                    await rpc(connection, 2, 'account/login/start', {'type': 'apiKey', 'apiKey': token})
                    account = await rpc(connection, 3, 'account/read', {'refreshToken': False})
                    if account.get('account', {}).get('type') != 'apiKey':
                        raise IntegrationError('Codex did not accept the private billing grant. No prompt submitted.')
            yield 'unix://' + socket
        except (IntegrationError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - Redact credential-bearing native exceptions.
            raise IntegrationError('Codex billing connection failed. No automatic prompt or retry was sent.') from None
        finally:
            if process is not None:
                await stop(process, process_group=process_group)
