"""Offline launcher contracts. Installed-host rehearsals require explicit opt-in."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from openvegas.integrations import native as launcher
from openvegas.integrations.broker import Broker
from openvegas.integrations.credentials import IntegrationError

DEVICE = 'ov_device_offline_fixture_only'
TOKEN = 'ov_native_offline_fixture_only'
PRICING = {'fingerprint': 'a' * 64, 'v_per_usd': '100', 'multiplier': '1.73',
           'v_per_supplier_usd': '173', 'supplier_input_usd_per_million': '0.25',
           'supplier_output_usd_per_million': '2'}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    home = tmp_path / 'native-home'
    home.mkdir()
    (home / '.credentials.json').write_text('{"subscription":"unchanged-fixture"}')
    (home / 'config.toml').write_text('model = "saved-model"\n')
    script = tmp_path / 'stub-host'
    capture = tmp_path / 'child.json'
    script.write_text(f'#!{sys.executable}\n' +
        'import json, os, pathlib, sys\n' +
        f'pathlib.Path({str(capture)!r}).write_text(json.dumps({{"argv":sys.argv[1:],"cwd":os.getcwd(),"env":dict(os.environ)}}))\n' +
        'sys.exit(7)\n')
    script.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('OPENVEGAS_HOST_HOME', str(home))
    monkeypatch.setenv('OPENVEGAS_HOST_CODEX_HOME', str(home))
    monkeypatch.setenv('OPENVEGAS_HOST_CLAUDE_CONFIG_DIR', str(home))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'not-forwarded-fixture')
    monkeypatch.setenv('OPENAI_API_KEY', 'not-forwarded-fixture')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'not-forwarded-fixture')
    monkeypatch.setenv('OPENVEGAS_NATIVE_TOKEN', 'stale-fixture')
    monkeypatch.setattr(launcher, 'detect', lambda host: {'executable': str(script), 'version': '1.2.3'})
    calls, messages = [], []
    state = {'device': DEVICE, 'quote_patch': {}, 'session_patch': {}, 'status_patch': {}, 'close_fail': False}

    def request(req):
        data = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, req.headers.get('authorization'), data))
        if req.url.path.endswith('/quote'):
            return httpx.Response(200, json={**data, 'pricing': PRICING, **state['quote_patch']})
        if req.url.path.endswith('/sessions'):
            return httpx.Response(200, json={'token': TOKEN, 'session_id': 'fixture-session',
                'expires_in': 600, 'budget_v': data['limit_v'], 'pricing': PRICING, **state['session_patch']})
        if req.method == 'DELETE':
            if state['close_fail']:
                raise RuntimeError(TOKEN)
            return httpx.Response(200, json={'closed': True})
        return httpx.Response(200, json={'session_id': 'fixture-session', **state['status_patch']})

    broker = Broker(backend='http://127.0.0.1:12345', transport=httpx.MockTransport(request))
    broker.credentials = SimpleNamespace(load=lambda: state['device'])
    def run(host='claude', **kwargs):
        async def invoke():
            try:
                return await launcher.launch(host, 'anthropic/exact' if host == 'claude' else 'openai/exact',
                    Decimal('2.125'), broker=broker, emit=messages.append, **kwargs)
            finally:
                await broker.close()
        return asyncio.run(invoke())
    return SimpleNamespace(run=run, calls=calls, messages=messages, state=state, home=home,
                           tmp=tmp_path, capture=capture, script=script, broker=broker)


@pytest.mark.parametrize('host,args', [('claude', ['--continue', '--permission-mode', 'default']),
                                      ('codex', ['resume', '--last', '--cd', 'project', '--sandbox', 'read-only'])])
def test_real_stub_process_exact_grant_environment_cwd_and_exit(rig, host, args):
    before = {p.name: p.read_bytes() for p in rig.home.iterdir()}
    assert rig.run(host, yes=True, host_args=args, local_tools_only=host == 'codex') == 7
    captured = json.loads(rig.capture.read_text())
    env = captured['env']
    assert captured['cwd'] == str(rig.tmp)
    assert env['HOME'] == str(rig.home)
    assert env['OPENVEGAS_NATIVE_TOKEN'] == TOKEN
    assert 'OPENAI_API_KEY' not in env and 'OPENROUTER_API_KEY' not in env
    assert TOKEN not in ' '.join(captured['argv']) and DEVICE not in str(captured)
    if host == 'claude':
        assert env['ANTHROPIC_API_KEY'] == ''
        assert env['ANTHROPIC_AUTH_TOKEN'] == TOKEN
        assert env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] == '2048'
        assert env['ANTHROPIC_BASE_URL'].endswith('/integrations/native')
        for family in ('HAIKU', 'SONNET', 'OPUS', 'FABLE'):
            assert f'ANTHROPIC_DEFAULT_{family}_MODEL' not in env
        assert captured['argv'] == ['--model', 'anthropic/exact', *args]
    else:
        assert captured['argv'][0] == 'resume'
        assert captured['argv'][-len(args[1:]):] == args[1:]
        assert env['CODEX_HOME'] == str(rig.home)
    assert {p.name: p.read_bytes() for p in rig.home.iterdir()} == before
    assert [c[:2] for c in rig.calls] == [('POST', '/integrations/native/quote'),
        ('POST', '/integrations/native/sessions'), ('GET', '/integrations/native/session'),
        ('DELETE', '/integrations/native/session')]
    assert [c[2] for c in rig.calls] == ['Bearer ' + DEVICE] * 2 + ['Bearer ' + TOKEN] * 2
    assert rig.calls[1][3] == {'host': host, 'model': 'anthropic/exact' if host == 'claude' else 'openai/exact',
        'max_tokens': 2048, 'limit_v': '2.125', 'confirmed': True, 'pricing_fingerprint': PRICING['fingerprint']}
    assert 'markup 73.00%' in '\n'.join(rig.messages)
    assert 'Quoted supplier USD per million tokens: input 0.25; output 2.' in rig.messages
    assert 'Converted retail $V per million tokens: input $V 43.25; output $V 346.00.' in rig.messages
    assert TOKEN not in '\n'.join(rig.messages)


def test_declined_quote_never_opens_session(rig):
    assert rig.run(confirm=lambda: False) == 0
    assert len(rig.calls) == 1 and not rig.capture.exists()


def test_missing_device_never_enrolls_or_calls_server(rig):
    rig.state['device'] = None
    with pytest.raises(IntegrationError, match='Existing device sign-in'):
        rig.run(yes=True)
    assert not rig.calls and not rig.capture.exists()


def test_dry_run_no_auth_detect_process_or_mutation(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('dry run attempted side effects')
    monkeypatch.setattr(launcher, 'Broker', forbidden)
    monkeypatch.setattr(launcher, 'detect', forbidden)
    monkeypatch.setattr(launcher, 'run_host', forbidden)
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'http://127.0.0.1:12345')
    result = CliRunner().invoke(launcher.native, ['codex', '--model', 'openai/exact', '--limit-v',
        '1.25', '--dry-run', '--', 'resume', '--last'])
    assert result.exit_code == 0, result.output
    assert 'no authentication' in result.output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('host,model,budget', [
    ('claude', 'anthropic/claude-haiku-4.5', '50'),
    ('codex', 'openai/gpt-5.4-nano', '10'),
])
def test_short_launch_defaults_require_consent(monkeypatch, host, model, budget):
    calls = []

    async def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(launcher, 'launch', launch)
    result = CliRunner().invoke(launcher.native, [host])
    assert result.exit_code == 0, result.output
    assert calls[0][0][:3] == (host, model, budget)
    assert calls[0][1]['yes'] is False
    calls.clear()
    result = CliRunner().invoke(launcher.native, [host, '--yes'])
    assert result.exit_code != 0 and 'requires explicit' in result.output
    assert not calls


@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_direct_host_command_reuses_consent_and_argument_boundary(monkeypatch, host):
    calls = []

    async def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(launcher, 'launch', launch)
    command = launcher.HostCommand(host)
    result = CliRunner().invoke(command, [])
    assert result.exit_code == 0, result.output
    assert calls[-1][0][0] == host
    assert not calls[-1][1]['yes']
    result = CliRunner().invoke(command, ['--yes'])
    assert result.exit_code != 0 and len(calls) == 1
    result = CliRunner().invoke(command, ['--model', 'reviewed/model', '--limit-v', '1',
                                        '--yes', '--', 'resume', '--last'])
    assert result.exit_code == 0, result.output
    assert calls[-1][0][:3] == (host, 'reviewed/model', Decimal(1))
    assert calls[-1][1]['yes'] and calls[-1][1]['host_args'] == ('resume', '--last')
    result = CliRunner().invoke(command, ['--', 'exec', '--yes'])
    assert result.exit_code == 0, result.output
    assert calls[-1][1]['host_args'] == ('exec', '--yes') and not calls[-1][1]['yes']
    before = len(calls)
    result = CliRunner().invoke(command, ['--limit-v', '1', '--limit-v', '2'])
    assert result.exit_code != 0 and len(calls) == before
    result = CliRunner().invoke(command, ['--help'])
    assert result.exit_code == 0 and ' HOST' not in result.output


@pytest.mark.parametrize('patch', [{'model': 'changed'}, {'max_tokens': 999},
    {'pricing': {**PRICING, 'multiplier': 'NaN'}}])
def test_bad_quote_never_creates_session(rig, patch):
    rig.state['quote_patch'] = patch
    with pytest.raises(IntegrationError):
        rig.run(yes=True)
    assert len(rig.calls) == 1 and not rig.capture.exists()


@pytest.mark.parametrize('patch', [{'budget_v': '3'}, {'expires_in': 0},
    {'pricing': {**PRICING, 'multiplier': '1.74'}}, {'session_id': '\ninvalid'}])
def test_malformed_grant_is_closed_without_launch(rig, patch):
    rig.state['session_patch'] = patch
    with pytest.raises(IntegrationError):
        rig.run(yes=True)
    assert rig.calls[-1][0] == 'DELETE' and not rig.capture.exists()


def test_inactive_session_is_closed(rig):
    rig.state['status_patch'] = {'active': False}
    with pytest.raises(IntegrationError, match='not active'):
        rig.run(yes=True)
    assert rig.calls[-1][0] == 'DELETE' and not rig.capture.exists()


def test_failed_spawn_sanitizes_and_closes(rig, monkeypatch):
    async def fail(*args):
        raise OSError(TOKEN)
    monkeypatch.setattr(launcher, 'run_host', fail)
    with pytest.raises(IntegrationError, match='Native launch failed') as error:
        rig.run(yes=True)
    assert TOKEN not in str(error.value) and rig.calls[-1][0] == 'DELETE'


def test_close_failure_preserves_exit_and_sanitizes(rig):
    rig.state['close_fail'] = True
    assert rig.run(yes=True) == 7
    assert 'could not be confirmed' in rig.messages[-1]
    assert TOKEN not in '\n'.join(rig.messages)


@pytest.mark.parametrize('host,args', [
    ('claude', ['--model', 'changed']), ('claude', ['--settings={"env":{}}']),
    ('claude', ['--fallback-model', 'x']), ('claude', ['--bare']), ('claude', ['--mod=x']),
    ('claude', ['--agent', 'changed']), ('codex', ['resume', '-m', 'x']),
    ('codex', ['exec', '--config', 'model="x"']), ('codex', ['-p', 'x']),
    ('codex', ['-cmodel="x"']), ('codex', ['--remote', 'x']), ('codex', ['--oss']),
    ('codex', ['--enable', 'x']), ('codex', ['login']), ('claude', ['auth']),
    ('codex', ['resume', '--yes']), ('codex', ['--', 'bad\x00value'])])
def test_unsafe_arguments_rejected_before_auth(rig, host, args):
    with pytest.raises(IntegrationError):
        rig.run(host, yes=True, host_args=args)
    assert not rig.calls


def test_codex_provider_and_review_compatible_overrides():
    for args in ([], ['resume', '--last'], ['exec', '--skip-git-repo-check', 'hello'], ['review', '--uncommitted']):
        argv = launcher.host_argv('/fixture/codex', 'codex', 'http://127.0.0.1:1', 'openai/exact', args)
        overrides = [argv[i + 1] for i, value in enumerate(argv) if value == '-c']
        config = {}
        for override in overrides:
            config.update(tomllib.loads(override))
        provider = config['model_providers']['openvegas_v']
        assert config['model_provider'] == 'openvegas_v' and config['model'] == 'openai/exact'
        assert config['review_model'] == 'openai/exact'
        assert provider['base_url'].endswith('/integrations/native/v1')
        assert provider['env_key'] == 'OPENVEGAS_NATIVE_TOKEN'
        assert provider['wire_api'] == 'responses'
        assert provider['requires_openai_auth'] is False and provider['supports_websockets'] is False
        assert provider['request_max_retries'] == provider['stream_max_retries'] == 0
        assert TOKEN not in str(argv)
        if args and args[0] == 'review':
            assert '--model' not in argv


@pytest.mark.parametrize('tail', [['--model', 'duplicate'], ['--yes', '--yes'], ['--limit-v', '2']])
def test_duplicate_launcher_options_rejected(tail):
    result = CliRunner().invoke(launcher.native,
        ['codex', '--model', 'openai/exact', '--limit-v', '1', '--yes', '--dry-run', *tail])
    assert result.exit_code == 2 and 'Repeated launcher' in result.output


@pytest.mark.parametrize('budget', ['NaN', 'Infinity', '-1', '0', '1e9999'])
def test_invalid_decimal_never_launches(budget):
    result = CliRunner().invoke(launcher.native,
        ['codex', '--model', 'openai/exact', '--limit-v', budget, '--dry-run'])
    assert result.exit_code == 2


def test_trailing_host_args_not_reparsed_as_consent(monkeypatch):
    captured = {}
    async def launch(*args, **kwargs):
        captured.update(kwargs)
        return 0
    monkeypatch.setattr(launcher, 'launch', launch)
    result = CliRunner().invoke(launcher.native,
        ['codex', '--model', 'openai/exact', '--limit-v', '1', '--', 'exec', '--', '--yes'])
    assert result.exit_code == 0, result.output
    assert captured['yes'] is False and captured['host_args'] == ('exec', '--', '--yes')


def test_stdio_and_cwd_inherited_and_signal_exit(monkeypatch):
    calls = []
    class Process:
        async def wait(self):
            return -15
    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    assert asyncio.run(launcher.run_host(['/host', 'resume'], {'HOME': '/original'})) == 143
    assert calls == [(('/host', 'resume'), {'env': {'HOME': '/original'}})]


@pytest.mark.skipif(os.getenv('OV_TEST_NATIVE_LAUNCHER_HOSTS') != '1',
                    reason='Explicit opt-in for installed CLIs in isolated loopback-only profiles')
@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_installed_host_tool_roundtrip_loopback_only(host, tmp_path, monkeypatch):
    import shutil
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    executable = shutil.which(host)
    hosted_web = host == 'codex' and os.getenv('OV_TEST_NATIVE_WEB') == '1'
    if not executable or sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists():
        pytest.skip('Installed host and macOS loopback-only sandbox required')
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'codex').mkdir()
    (home / 'claude').mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'fixture.txt').write_text('NATIVE_OFFLINE_TOOL_CONTENT\n')
    requests, auth_ok, seen_paths = [], [], []
    active_token = TOKEN
    model = 'anthropic/claude-haiku-4.5' if host == 'claude' else 'openai/gpt-4.1-nano'

    def event(kind, **data):
        return 'event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}) + '\n\n'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            path = urlsplit(self.path).path
            seen_paths.append(path)
            auth_ok.append(self.headers.get('Authorization') == 'Bearer ' + active_token)
            if path not in {'/integrations/native/v1/messages', '/integrations/native/v1/responses'}:
                self.send_response(404)
                self.end_headers()
                return
            requests.append(body)
            first = len(requests) == 1
            if host == 'claude':
                block = ({'type': 'tool_use', 'id': 'tool_offline', 'name': 'Read',
                          'input': {'file_path': str(workspace / 'fixture.txt')}} if first else
                         {'type': 'text', 'text': 'NATIVE_OFFLINE_DONE'})
                message = {'id': 'msg_offline_' + str(len(requests)), 'type': 'message', 'role': 'assistant',
                    'model': model, 'content': [block], 'stop_reason': 'tool_use' if first else 'end_turn',
                    'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
                if not body.get('stream'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(message).encode())
                    return
                payload = event('message_start', message={**message, 'content': [], 'stop_reason': None})
                start = {**block, 'input': {}} if first else {'type': 'text', 'text': ''}
                payload += event('content_block_start', index=0, content_block=start)
                delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])} if first else
                         {'type': 'text_delta', 'text': block['text']})
                payload += event('content_block_delta', index=0, delta=delta)
                payload += event('content_block_stop', index=0)
                payload += event('message_delta', delta={'stop_reason': message['stop_reason'], 'stop_sequence': None},
                                 usage={'output_tokens': 5})
                payload += event('message_stop')
            else:
                tools = body.get('tools', [])
                names = {tool.get('name') for tool in tools}
                name = next((n for n in ('exec_command', 'shell_command', 'shell') if n in names), 'exec_command')
                arguments = ({'cmd': 'cat fixture.txt', 'max_output_tokens': 100} if name == 'exec_command' else
                             {'command': 'cat fixture.txt'} if name == 'shell_command' else
                             {'command': ['cat', 'fixture.txt'], 'workdir': str(workspace)})
                item = ({'id': 'fc_offline', 'type': 'function_call', 'call_id': 'call_offline', 'name': name,
                         'arguments': json.dumps(arguments), 'status': 'completed'} if first else
                        {'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                         'content': [{'type': 'output_text', 'text': 'NATIVE_OFFLINE_DONE', 'annotations': []}]})
                response = {'id': 'resp_offline_' + str(len(requests)), 'object': 'response', 'model': model,
                    'status': 'completed', 'output': [item], 'usage': {'input_tokens': 10, 'output_tokens': 5,
                        'total_tokens': 15, 'input_tokens_details': {'cached_tokens': 0},
                        'output_tokens_details': {'reasoning_tokens': 0}}}
                payload = event('response.created', response={**response, 'status': 'in_progress', 'output': []})
                payload += event('response.output_item.added', output_index=0, item=item)
                if not first:
                    payload += event('response.output_text.delta', output_index=0, content_index=0,
                                     item_id=item['id'], delta='NATIVE_OFFLINE_DONE')
                payload += event('response.output_item.done', output_index=0, item=item)
                payload += event('response.completed', response=response)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload.encode())))
            self.end_headers()
            self.wfile.write(payload.encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = f'http://127.0.0.1:{server.server_port}'
    # The subprocess receives no developer/provider keys. Seatbelt denies all
    # network destinations except this ephemeral fake server, including DNS.
    clean = {'PATH': os.environ['PATH'], 'HOME': str(home), 'USERPROFILE': str(home),
             'CODEX_HOME': str(home / 'codex'), 'CLAUDE_CONFIG_DIR': str(home / 'claude'),
             'TMPDIR': str(tmp_path), 'TERM': 'dumb', 'LANG': 'en_US.UTF-8',
             'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
             'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1'}
    monkeypatch.setattr(launcher, 'host_environment', lambda: clean)
    env = launcher.child_environment(host, backend, model, 2048, TOKEN)
    args = (['--print', '--output-format', 'json', '--max-turns', '3',
             '--allowedTools', 'Read', '--', 'Read fixture.txt then say NATIVE_OFFLINE_DONE.'] if host == 'claude' else
            # Seatbelt cannot be nested on macOS. This test's outer sandbox
            # remains authoritative; the fake model only requests cat fixture.txt.
            ['exec', '--skip-git-repo-check', '--sandbox', 'danger-full-access',
             'Read fixture.txt then say NATIVE_OFFLINE_DONE.'])
    argv = launcher.host_argv(executable, host, backend, model, args,
                             local_tools_only=host == 'codex' and not hosted_web, hosted_web=hosted_web)
    policy = ('(version 1)(allow default)(deny network*)'
              f'(allow network-outbound (remote ip "localhost:{server.server_port}"))')
    try:
        completed = subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, *argv], cwd=workspace,
                                   env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=45, check=False)
        assert completed.returncode == 0, completed.stderr[-3000:]
        first_count = len(requests)
        active_token = 'ov_native_new_resume_fixture_only'
        resumed_env = launcher.child_environment(host, backend, model, 2048, active_token)
        resume_args = (['--continue', '--print', '--output-format', 'json', '--max-turns', '1',
                        '--allowedTools', 'Read', '--', 'RESUME_MARKER: recall the file you just read.']
                       if host == 'claude' else
                       ['exec', 'resume', '--last', '--skip-git-repo-check',
                        'RESUME_MARKER: recall the file you just read.'])
        resume_argv = launcher.host_argv(executable, host, backend, model, resume_args,
                                        local_tools_only=host == 'codex' and not hosted_web, hosted_web=hosted_web)
        resumed = subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, *resume_argv], cwd=workspace,
                                 env=resumed_env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=45, check=False)
        assert resumed.returncode == 0, resumed.stderr[-3000:]
        resumed_requests = requests[first_count:]
        assert resumed_requests, 'Resume made no request'
        for marker in ('RESUME_MARKER', 'NATIVE_OFFLINE_TOOL_CONTENT', 'NATIVE_OFFLINE_DONE'):
            assert marker in json.dumps(resumed_requests), 'Resume lost native history: ' + marker
        assert all(auth_ok), 'Resume did not use the fresh process-scoped grant'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    summary = {'host': host, 'exit': completed.returncode, 'resume_exit': resumed.returncode,
               'resume_kept_tool_history': True, 'fresh_resume_grant': True,
               'hosted_web_schema': hosted_web, 'paths': seen_paths,
               'fields': [sorted(body) for body in requests],
               'tool_types': sorted({str(tool.get('type', 'custom')) for body in requests for tool in body.get('tools', [])}),
               'tool_names': sorted({str(tool.get('name')) for body in requests for tool in body.get('tools', [])})}
    print(json.dumps(summary, sort_keys=True))
    capture = os.getenv('OV_NATIVE_FIXTURE_CAPTURE_DIR')
    if capture:
        # Only this opt-in, isolated-home, synthetic fixture test writes captures.
        # No headers, provider keys or existing user histories enter this server.
        target = Path(capture)
        target.mkdir(parents=True, exist_ok=True)
        (target / (host + '-requests.json')).write_text(json.dumps(requests))
        print(json.dumps({'host': host, 'thinking': requests[0].get('thinking'),
                          'output_config': requests[0].get('output_config')}, sort_keys=True))
    assert completed.returncode == 0, (summary, completed.stderr[-3000:])
    assert len(requests) >= 2 and all(auth_ok), summary
    assert all(body['model'] == model for body in requests)
    assert 'NATIVE_OFFLINE_DONE' in completed.stdout
    assert 'NATIVE_OFFLINE_TOOL_CONTENT' in json.dumps(requests[1:]), 'Host did not return the actual read result'
    # Exercise the current coordinator wire validator without DB, Redis or a
    # provider. Only the catalog context lookup is a deterministic fixture.
    from openvegas.gateway import native_wire
    monkeypatch.setattr(native_wire, 'model_capabilities', lambda *_: {'context_window_tokens': 1_000_000})
    pricing = dict(PRICING)
    if hosted_web:
        pricing['native_web'] = {'tool': 'web_search', 'max_calls': 1,
                                 'supplier_usd_per_call': '0.01', 'max_model_rounds': 2}
    session = {'host': host, 'model': model, 'max_tokens': 2048, 'pricing': pricing,
               'user_id': 'offline', 'session_id': 'offline'}
    for body in requests:
        native_wire.prepare_request(session, body, 'messages' if host == 'claude' else 'responses')
    if host == 'claude':
        assert requests[0]['thinking']['type'] == 'enabled'
        assert 'output_config' not in requests[0]
        # Exercise the real default tool set, not a reduced Read-only schema.
        assert {'Read', 'Bash', 'Edit', 'Write'} <= set(summary['tool_names'])
    else:
        assert any(tool.get('type') == 'web_search' for tool in requests[0]['tools']) == hosted_web
        assert requests[0].get('reasoning', {}) == {}


def test_codex_requires_explicit_web_limitation_without_reviewed_quote(rig):
    with pytest.raises(IntegrationError, match='local-tools-only'):
        rig.run('codex', yes=True)
    assert len(rig.calls) == 1 and rig.calls[0][1].endswith('/quote')
    assert not rig.capture.exists()


def test_codex_conflicting_search_is_rejected_before_quote(rig):
    with pytest.raises(IntegrationError, match='conflicts'):
        rig.run('codex', yes=True, local_tools_only=True, host_args=['--search'])
    assert not rig.calls and not rig.capture.exists()


@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_packaged_environment_strips_server_secrets_without_original_home(monkeypatch, host):
    secrets = ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'SUPABASE_SERVICE_ROLE_KEY',
        'SUPABASE_ANON_KEY', 'DATABASE_URL', 'REDIS_URL', 'DB_URL', 'PGPASSWORD', 'PGPASSFILE',
        'PGSERVICEFILE', 'MISTRAL_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GROQ_API_KEY',
        'COHERE_API_KEY', 'XAI_API_KEY', 'DEEPSEEK_API_KEY', 'TOGETHER_API_KEY',
        'FIREWORKS_API_KEY', 'PERPLEXITY_API_KEY', 'PRIMARY_CRYPTO_KEY', 'JWTSECRET',
        'JWT_SECRET', 'ENCRYPTION_KEY', 'APP_SECRET', 'CUSTOM_SIGNING_KEY', 'FERNET_KEY',
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AZURE_CLIENT_SECRET',
        'GOOGLE_APPLICATION_CREDENTIALS', 'SMTP_PASSWORD', 'SENDGRID_API_KEY', 'HF_TOKEN',
        'AUTH_TOKEN', 'OPENVEGAS_NATIVE_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY',
        'OPENROUTER_API_KEY', 'CODEX_ACCESS_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK')
    keep = {'HOME': '/native/home', 'PATH': '/usr/bin:/bin', 'TERM': 'xterm-256color',
            'SHELL': '/bin/zsh', 'SSH_AUTH_SOCK': '/ssh/agent.sock', 'GIT_SSH_COMMAND': 'ssh',
            'GIT_AUTHOR_NAME': 'Fixture', 'CODEX_HOME': '/native/codex',
            'CLAUDE_CONFIG_DIR': '/native/claude', 'LANG': 'en_US.UTF-8',
            'MY_PROJECT_SETTING': 'preserved', 'CLAUDE_CODE_USE_POWERSHELL_TOOL': '1'}
    # Exercise the real installer fallback, not the local-wrapper allowlist.
    monkeypatch.setattr(os, 'environ', {**keep, **dict.fromkeys(secrets, 'operator-fixture')})
    assert launcher.host_environment() is None
    result = launcher.child_environment(host, 'http://127.0.0.1:1', 'exact-model', 2048, TOKEN)
    assert all(result[key] == value for key, value in keep.items())
    assert 'operator-fixture' not in result.values()
    assert result['OPENVEGAS_NATIVE_TOKEN'] == TOKEN
    assert TOKEN not in os.environ.values()
    if host == 'claude':
        assert result['ANTHROPIC_API_KEY'] == '' and result['ANTHROPIC_AUTH_TOKEN'] == TOKEN


def test_cancellation_closes_grant(rig, monkeypatch):
    async def cancel(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(launcher, 'run_host', cancel)
    with pytest.raises(asyncio.CancelledError):
        rig.run(yes=True)
    assert rig.calls[-1][0] == 'DELETE'


@pytest.mark.parametrize('patch', [
    {'supplier_input_usd_per_million': '-1'}, {'supplier_output_usd_per_million': 'NaN'},
    {'v_per_supplier_usd': '174'},
])
def test_invalid_model_unit_prices_fail_before_consent(rig, patch):
    rig.state['quote_patch'] = {'pricing': {**PRICING, **patch}}
    with pytest.raises(IntegrationError):
        rig.run(yes=True)
    assert len(rig.calls) == 1 and not rig.capture.exists()


def test_free_input_unit_price_is_not_invented(rig):
    rig.state['quote_patch'] = {'pricing': {**PRICING, 'supplier_input_usd_per_million': '0'}}
    assert rig.run(confirm=lambda: False) == 0
    assert 'Quoted supplier USD per million tokens: input 0; output 2.' in rig.messages


@pytest.mark.parametrize('token', ['sk-provider-fixture-long-lived', 'arbitrary-fixture-bearer',
    DEVICE, 'ov_host_fixture-long-lived', 'ov_native_bad token', 'ov_native_short'])
def test_only_explicit_native_session_bearer_can_reach_host(rig, token):
    rig.state['session_patch'] = {'token': token}
    with pytest.raises(IntegrationError, match='does not match'):
        rig.run(yes=True)
    assert not rig.capture.exists()
    assert all(call[2] == 'Bearer ' + DEVICE for call in rig.calls)
    assert [call[0] for call in rig.calls] == ['POST', 'POST']
    assert token not in '\n'.join(rig.messages)


@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_personal_codex_access_token_stays_only_in_original_host(monkeypatch, host):
    original = {'PATH': '/usr/bin:/bin', 'HOME': '/fixture/home',
                'CODEX_ACCESS_TOKEN': 'synthetic-personal-token', 'TERM': 'dumb'}
    before = dict(original)
    monkeypatch.setattr(launcher, 'host_environment', lambda: original)
    wallet = launcher.child_environment(host, 'http://127.0.0.1:1', 'model', 2048, TOKEN)
    assert 'CODEX_ACCESS_TOKEN' not in wallet
    assert original == before
    # Descendants inherit the sanitized wallet environment, never host auth.
    result = subprocess.run([sys.executable, '-c',
        'import os; assert "CODEX_ACCESS_TOKEN" not in os.environ'],
        env=wallet, capture_output=True, check=False)
    assert result.returncode == 0


def test_codex_resume_preserves_exact_quote_without_persistent_model_migration():
    argv = launcher.codex_resume_argv('codex', 'unix:///fixture.sock', 'gpt-5.6-luna', ['resume', 'fixture'])
    assert 'notice.model_migrations={"gpt-5.6-luna"="gpt-6-luna"}' in argv
    assert argv[-2:] == ['--model', 'gpt-5.6-luna']
    assert not any('trust' in arg or 'auth' in arg for arg in argv)
    other = launcher.codex_resume_argv('codex', 'unix:///fixture.sock', 'different', ['resume', 'fixture'])
    assert not any('notice.' in arg for arg in other)
    assert other[-2:] == ['--model', 'different']
