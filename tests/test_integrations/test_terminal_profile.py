import json
import os
from pathlib import Path
import subprocess

import pytest
from click.testing import CliRunner

from openvegas.integrations import commands
from openvegas.integrations.terminal_profile import PROFILE, configure, profile_spec


def test_frozen_profile_uses_installed_binary_not_python_or_extracted_sources(monkeypatch, tmp_path):
    from openvegas.integrations import terminal_profile
    monkeypatch.setattr(terminal_profile.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(terminal_profile.sys, 'executable', '/installed path/openvegas')
    profile = profile_spec(tmp_path, account=('http://127.0.0.1:8000', tmp_path / 'account'))
    assert profile['args'][-5:] == ['/installed path/openvegas', '--_terminal-relay', '--', '/bin/zsh', '-l']
    assert '-I' not in profile['args'] and '-c' not in profile['args']
    assert profile['env']['OPENVEGAS_INTEGRATION_PROFILE'] == str(tmp_path / 'account')
    assert '--_terminal-relay' not in profile_spec(tmp_path, game_handoff=False)['args']


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / 'Library/Application Support/Code/User/settings.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'editor.fontSize': 17,
                               'terminal.integrated.defaultProfile.osx': 'zsh',
                               'terminal.integrated.profiles.osx': {'Other': {'path': '/bin/zsh'}}}))
    return path


def test_dry_run_no_mutations(settings, tmp_path):
    before = settings.read_bytes()
    assert configure(tmp_path, dict(os.environ))['applied'] is False
    assert settings.read_bytes() == before
    assert not (tmp_path / 'Library/Application Support/OpenVegas').exists()


def test_real_profile_preserves_settings_and_runs_normal_shell(settings, tmp_path):
    before = settings.read_bytes()
    configure(tmp_path, dict(os.environ), apply=True)
    after = settings.read_bytes()
    data = json.loads(after)
    assert data['editor.fontSize'] == 17
    assert data['terminal.integrated.defaultProfile.osx'] == 'zsh'
    assert data['terminal.integrated.profiles.osx']['Other'] == {'path': '/bin/zsh'}
    assert settings.with_name('settings.json.openvegas-graphics-backup').read_bytes() == before
    assert data['terminal.integrated.enableImages'] is True
    profile = data['terminal.integrated.profiles.osx'][PROFILE]
    # Emulate VS Code injecting its brand after profile env. No host is wrapped.
    env = {**os.environ, 'HOME': str(tmp_path), 'TERM_PROGRAM': 'vscode', 'TERM_PROGRAM_VERSION': '1.135'}
    run = subprocess.run([profile['path'], *profile['args'], '-c',
                          'test -z "$TERM_PROGRAM" && test "$TERM" = openvegas-sixel && infocmp "$TERM"'],
                         env=env, capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert b'colors#256' in run.stdout
    assert 'codex' not in profile['args'] and 'claude' not in profile['args']
    assert 'terminal_relay' in ' '.join(profile['args'])
    configure(tmp_path, dict(os.environ), apply=True)
    assert settings.read_bytes() == after
    assert settings.with_name('settings.json.openvegas-graphics-backup').read_bytes() == before


@pytest.mark.parametrize('original', ['// user comment\n{}', '[]', '{"terminal.integrated.profiles.osx":false}',
                                     '{"terminal.integrated.profiles.osx":{"OpenVegas Graphics":{"path":"mine"}}}'])
def test_invalid_or_custom_settings_preserved(settings, tmp_path, original):
    settings.write_text(original)
    with pytest.raises(ValueError):
        configure(tmp_path, dict(os.environ), apply=True)
    assert settings.read_text() == original
    assert not (tmp_path / 'Library/Application Support/OpenVegas').exists()


def test_symlinked_settings_untouched(settings, tmp_path):
    target = tmp_path / 'other.json'
    target.write_text('{}')
    settings.unlink()
    settings.symlink_to(target)
    with pytest.raises(ValueError, match='Symlinked'):
        configure(tmp_path, dict(os.environ), apply=True)
    assert target.read_text() == '{}'


def test_owned_pet_only_profile_upgrades_without_changing_default(settings, tmp_path):
    configure(tmp_path, dict(os.environ), apply=True)
    root = tmp_path / 'Library/Application Support/OpenVegas/native-terminal'
    data = json.loads(settings.read_text())
    data['terminal.integrated.profiles.osx'][PROFILE] = profile_spec(root, game_handoff=False)
    settings.write_text(json.dumps(data))
    configure(tmp_path, dict(os.environ), apply=True)
    result = json.loads(settings.read_text())
    assert result['terminal.integrated.defaultProfile.osx'] == 'zsh'
    assert 'terminal_relay' in ' '.join(result['terminal.integrated.profiles.osx'][PROFILE]['args'])


def test_unowned_pet_profile_is_not_silently_adopted(settings, tmp_path):
    root = tmp_path / 'Library/Application Support/OpenVegas/native-terminal'
    data = json.loads(settings.read_text())
    data['terminal.integrated.profiles.osx'][PROFILE] = profile_spec(root, game_handoff=False)
    settings.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='customized'):
        configure(tmp_path, dict(os.environ), apply=True)
    assert json.loads(settings.read_text()) == data


def test_owned_unbound_profile_pins_plugin_account_without_changing_login(settings, tmp_path, monkeypatch):
    from openvegas.integrations import credentials
    monkeypatch.setattr(credentials, 'backend_url', lambda: 'http://127.0.0.1:8000')
    monkeypatch.setattr(credentials, 'profile', lambda: tmp_path / 'local-account')
    configure(tmp_path, dict(os.environ), apply=True)
    root = tmp_path / 'Library/Application Support/OpenVegas/native-terminal'
    data = json.loads(settings.read_text())
    data['terminal.integrated.profiles.osx'][PROFILE] = profile_spec(root)
    settings.write_text(json.dumps(data))
    configure(tmp_path, dict(os.environ), apply=True)
    updated = json.loads(settings.read_text())
    bound = updated['terminal.integrated.profiles.osx'][PROFILE]
    assert bound['env'] == {'OPENVEGAS_BACKEND_URL': 'http://127.0.0.1:8000',
                            'OPENVEGAS_INTEGRATION_PROFILE': str(tmp_path / 'local-account')}
    assert updated['terminal.integrated.defaultProfile.osx'] == data['terminal.integrated.defaultProfile.osx']
    assert updated['terminal.integrated.profiles.osx']['Other'] == data['terminal.integrated.profiles.osx']['Other']
    assert 'HOME' not in bound['env']
    # Later setup using a different account cannot silently repoint this profile.
    before = settings.read_bytes()
    monkeypatch.setattr(credentials, 'backend_url', lambda: 'https://example.test')
    with pytest.raises(ValueError, match='customized'):
        configure(tmp_path, dict(os.environ), apply=True)
    assert settings.read_bytes() == before


def test_terminal_command_uses_real_host_home(monkeypatch, tmp_path):
    from openvegas.integrations import terminal_profile
    monkeypatch.setattr(commands.sys, 'platform', 'darwin')
    monkeypatch.setattr(commands, 'host_environment', lambda: {'HOME': str(tmp_path)})
    calls = []
    def fake(home, env, **kwargs):
        calls.append((home, env, kwargs))
        return {'applied': True, 'profile': PROFILE}
    monkeypatch.setattr(terminal_profile, 'configure', fake)
    result = CliRunner().invoke(commands.integration, ['terminal', '--install'])
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, {'HOME': str(tmp_path)}, {'apply': True})]
    assert 'Run claude or codex normally' in result.output


def test_explicit_setup_can_replace_owned_local_binding(settings, tmp_path, monkeypatch):
    from openvegas.integrations import credentials, terminal_profile
    monkeypatch.setattr(credentials, 'backend_url', lambda: 'http://127.0.0.1:8000')
    monkeypatch.setattr(credentials, 'profile', lambda: tmp_path / 'local-account')
    configure(tmp_path, dict(os.environ), apply=True)
    before = json.loads(settings.read_text())
    monkeypatch.setattr(credentials, 'backend_url', lambda: 'https://app.openvegas.ai')
    monkeypatch.setattr(credentials, 'profile', lambda: tmp_path / '.openvegas')
    monkeypatch.setattr(terminal_profile.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(terminal_profile.sys, 'executable', '/installed/openvegas-darwin-arm64')
    configure(tmp_path, dict(os.environ), apply=True, replace_owned=True)
    after = json.loads(settings.read_text())
    assert after['terminal.integrated.profiles.osx'][PROFILE]['env'] == {
        'OPENVEGAS_BACKEND_URL': 'https://app.openvegas.ai',
        'OPENVEGAS_INTEGRATION_PROFILE': str(tmp_path / '.openvegas')}
    assert after['terminal.integrated.defaultProfile.osx'] == before['terminal.integrated.defaultProfile.osx']
    assert after['terminal.integrated.profiles.osx']['Other'] == before['terminal.integrated.profiles.osx']['Other']


def test_explicit_setup_preserves_custom_shell_even_when_owned(settings, tmp_path):
    configure(tmp_path, dict(os.environ), apply=True)
    data = json.loads(settings.read_text())
    data['terminal.integrated.profiles.osx'][PROFILE]['args'].append('user-custom-command')
    settings.write_text(json.dumps(data))
    before = settings.read_bytes()
    with pytest.raises(ValueError, match='customized'):
        configure(tmp_path, dict(os.environ), apply=True, replace_owned=True)
    assert settings.read_bytes() == before


@pytest.mark.parametrize('present', [True, False])
def test_default_opt_in_and_undo_preserve_other_settings(settings, tmp_path, present):
    original = json.loads(settings.read_text())
    if not present:
        original.pop('terminal.integrated.defaultProfile.osx')
        settings.write_text(json.dumps(original))
    configure(tmp_path, dict(os.environ), make_default=True)  # Dry-run.
    assert json.loads(settings.read_text()) == original
    configure(tmp_path, dict(os.environ), apply=True, make_default=True)
    after = settings.read_bytes()
    assert json.loads(after)['terminal.integrated.defaultProfile.osx'] == PROFILE
    configure(tmp_path, dict(os.environ), apply=True, make_default=True)
    assert settings.read_bytes() == after
    changed = json.loads(after)
    changed['editor.fontSize'] = 23
    settings.write_text(json.dumps(changed))
    configure(tmp_path, dict(os.environ), apply=True, restore_default=True)
    restored = json.loads(settings.read_text())
    assert restored['editor.fontSize'] == 23
    assert restored.get('terminal.integrated.defaultProfile.osx') == original.get('terminal.integrated.defaultProfile.osx')
    assert restored['terminal.integrated.profiles.osx']['Other'] == original['terminal.integrated.profiles.osx']['Other']


def test_restore_will_not_clobber_a_later_default(settings, tmp_path):
    configure(tmp_path, dict(os.environ), apply=True, make_default=True)
    data = json.loads(settings.read_text())
    data['terminal.integrated.defaultProfile.osx'] = 'My new shell'
    settings.write_text(json.dumps(data))
    before = settings.read_bytes()
    with pytest.raises(ValueError, match='preserving your newer choice'):
        configure(tmp_path, dict(os.environ), apply=True, restore_default=True)
    assert settings.read_bytes() == before


@pytest.mark.parametrize('answer,expected', [('y\n', True), ('n\n', False), ('\n', False), ('', False)])
def test_cli_confirmation_is_blue_default_no_and_eof_safe(settings, tmp_path, monkeypatch, answer, expected):
    monkeypatch.setattr(commands.sys, 'platform', 'darwin')
    monkeypatch.setattr(commands, 'host_environment', lambda: {**os.environ, 'HOME': str(tmp_path)})
    monkeypatch.setattr(commands, '_interactive_terminal', lambda: True)
    before = settings.read_bytes()
    result = CliRunner().invoke(commands.integration, ['terminal', '--install', '--make-default'], input=answer, color=True)
    assert result.exit_code == 0, result.output
    assert '\x1b[34mMake OpenVegas Graphics' in result.output
    assert '[y/N]' in result.output and 'ALL new VS Code' in result.output
    assert (json.loads(settings.read_text())['terminal.integrated.defaultProfile.osx'] == PROFILE) is expected
    if not expected:
        assert settings.read_bytes() == before


def test_cli_noninteractive_cannot_change_default(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(commands.sys, 'platform', 'darwin')
    monkeypatch.setattr(commands, 'host_environment', lambda: {**os.environ, 'HOME': str(tmp_path)})
    before = settings.read_bytes()
    result = CliRunner().invoke(commands.integration, ['terminal', '--install', '--make-default'], input='y\n')
    assert result.exit_code == 0 and 'Interactive confirmation required' in result.output
    assert settings.read_bytes() == before


def test_go_yes_does_not_consent_to_default_and_dry_run_never_asks(settings, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr('openvegas.integrations.game_permissions.configure',
                        lambda *a, **kw: {'status': 'unchanged'})
    monkeypatch.setattr(commands.sys, 'platform', 'darwin')
    monkeypatch.setattr(commands, 'detect', lambda _: {'version': '0.163.0'})
    monkeypatch.setattr(commands, 'other_installations', lambda _: [])
    monkeypatch.setattr(commands, 'host_environment', lambda: {**os.environ, 'HOME': str(tmp_path)})
    monkeypatch.setattr(commands, '_interactive_terminal', lambda: True)
    monkeypatch.setattr(commands, 'install', lambda host, **kw: {'host': host, 'version': '0.163.0',
                      'mode': 'fixture', 'path': 'fixture', 'host_home': str(tmp_path), 'reload': 'restart'})
    login = AsyncMock(return_value={'balance': '10'})
    monkeypatch.setattr(commands, 'browser_login', login)
    (tmp_path / '.codex').mkdir()
    result = CliRunner().invoke(commands.go, ['--host', 'codex', '--dry-run'])
    assert result.exit_code == 0 and 'Make OpenVegas Graphics' not in result.output
    login.assert_not_called()
    before = settings.read_bytes()
    result = CliRunner().invoke(commands.go, ['--host', 'codex', '--yes'], input='n\n')
    assert result.exit_code == 0, result.output
    assert '[y/N]' in result.output and settings.read_bytes() == before
    assert len(list((tmp_path / '.codex/pets').iterdir())) == 6
    result = CliRunner().invoke(commands.go, ['--host', 'codex', '--yes'], input='y\n')
    assert result.exit_code == 0, result.output
    assert json.loads(settings.read_text())['terminal.integrated.defaultProfile.osx'] == PROFILE
    result = CliRunner().invoke(commands.go, ['--host', 'codex', '--yes'])
    assert result.exit_code == 0 and 'already your VS Code default' in result.output
    assert '[y/N]' not in result.output


def test_ci_does_not_prompt_even_with_a_tty(monkeypatch):
    monkeypatch.setattr(commands.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(commands.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setenv('CI', '1')
    assert not commands._interactive_terminal()


def test_failed_default_write_can_be_retried(settings, tmp_path, monkeypatch):
    from openvegas.integrations import terminal_profile
    original = settings.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(terminal_profile, '_write_settings', lambda *a: (_ for _ in ()).throw(OSError('fixture')))
        with pytest.raises(OSError):
            configure(tmp_path, dict(os.environ), apply=True, make_default=True)
    assert settings.read_bytes() == original
    assert not (tmp_path / 'Library/Application Support/OpenVegas/native-terminal/default-profile.json').exists()
    configure(tmp_path, dict(os.environ), apply=True, make_default=True)
    assert json.loads(settings.read_text())['terminal.integrated.defaultProfile.osx'] == PROFILE


def test_games_profile_is_optional_and_preserves_existing_default(settings, tmp_path):
    from openvegas.integrations.terminal_profile import GAME_PROFILE
    result = configure(tmp_path, dict(os.environ), apply=True, game_handoff=True)
    data = json.loads(settings.read_text())
    assert result['profile'] == GAME_PROFILE
    assert data['terminal.integrated.defaultProfile.osx'] == 'zsh'
    profile = data['terminal.integrated.profiles.osx'][GAME_PROFILE]
    assert 'terminal_relay' in ' '.join(profile['args'])
    assert not {'codex', 'claude'} & set(profile['args'])
    run = subprocess.run([profile['path'], *profile['args'], '-c', 'printf normal-shell'],
        env={**os.environ, 'HOME': str(tmp_path)}, capture_output=True, timeout=10)
    assert run.returncode == 0 and run.stdout == b'normal-shell'


def test_attach_uses_existing_terminal_and_real_host_home(monkeypatch, tmp_path):
    from openvegas.integrations import terminal_relay
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'http://127.0.0.1:8000')
    monkeypatch.setenv('OPENVEGAS_INTEGRATION_PROFILE', str(tmp_path / 'local-account'))
    monkeypatch.delenv('OPENVEGAS_TERMINAL_SOCKET', raising=False)
    monkeypatch.setattr(commands, 'host_environment', lambda: {'HOME':str(tmp_path), 'SHELL':'/bin/zsh'})
    calls = []
    monkeypatch.setattr(terminal_relay, 'run', lambda shell: calls.append((shell, os.environ['HOME'])))
    before = dict(os.environ)
    try:
        result = CliRunner().invoke(commands.integration, ['terminal','--attach'])
        assert result.exit_code == 0, result.output
        assert calls == [(['/bin/zsh','-l'], str(tmp_path))]
        assert os.environ['OPENVEGAS_BACKEND_URL'] == 'http://127.0.0.1:8000'
        assert os.environ['OPENVEGAS_INTEGRATION_PROFILE'] == str(tmp_path / 'local-account')
        assert 'No new tab' in result.output
        assert not (tmp_path/'Library').exists()
    finally:
        os.environ.clear()
        os.environ.update(before)


def test_check_does_not_open_terminal_or_wallet(monkeypatch):
    from unittest.mock import AsyncMock

    from openvegas.integrations import terminal_handoff, terminal_profile, terminal_relay

    monkeypatch.setattr(terminal_relay, 'run', lambda *a: pytest.fail('Started a shell'))
    monkeypatch.setattr(terminal_profile, 'configure', lambda *a, **k: pytest.fail('Changed profile'))
    monkeypatch.setattr(terminal_handoff, 'check', AsyncMock(return_value={
        'available': True, 'surface': 'same_terminal'}))
    result = CliRunner().invoke(commands.integration, ['terminal', '--check'])
    assert result.exit_code == 0 and 'same_terminal' in result.output
    terminal_handoff.check.assert_awaited_once()
