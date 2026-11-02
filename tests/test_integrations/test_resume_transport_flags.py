"""Transport/catalog contracts; installed CLI probes never submit a model turn."""

import asyncio
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from openvegas.integrations import codex_billing_route as route_module
from openvegas.integrations import native
from openvegas.integrations.credentials import IntegrationError
from openvegas.integrations.native import codex_resume_argv

ENDPOINT = 'unix:///fixture/private.sock'
MODEL = 'gpt-5.6-luna'
BASE = 'http://127.0.0.1:1'
TOKEN = 'ov_native_transport_offline_fixture'
REVIEWED = [{'provider': 'openai', 'host': 'codex', 'model': MODEL}]
# Synthetic metadata tests copying/validation, not actual model capabilities.
NATIVE_MODEL = {
    'slug': MODEL, 'display_name': 'Fixture model', 'description': 'Offline fixture',
    'visibility': 'list', 'supported_in_api': True, 'default_reasoning_level': 'medium',
    'supported_reasoning_levels': [
        {'effort': 'low', 'description': 'Fixture low'},
        {'effort': 'medium', 'description': 'Fixture medium'},
    ],
    'input_modalities': ['text'], 'context_window': 10000,
    'base_instructions': 'Fixture instructions must be preserved verbatim.',
    'upgrade': {'model': 'unreviewed-model', 'migration_markdown': 'Not reviewed'},
}


@pytest.mark.parametrize('host_args', [
    ['--no-daemon', 'resume', 'fixture'],
    ['resume', 'fixture', '--no-daemon'],
    ['--no-daemon', 'resume', '--no-daemon', 'fixture'],
    ['--no-daemon', '--no-alt-screen', 'resume', '--last'],
])
def test_remote_tui_drops_only_local_no_daemon_without_mutating_restore_args(host_args):
    original = list(host_args)
    argv = codex_resume_argv('codex', ENDPOINT, MODEL, host_args)
    assert '--no-daemon' not in argv
    assert argv[argv.index('--remote') + 1] == ENDPOINT
    assert argv[argv.index('--model') + 1] == MODEL
    assert argv.count('--remote') == argv.count('--model') == 1
    assert host_args == original
    # This same list remains available for the ordinary host restoration path.
    assert ['codex', *host_args] == ['codex', *original]
    if '--no-alt-screen' in original:
        assert '--no-alt-screen' in argv


@pytest.mark.parametrize('permissions', [
    ['--sandbox', 'read-only', '--ask-for-approval', 'on-request'],
    ['-s', 'workspace-write', '-a', 'never', '--add-dir', '/fixture/extra'],
    ['--sandbox=read-only', '--ask-for-approval=on-request', '--cd=/fixture/project'],
    ['--approve-for-me'],
    ['--dangerously-bypass-approvals-and-sandbox'],
    ['--add-dir=/fixture/extra'], ['--full-auto'], ['--yolo'],
    ['--permissions=restricted'], ['--dangerously-bypass-hook-trust'],
])
def test_remote_tui_rejects_unverified_permissions_without_mutating_original(permissions):
    host_args = ['resume', 'fixture', *permissions, '--no-daemon']
    before = list(host_args)
    with pytest.raises(IntegrationError, match='permission overrides'):
        codex_resume_argv('codex', ENDPOINT, MODEL, host_args)
    assert host_args == before


@pytest.mark.parametrize('permissions', [
    ['--sandbox', 'read-only'], ['-s', 'workspace-write', '-a', 'never'],
    ['--ask-for-approval=on-request', '--add-dir=/fixture/extra'],
    ['--approve-for-me'], ['--dangerously-bypass-approvals-and-sandbox'],
])
def test_ordinary_host_argv_preserves_permissions(permissions):
    original = ['resume', 'fixture', *permissions, '--no-daemon']
    before = list(original)
    argv = native.host_argv('codex', 'codex', BASE, MODEL, original,
                            provider_identity='original', local_tools_only=True)
    for argument in permissions:
        assert argument in argv
    assert '--no-daemon' in argv
    assert original == before


@pytest.mark.parametrize('permission', ['--sandbox', '--ask-for-approval', '--add-dir', '--yolo'])
def test_permission_like_literal_input_is_not_stripped_or_reinterpreted(permission):
    original = ['resume', 'fixture', '--', permission]
    argv = codex_resume_argv('codex', ENDPOINT, MODEL, original)
    assert argv[argv.index('--') + 1:] == [permission]
    assert original == ['resume', 'fixture', '--', permission]


def test_permission_like_option_values_are_not_reinterpreted():
    original = ['--cd', '/fixture', '--image=--sandbox', 'resume', 'fixture']
    before = list(original)
    native.validate_codex_wallet_handoff(original, 'openai')
    assert original == before


@pytest.mark.parametrize('provider', [None, 'original', 'openvegas_v', 'ollama', 'lmstudio'])
def test_custom_provider_wallet_handoff_is_rejected_without_changing_arguments(provider):
    original = ['--no-daemon', 'resume', 'fixture']
    before = list(original)
    with pytest.raises(IntegrationError, match='original built-in openai'):
        native.validate_codex_wallet_handoff(original, provider)
    assert original == before


@pytest.mark.parametrize('wallet_mode,provider,resume_provider,args', [
    (False, 'openai', 'openai', ['resume', 'fixture', '--sandbox', 'read-only']),
    (True, 'openai', 'openai', ['resume', 'fixture', '--approve-for-me']),
    (True, 'openai', 'original', ['resume', 'fixture']),
    (True, 'openai', None, ['resume', 'fixture']),
    (True, 'openrouter', 'openai', ['resume', 'fixture']),
])
def test_unsupported_handoff_fails_before_detection_credentials_or_quote(
    monkeypatch, wallet_mode, provider, resume_provider, args,
):
    def forbidden(*args, **kwargs):
        pytest.fail('Unsupported handoff reached executable detection or billing setup')

    monkeypatch.setattr(native, 'detect', forbidden)
    monkeypatch.setattr(native, 'Broker', forbidden)
    original = list(args)
    with pytest.raises(IntegrationError):
        asyncio.run(native.launch('codex', MODEL, '10', provider=provider,
                                  resume_provider=resume_provider, host_args=args,
                                  wallet_mode=wallet_mode, local_tools_only=True, yes=True))
    assert args == original


@pytest.mark.parametrize('literal', ['--no-daemon', '--remote', '--model', 'keep --no-daemon'])
def test_remote_controls_precede_literal_separator_without_rewriting_prompt(literal):
    host_args = ['--no-daemon', 'resume', 'fixture', '--', literal]
    before = list(host_args)
    argv = codex_resume_argv('codex', ENDPOINT, MODEL, host_args)
    separator = argv.index('--')
    assert argv[separator + 1:] == [literal]
    assert '--no-daemon' not in argv[:separator]
    assert argv.index('--remote') < separator
    assert argv.index('--model') < separator
    assert host_args == before


def test_transport_like_option_values_and_prompt_text_are_preserved():
    values = ['--cd=/fixture/--no-daemon', '--image=--no-daemon', 'explain --no-daemon']
    argv = codex_resume_argv('codex', ENDPOINT, MODEL, ['resume', 'fixture', *values])
    for value in values:
        assert value in argv


@pytest.mark.parametrize('override', [
    ['--remote', 'unix:///other.sock'], ['--remote=unix:///other.sock'],
    ['--remote-auth-token-env', 'OTHER_TOKEN'], ['--model', 'unreviewed'],
    ['-c', 'model_provider="other"'], ['--no-daemon=false'],
])
def test_resume_rejects_ambiguous_or_route_overriding_flags(override):
    with pytest.raises(IntegrationError):
        codex_resume_argv('codex', ENDPOINT, MODEL, ['resume', 'fixture', *override])


def _listed(catalog):
    return {'data': [{
        'id': row['slug'], 'model': row['slug'], 'hidden': False,
        'defaultReasoningEffort': row['default_reasoning_level'],
        'supportedReasoningEfforts': [
            {'reasoningEffort': level['effort'], 'description': level['description']}
            for level in row['supported_reasoning_levels']
        ],
        'inputModalities': row['input_modalities'], 'upgrade': None,
    } for row in catalog['models']], 'nextCursor': None}


def test_filter_uses_exact_installed_metadata_without_fabricating_capabilities():
    bundled = {'models': [deepcopy(NATIVE_MODEL), {**NATIVE_MODEL, 'slug': 'unreviewed-model'}]}
    before = deepcopy(bundled)
    actual = route_module._filter_catalog(bundled, [MODEL])
    assert actual == {'models': [{**NATIVE_MODEL, 'upgrade': None}]}
    assert bundled == before
    assert route_module._reviewed_ids(REVIEWED, MODEL) == [MODEL]
    assert route_module._reviewed_ids([MODEL], MODEL) == [MODEL]
    assert route_module._reviewed_ids([{'provider': 'openai', 'model_id': MODEL}], MODEL) == [MODEL]
    route_module._verify_catalog(_listed(actual), actual)


@pytest.mark.parametrize('rows', [
    [], None, [None], [{'provider': 'anthropic', 'model': MODEL}],
    [{'provider': 'openai', 'model': 'openai/' + MODEL}],
    [{'provider': 'openai', 'host': 'claude', 'model': MODEL}],
    [{'provider': 'openai', 'model': MODEL, 'model_id': 'different'}],
    REVIEWED + REVIEWED,
])
def test_reviewed_descriptors_fail_closed(rows):
    with pytest.raises(IntegrationError):
        route_module._reviewed_ids(rows, MODEL)


@pytest.mark.parametrize('count', [100, 101, 128, 129])
def test_reviewed_catalog_limit_matches_wallet_contract(count):
    rows = [MODEL, *(f'fixture-reviewed-{index}' for index in range(1, count))]
    if count <= 128:
        assert route_module._reviewed_ids(rows, MODEL) == rows
        bundled = {'models': [{**NATIVE_MODEL, 'slug': identifier} for identifier in rows]}
        catalog = route_module._filter_catalog(bundled, rows)
        assert len(catalog['models']) == count
        route_module._verify_catalog(_listed(catalog), catalog)
    else:
        with pytest.raises(IntegrationError):
            route_module._reviewed_ids(rows, MODEL)


@pytest.mark.parametrize('bundled', [
    None, {'models': []}, {'models': [None]}, {'models': [NATIVE_MODEL, NATIVE_MODEL]},
    {'models': [{**NATIVE_MODEL, 'slug': 'openai/' + MODEL}]},
    {'models': [{**NATIVE_MODEL, 'supported_reasoning_levels': []}]},
    {'models': [{**NATIVE_MODEL, 'default_reasoning_level': 'invented'}]},
    {'models': [{**NATIVE_MODEL, 'supported_in_api': False}]},
    {'models': [{**NATIVE_MODEL, 'visibility': 'hidden'}]},
])
def test_missing_or_ambiguous_installed_metadata_fails_closed(bundled):
    with pytest.raises(IntegrationError):
        route_module._filter_catalog(bundled, [MODEL])


@pytest.mark.parametrize('field,value', [
    ('model', 'unreviewed'), ('id', 'unreviewed'), ('hidden', True),
    ('defaultReasoningEffort', 'invented'), ('supportedReasoningEfforts', []),
    ('inputModalities', ['text', 'image']), ('upgrade', 'unreviewed'),
])
def test_effective_catalog_must_match_reviewed_metadata(field, value):
    catalog = route_module._filter_catalog({'models': [NATIVE_MODEL]}, [MODEL])
    response = _listed(catalog)
    response['data'][0][field] = value
    with pytest.raises(IntegrationError):
        route_module._verify_catalog(response, catalog)


def test_effective_catalog_rejects_unverified_pagination_and_extra_models():
    catalog = route_module._filter_catalog({'models': [NATIVE_MODEL]}, [MODEL])
    response = _listed(catalog)
    with pytest.raises(IntegrationError):
        route_module._verify_catalog({**response, 'nextCursor': 'more'}, catalog)
    with pytest.raises(IntegrationError):
        route_module._verify_catalog({'data': response['data'] * 2}, catalog)


@pytest.fixture
def route_rig(monkeypatch):
    rig = SimpleNamespace(calls=[], spawns=[], paths=[], config_override={}, listing_override=None)
    process = SimpleNamespace(returncode=None, terminated=0)

    def terminate():
        process.returncode = 0
        process.terminated += 1

    async def wait():
        return process.returncode

    process.terminate = process.kill = terminate
    process.wait = wait
    rig.process = process

    async def bundled(*args):
        return {'models': [deepcopy(NATIVE_MODEL)]}

    async def spawn(*args, **kwargs):
        rig.spawns.append((args, kwargs))
        Path(args[-1].removeprefix('unix://')).touch()
        config = {value.partition('=')[0]: json.loads(value.partition('=')[2])
                  for index, value in enumerate(args) if index and args[index - 1] == '-c'}
        path = Path(config['model_catalog_json'])
        rig.paths.append(path)
        rig.catalog = json.loads(path.read_text())
        rig.config = {**config, **rig.config_override}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        return process

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            self.value = json.loads(message)
            rig.calls.append(self.value)

        async def recv(self):
            method = self.value['method']
            result = {'config': rig.config} if method == 'config/read' else (
                (rig.listing_override or _listed(rig.catalog)) if method == 'model/list' else (
                    {'account': {'type': 'apiKey'}} if method == 'account/read' else {}))
            return json.dumps({'id': self.value['id'], 'result': result})

    monkeypatch.setattr(route_module, '_bundled_catalog', bundled)
    monkeypatch.setattr(route_module.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(route_module, 'unix_connect', lambda *a, **k: Connection())
    return rig


def test_private_catalog_and_server_transport_are_verified_before_auth_and_cleaned(route_rig):
    env = {'OPENVEGAS_NATIVE_TOKEN': TOKEN, 'HOME': '/fixture/original-home'}
    before = dict(env)

    async def check():
        async with route_module.route('/fixture/codex', BASE, MODEL, env, allowed_models=REVIEWED):
            assert route_rig.paths[0].exists()

    asyncio.run(check())
    argv, kwargs = route_rig.spawns[0]
    assert argv.index('--no-daemon') < argv.index('app-server')
    assert '--remote' not in argv and TOKEN not in str(argv)
    assert kwargs['start_new_session'] is True
    assert kwargs['env'] == before and env == before
    methods = [call['method'] for call in route_rig.calls]
    assert methods.index('config/read') < methods.index('model/list') < methods.index('account/login/start')
    assert all(not path.parent.exists() for path in route_rig.paths)
    assert route_rig.process.terminated == 1


@pytest.mark.parametrize('mismatch', ['config', 'listing'])
def test_unverified_catalog_never_receives_grant(route_rig, mismatch):
    if mismatch == 'config':
        route_rig.config_override['model_catalog_json'] = '/fixture/unowned.json'
    else:
        route_rig.listing_override = {'data': [], 'nextCursor': None}

    async def check():
        async with route_module.route('/fixture/codex', BASE, MODEL,
                                      {'OPENVEGAS_NATIVE_TOKEN': TOKEN}, allowed_models=REVIEWED):
            pytest.fail('unverified catalog accepted')

    with pytest.raises(IntegrationError):
        asyncio.run(check())
    assert not any(call['method'] == 'account/login/start' for call in route_rig.calls)
    assert all(not path.parent.exists() for path in route_rig.paths)
    assert route_rig.process.terminated == 1


def test_bundled_discovery_is_isolated_and_does_not_receive_auth(monkeypatch, tmp_path):
    calls = []
    process = SimpleNamespace(returncode=0)

    async def communicate():
        return json.dumps({'models': [NATIVE_MODEL]}).encode(), None

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    process.communicate = communicate
    monkeypatch.setattr(route_module.asyncio, 'create_subprocess_exec', spawn)
    original = {'PATH': '/fixture/bin', 'HOME': '/original', 'CODEX_HOME': '/original/codex',
                'OPENVEGAS_NATIVE_TOKEN': TOKEN, 'CODEX_ACCESS_TOKEN': 'fixture-not-real',
                'CODEX_SQLITE_HOME': '/original/state'}
    result = asyncio.run(route_module._bundled_catalog('/fixture/codex', str(tmp_path), original))
    assert result == {'models': [NATIVE_MODEL]}
    argv, kwargs = calls[0]
    assert argv == ('/fixture/codex', 'debug', 'models', '--bundled')
    assert set(kwargs['env']) == {'PATH', 'HOME', 'CODEX_HOME', 'TMPDIR'}
    assert kwargs['env']['HOME'] == str(tmp_path / 'catalog-home')
    assert kwargs['env']['CODEX_HOME'] == kwargs['env']['HOME']
    assert kwargs['cwd'] == tmp_path / 'catalog-home'
    assert kwargs['start_new_session'] is True


@pytest.fixture
def installed_codex(tmp_path):
    if os.getenv('OV_TEST_CODEX_TRANSPORT') != '1':
        pytest.skip('Set OV_TEST_CODEX_TRANSPORT=1 for isolated, network-denied CLI probes')
    executable = shutil.which('codex')
    if sys.platform != 'darwin' or not executable or not Path('/usr/bin/sandbox-exec').exists():
        pytest.skip('Requires installed Codex and macOS network-denial sandbox')
    home = tmp_path / 'home'
    home.mkdir()
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'CODEX_HOME': str(home),
           'TMPDIR': str(tmp_path), 'LANG': 'en_US.UTF-8', 'TERM': 'xterm-256color'}
    policy = ('(version 1)(allow default)(deny network*)'
              '(allow network-inbound (local unix-socket))'
              '(allow network-outbound (remote unix-socket))')
    wrapper = tmp_path / 'isolated-codex'
    wrapper.write_text('#!/bin/sh\nexec /usr/bin/sandbox-exec -p ' + shlex.quote(policy)
                       + ' ' + shlex.quote(executable) + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper, env


def test_installed_cli_confirms_conflict_and_accepts_sanitized_remote_argv(installed_codex, tmp_path):
    executable, env = installed_codex

    def run(args):
        return subprocess.run([str(executable), *args], env=env, cwd=tmp_path,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=15, check=False)

    version = run(['--version'])
    assert version.returncode == 0, version.stderr
    assert '0.166.0' in version.stdout
    endpoint = 'unix://' + str(tmp_path / 'absent.sock')
    conflict = run(['resume', 'fixture', '--no-daemon', '--remote', endpoint])
    assert conflict.returncode != 0
    assert '--no-daemon cannot be used with --remote' in conflict.stderr
    argv = codex_resume_argv(str(executable), endpoint, MODEL,
                             ['resume', 'fixture', '--no-daemon'])
    sanitized = run(argv[1:])
    assert sanitized.returncode != 0  # Absent socket, not a model/turn invocation.
    assert '--no-daemon cannot be used with --remote' not in sanitized.stderr
    assert 'unexpected argument' not in sanitized.stderr


@pytest.mark.parametrize('allowed_ids', [[MODEL], [MODEL, 'gpt-6.1-sol']])
def test_installed_route_exposes_only_reviewed_native_model_and_real_efforts(
    installed_codex, tmp_path, allowed_ids,
):
    executable, env = installed_codex
    env['OPENVEGAS_NATIVE_TOKEN'] = TOKEN
    home = Path(env['CODEX_HOME'])
    config = home / 'config.toml'
    config.write_text('cli_auth_credentials_store="ephemeral"\n')
    original_config = config.read_bytes()

    async def check():
        async with (
            route_module.route(str(executable), BASE, MODEL, env, allowed_models=allowed_ids) as endpoint,
            route_module.unix_connect(endpoint.removeprefix('unix://')) as connection,
        ):
            await route_module.rpc(connection, 1, 'initialize', {
                'clientInfo': {'name': 'ov-offline-model-list', 'version': '1'}})
            await connection.send(json.dumps({'method': 'initialized', 'params': {}}))
            result = await route_module.rpc(connection, 2, 'model/list', {'includeHidden': True})
            assert {row['model'] for row in result['data']} == set(allowed_ids)
            assert result['data'][0]['upgrade'] is None
            assert result['data'][0]['supportedReasoningEfforts']
            return Path(endpoint.removeprefix('unix://')).parent

    directory = asyncio.run(check())
    assert not directory.exists()
    assert config.read_bytes() == original_config
    assert not (home / 'auth.json').exists()
