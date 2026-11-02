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
from copy import deepcopy
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


def _reviewed_ids(models, selected):
    """Accept backend selection descriptors, not inferred provider aliases."""
    if not isinstance(models, (list, tuple)) or not 1 <= len(models) <= 128:
        raise IntegrationError('Codex requires a nonempty reviewed model catalog.')
    identifiers = []
    for row in models:
        if isinstance(row, str):
            row = {'provider': 'openai', 'model': row}
        if not isinstance(row, dict):
            raise IntegrationError('Invalid reviewed Codex model descriptor.')
        identifier = row.get('model', row.get('model_id'))
        if (row.get('provider') != 'openai' or row.get('host', 'codex') != 'codex'
                or not isinstance(identifier, str) or not identifier
                or len(identifier) > 200 or '\x00' in identifier
                or row.get('model_id', identifier) != identifier or identifier in identifiers):
            raise IntegrationError('Invalid reviewed Codex model descriptor.')
        identifiers.append(identifier)
    if selected not in identifiers:
        raise IntegrationError('Selected Codex model is not in the reviewed catalog.')
    return identifiers


def _filter_catalog(bundled, identifiers):
    """Keep the installed CLI's exact capabilities and effort metadata."""
    rows = bundled.get('models') if isinstance(bundled, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise IntegrationError('Codex bundled model metadata is unavailable.')
    selected = []
    for identifier in identifiers:
        matches = [row for row in rows if row.get('slug') == identifier]
        if len(matches) != 1:
            raise IntegrationError('A reviewed model has no exact installed Codex metadata.')
        row = deepcopy(matches[0])
        efforts = row.get('supported_reasoning_levels')
        if (row.get('supported_in_api') is not True or row.get('visibility') != 'list'
                or not isinstance(efforts, list) or not efforts
                or any(not isinstance(level, dict) or not isinstance(level.get('effort'), str)
                       or not isinstance(level.get('description'), str) for level in efforts)
                or row.get('default_reasoning_level') not in [level['effort'] for level in efforts]
                or not isinstance(row.get('input_modalities'), list)):
            raise IntegrationError('Reviewed model lacks usable native picker metadata.')
        # A native upgrade prompt must not escape the backend's allowlist.
        upgrade = row.get('upgrade')
        if upgrade is not None and (not isinstance(upgrade, dict)
                                    or upgrade.get('model') not in identifiers):
            row['upgrade'] = None
        selected.append(row)
    return {'models': selected}


async def _bundled_catalog(executable, directory, env):
    home = Path(directory) / 'catalog-home'
    home.mkdir(mode=0o700)
    # The supported --bundled path never refreshes. It also gets no host auth,
    # grant, config, daemon address or provider environment from this process.
    isolated = {key: env[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in env}
    isolated.update(HOME=str(home), CODEX_HOME=str(home), TMPDIR=str(home))
    process = None
    process_group = None
    try:
        process, cancelled = await _settle(asyncio.create_task(asyncio.create_subprocess_exec(
            executable, 'debug', 'models', '--bundled', env=isolated, cwd=home,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)))
        process_group = getattr(process, 'pid', None)
        if cancelled:
            raise asyncio.CancelledError
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode != 0 or len(stdout) > 8 * 1024 * 1024:
            raise IntegrationError('Codex bundled model metadata is unavailable.')
        return json.loads(stdout)
    finally:
        if process is not None:
            await stop(process, process_group=process_group)


def _verify_catalog(result, catalog):
    rows = result.get('data') if isinstance(result, dict) else None
    expected = {row['slug']: row for row in catalog['models']}
    if (not isinstance(rows, list) or len(rows) != len(expected) or result.get('nextCursor')
            or any(not isinstance(row, dict) or not isinstance(row.get('model'), str)
                   for row in rows)
            or {row['model'] for row in rows} != set(expected)):
        raise IntegrationError('Codex did not isolate the reviewed native model catalog.')
    for row in rows:
        metadata = expected[row['model']]
        efforts = [{'reasoningEffort': level['effort'], 'description': level['description']}
                   for level in metadata['supported_reasoning_levels']]
        if (row.get('hidden') is not False or row.get('id') != row['model']
                or row.get('defaultReasoningEffort') != metadata['default_reasoning_level']
                or row.get('supportedReasoningEfforts') != efforts
                or row.get('inputModalities') != metadata['input_modalities']
                or row.get('upgrade') not in (None, *expected)):
            raise IntegrationError('Codex native model capabilities differ from installed metadata.')


@asynccontextmanager
async def route(executable, backend, model, env, *, local_tools_only=True, allowed_models=None):
    """Yield a private endpoint with optional backend-reviewed native model choices.

    allowed_models contains direct OpenAI IDs or selection descriptors (provider
    and model/model_id), not fabricated Codex capability metadata. Missing exact
    installed descriptors fail closed; the backend still owns spending policy.
    """
    token = env.get('OPENVEGAS_NATIVE_TOKEN', '')
    if not token.startswith('ov_native_'):
        raise IntegrationError('Missing OpenVegas spending authorization; no Codex route opened.')
    identifiers = _reviewed_ids(allowed_models, model) if allowed_models is not None else None
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
            catalog = None
            catalog_path = None
            if identifiers is not None:
                catalog = _filter_catalog(await _bundled_catalog(executable, directory, env), identifiers)
                catalog_path = Path(directory) / 'models.json'
                with catalog_path.open('x', encoding='utf-8') as output:
                    os.chmod(catalog_path, 0o600)
                    json.dump(catalog, output)
                controls += ['-c', 'model_catalog_json=' + json.dumps(str(catalog_path))]
            process, cancelled = await _settle(asyncio.create_task(asyncio.create_subprocess_exec(
                executable, '--no-daemon', *controls, 'app-server', '--listen', 'unix://' + socket,
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
                    if catalog is not None:
                        configured = effective.get('model_catalog_json')
                        if (not isinstance(configured, str)
                                or Path(configured).resolve() != catalog_path.resolve()):
                            raise IntegrationError('Codex configuration cannot isolate the model catalog.')
                        listed = await rpc(connection, 5, 'model/list', {
                            'includeHidden': True, 'limit': len(identifiers) + 1})
                        _verify_catalog(listed, catalog)
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
