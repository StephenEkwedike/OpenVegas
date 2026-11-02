#!/usr/bin/env python3
"""Installed-host, no-spend hook probes. Never use a real account or native HOME.

The interactive mode drives the actual installed CLI TUI in a disposable PTY,
not `exec`/`--print`. A rejecting loopback HTTP fixture is not a subscription
quota cache; the synthetic supervisor acknowledges IPC, not gameplay or billing.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ('openvegas play', 'openvegas play blackjack', 'openvegas',
            'openvegas use v', 'openvegas use host')
EXPECTED_VERSIONS = {'codex': '0.166.0', 'claude': '2.1.301'}
REFUSAL = 'ZERO_QUOTA_FIXTURE_429: synthetic quota exhausted; no inference performed'
CONTROL_PROMPT = 'Reply with ZERO_QUOTA_CONTROL only.'
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def clean_environment(home: Path, endpoint: str) -> dict[str, str]:
    """Whitelist, not an inherited environment with a few secrets removed."""
    env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
        'USERPROFILE': str(home), 'CODEX_HOME': str(home / 'codex'),
        'CLAUDE_CONFIG_DIR': str(home / 'claude'), 'XDG_CONFIG_HOME': str(home / 'xdg'),
        'TMPDIR': str(home / 'tmp'), 'TERM': 'xterm-256color', 'LANG': 'en_US.UTF-8',
        'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1', 'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1',
        'ANTHROPIC_BASE_URL': endpoint + '/integrations/native',
        'ANTHROPIC_API_KEY': 'synthetic-not-a-real-provider-key',
        'OPENVEGAS_NATIVE_TOKEN': 'synthetic-not-a-real-provider-key',
        'OPENVEGAS_BACKEND_URL': endpoint,
        'OPENVEGAS_INTEGRATION_PROFILE': str(home / 'openvegas'),
    }
    for directory in ('codex', 'claude', 'xdg', 'tmp', 'openvegas'):
        (home / directory).mkdir(parents=True, exist_ok=True)
    return env


def sandbox_profile(root: Path, personal_home: Path) -> str:
    # The only network exception is loopback and
    # private Unix socket. Deny native personal auth/history and macOS keychain.
    paths = [personal_home / name for name in
             ('.codex', '.claude', '.claude.json', '.openvegas', 'Library/Keychains')]
    paths += [ROOT / '.env']
    deny = ''.join('(deny file-read* (subpath ' + json.dumps(str(p)) + '))' for p in paths)
    return ('(version 1)(allow default)(deny network*)'
            '(allow network-outbound (remote ip "localhost:*"))'
            '(allow network-inbound (local ip "localhost:*"))'
            '(allow network* (subpath ' + json.dumps(str(root)) + '))' + deny +
            '(deny process-exec (literal "/usr/bin/security"))'
            '(deny mach-lookup (global-name "com.apple.securityd.xpc"))')


class RejectingProvider:
    def __init__(self):
        self.posts = []
        self.gets = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.gets.append({'path': self.path, 'at': time.monotonic()})
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = min(int(self.headers.get('Content-Length', '0')), 2_000_000)
                raw = self.rfile.read(length)
                # Retain only structural metadata and known synthetic markers,
                # never auth headers, host system prompts or transcript bodies.
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                serialized = json.dumps(body)
                owner.posts.append({'path': self.path, 'at': time.monotonic(), 'status': 429,
                    'model': body.get('model'), 'max_tokens': body.get('max_tokens'),
                    'tools_count': len(body.get('tools', [])),
                    'title_generation_request': 'You are naming a coding session' in serialized,
                    'stream': body.get('stream'), 'body_sha256': hashlib.sha256(raw).hexdigest(),
                    'known_prompt_markers': [p for p in (CONTROL_PROMPT, *COMMANDS) if p in serialized],
                    'purpose_markers': [p for p in ('title', 'suggestion', 'summariz', 'prefix', 'topic')
                                        if p in serialized.lower()]})
                body = json.dumps({'type': 'error', 'error': {
                    'type': 'rate_limit_error', 'code': 'insufficient_quota',
                    'message': REFUSAL}}).encode()
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('x-should-retry', 'false')
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class SyntheticSupervisor:
    """Private authenticated contract fixture, deliberately not real controls."""
    def __init__(self, root: Path):
        self.path = root / 'control.sock'
        self.token = 'synthetic-private-control-token'
        self.requests = []
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(3)
                data = json.loads(self.rfile.readline(8193))
                ok = (data.get('token') == owner.token and
                      data.get('host') in ('codex', 'claude') and
                      data.get('action') in ('register', 'play', 'v', 'host'))
                owner.requests.append({key: data.get(key) for key in
                                       ('host', 'action', 'game', 'session_id')})
                message = ('Synthetic supervisor accepted ' + str(data.get('action')) +
                           '; no menu, game, billing switch, or spending performed.')
                self.wfile.write(json.dumps({'ok': ok, 'message': message}).encode() + b'\n')

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def configure_hooks(host, home, work, env, sandbox, events):
    # Call the actual installer generator in a clean child environment. Only its
    # hooks JSON is retained; no MCP/game helper or personal profile is executed.
    generated = subprocess.run(
        ['/usr/bin/sandbox-exec', '-p', sandbox, sys.executable, str(Path(__file__).resolve()),
         '--fixture-hooks', host], env=env, cwd=work, capture_output=True, text=True,
        timeout=15, check=True)
    hooks = json.loads(generated.stdout)
    command = hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    if 'node' not in command or '--billing-hook' not in command:
        raise RuntimeError('The real Node hook must be available; Python fallback is not proof.')
    wrapper = home / 'observe-hook.cjs'
    wrapper.write_text(
        "const fs=require('node:fs'), cp=require('node:child_process');\n"
        "const input=fs.readFileSync(0,'utf8');\n"
        f"const r=cp.spawnSync('/bin/sh',['-c',{json.dumps(command)}],"
        "{input,encoding:'utf8',timeout:7000});\n"
        "let d={}, result={}; try{d=JSON.parse(input)}catch{};"
        "try{result=JSON.parse(r.stdout)}catch{};\n"
        f"fs.appendFileSync({json.dumps(str(events))},JSON.stringify({{"
        "event:d.hook_event_name,prompt:d.prompt,session_id:d.session_id,"
        "result,exit:r.status,error:r.error?.code})+'\\n');\n"
        "process.stdout.write(r.stdout||''); process.stderr.write(r.stderr||'');"
        "process.exit(r.status===null?1:r.status);\n")
    for entries in hooks['hooks'].values():
        entries[0]['hooks'][0]['command'] = shlex.join([shutil.which('node'), str(wrapper)])
    config = home / host
    (config / ('hooks.json' if host == 'codex' else 'settings.json')).write_text(json.dumps(hooks))
    if host == 'claude':
        # Generated fixture onboarding/trust only; never alter personal files.
        (config / '.claude.json').write_text(json.dumps({
            'hasCompletedOnboarding': True, 'theme': 'dark',
            'projects': {str(work): {'hasTrustDialogAccepted': True}},
            'customApiKeyResponses': {'approved': ['synthetic-not-a-real-provider-key']}}))
    else:
        (config / 'config.toml').write_text(
            '[projects.' + json.dumps(str(work)) + ']\ntrust_level = "trusted"\n')
    return hashlib.sha256(command.encode()).hexdigest()


class NativePTY:
    def __init__(self, argv, env, cwd):
        # Keep test collection/import portable; run_probe rejects non-macOS
        # execution before this native-only implementation is instantiated.
        import fcntl
        import pty
        import termios

        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 42, 140, 0, 0))
        self.process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=slave, stdout=slave,
                                        stderr=slave, start_new_session=True)
        os.close(slave)
        self.output = b''

    def pump(self, seconds=.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.master], [], [], min(.1, end - time.monotonic()))[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        return
                    raise
                self.output += data
                if b'\x1b[6n' in data:
                    os.write(self.master, b'\x1b[1;1R')
                if b'\x1b]11;?' in data:
                    os.write(self.master, b'\x1b]11;rgb:0000/0000/0000\x1b\\')

    def until(self, condition, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end and self.process.poll() is None:
            self.pump()
            if condition():
                return True
        return bool(condition())

    def send(self, text):
        os.write(self.master, text.encode())

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
        os.close(self.master)


def launch_argv(binary, host, url, mode, prompt):
    sys.path.insert(0, str(ROOT))
    from openvegas.integrations.native import host_argv
    if host == 'codex':
        # Fixture-only trust for precisely the generated hook; never shipped.
        args = ['--no-daemon', '--dangerously-bypass-hook-trust']
        args += (['exec', '--skip-git-repo-check', '--sandbox', 'read-only', '--json', prompt]
                 if mode == 'cli' else ['--no-alt-screen', '--sandbox', 'read-only', prompt])
        model = 'openai/gpt-4.1-nano'
    else:
        args = (['--print', '--output-format', 'json', '--max-turns', '1', '--tools', '', '--', prompt]
                if mode == 'cli' else ['--tools', '', '--', prompt])
        model = 'anthropic/claude-haiku-4.5'
    return host_argv(binary, host, url, model, args, local_tools_only=host == 'codex')


def run_probe(host, mode='interactive', missing_supervisor=False, commands=COMMANDS,
              disable_terminal_title=False, cold_start=False):
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists():
        raise RuntimeError('This probe requires macOS sandbox-exec; never run without it.')
    binary = shutil.which(host)
    if not binary or not shutil.which('node'):
        raise RuntimeError('Installed native host and Node required.')
    personal_home = Path.home()
    report = {'host': host, 'mode': mode, 'missing_supervisor': missing_supervisor,
              'disable_terminal_title': disable_terminal_title,
              'cold_start': cold_start,
              'created_at_utc': datetime.now(UTC).isoformat(),
              'python_version': sys.version.split()[0],
              'source_revision': subprocess.check_output(
                  ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
              'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (Path(__file__).resolve(),
                                             ROOT / 'openvegas/integrations/assets/billing-hook.js',
                                             ROOT / 'openvegas/integrations/use.py',
                                             ROOT / 'openvegas/integrations/installer.py')},
              'real_subscription_quota_cache_proven': False, 'commands': [],
              'limits': ['Synthetic API auth and loopback 429, not subscription quota cache.',
                         'Synthetic supervisor acknowledgement is not a rendered local menu.',
                         'Generated source hook, not proof the published plugin contains it.']}
    transcript = ''
    with tempfile.TemporaryDirectory(prefix='ov-zq-', dir='/private/tmp') as temp:
        root = Path(temp)
        root.chmod(0o700)
        home, work = root / 'home', root / 'work'
        home.mkdir()
        work.mkdir()
        sandbox = sandbox_profile(root, personal_home)
        with RejectingProvider() as provider, SyntheticSupervisor(root) as supervisor:
            env = clean_environment(home, provider.url)
            if disable_terminal_title:
                env['CLAUDE_CODE_DISABLE_TERMINAL_TITLE'] = '1'
            network_check = subprocess.run(
                ['/usr/bin/sandbox-exec', '-p', sandbox, sys.executable, '-I', '-c',
                 ('import socket,errno\ns=socket.socket();s.settimeout(1)\n'
                 'try:s.connect(("192.0.2.1",9))\n'
                 'except OSError as e:print(e.errno);raise SystemExit(0 if e.errno==errno.EPERM else 1)\n'
                 'raise SystemExit(2)')], env=env, cwd=work, capture_output=True, text=True,
                timeout=5, check=False)
            if network_check.returncode != 0:
                raise RuntimeError('External network OS-deny check failed; no host launched: ' +
                                   network_check.stdout + network_check.stderr)
            report['external_network_denial_errno'] = int(network_check.stdout.strip())
            if not missing_supervisor:
                env.update(OPENVEGAS_SWITCH_SOCKET=str(supervisor.path),
                           OPENVEGAS_SWITCH_TOKEN=supervisor.token)
            version = subprocess.run(['/usr/bin/sandbox-exec', '-p', sandbox, binary, '--version'],
                                     env=env, cwd=work, capture_output=True, text=True, timeout=15,
                                     check=False)
            report['version'] = version.stdout.strip()
            if EXPECTED_VERSIONS[host] not in report['version']:
                raise RuntimeError('Required installed host version not found: ' +
                                   report['version'] + '\n' + version.stderr)
            events = root / 'events.jsonl'
            report['generated_hook_sha256'] = configure_hooks(host, home, work, env, sandbox, events)
            report['isolation'] = {'disposable_home': True, 'external_network': 'OS denied',
                                   'personal_native_config': 'OS read denied',
                                   'hook_trust': 'generated fixture only'}
            if mode == 'cli':
                control = (subprocess.CompletedProcess([], 0, '', '') if cold_start else subprocess.run(
                    ['/usr/bin/sandbox-exec', '-p', sandbox,
                     *launch_argv(binary, host, provider.url, mode, CONTROL_PROMPT)],
                    env=env, cwd=work, capture_output=True, text=True, timeout=35, check=False))
                report['control_provider_posts'] = len(provider.posts)
                report['native_429_displayed'] = (REFUSAL in control.stdout + control.stderr or
                    'Quota exceeded.' in control.stdout + control.stderr)
                transcript += '\n--- ordinary control ---\n' + control.stdout + control.stderr
                for command in commands:
                    before = len(provider.posts)
                    argv = launch_argv(binary, host, provider.url, mode, command)
                    result = subprocess.run(['/usr/bin/sandbox-exec', '-p', sandbox, *argv],
                                            env=env, cwd=work, capture_output=True, text=True, timeout=35,
                                            check=False)
                    transcript += f'\n--- {command} ---\n' + result.stdout + result.stderr
                    matching = [e for e in read_events(events) if e.get('prompt') == command]
                    report['commands'].append({'command': command, 'hook': matching[-1:] or None,
                                               'provider_posts_delta': len(provider.posts) - before,
                                               'returncode': result.returncode})
            else:
                argv = launch_argv(binary, host, provider.url, mode,
                                   commands[0] if cold_start else CONTROL_PROMPT)
                terminal = NativePTY(['/usr/bin/sandbox-exec', '-p', sandbox, *argv], env, work)
                try:
                    report['pid'] = terminal.process.pid
                    approved_fixture_key = False

                    def refusal_ready():
                        nonlocal approved_fixture_key
                        if (host == 'claude' and not approved_fixture_key and
                                b'ANTHROPIC_API_KEY' in terminal.output and b'Detected' in terminal.output):
                            # Select Yes for the synthetic key in this disposable
                            # host's real confirmation dialog, never native auth.
                            terminal.send('\x1b[A\r')
                            approved_fixture_key = True
                        text = ANSI.sub('', terminal.output.decode(errors='replace'))
                        compact = re.sub(r'\s+', '', text)
                        if cold_start:
                            return any(e.get('prompt') == commands[0] for e in read_events(events))
                        return len(provider.posts) > 0 and (
                            'ZERO_QUOTA_FIXTURE_429' in compact or 'Quotaexceeded.' in compact)

                    reached = terminal.until(refusal_ready, 35)
                    report['synthetic_key_confirmed_in_fixture'] = approved_fixture_key
                    report['native_429_displayed'] = reached and not cold_start
                    report['control_provider_posts'] = 0 if cold_start else len(provider.posts)
                    # Do not infer readiness from an echoed input line: require
                    # a matching UserPromptSubmit result and stable POST count.
                    if reached:
                        terminal.pump(2)
                        if cold_start:
                            matching = [e for e in read_events(events) if e.get('prompt') == commands[0]]
                            report['commands'].append({'command': commands[0], 'hook': matching[-1:],
                                'provider_posts_delta': len(provider.posts), 'hook_observed': True,
                                'process_alive': terminal.process.poll() is None})
                        else:
                            report['control_provider_posts'] = len(provider.posts)
                        for command in commands[1:] if cold_start else commands:
                            before = len(provider.posts)
                            event_count = len(read_events(events))
                            # Claude restores a blocked prompt into its editor.
                            # Replace it, rather than accidentally testing a
                            # concatenation of the old and new command.
                            terminal.send('\x15')
                            terminal.pump(.1)
                            terminal.send(command)
                            terminal.pump(.2)
                            terminal.send('\r')
                            handled = terminal.until(lambda command=command, event_count=event_count: any(
                                e.get('prompt') == command for e in read_events(events)[event_count:]), 12)
                            terminal.pump(1.5)
                            matching = [e for e in read_events(events)[event_count:]
                                        if e.get('prompt') == command]
                            report['commands'].append({'command': command, 'hook': matching[-1:] or None,
                                'provider_posts_delta': len(provider.posts) - before,
                                'process_alive': terminal.process.poll() is None, 'hook_observed': handled})
                    report['same_process_alive'] = terminal.process.poll() is None
                    transcript = ANSI.sub('', terminal.output.decode(errors='replace'))
                finally:
                    terminal.close()
            report['provider_posts'] = provider.posts
            report['provider_gets'] = provider.gets
            report['supervisor_requests'] = supervisor.requests
            report['hook_events'] = read_events(events)
    report['passed'] = len(report['commands']) == len(commands) and all(
        item['provider_posts_delta'] == 0 and item['hook'] and
        (item['hook'][0]['result'].get('decision') == 'block' if missing_supervisor else
         item['hook'][0]['result'].get('continue') is False)
        for item in report['commands'])
    report['passed'] = bool(report['passed'] and (cold_start or
                            (report['native_429_displayed'] and report['control_provider_posts'] > 0)))
    report['passed'] = bool(report['passed'] and len(report['provider_posts']) ==
                            (0 if cold_start else report['control_provider_posts']))
    if mode == 'interactive':
        report['passed'] = bool(report['passed'] and report['same_process_alive'])
    return report, transcript


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['codex', 'claude'], default='codex')
    parser.add_argument('--mode', choices=['cli', 'interactive'], default='interactive')
    parser.add_argument('--missing-supervisor', action='store_true')
    parser.add_argument('--disable-terminal-title', action='store_true',
                        help='Explicit fixture-only Claude host-internal title-request experiment')
    parser.add_argument('--cold-start', action='store_true',
                        help='Submit exact command first, without an ordinary control prompt or prior 429')
    parser.add_argument('--command', action='append', dest='commands')
    parser.add_argument('--output', type=Path, help='Unique report prefix; refuses overwrites')
    parser.add_argument('--fixture-hooks', choices=['codex', 'claude'], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture_hooks:
        sys.path.insert(0, str(ROOT))
        from openvegas.integrations.installer import bundle_files
        files = bundle_files({'host': args.fixture_hooks, 'mod': False}, 'zero-quota-fixture')
        print(files['plugins/openvegas/hooks/hooks.json'])
        return 0
    report, transcript = run_probe(args.host, args.mode, args.missing_supervisor,
                                   args.commands or COMMANDS, args.disable_terminal_title, args.cold_start)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        for suffix, content in (('.json', json.dumps(report, indent=2) + '\n'),
                                ('.txt', transcript)):
            with Path(str(args.output) + suffix).open('x') as handle:
                handle.write(content)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
