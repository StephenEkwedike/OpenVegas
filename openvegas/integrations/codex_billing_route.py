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
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

from websockets.asyncio.client import unix_connect

from .credentials import IntegrationError
from .host_adapter import LocalServerLaunch
from .native_owned_process import OwnedProcess
from .native_owned_process import spawn as _spawn_owned

_OWNED_SERVERS = {}
_POLICY_CONTEXT = {}
_LIVE_SOURCE_PROOFS = {}


def _handoff_server_argv(argv):
    # Shorten only Codex's own grace period for idle, unsubscribed runtimes.
    # Never infer that a second runtime is safe to terminate.
    return [argv[0], '-c', 'thread_unload_delay_secs=2', *argv[1:]]


def _launch_scope(env, cwd):
    # Compared in memory, never logged. HOME/CODEX_HOME locate trust and
    # TMPDIR may be writable without appearing among explicit writableRoots.
    return {k:v for k,v in env.items() if k in ('HOME', 'USERPROFILE', 'PATH', 'TMPDIR', 'CODEX_HOME')
            or k.startswith(('XDG_', 'CODEX_SANDBOX', 'CODEX_MANAGED', 'CODEX_CONFIG'))} | {
                'cwd': os.path.realpath(cwd)}


@asynccontextmanager
async def _private_server(argv, env, cwd):
    with tempfile.TemporaryDirectory(prefix='ov-policy-', dir='/tmp') as directory:
        socket = Path(directory) / 'native.sock'
        endpoint = 'unix://' + str(socket)
        process, cancelled = await _settle(asyncio.create_task(_spawn_owned(
            *argv(endpoint), env=env, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)))
        try:
            if cancelled:
                raise asyncio.CancelledError
            async with asyncio.timeout(12):
                while not socket.exists():
                    if process.returncode is not None:
                        raise IntegrationError('Private Codex server did not start.')
                    await asyncio.sleep(.05)
            socket.chmod(0o600)
            _OWNED_SERVERS[endpoint] = process
            _POLICY_CONTEXT[endpoint] = _launch_scope(env, cwd)
            yield endpoint
        finally:
            await _release_owned(process, process.pid, endpoint)


@asynccontextmanager
async def host_server(executable, env, *, cwd, server_args=()):
    """Original native auth/config only. Do not reinterpret arbitrary CLI flags."""
    if (env.get('OPENVEGAS_NATIVE_TOKEN') or not Path(cwd).is_absolute()
            or not Path(cwd).is_dir() or server_args):
        raise IntegrationError('Unsupported original Codex server arguments or environment.')
    async with _private_server(lambda endpoint: _handoff_server_argv(
            [executable, '--no-daemon', 'app-server', '--listen', endpoint]), env, cwd) as endpoint:
        yield endpoint


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
    # A numeric PID or an asyncio Process (whose watcher reaps automatically)
    # cannot authorize a group signal. Only our retained child lifetime can.
    if (not isinstance(process, OwnedProcess)
            or type(process_group) is not int or process_group <= 1
            or process_group != process.pid or process_group == os.getpgrp()):
        raise IntegrationError('Refusing to stop an unowned Codex process group.')
    async with process.stop_lock:
        if process.retired:
            return
        _ = process.returncode  # Verify the retained lifetime even for an exited leader.
        if await _retired_group(process_group):
            process.reap_retired_group()
            return
        denied = False
        try:
            process.signal_group(signal.SIGTERM)
        except PermissionError:
            denied = True
        deadline = asyncio.get_running_loop().time() + _STOP_GRACE_SECONDS
        while not await _retired_group(process_group):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if denied:
                    raise IntegrationError('Cannot verify retirement of owned Codex processes.')
                process.signal_group(signal.SIGKILL)
                async with asyncio.timeout(_STOP_KILL_SECONDS):
                    while not await _retired_group(process_group):
                        await asyncio.sleep(.05)
                break
            await asyncio.sleep(min(.05, remaining))
        await asyncio.wait_for(process.wait(), _STOP_KILL_SECONDS)
        process.reap_retired_group()


async def _retired_group(group):
    if sys.platform == 'darwin':
        from .native_process import darwin_group_retired
        return await asyncio.to_thread(darwin_group_retired, group)
    try:
        inspector = await asyncio.to_thread(subprocess.run, ['/bin/ps', '-axo', 'pgid=,state='],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    rows = [line.split() for line in inspector.stdout.splitlines()]
    if inspector.returncode != 0 or not rows or any(len(r) != 2 or not r[0].isdigit() for r in rows):
        return False
    return not any(int(pgid) == group and not state.startswith(b'Z') for pgid, state in rows)


async def stop(process, *, process_group=None):
    _, cancelled = await _settle(asyncio.create_task(_stop(process, process_group)))
    if cancelled:
        raise asyncio.CancelledError


async def _release_owned(process, process_group, endpoint):
    async def release():
        await _stop(process, process_group)
        # Cancellation cannot interrupt successful stop + ownership removal.
        # Failed retirement retains its evidence; never remove a newer owner.
        if _OWNED_SERVERS.get(endpoint) is process:
            _OWNED_SERVERS.pop(endpoint)
            _POLICY_CONTEXT.pop(endpoint, None)
            _LIVE_SOURCE_PROOFS.pop(endpoint, None)

    _, cancelled = await _settle(asyncio.create_task(release()))
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


async def preflight_wallet(executable, backend, model, env, *, local_tools_only, source):
    """No grant or inference. Recheck the actual grant-bound process in route()."""
    from .codex_handoff_policy import verify_target
    if (env.get('OPENVEGAS_NATIVE_TOKEN') or env.get('OPENVEGAS_SWITCH_TOKEN')
            or env.get('OPENVEGAS_TERMINAL_TOKEN')):
        raise IntegrationError('Preflight must not have wallet credentials.')
    async with _private_server(lambda endpoint: _handoff_server_argv(LocalServerLaunch(
            executable, endpoint, backend, model, local_tools_only).argv()), env, source.cwd) as endpoint:
        await verify_target(endpoint, source, model=model)


def _filter_catalog(bundled, identifiers):
    """Keep the installed CLI's exact capabilities and effort metadata."""
    if (not isinstance(identifiers, (list, tuple)) or not 1 <= len(identifiers) <= 128
            or any(not isinstance(value, str) or not value or len(value) > 200
                   or '\x00' in value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise IntegrationError('Codex requires a nonempty exact reviewed model catalog.')
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
                or not isinstance(row.get('display_name'), str) or not row['display_name']
                or not isinstance(efforts, list) or not efforts
                or any(not isinstance(level, dict) or not isinstance(level.get('effort'), str)
                       or not isinstance(level.get('description'), str) for level in efforts)
                or row.get('default_reasoning_level') not in [level['effort'] for level in efforts]
                or len({level['effort'] for level in efforts}) != len(efforts)
                or any(not level['effort'] for level in efforts)
                or not isinstance(row.get('input_modalities'), list)
                or not row['input_modalities']
                or any(not isinstance(value, str) or not value for value in row['input_modalities'])):
            raise IntegrationError('Reviewed model lacks usable native picker metadata.')
        # A native upgrade prompt must not escape the backend's allowlist.
        upgrade = row.get('upgrade')
        if upgrade is not None and (not isinstance(upgrade, dict)
                                    or upgrade.get('model') not in identifiers):
            row['upgrade'] = None
        selected.append(row)
    return {'models': selected}


def _catalog_digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _validate_grant_catalog(value, *, version, session_id, catalog_hash, identifiers):
    import re
    import time

    from .codex_catalog_schema import valid_native_row
    from .host_adapter import require_version
    require_version('codex', version)
    message = 'Reviewed native metadata does not match this grant; no model substituted.'
    if (not isinstance(value, dict) or set(value) != {
            'schema_version', 'client_version', 'session_id', 'catalog_hash', 'catalog_fence_hash',
            'review_revisions', 'artifact_sha256', 'source', 'expires_at', 'models_response',
            'catalog_sha256', 'delivery_sha256'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['client_version'] != version or value['session_id'] != session_id
            or not isinstance(catalog_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', catalog_hash)
            or value['catalog_hash'] != catalog_hash
            or type(value['expires_at']) is not int or not time.time() < value['expires_at'] <= time.time() + 8 * 3600
            or not isinstance(value['review_revisions'], dict)
            or set(value['review_revisions']) != set(identifiers)):
        raise IntegrationError(message)
    digests = [value[key] for key in ('artifact_sha256', 'catalog_sha256', 'catalog_fence_hash', 'delivery_sha256')]
    if any(not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
           for digest in [*digests, *value['review_revisions'].values()]):
        raise IntegrationError(message)
    source = value['source']
    if (not isinstance(source, dict) or set(source) != {
            'kind', 'client_version', 'binary_sha256', 'catalog_sha256', 'captured_at'}
            or source['kind'] != 'codex_bundled_export' or source['client_version'] != version
            or any(not isinstance(source[key], str) or not re.fullmatch(r'[a-f0-9]{64}', source[key])
                   for key in ('binary_sha256', 'catalog_sha256'))
            or type(source['captured_at']) is not int or not 0 < source['captured_at'] <= time.time()
            or _catalog_digest({k: v for k, v in value.items() if k != 'delivery_sha256'}) != value['delivery_sha256']
            or _catalog_digest(value['models_response']) != value['catalog_sha256']):
        raise IntegrationError(message)
    catalog = _filter_catalog(value['models_response'], identifiers)
    for row in catalog['models']:
        if not valid_native_row(row):
            raise IntegrationError(message)
    # Reject extra rows or a server-supplied migration outside the frozen scope,
    # rather than silently repairing the authenticated delivery.
    if catalog != value['models_response']:
        raise IntegrationError(message)
    return catalog


async def _grant_catalog(broker, token, *, version, session_id, catalog_hash, identifiers):
    """Opt-in bounded metadata read from the already selected backend only."""
    import httpx

    from .host_adapter import require_version
    setting = os.getenv('OPENVEGAS_CODEX_CATALOG_DELIVERY', '')
    if not setting:
        return None
    if setting != 'reviewed-v1':
        raise IntegrationError('Unsupported Codex metadata delivery mode.')
    require_version('codex', version)
    import re
    if (not isinstance(token, str) or not re.fullmatch(r'ov_native_[A-Za-z0-9_-]{16,128}', token)
            or not isinstance(catalog_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', catalog_hash)):
        raise IntegrationError('Reviewed Codex metadata requires an exact native grant binding.')
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        async with asyncio.timeout(10):
            async with broker.http.stream('GET', '/integrations/native/codex/catalog',
                    params={'client_version': version}, follow_redirects=False,
                    headers={'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}) as response:
                if (response.status_code != 200
                        or response.headers.get('content-encoding', 'identity') != 'identity'):
                    raise IntegrationError('Reviewed Codex metadata unavailable; no bundled fallback or retry.')
                raw = bytearray()
                async for part in response.aiter_bytes(chunk_size=65536):
                    raw.extend(part)
                    if len(raw) > 1024 * 1024:
                        raise IntegrationError('Reviewed Codex metadata exceeds the delivery limit.')
        value = json.loads(raw, object_pairs_hook=pairs)
        _validate_grant_catalog(value, version=version, session_id=session_id,
                                catalog_hash=catalog_hash, identifiers=identifiers)
        return value
    except IntegrationError:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, RecursionError):
        raise IntegrationError('Reviewed Codex metadata could not be verified; no fallback or retry.') from None


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
        process, cancelled = await _settle(asyncio.create_task(_spawn_owned(
            executable, 'debug', 'models', '--bundled', env=isolated, cwd=home,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)))
        process_group = getattr(process, 'pid', None)
        if cancelled:
            raise asyncio.CancelledError
        stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode != 0 or len(stdout) > 8 * 1024 * 1024:
            raise IntegrationError('Codex bundled model metadata is unavailable.')
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeError):
            raise IntegrationError('Codex bundled model metadata is unavailable; '
                                   'validate the installed CLI catalog schema.') from None
    finally:
        if process is not None:
            await stop(process, process_group=process_group)


def _verify_catalog(result, catalog):
    rows = result.get('data') if isinstance(result, dict) else None
    expected = {row['slug']: row for row in catalog['models']}
    if (not isinstance(rows, list) or len(rows) != len(expected)
            or result.get('nextCursor') is not None
            or any(not isinstance(row, dict) or not isinstance(row.get('model'), str)
                   for row in rows)
            or {row['model'] for row in rows} != set(expected)):
        raise IntegrationError('Codex did not isolate the reviewed native model catalog.')
    for row in rows:
        metadata = expected[row['model']]
        efforts = [{'reasoningEffort': level['effort'], 'description': level['description']}
                   for level in metadata['supported_reasoning_levels']]
        upgrade = metadata.get('upgrade')
        upgrade_target = upgrade['model'] if upgrade is not None else None
        upgrade_info = row.get('upgradeInfo')
        if (row.get('hidden') is not False or row.get('id') != row['model']
                or row.get('displayName') != metadata['display_name']
                or row.get('defaultReasoningEffort') != metadata['default_reasoning_level']
                or row.get('supportedReasoningEfforts') != efforts
                or row.get('inputModalities') != metadata['input_modalities']
                or row.get('upgrade') != upgrade_target
                or ('upgradeInfo' in row and (
                    (upgrade_target is None and upgrade_info is not None)
                    or (upgrade_target is not None and (
                        not isinstance(upgrade_info, dict)
                        or upgrade_info.get('model') != upgrade_target))))):
            raise IntegrationError('Codex native model capabilities differ from installed metadata.')


async def _list_catalog(connection, *, maximum=128):
    """Follow the installed schema's cursor without trusting unbounded pages."""
    if type(maximum) is not int or not 1 <= maximum <= 128:
        raise IntegrationError('Invalid reviewed Codex model catalog bound.')
    rows, cursors, cursor = [], set(), None
    for page in range(maximum):
        params = {'includeHidden': True, 'limit': min(maximum + 1, 100)}
        if cursor is not None:
            params['cursor'] = cursor
        result = await rpc(connection, 100 + page, 'model/list', params)
        if not isinstance(result, dict) or not isinstance(result.get('data'), list):
            raise IntegrationError('Unsupported Codex model catalog schema.')
        rows.extend(result['data'])
        if len(rows) > maximum:
            raise IntegrationError('Codex model catalog exceeds the reviewed scope.')
        cursor = result.get('nextCursor')
        if cursor is None:
            return {'data': rows, 'nextCursor': None}
        if len(rows) == maximum:
            raise IntegrationError('Codex model catalog exceeds the reviewed scope.')
        if (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
                or cursor in cursors or not result['data']):
            raise IntegrationError('Invalid Codex model catalog pagination.')
        cursors.add(cursor)
    raise IntegrationError('Codex model catalog pagination exceeded its bound.')


@asynccontextmanager
async def route(executable, backend, model, env, *, local_tools_only=True, allowed_models=None,
                handoff_source=None, reviewed_catalog=None, catalog_binding=None):
    """Yield a private endpoint with optional backend-reviewed native model choices.

    allowed_models contains direct OpenAI IDs or selection descriptors (provider
    and model/model_id), not fabricated Codex capability metadata. Missing exact
    installed descriptors fail closed; the backend still owns spending policy.
    """
    token = env.get('OPENVEGAS_NATIVE_TOKEN', '')
    if not token.startswith('ov_native_'):
        raise IntegrationError('Missing OpenVegas spending authorization; no Codex route opened.')
    identifiers = _reviewed_ids(allowed_models, model) if allowed_models is not None else None
    if (reviewed_catalog is not None and identifiers is None
            or catalog_binding is not None and reviewed_catalog is None):
        raise IntegrationError('Reviewed Codex metadata requires a frozen model scope.')
    with tempfile.TemporaryDirectory(prefix='ov-codex-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        socket = str(Path(directory) / 'native.sock')
        process = None
        process_group = None
        try:
            catalog = None
            catalog_path = None
            if identifiers is not None:
                if reviewed_catalog is not None:
                    catalog = _validate_grant_catalog(reviewed_catalog, identifiers=identifiers,
                                                      **(catalog_binding or {}))
                else:
                    catalog = _filter_catalog(await _bundled_catalog(executable, directory, env), identifiers)
                catalog_path = Path(directory) / 'models.json'
                with catalog_path.open('x', encoding='utf-8') as output:
                    os.chmod(catalog_path, 0o600)
                    json.dump(catalog, output)
            launch = LocalServerLaunch(executable, 'unix://' + socket, backend, model,
                                       local_tools_only, str(catalog_path) if catalog_path else None)
            process, cancelled = await _settle(asyncio.create_task(_spawn_owned(
                *(_handoff_server_argv(launch.argv()) if handoff_source is not None else launch.argv()),
                **({'cwd': handoff_source.cwd} if handoff_source is not None else {}),
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
                        listed = await _list_catalog(connection, maximum=len(identifiers))
                        _verify_catalog(listed, catalog)
                        if reviewed_catalog is not None:
                            _validate_grant_catalog(reviewed_catalog, identifiers=identifiers,
                                                    **(catalog_binding or {}))
                    await rpc(connection, 2, 'account/login/start', {'type': 'apiKey', 'apiKey': token})
                    account = await rpc(connection, 3, 'account/read', {'refreshToken': False})
                    if account.get('account', {}).get('type') != 'apiKey':
                        raise IntegrationError('Codex did not accept the private billing grant. No prompt submitted.')
            _OWNED_SERVERS['unix://' + socket] = process
            _POLICY_CONTEXT['unix://' + socket] = _launch_scope(env, handoff_source.cwd if handoff_source else os.getcwd())
            if handoff_source is not None:
                from .codex_handoff_policy import verify_target
                await verify_target('unix://' + socket, handoff_source, model=model)
            yield 'unix://' + socket
        except (IntegrationError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - Redact credential-bearing native exceptions.
            raise IntegrationError('Codex billing connection failed. No automatic prompt or retry was sent.') from None
        finally:
            if process is not None:
                await _release_owned(process, process_group, 'unix://' + socket)
