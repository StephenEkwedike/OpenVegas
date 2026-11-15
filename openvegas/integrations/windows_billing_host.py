"""Ordinary Windows launch, explicit customer routing, or an isolated rehearsal.

Only the rehearsal uses a disposable profile and synthetic credentials. Never
infer customer consent from the fixture switch or an ordinary native launch.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import click

from . import windows_terminal as terminal
from .credentials import IntegrationError
from .inference_router_adapter import VERSIONS, ProviderAdapter
from .inference_router_fixture import SYNTHETIC_KEY


def find_entry(host, path):
    # Windows SearchPath/shutil.which on older Python can implicitly search
    # the project cwd before PATH. Never trust the project as a host installer.
    for item in path.split(os.pathsep):
        directory = Path(item.strip('"'))
        if not directory.is_absolute() or directory == Path.cwd():
            continue
        for suffix in ('.exe', '.cmd', '.ps1'):
            entry = directory / (host + suffix)
            if entry.is_file():
                return str(entry)
    raise IntegrationError('Native Windows host not found on an absolute PATH entry.')


def ordinary_image(host, entry):
    from .windows_native_image import _locations
    candidates = [str(p) for p in _locations(host, entry) if Path(p).is_file()]
    if len(candidates) != 1:
        raise IntegrationError('Native executable not found unambiguously. Launch your usual host directly.')
    return candidates[0]


class RehearsalAdapter(ProviderAdapter):
    """Retain the shared verb/path/header policy; never dial an official origin."""
    def __init__(self, host, port):
        if type(port) is not int or not 1 <= port <= 65535 or host not in VERSIONS:
            raise IntegrationError('A local native fixture and supported host are required.')
        base = 'http://127.0.0.1:' + str(port)
        super().__init__(host, 'https://api.openai.com/v1' if host == 'codex'
                         else 'https://api.anthropic.com', base + '/integrations/native')

    def request_plan(self, method, path, headers, mode, wallet_token=None):
        # Check before wallet credential substitution, including WS handshakes.
        entries = headers.items() if isinstance(headers, dict) else headers
        auth = [(k.lower(), v) for k, v in entries if k.lower() in {
            'authorization', 'x-api-key', 'cookie', 'chatgpt-account-id',
            'openai-organization', 'openai-project', 'proxy-authorization'}]
        if auth not in ([('authorization', 'Bearer ' + SYNTHETIC_KEY)], [('x-api-key', SYNTHETIC_KEY)]):
            raise IntegrationError('Non-fixture native authentication refused.')
        if mode == 'v' and not (wallet_token or '').startswith('ov_native_fixture_'):
            raise IntegrationError('Non-fixture wallet access refused.')
        plan = super().request_plan(method, path, headers, mode, wallet_token)
        upstream = urlsplit(plan.endpoint)
        local = urlsplit(self.wallet_base)
        scheme = 'ws' if plan.transport == 'websocket' else 'http'
        suffix = ('/_host' if mode == 'host' else '') + upstream.path
        return replace(plan, endpoint=f'{scheme}://{local.netloc}{suffix}'
                       + ('?' + upstream.query if upstream.query else ''))


def _fixture_env(host, root, system):
    env = {'SystemRoot': str(system), 'WINDIR': str(system), 'PATH': str(system / 'System32'),
           'ComSpec': str(system / 'System32' / 'cmd.exe'), 'HOME': str(root), 'USERPROFILE': str(root),
           'APPDATA': str(root / 'roaming'), 'LOCALAPPDATA': str(root / 'local'),
           'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'CODEX_HOME': str(root / 'codex'),
           'CLAUDE_CONFIG_DIR': str(root / 'claude'), 'DISABLE_TELEMETRY': '1',
           'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
           'CLAUDE_CODE_DISABLE_TERMINAL_TITLE': '1'}
    env['OPENAI_API_KEY' if host == 'codex' else 'ANTHROPIC_API_KEY'] = SYNTHETIC_KEY
    return env


_DIRS = ('codex', 'claude', 'roaming', 'local', 'tmp', 'work')


def _fixture_policy(host, env, cwd):
    """No personal profile, unknown env override, or prepopulated project."""
    root, system = Path(env.get('USERPROFILE', '')), Path(env.get('SystemRoot', ''))
    if (host not in VERSIONS or not root.is_absolute() or not system.is_absolute()
            or not re.fullmatch('ov-rehearsal-[0-9a-f]{32}', root.name)
            or Path(cwd) != root / 'work' or env != _fixture_env(host, root, system)
            or {p.name for p in root.iterdir()} != set(_DIRS)
            or any(any((root / name).iterdir()) for name in _DIRS)):
        raise IntegrationError('Unverified synthetic Windows profile; no routed launch.')
    return True


@contextmanager
def fresh_environment(host):
    from openvegas.emotes._windows_state import private_windows_directory
    root = Path(tempfile.gettempdir()) / ('ov-rehearsal-' + secrets.token_hex(16))
    # Leave the disposable profile for inspection. Do not recursively delete a
    # tree the tool-capable native child could have populated with junctions.
    if root.exists():
        raise IntegrationError('Disposable Windows workspace collision; no launch.')
    with ExitStack() as stack:
        stack.enter_context(private_windows_directory(root))
        for name in _DIRS:
            directory = stack.enter_context(private_windows_directory(root / name))
            if directory.names(1):
                raise IntegrationError('Disposable native profile is not empty.')
        system = Path(os.environ['SystemRoot'])
        yield _fixture_env(host, root, system), str(root / 'work')


def _spawn(argv, env, cwd):
    return subprocess.Popen(argv, env=env, cwd=cwd, shell=False)


def _prepare_rehearsal_claude(env, cwd):
    from openvegas.emotes._windows_state import private_windows_directory
    root = Path(env['USERPROFILE']).resolve(strict=True)
    work = Path(cwd).resolve(strict=True)
    if (work != root / 'work' or not re.fullmatch('ov-rehearsal-[0-9a-f]{32}', root.name)
            or Path(env['CLAUDE_CONFIG_DIR']).resolve(strict=True) != root / 'claude'
            or env.get('ANTHROPIC_API_KEY') != SYNTHETIC_KEY):
        raise IntegrationError('Invalid disposable rehearsal workspace.')
    with (private_windows_directory(work) as project,
          private_windows_directory(root / 'claude') as profile):
        if project.names(1) or profile.names(1):
            raise IntegrationError('Disposable Claude workspace and profile must be empty.')
        data = json.dumps({
            'hasCompletedOnboarding': True, 'theme': 'dark',
            'customApiKeyResponses': {'approved': [SYNTHETIC_KEY[-20:]]},
            'projects': {work.as_posix(): {'hasTrustDialogAccepted': True}},
        }, ensure_ascii=True).encode('ascii')
        profile.atomic_write('.claude.json', data)
        if profile.read('.claude.json', len(data)) != data:
            raise IntegrationError('Disposable Claude setup could not be verified.')


class WindowsConsoleHost:
    """Own one foreground process and one authenticated, request-scoped router."""
    def __init__(self):
        self.process = None

    def alive(self, *_):
        if self.process is None or self.process.poll() is not None:
            raise IntegrationError('Native Windows child is no longer live; no payer commit.')

    async def run(self, session, argv, env, cwd, directory, announce):
        host = session.adapter.host
        if (self.process is not None or host not in VERSIONS or not session.state.request_boundary
                or session.router.auth_mode != ('peer' if host == 'codex' else 'header')):
            raise IntegrationError('Native Windows terminal requires its own authenticated request router.')

        async def command(action):
            self.alive()
            if action != 'status':
                result = await session.command('openvegas use ' + action)
                if result.billing != action:
                    return 'held' if session.state.pending is not None else 'refused'
            return 'held' if session.state.pending is not None else session.state.mode

        # Keep the actual process handle for WindowsTCPPeerAuth; never reopen by PID.
        async with terminal.controls(command, directory=directory) as sid:
            announce(sid)  # No concurrent terminal output over the native UI.
            process = self.process = _spawn(argv, env, cwd)
            try:
                if host == 'codex':
                    session.bind_native(process)
                while process.poll() is None:
                    await asyncio.sleep(.05)
                return process.returncode
            finally:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.to_thread(process.wait)


async def run_routed(host, executable, port, env, cwd, directory, announce):
    from .inference_router_session import RouterSession, WalletAccess

    owner = WindowsConsoleHost()
    async def prepare(_ticket):
        owner.alive()
        return WalletAccess('ov_native_fixture_' + secrets.token_hex(16), time.time() + 3600)

    async def revoke(_access):
        return True  # In-process synthetic capability only; there was no financial reservation.

    session = RouterSession(RehearsalAdapter(host, port), request_boundary=True,
                            prepare_wallet=prepare, revoke_wallet=revoke, commit_guard=owner.alive,
                            local_auth='peer' if host == 'codex' else 'header')
    # Only this isolated synthetic path uses an overlay, not a production policy assertion.
    async with session.launch(version=VERSIONS[host], env=env, args=[],
                              effective_policy_verified=_fixture_policy(host, env, cwd)) as (child_env, argv):
        if host == 'claude':
            _prepare_rehearsal_claude(env, cwd)
            child_env = dict(child_env, TERM="xterm-256color")

        return await owner.run(session, [executable, *argv], child_env, cwd, directory, announce)


def launch(host, args=(), *, fixture_port=None, managed=False):
    from .windows_native_image import pinned_windows_image

    if sys.platform != 'win32' or host not in VERSIONS:
        raise IntegrationError('Native Windows console and a supported host are required.')
    if os.environ.get('OPENVEGAS_NATIVE_FIXTURE') is not None:
        raise IntegrationError('Do not mix POSIX fixtures with the separate Windows rehearsal.')
    entry = find_entry(host, os.environ.get('PATH', ''))
    if managed:
        if args or fixture_port is not None:
            raise IntegrationError('Managed Windows billing accepts no fixture, prompt or launch overrides.')
        from . import windows_customer
        from .native import host_environment
        with terminal.console_modes(), pinned_windows_image(host, entry, VERSIONS[host]) as exe:
            try:
                return asyncio.run(windows_customer.run(host, exe, host_environment() or os.environ, os.getcwd()))
            except (OSError, ImportError, ValueError, TypeError, KeyError, TimeoutError):
                raise IntegrationError('Managed Windows billing unavailable. No automatic payer fallback.') from None
    if fixture_port is not None and (args or type(fixture_port) is not int or not 1 <= fixture_port <= 65535):
        raise IntegrationError('The isolated Windows rehearsal accepts no native overrides or prompt.')
    if fixture_port is None:
        # Do not pin a version, create an overlay, or open a wallet on the ordinary path.
        from .host_invocation import passthrough
        from .native import host_environment, run_host
        exe = ordinary_image(host, entry)
        if not os.isatty(0) or passthrough(host, args):
            return asyncio.run(run_host([exe, *args], host_environment() or os.environ))
        with terminal.console_modes():
            return asyncio.run(run_host([exe, *args], host_environment() or os.environ))
    with (pinned_windows_image(host, entry, VERSIONS[host]) as exe, terminal.console_modes(),
          fresh_environment(host) as (env, cwd), terminal._directory() as directory):
        def announce(sid):
            click.echo('Synthetic payer rehearsal only. From another terminal: '
                       f'ov integration terminal --windows-session {sid} --payer status '
                       '(replace status with host or v to switch).', err=True)
        return asyncio.run(run_routed(host, exe, fixture_port, env, cwd, directory, announce))
