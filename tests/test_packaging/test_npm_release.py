"""Offline assembly/launcher contracts, not cross-platform native acceptance."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.assemble_npm_release import assemble, inspect_candidates, TARGETS


@pytest.fixture
def candidates(tmp_path):
    root = tmp_path.resolve()
    result = []
    for target in sorted(TARGETS):
        platform, arch = target.split('-')
        folder = root / target
        (folder / 'bin').mkdir(parents=True)
        binary = folder / 'bin' / ('openvegas-' + target + ('.exe' if platform == 'win32' else ''))
        binary.write_bytes(b'#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n')
        binary.chmod(0o755)
        (folder / 'candidate.json').write_text(json.dumps({
            'version': '0.3.7', 'os': platform, 'arch': arch, 'binary': binary.name,
            'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'source_sha256': '1' * 64, 'npm_install_verified': True,
            'terminal_profile_command_verified': True,
            'includes_private_review_packs': False,
        }))
        result.append(folder)
    return result


def test_complete_matrix_pins_all_optional_dependencies_without_scripts(candidates, tmp_path):
    package = assemble(candidates, tmp_path.resolve() / 'release')
    manifest = json.loads((package / 'package.json').read_text())
    assert manifest['name'] == 'openvegas-ai'
    assert manifest['bin'] == {k: 'bin/openvegas.js' for k in ('openvegas-ai', 'openvegas', 'ov')}
    assert len(manifest['optionalDependencies']) == 5
    assert set(manifest['optionalDependencies'].values()) == {'0.3.7'}
    assert 'scripts' not in manifest
    assert not list(package.rglob('*.py'))
    for target in TARGETS:
        native = json.loads((package.parent / target / 'package.json').read_text())
        assert native['name'] == '@lordesteph/openvegas-' + target
        assert native['os'] == [target.split('-')[0]]
        assert native['cpu'] == [target.split('-')[1]]
        assert 'scripts' not in native


def test_missing_and_duplicate_platforms_cannot_assemble(candidates):
    with pytest.raises(ValueError, match='Missing verified'):
        inspect_candidates(candidates[:-1])
    with pytest.raises(ValueError, match='duplicate'):
        inspect_candidates(candidates + [candidates[0]])


def test_ci_builds_public_source_and_assembles_every_supported_target():
    import yaml
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / '.github/workflows/emote-frozen.yml').read_text())
    job = workflow['jobs']['frozen']
    matrix = job['strategy']['matrix']['include']
    assert {row['target'] for row in matrix} == TARGETS
    assert len({row['runner'] for row in matrix}) == len(TARGETS)
    steps = job['steps']
    export = next(s for s in steps if 'prepare_public_release.py' in s.get('run', ''))
    build = next(s for s in steps if 'build_cli_candidate.py' in s.get('run', ''))
    assert steps.index(export) < steps.index(build)
    assert build['working-directory'] == 'dist/public-source'
    assert '--include-review-packs' not in build['run']
    upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact')
                  and s.get('with', {}).get('name', '').startswith('unsigned-native-candidate-'))
    assert 'dist/native-candidate/bin/*' in upload['with']['path']
    assembly = workflow['jobs']['assemble']
    assert assembly['needs'] == 'frozen'
    command = next(s['run'] for s in assembly['steps'] if 'assemble_npm_release.py' in s.get('run', ''))
    for target in TARGETS:
        assert '--candidate dist/candidates/unsigned-native-candidate-' + target in command
    text = (root / '.github/workflows/emote-frozen.yml').read_text()
    assert 'npm publish' not in text and 'NPM_TOKEN' not in text


@pytest.mark.parametrize('change', [
    {'version': '1.0.0'}, {'version': '../bad'}, {'source_sha256': '2' * 64},
    {'source_sha256': 'bad'}, {'includes_private_review_packs': True},
    {'includes_private_review_packs': None}, {'npm_install_verified': False},
    {'terminal_profile_command_verified': False}, {'binary': '../bad'},
    {'sha256': '0' * 64}, {'arch': 'ppc'},
])
def test_invalid_candidate_never_writes_output(candidates, tmp_path, change):
    file = candidates[0] / 'candidate.json'
    data = json.loads(file.read_text())
    file.write_text(json.dumps({**data, **change}))
    output = tmp_path.resolve() / 'release'
    with pytest.raises(ValueError):
        assemble(candidates, output)
    assert not output.exists()


def test_symlinks_and_existing_outputs_refused(candidates, tmp_path):
    with pytest.raises(ValueError):
        assemble(candidates, tmp_path)
    file = candidates[0] / 'candidate.json'
    original = file.with_suffix('.original')
    file.rename(original)
    file.symlink_to(original)
    with pytest.raises(ValueError, match='symlinked'):
        inspect_candidates(candidates)


@pytest.fixture
def installed(candidates, tmp_path):
    node = shutil.which('node')
    if not node or os.name != 'posix':
        pytest.skip('POSIX fake runtime and Node required')
    target = subprocess.check_output([node, '-p', 'process.platform+"-"+process.arch'], text=True).strip()
    package = assemble(candidates, tmp_path.resolve() / 'release')
    native = package / 'node_modules' / '@lordesteph' / ('openvegas-' + target)
    shutil.copytree(package.parent / target, native)
    env = {'HOME': str(tmp_path), 'PATH': '/nonexistent', 'CI': '1'}
    def invoke(*args, extra_env=None):
        return subprocess.run([node, str(package / 'bin/openvegas.js'), *args], env={**env, **(extra_env or {})},
                              capture_output=True, text=True, timeout=10)
    return package, native, invoke


def environment_probe(package, native):
    binary = next((native / 'bin').iterdir())
    binary.write_bytes(b'''#!/bin/sh
printf '%s\\n' "$HOME" "$OPENVEGAS_BACKEND_URL" "$OPENVEGAS_INTEGRATION_PROFILE" "$OPENVEGAS_LOAD_PROJECT_ENV" "$OPENVEGAS_ENV_FILE" "$OPENVEGAS_NATIVE_TOKEN" "$OPENVEGAS_TERMINAL_SOCKET" "$@"
''')
    index = package / 'release.json'
    data = json.loads(index.read_text())
    for value in data['platforms'].values():
        if value['binary'] == binary.name:
            value['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    index.write_text(json.dumps(data))


def test_public_launcher_ignores_inherited_local_account(installed):
    package, native, invoke = installed
    environment_probe(package, native)
    result = invoke('go', extra_env={
        'HOME': '/dev-account', 'OPENVEGAS_HOST_HOME': '/real-home',
        'OPENVEGAS_BACKEND_URL': 'http://127.0.0.1:8000',
        'OPENVEGAS_INTEGRATION_PROFILE': '/dev-account/.openvegas',
        'OPENVEGAS_ENV_FILE': '/project/.env', 'OPENVEGAS_DOTENV_OVERRIDE': '1',
        'OPENVEGAS_NATIVE_TOKEN': 'fixture-local-token',
        'OPENVEGAS_TERMINAL_SOCKET': '/tmp/owned-terminal/game.sock',
    })
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['/real-home', 'https://app.openvegas.ai',
        '/real-home/.openvegas', '0', '', '', '/tmp/owned-terminal/game.sock', 'go']


def test_local_mode_is_explicit_and_keeps_separate_profile(installed):
    package, native, invoke = installed
    environment_probe(package, native)
    result = invoke('--local', 'go', extra_env={'HOME': '/dev-account',
        'OPENVEGAS_BACKEND_URL': 'http://127.0.0.1:8000',
        'OPENVEGAS_INTEGRATION_PROFILE': '/dev-account/.openvegas'})
    assert result.returncode == 0
    assert result.stdout.splitlines()[:4] == ['/dev-account', 'http://127.0.0.1:8000',
                                            '/dev-account/.openvegas', '0']
    assert result.stdout.splitlines()[-1] == 'go'
    rejected = invoke('--local', 'go', extra_env={'OPENVEGAS_BACKEND_URL': 'https://app.openvegas.ai'})
    assert rejected.returncode == 1 and not rejected.stdout


def test_installed_launcher_preserves_arguments_and_failure_without_fallback(installed):
    package, _native, invoke = installed
    result = invoke('play', 'argument with spaces', '$(not-a-shell-command)')
    assert result.returncode == 23
    assert result.stdout.splitlines() == ['play', 'argument with spaces', '$(not-a-shell-command)']
    source = (package / 'bin/openvegas.js').read_text()
    assert 'pipx' not in source and 'python' not in source


@pytest.mark.parametrize('damage', ['missing', 'version', 'checksum', 'platform'])
def test_launcher_fails_closed_before_execution(installed, damage):
    package, native, invoke = installed
    manifest = native / 'package.json'
    if damage == 'missing':
        manifest.unlink()
    elif damage == 'version':
        value = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**value, 'version': '0.0.1'}))
    elif damage == 'checksum':
        next((native / 'bin').iterdir()).write_bytes(b'changed')
    else:
        index = package / 'release.json'
        value = json.loads(index.read_text())
        index.write_text(json.dumps({**value, 'platforms': {}}))
    result = invoke('play')
    assert result.returncode == 1 and not result.stdout
    assert 'OpenVegas:' in result.stderr


@pytest.mark.skipif(os.name != 'posix', reason='POSIX PTY contract')
@pytest.mark.parametrize('finish', ['success', 'failure', 'cancel'])
def test_loading_preserves_tty_and_clears_before_prompt(installed, tmp_path, finish):
    import pty
    import select
    import sys
    import time
    package, native, _ = installed
    binary = next((native / 'bin').iterdir())
    binary.write_text(f'''#!{sys.executable}
import os, time
time.sleep(.3)
assert all(os.isatty(fd) for fd in (0,1,2))
os.write(3, b'ready\\n')
assert os.read(3,1) == b'1'
print('PROMPT', flush=True)
answer = input()
print('FINISHED', flush=True)
raise SystemExit(7 if answer == 'failure' else 0)
''')
    index = package / 'release.json'
    data = json.loads(index.read_text())
    for value in data['platforms'].values():
        if value['binary'] == binary.name:
            value['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    index.write_text(json.dumps(data))
    master, slave = pty.openpty()
    proc = subprocess.Popen([shutil.which('node'), str(package / 'bin/openvegas.js'), 'go'],
        stdin=slave, stdout=slave, stderr=slave,
        env={'HOME': str(tmp_path), 'PATH': '/nonexistent', 'TERM': 'xterm'})
    os.close(slave)
    output = b''
    sent = False
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if b'PROMPT' in output and not sent:
                sent = True
                if finish == 'cancel':
                    proc.terminate()
                else:
                    os.write(master, (finish + '\n').encode())
            if proc.poll() is not None:
                break
        code = proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    assert b'\x1b[34m' in output and b'Starting OpenVegas...' in output
    assert output.index(b'\x1b[2K') < output.index(b'PROMPT')
    assert b'Starting OpenVegas' not in output.split(b'PROMPT', 1)[1]
    assert code == {'success': 0, 'failure': 7, 'cancel': -15}[finish]
