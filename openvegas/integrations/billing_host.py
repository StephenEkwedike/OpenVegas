"""Same-terminal native process supervisor, not an agent or alternate chat UI.

Only an exact pre-model command queues a switch. The supervisor owns the child;
it never discovers/kills unrelated processes, edits auth files or injects input.
"""
from __future__ import annotations

from openvegas.integrations.game_result import amount as format_v

import asyncio
import json
import os
import re
import secrets
import shutil
import tempfile
import termios
import tomllib
from pathlib import Path
from uuid import UUID

import click

from . import native
from .broker import Broker
from .credentials import IntegrationError
from .use import SOCKET, TOKEN


def session_id(value):
    try:
        return str(UUID(value)) if isinstance(value, str) and str(UUID(value)) == value else None
    except ValueError:
        return None


def resume_args(host, args, identifier, model=None):
    """Preserve permissions/tool options, never replay an original task prompt."""
    if not session_id(identifier):
        raise IntegrationError('Native conversation ID is unavailable; billing unchanged.')
    result, index = [], 0
    if host == 'codex' and 'resume' in args:
        args = list(args)
        at = args.index('resume')
        args.pop(at)
        if at < len(args) and session_id(args[at]):
            args.pop(at)
    while index < len(args):
        arg = args[index]
        if arg in ('--continue', '--last', '-c' if host == 'claude' else '--all'):
            index += 1
            continue
        if arg in ('--resume', '-r') and host == 'claude':
            index += 2 if index + 1 < len(args) and session_id(args[index + 1]) else 1
            continue
        if arg in ('--model', '-m'):
            index += 2
            continue
        if arg.startswith('--model='):
            index += 1
            continue
        if not arg.startswith('-') or arg == '--':
            raise IntegrationError('This launch contains a task prompt or unsupported command; no switch was made.')
        key = arg.split('=')[0]
        result.append(arg)
        if key in native._VALUE_OPTIONS[host] and '=' not in arg:
            if index + 1 >= len(args):
                raise IntegrationError('Incomplete host option; billing unchanged.')
            result.append(args[index + 1])
            index += 1
        index += 1
    native.validate_host_args(host, result)
    if host == 'codex':
        return [*result, 'resume', identifier, *(['--model', model] if model else [])]
    return [*result, '--resume', identifier, *(['--model', model] if model else [])]


class Supervisor:
    def __init__(self, host, executable, args, env, *, emit=click.echo):
        self.host, self.executable, self.args, self.env = host, executable, list(args), dict(env)
        self.emit = emit
        self.child = None
        self.identifier = self.original_model = None
        self.mode, self.pending = 'host', None
        self.v_child_started = False
        self.token = secrets.token_urlsafe(32)
        self.changed = asyncio.Event()
        self.original_provider = None
        self.wallet_credentials = None
        if host == 'codex':
            # Read routing labels only. Saved authentication is never read/copied.
            path = Path(env.get('CODEX_HOME', str(Path(env['HOME']) / '.codex'))) / 'config.toml'
            config = tomllib.loads(path.read_text()) if path.is_file() else {}
            self.original_provider = config.get('model_provider', 'openai')
            if not isinstance(self.original_provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', self.original_provider):
                raise IntegrationError('Custom provider configuration requires an explicit native restart.')

    async def handle(self, reader, writer):
        response = {'ok': False, 'message': 'Billing request rejected; no change.'}
        try:
            async with asyncio.timeout(3):
                data = json.loads(await reader.readline())
                if (type(data) is not dict or not isinstance(data.get('token'), str)
                        or not secrets.compare_digest(data['token'], self.token)
                        or data.get('host') not in (None, self.host)
                        or self.child is None or self.child.returncode is not None):
                    raise ValueError
                identifier = session_id(data.get('session_id'))
                if data.get('action') == 'register':
                    if not identifier or self.identifier not in (None, identifier):
                        raise ValueError
                    self.identifier = identifier
                    if self.mode == 'host' and self.original_model is None:
                        model = data.get('model')
                        if isinstance(model, str) and native._MODEL.fullmatch(model):
                            self.original_model = model
                    response = {'ok': True, 'message': 'Native billing command connected.'}
                elif data.get('action') in ('v', 'host'):
                    # Codex may trust plugin hooks after SessionStart. The first
                    # exact prompt supplies the same host-owned identity fields.
                    if self.identifier is None and identifier is not None:
                        self.identifier = identifier
                        model = data.get('model')
                        if isinstance(model, str) and native._MODEL.fullmatch(model):
                            self.original_model = model
                    if identifier is not None and identifier != self.identifier:
                        raise ValueError
                    if not self.identifier or self.pending:
                        raise IntegrationError('Native session is not ready or a switch is already pending.')
                    resume_args(self.host, self.args, self.identifier)
                    target = data['action']
                    if target == self.mode:
                        response = {'ok': True, 'message': 'Already using ' + ('$V.' if target == 'v' else 'host billing.')}
                    else:
                        self.pending = target
                        response = {'ok': True, 'message': 'Opening billing controls in this terminal. No prompt sent.'}
                        # Allow the hook to return its block decision before the
                        # owned child receives a graceful termination request.
                        asyncio.get_running_loop().call_later(0.75, self.changed.set)
                else:
                    raise ValueError
        except IntegrationError as exc:
            response['message'] = str(exc)
        except (OSError, ValueError, TimeoutError, TypeError):
            pass
        try:
            writer.write(json.dumps(response).encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run_child(self, argv, env):
        self.changed.clear()
        attributes = termios.tcgetattr(0) if os.isatty(0) else None
        wallet = None
        if self.host == 'codex' and self.env.get('OPENVEGAS_CODEX_WALLET') != '0':
            from .terminal_wallet import CredentialReader, monitor
            if self.wallet_credentials is None:
                self.wallet_credentials = CredentialReader()
            ready = asyncio.Event()
            wallet = asyncio.create_task(monitor(env, self.mode, ready=ready,
                                                 credentials=self.wallet_credentials))
            try:
                await asyncio.wait_for(ready.wait(), 3.5)
            except TimeoutError:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
                wallet = None
        try:
            self.child = await asyncio.create_subprocess_exec(*argv, env=env)
        except BaseException:
            if wallet:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
            raise
        finished = asyncio.create_task(self.child.wait())
        change = asyncio.create_task(self.changed.wait())
        try:
            await asyncio.wait([finished, change], return_when=asyncio.FIRST_COMPLETED)
            if not finished.done() and self.pending:
                self.child.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(finished), 10)
                except TimeoutError:
                    self.emit('Native host is still closing. No new host or billing route was started.')
                    self.pending = None
            return await finished
        except asyncio.CancelledError:
            self.pending = None
            if self.child.returncode is None:
                self.child.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(finished), 5)
                except TimeoutError:
                    self.child.kill()
                    await self.child.wait()
            raise
        finally:
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)
            if wallet:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
            if attributes is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, attributes)

    async def choose_v(self):
        self.v_child_started = False
        broker = Broker()
        try:
            device = broker.credentials.load()
            if not isinstance(device, str) or not device.startswith('ov_device_'):
                raise IntegrationError('Sign in to OpenVegas first; no provider account is required from you.')
            options = await broker.request('GET', '/native/options?host=' + self.host, token=device)
            if (options.get('host') != self.host or options.get('provider') !=
                    ('anthropic' if self.host == 'claude' else 'openai')):
                raise IntegrationError('The backend returned an invalid native route.')
            models = options.get('models', [])
            if not models:
                raise IntegrationError('No reviewed direct API model is enabled on this backend. Billing unchanged.')
            balance = native._decimal(options.get('balance_v'), allow_zero=True)
            if balance <= 0:
                raise IntegrationError('Your OpenVegas balance is empty; no spending session opened.')
            self.emit('OPENVEGAS / USE $V\nBalance: ' + format_v(balance))
            for i, item in enumerate(models, 1):
                if not isinstance(item.get('model'), str) or not native._MODEL.fullmatch(item['model']):
                    raise IntegrationError('Invalid model options.')
                self.emit(f'{i}. {item["model"]} / {options["provider"]} direct')
            index = click.prompt('Model', type=click.IntRange(1, len(models)), default=1) - 1
            chosen = models[index]
            limit = click.prompt('Session limit ($V)', type=native.Budget(), default=str(min(balance, 50)))
            if limit > balance:
                raise IntegrationError('Session limit exceeds your current balance; no spending started.')
            self.mode = 'v'
            launched = False
            async def runner(argv, env):
                nonlocal launched
                launched = True
                self.v_child_started = True
                return await self.run_child(argv, {**env, SOCKET: self.env[SOCKET], TOKEN: self.token})
            code = await native.launch(self.host, chosen['model'], limit, chosen['max_tokens'],
                provider=options['provider'], broker=broker, emit=self.emit, runner=runner,
                host_args=resume_args(self.host, self.args, self.identifier),
                local_tools_only=self.host == 'codex')
            if not launched:
                self.pending = 'host'  # Cancel before activation restores the original host.
            return code
        finally:
            await broker.close()

    async def run(self):
        with tempfile.TemporaryDirectory(prefix='ov-use-', dir='/tmp') as directory:
            os.chmod(directory, 0o700)
            path = str(Path(directory) / 'switch.sock')
            server = await asyncio.start_unix_server(self.handle, path, limit=8192)
            os.chmod(path, 0o600)
            self.env.update({SOCKET: path, TOKEN: self.token})
            async with server:
                code = await self.run_child([self.executable, *self.args], self.env)
                while self.pending:
                    target, self.pending = self.pending, None
                    if target == 'v':
                        try:
                            code = await self.choose_v()
                        except (IntegrationError, click.Abort) as exc:
                            self.emit(str(exc) if isinstance(exc, IntegrationError) else 'Billing switch cancelled.')
                            # Never reissue a task or switch after a dispatched V child failure.
                            if not self.v_child_started:
                                self.pending = 'host'
                        continue
                    self.mode = 'host'
                    args = resume_args(self.host, self.args, self.identifier, self.original_model)
                    if self.host == 'codex':
                        args = ['-c', 'model_provider=' + json.dumps(self.original_provider), *args]
                    self.emit('Host billing restored. Resuming the same conversation; no prompt submitted.')
                    code = await self.run_child([self.executable, *args], self.env)
                return code


def main(host, args):
    env = dict(native.host_environment() or os.environ)
    # Noninteractive/admin invocations stay completely outside the supervisor.
    if (not os.isatty(0) or any(arg in {'--help', '-h', '--version', '-v', '-p', '--print',
        'exec', 'e', 'login', 'logout', 'auth', 'mcp', 'plugin', 'update'} for arg in args)):
        executable = shutil.which(host)
        if not executable:
            raise IntegrationError('Native executable unavailable.')
        os.execve(executable, [executable, *args], env)
    info = native.detect(host)
    if not info:
        raise IntegrationError('Native executable unavailable.')
    return asyncio.run(Supervisor(host, info['executable'], args, env).run())
