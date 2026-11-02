"""Clean local source bundles, without installation, host trust, or provider access."""
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from openvegas.integrations import installer, use

LAYOUTS = [('codex', False), ('claude', False), ('claude', True)]


@pytest.fixture(params=LAYOUTS, ids=['codex', 'claude-portable', 'claude-mod'])
def clean_bundle(request, monkeypatch, tmp_path):
    host, mod = request.param
    node = shutil.which('node')
    if not node:
        pytest.skip('Real Node is required to validate generated hook bytes')
    home = tmp_path / 'isolated home'
    home.mkdir(mode=0o700)
    env = {
        'HOME': str(home), 'USERPROFILE': str(home),
        'CODEX_HOME': str(home / '.codex'),
        'CLAUDE_CONFIG_DIR': str(home / '.claude'),
        'OPENVEGAS_BACKEND_URL': 'https://bundle-fixture.invalid',
        'OPENVEGAS_INTEGRATION_PROFILE': str(home / '.openvegas'),
        'PATH': str(Path(node).parent),
    }
    # The generator sees no personal HOME, host policy, provider keys, or tokens.
    monkeypatch.setattr(os, 'environ', env)

    def generate(name):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        files = installer.bundle_files({'host': host, 'mod': mod}, 'clean-bundle-fixture')
        for relative, content in files.items():
            path = Path(relative)
            assert not path.is_absolute() and '..' not in path.parts
            installer._write(root / path, content)
        return root, files

    root, files = generate('plugin bundle with spaces')
    yield {'root': root, 'files': files, 'host': host, 'mod': mod, 'env': env,
           'node': node, 'generate': generate, 'home': home}
    # No registration, settings, receipts, credentials, or trust files were created.
    assert list(home.rglob('*')) == []


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def hook_spec(bundle):
    return read_json(bundle['root'] / 'plugins/openvegas/hooks/hooks.json')


def invoke(argv, data, bundle):
    result = subprocess.run(argv, input=json.dumps(data), text=True,
                            capture_output=True, timeout=15, check=False,
                            cwd=bundle['root'], env=bundle['env'])
    assert result.returncode == 0 and not result.stderr
    return json.loads(result.stdout)


def test_clean_files_reproduce_exact_generator_and_private_modes(clean_bundle):
    bundle = clean_bundle
    root, expected = bundle['root'], bundle['files']
    actual = {path.relative_to(root).as_posix(): path.read_text(encoding='utf-8')
              for path in root.rglob('*') if path.is_file()}
    assert actual == expected
    assert installer.bundle_files({'host': bundle['host'], 'mod': bundle['mod']},
                                  'clean-bundle-fixture') == expected
    if os.name == 'posix':
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.rglob('*'):
        assert not path.is_symlink()
        if os.name == 'posix':
            mode = stat.S_IMODE(path.stat().st_mode)
            # mkdir(parents=True) leaves intermediate modes to umask. The private
            # ancestor is the access boundary; every generated file is owner-only.
            assert mode & 0o022 == 0 if path.is_dir() else mode == 0o600
            assert path.stat().st_uid == os.getuid()
        if path.suffix == '.json':
            assert isinstance(read_json(path), dict)
    print(f"clean-bundle {bundle['host']} mod={bundle['mod']} files={len(actual)}")


def test_disk_hook_embeds_exact_current_parser_for_both_events(clean_bundle):
    hooks = hook_spec(clean_bundle)
    source = (installer.ASSETS / 'billing-hook.js').read_text(encoding='utf-8')
    expected = source.replace('__OPENVEGAS_MISSING__', json.dumps(use.MISSING))
    assert set(hooks['hooks']) == {'SessionStart', 'UserPromptSubmit'}
    for event in hooks['hooks'].values():
        assert len(event) == 1 and set(event[0]) == {'hooks'}
        assert len(event[0]['hooks']) == 1
        entry = event[0]['hooks'][0]
        assert entry['type'] == 'command' and entry['timeout'] == 5
        argv = shlex.split(entry['command'])
        assert argv == [clean_bundle['node'], '-e', expected, clean_bundle['host'], '--billing-hook']
        assert '__OPENVEGAS_MISSING__' not in argv[2]
    assert hooks['hooks']['SessionStart'] == hooks['hooks']['UserPromptSubmit']
    print('embedded-hook-sha256=' + hashlib.sha256(expected.encode()).hexdigest())


def test_disk_skill_preserves_current_credit_consent_and_lowercase_shortcut_copy(clean_bundle):
    bundle = clean_bundle
    skills = list((bundle['root'] / 'plugins/openvegas/skills').glob('*/SKILL.md'))
    assert len(skills) == 1
    expected = (installer.ASSETS / 'SKILL.md').read_text(encoding='utf-8').replace(
        '__OPENVEGAS_CONTROLS_TABLE__', installer.skill_controls())
    name = 'openvegas-help' if bundle['mod'] else 'openvegas'
    if bundle['mod']:
        expected = expected.replace('name: openvegas\n', 'name: openvegas-help\n')
    assert skills[0].parent.name == name
    skill = skills[0].read_text(encoding='utf-8')
    assert skill == expected and '__OPENVEGAS_CONTROLS_TABLE__' not in skill
    for phrase in (
        'Managed-terminal exact `openvegas`, `openvegas play [game]`',
        'the user has trusted the installed hook',
        'A bare exact command opens the lobby, not a paid prompt.',
        'This does not make arbitrary requests, agent decisions or MCP tool selection free.',
        'fallback DOES require available host inference',
        'Do not claim it works at zero quota.',
        'Hold Control and press ], release', 'lowercase o (no Shift)',
        'Only use the menu shortcut when the HUD shows it',
        'Local menu entry never places a wager or switches billing automatically.',
        'signing in and paid actions keep their separate requirements',
        'Do not auto-trust hooks',
    ):
        assert phrase in skill
    assert 'Hold Control and press X' not in skill


def test_manifest_references_are_local_complete_and_do_not_grant_play_approval(clean_bundle):
    bundle = clean_bundle
    root = bundle['root']
    plugin = root / 'plugins/openvegas'
    host = bundle['host']
    market = read_json(root / ('.agents/plugins/marketplace.json' if host == 'codex'
                               else '.claude-plugin/marketplace.json'))
    assert market['name'] == 'clean-bundle-fixture'
    source = market['plugins'][0]['source']
    assert source == ({'source': 'local', 'path': './plugins/openvegas'}
                      if host == 'codex' else './plugins/openvegas')
    manifest = read_json(plugin / ('.codex-plugin/plugin.json' if host == 'codex'
                                  else '.claude-plugin/plugin.json'))
    assert manifest['name'] == 'openvegas'
    if host == 'codex':
        assert manifest['hooks'] == './hooks/hooks.json'
        assert manifest['skills'] == './skills/'
        assert manifest['mcpServers'] == './.mcp.json'
        assert not (plugin / 'plugin.json').exists()
        for key in ('hooks', 'skills', 'mcpServers'):
            assert (plugin / manifest[key]).resolve().is_relative_to(plugin.resolve())
            assert (plugin / manifest[key]).exists()
    server = read_json(plugin / '.mcp.json')['mcpServers']['openvegas']
    assert [server['command'], *server['args']] == installer.helper(host)
    assert 'tools' not in server and 'env' not in server
    assert server['args'][server['args'].index('--backend') + 1] == 'https://bundle-fixture.invalid'
    assert server['args'][server['args'].index('--profile') + 1] == bundle['env']['OPENVEGAS_INTEGRATION_PROFILE']
    hooks = hook_spec(bundle)
    assert hooks.get('modules', []) == (['./register.js'] if bundle['mod'] else [])
    for module in hooks.get('modules', []):
        assert (plugin / 'hooks' / module).is_file()


def test_generated_node_command_executes_from_clean_directory_without_model_fallthrough(clean_bundle):
    spec = hook_spec(clean_bundle)['hooks']['UserPromptSubmit'][0]['hooks'][0]
    argv = shlex.split(spec['command'])
    controls = ['OpenVegas', '$openvegas:openvegas\t play\tROULETTE',
                '/openvegas  use v', 'openvegas:openvegas use host']
    for prompt in controls:
        result = invoke(argv, {'hook_event_name': 'UserPromptSubmit', 'prompt': prompt}, clean_bundle)
        assert result == {'decision': 'block', 'reason': use.MISSING}
    for event in (
        {'hook_event_name': 'UserPromptSubmit', 'prompt': 'Explain OpenVegas'},
        {'hook_event_name': 'UserPromptSubmit', 'prompt': 'openvegas use v\nthen work'},
        {'hook_event_name': 'UserPromptSubmit', 'prompt': 'openvegas play --stake 50'},
        {'hook_event_name': 'UserPromptSubmit', 'prompt': 'openvegas play', 'agent_id': 'worker'},
        {'hook_event_name': 'SessionStart'},
    ):
        assert invoke(argv, event, clean_bundle) == {}


def test_no_node_source_fallback_is_pinned_and_runs_without_installation(clean_bundle, monkeypatch):
    bundle = clean_bundle
    monkeypatch.setitem(bundle['env'], 'PATH', '')
    assert shutil.which('node') is None
    root, files = bundle['generate']('python fallback bundle with spaces')
    hooks = read_json(root / 'plugins/openvegas/hooks/hooks.json')
    assert hooks['hooks']['SessionStart'] == hooks['hooks']['UserPromptSubmit']
    spec = hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]
    argv = shlex.split(spec['command'])
    assert spec['timeout'] == 15
    assert argv == installer.helper(bundle['host']) + ['--billing-hook']
    assert '-I' in argv and Path(argv[0]).is_absolute()
    fallback = {**bundle, 'root': root, 'files': files}
    assert invoke(argv, {'hook_event_name': 'UserPromptSubmit',
                         'prompt': 'openvegas'}, fallback) == {'decision': 'block', 'reason': use.MISSING}
    assert invoke(argv, {'hook_event_name': 'UserPromptSubmit',
                         'prompt': 'ordinary task'}, fallback) == {}
