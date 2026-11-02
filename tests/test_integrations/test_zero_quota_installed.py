"""Real native processes with OS-denied external network; explicit opt-in only."""
import builtins
import errno
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('zero_quota_probe', ROOT / 'scripts/verify_zero_quota_entry.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def record_evidence(report, transcript):
    directory = os.getenv('OV_ZERO_QUOTA_EVIDENCE_DIR')
    if not directory:
        return
    prefix = Path(directory) / (f'native-worker-check-{time.time_ns()}-'
                                f'{report["host"]}-{report["mode"]}')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    for suffix, content in (('.json', json.dumps(report, indent=2) + '\n'), ('.txt', transcript)):
        with Path(str(prefix) + suffix).open('x') as handle:
            handle.write(content)
    print('evidence=' + str(prefix))


def test_fixture_environment_is_allowlisted(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                'OPENVEGAS_SWITCH_TOKEN', 'OPENVEGAS_TERMINAL_SOCKET', 'CODEX_HOME'):
        monkeypatch.setenv(key, 'personal-value-must-not-escape')
    env = probe.clean_environment(tmp_path / 'home', 'http://127.0.0.1:1234')
    assert 'personal-value-must-not-escape' not in env.values()
    assert env['ANTHROPIC_API_KEY'] == 'synthetic-not-a-real-provider-key'
    assert env['CODEX_HOME'] == str(tmp_path / 'home/codex')
    assert 'OPENAI_API_KEY' not in env
    assert 'OPENVEGAS_SWITCH_TOKEN' not in env


def test_sandbox_denies_external_network_and_personal_auth(tmp_path):
    profile = probe.sandbox_profile(tmp_path, Path('/personal'))
    assert '(deny network*)' in profile
    assert '(remote ip "localhost:*")' in profile
    assert '(deny file-read* (subpath "/personal/.codex"))' in profile
    assert '(deny file-read* (subpath "/personal/.claude"))' in profile
    assert '(deny file-read* (subpath "/personal/Library/Keychains"))' in profile
    assert '(deny process-exec (literal "/usr/bin/security"))' in profile


def test_probe_import_does_not_require_unix_pty_modules(monkeypatch):
    original_import = builtins.__import__

    def import_without_unix(name, *args, **kwargs):
        if name in {'fcntl', 'pty', 'termios'}:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', import_without_unix)
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    assert module.COMMANDS == probe.COMMANDS


def test_unsupported_platform_refuses_native_execution(monkeypatch):
    monkeypatch.setattr(probe.sys, 'platform', 'win32')
    with pytest.raises(RuntimeError, match='requires macOS sandbox-exec'):
        probe.run_probe('codex')


@pytest.mark.skipif(os.getenv('OV_TEST_ZERO_QUOTA_HOSTS') != '1',
                    reason='Opt in to actual installed hosts with OS network denial')
@pytest.mark.parametrize('host', ['codex', 'claude'])
@pytest.mark.parametrize('mode', ['cli', 'interactive'])
@pytest.mark.parametrize('missing_supervisor', [False, True])
def test_installed_node_hooks_stop_before_provider(host, mode, missing_supervisor):
    if sys.platform != 'darwin' or not shutil.which(host) or not shutil.which('node'):
        pytest.skip('Installed macOS native hosts and Node required')
    # The supported managed-child safeguard suppresses Claude's independent
    # title-model request, which is outside UserPromptSubmit hook control.
    report, transcript = probe.run_probe(host, mode, missing_supervisor,
                                         disable_terminal_title=host == 'claude')
    record_evidence(report, transcript)
    assert report['external_network_denial_errno'] == errno.EPERM
    assert report['real_subscription_quota_cache_proven'] is False
    assert report['passed'], (json.dumps(report, indent=2), transcript[-7000:])
    assert report['control_provider_posts'] > 0
    for item in report['commands']:
        assert item['provider_posts_delta'] == 0
        result = item['hook'][0]['result']
        if missing_supervisor:
            assert result['decision'] == 'block'
        else:
            assert result['continue'] is False and result['suppressOutput'] is True
    if missing_supervisor:
        assert report['supervisor_requests'] == []
    else:
        assert {'play', 'v', 'host'} <= {r['action'] for r in report['supervisor_requests']}
    if mode == 'interactive':
        identifiers = {i['hook'][0]['session_id'] for i in report['commands']}
        assert len(identifiers) == 1
        assert report['same_process_alive']
    print(f'{host} {mode} missing={missing_supervisor}: '
          f'{len(report["commands"])} exact commands, zero additional provider POSTs')


@pytest.mark.skipif(os.getenv('OV_TEST_ZERO_QUOTA_HOSTS') != '1',
                    reason='Opt in to actual installed hosts with OS network denial')
@pytest.mark.parametrize('disable_title', [False, True])
def test_cold_claude_tui_attributes_title_requests_without_prior_inference(disable_title):
    if sys.platform != 'darwin' or not shutil.which('claude') or not shutil.which('node'):
        pytest.skip('Installed macOS Claude and Node required')
    report, transcript = probe.run_probe('claude', commands=('openvegas play',),
                                         disable_terminal_title=disable_title, cold_start=True)
    record_evidence(report, transcript)
    assert report['control_provider_posts'] == 0
    result = report['commands'][0]['hook'][0]['result']
    assert result['continue'] is False
    if disable_title:
        assert report['passed'] and not report['provider_posts']
    else:
        # Preserve the actual failure of the unmitigated zero-total-POST claim.
        assert report['passed'] is False
        assert report['provider_posts']
        assert all(p['title_generation_request'] for p in report['provider_posts'])
