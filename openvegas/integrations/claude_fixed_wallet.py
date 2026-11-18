"""Production Claude entry: native context, fixed payer, existing game shell."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager

import click

from . import native
from .broker import Broker
from .claude_wallet_launch import VERSION, session
from .credentials import IntegrationError


@contextmanager
def game_environment(env):
    # Existing game/menu helpers read the controller's generation from os.environ.
    # Scope only local IPC fields, never the wallet bearer or provider settings.
    names = ('OPENVEGAS_TERMINAL_SOCKET', 'OPENVEGAS_TERMINAL_TOKEN',
             'OPENVEGAS_TERMINAL_OWNER', 'OPENVEGAS_TERMINAL_GENERATION')
    previous = {key: os.environ.get(key) for key in names}
    try:
        for key in names:
            if key in env:
                os.environ[key] = env[key]
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def administrative(args):
    # A print-mode prompt is inference, not an administrative passthrough.
    return bool(args and args[0] in {'--help', '-h', '--version', '-v', '-V', 'auth', 'doctor',
                                    'mcp', 'plugin', 'plugins', 'install', 'update', 'upgrade', 'setup-token'})


def check_args(args):
    # Retain model, effort, tools, cwd and permission arguments byte-for-byte.
    # Unreviewed config/remote routing must fail before a wallet grant exists.
    blocked = {'--settings', '--setting-sources', '--bare', '--cloud', '--environment', '--desktop',
               '--teleport', '--remote-control', '--remote-control-session-name-prefix',
               '--bg', '--background', '--fallback-model'}
    if args and args[0] in native._COMMANDS['claude']:
        raise IntegrationError('Claude wallet launch requires foreground native inference. No host fallback started.')
    for arg in args:
        if arg == '--':
            break
        if arg.partition('=')[0] in blocked:
            raise IntegrationError('Claude routing/config override is not verified for wallet launch. '
                                   'No grant opened; native permissions are unchanged.')


class FixedClaude:
    """Only the payer is fixed. The existing supervisor owns games and the PTY."""
    target = task = switch_status = None

    def __init__(self, supervisor):
        self.s = supervisor

    async def preflight(self):
        raise IntegrationError('Claude billing is fixed for this launch. Exit and explicitly launch another payer.')

    async def arm(self, target):
        if target == self.s.mode:
            return False
        await self.preflight()

    async def cancel_arm(self):
        return None

    def begin(self):
        raise IntegrationError('No Claude payer change was armed.')

    async def controls(self, *, game, menu):
        from .local_controls import controls
        if menu:
            from .native_play import choice_model
            from .terminal_handoff import TerminalView, close_writer, connect
            reader, writer = await connect()
            try:
                view = TerminalView(reader, writer)
                answer = await asyncio.wait_for(view.ask(
                    'OPENVEGAS / LOCAL CONTROLS\nClaude payer is fixed for this launch. '
                    'Play does not place a wager. Esc returns to coding.',
                    choice_model('LocalControls', ['play', 'quit'])), 180)
                await view.send(kind='done')
                reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if not isinstance(reply, dict) or reply.get('closed') is not True:
                    raise IntegrationError('Local controls did not close; no game opened.')
            finally:
                await close_writer(writer)
            if answer is None or answer.choice != 'play':
                return
        await controls(self.s.mode, game=game)

    async def run(self, argv, env):
        env = dict(env)
        # The existing game relay borrows the alternate screen. Use Claude's
        # supported primary-screen default without changing permissions/tools.
        env.setdefault('CLAUDE_CODE_NO_FLICKER', '0')
        if self.s.initial_billing == 'host':
            # Explicit legacy means host-only, not the old two-payer router.
            with game_environment(env):
                return await self.s._terminal_child(argv, env)
        check_args(argv[1:])
        broker = Broker()

        async def child(args, child_env):
            self.s.mode = 'v'
            self.s.transition_state = 'MODE_COMMITTED'
            self.s.wallet_status_changed.set()
            with game_environment(child_env):
                return await self.s._terminal_child(args, child_env)

        try:
            return await session(broker, {'executable': self.s.executable,
                                         'version': self.s.launch_spec.version},
                                 child, self.s.emit, None, managed_args=argv[1:], source_env=env)
        except IntegrationError:
            raise
        except Exception:
            raise IntegrationError('Claude wallet launch failed. No automatic retry or host fallback.') from None
        finally:
            await broker.close()


async def public_launch(args):
    """The `openvegas claude` command joins or creates the ordinary game relay."""
    from .billing_host import Supervisor
    from .installer import host_command
    from .native_child_environment import external_child_environment
    from .wallet_startup import initial_billing

    args = list(args)
    env = external_child_environment(native.host_environment() or os.environ)
    if 'OPENVEGAS_INITIAL_BILLING' in os.environ:
        env['OPENVEGAS_INITIAL_BILLING'] = os.environ['OPENVEGAS_INITIAL_BILLING']
    if args[:1] == ['--host-billing']:
        env['OPENVEGAS_INITIAL_BILLING'] = 'host'
        args.pop(0)
    elif args[:1] == ['--wallet']:
        env['OPENVEGAS_INITIAL_BILLING'] = 'v'
        args.pop(0)
    info = native.detect('claude')
    if not info:
        raise IntegrationError('Native Claude executable unavailable. No grant opened.')
    if administrative(args):
        process = await asyncio.create_subprocess_exec(info['executable'], *args, env=env)
        return await process.wait()
    if not all(os.isatty(fd) for fd in (0, 1, 2)):
        raise IntegrationError('Claude wallet launch needs an interactive terminal. No host fallback started.')
    payer = initial_billing(env)
    if payer == 'v':
        check_args(args)
        if info['version'] != VERSION:
            raise IntegrationError('Claude wallet launch requires exactly 2.1.322. No grant opened.')
    if os.name == 'nt':
        from .windows_customer_claude import run_fixed
        return await run_fixed(info['executable'], env, os.getcwd(), version=info['version'], args=args)
    if not env.get('OPENVEGAS_TERMINAL_SOCKET'):
        from .terminal_relay import run
        # The relay needs inherited routing identifiers, not a copied account or HOME.
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            run([*host_command('claude', env=env), *args])
        finally:
            os.environ.clear()
            os.environ.update(old)
        return 0
    return await Supervisor('claude', info['executable'], args, env,
                            version=info['version'], initial_billing=payer).run()
