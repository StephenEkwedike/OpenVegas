"""Same-terminal native process supervisor, not an agent or alternate chat UI.

Only an exact pre-model command queues a switch. The supervisor owns the child;
it never discovers/kills unrelated processes, edits auth files or injects input.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
import shutil
import tempfile
import termios
import tomllib
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from uuid import UUID

import click

from . import native
from .billing_choices import model_choices, wallet_ceiling
from .broker import Broker
from .credentials import IntegrationError
from .host_adapter import HostLaunchSpec
from .use import SOCKET, TOKEN

ARM_SECONDS = 60


def _codex_management():
    # Mixed-version installations keep the ordinary native host; they do not
    # silently omit a policy argument when creating a wallet grant.
    try:
        from . import codex_billing_route, codex_handoff_policy
        if (not callable(codex_billing_route.host_server)
                or not all(callable(getattr(codex_handoff_policy, name, None))
                           for name in ('check_source_config', 'observe_intent', 'capture_source', 'assert_same_policy',
                                        'verify_target', 'wait_until_idle'))
                or 'codex_handoff_source' not in inspect.signature(native.launch).parameters):
            raise AttributeError
    except (ImportError, AttributeError, ValueError, TypeError):
        raise IntegrationError('Managed Codex policy verifier unavailable. Stay on the original native host.') from None
    return codex_billing_route.host_server, codex_handoff_policy


@dataclass(frozen=True)
class ExitIntent:
    source: str
    target: str
    identifier: str
    generation: int
    expires: float
    host_model: str | None = None
    guard: object = field(default=None, repr=False, compare=False)


def exact_model(value):
    return (isinstance(value, str) and native._MODEL.fullmatch(value) is not None
            and value.lower() not in {'auto', 'default', 'unknown', 'opus', 'sonnet', 'haiku',
                                      'opusplan', 'fable', 'best'})


def require_same_model(model, observed):
    if not exact_model(model) or model != observed:
        raise IntegrationError('The active native model is unknown or changed. Payer unchanged; '
                               'no model substituted or prompt replayed. Use the native /model picker '
                               'and request the switch again once the exact model is known.')
    return model


def wallet_row(host, options, model):
    require_same_model(model, model)
    rows = options['models']
    if model_choices(host, rows, model).selected_model != model:
        raise IntegrationError(f'{model} is not available on this wallet route. Payer unchanged; no '
                               'model substituted or prompt replayed. Keep your model or use the native /model picker.')
    return next(row for row in rows if row['model'] == model)


def live_wallet(options):
    policy = options.get('wallet_limit_policy')
    if policy is None:
        return False
    if policy == 'live_balance':
        return True
    raise IntegrationError('Unknown wallet limit policy. Payer unchanged; no spending session opened.')


def session_id(value):
    try:
        return str(UUID(value)) if isinstance(value, str) and str(UUID(value)) == value else None
    except ValueError:
        return None


def resume_args(host, args, identifier, model=None):
    """Preserve permissions/tool options, never replay an original task prompt."""
    if not session_id(identifier):
        raise IntegrationError('Native conversation ID is unavailable; billing unchanged.')
    result, index, command_seen = [], 0, False
    while index < len(args):
        arg = args[index]
        if host == 'codex' and arg == 'resume' and not command_seen:
            command_seen = True
            index += 1
            if index < len(args) and session_id(args[index]):
                index += 1
            continue
        if arg.partition('=')[0] in {'--fork-session', '--session-id', '--worktree', '-w'}:
            raise IntegrationError('Session-creating host options cannot preserve this conversation; billing unchanged.')
        if arg in ('--continue', '--last', '-c' if host == 'claude' else '--all'):
            index += 1
            continue
        if arg in ('--resume', '-r') and host == 'claude':
            index += 2 if index + 1 < len(args) and session_id(args[index + 1]) else 1
            continue
        if arg in ('--model', '-m'):
            if index + 1 >= len(args) or args[index + 1].startswith('-'):
                raise IntegrationError('Incomplete host model option; billing unchanged.')
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
    def __init__(self, host, executable, args, env, *, emit=click.echo, version='unverified'):
        self.host, self.executable, self.args, self.env = host, executable, list(args), dict(env)
        self.emit = emit
        self.child = None
        self.identifier = self.original_model = None
        self.mode, self.pending = 'host', None
        self.v_child_started = False
        self.v_child_ready = False
        self.token = secrets.token_urlsafe(32)
        self.changed = asyncio.Event()
        self.original_provider = None
        self.wallet_credentials = None
        self.local_task = None
        self.local_error = False
        self.checking_v = False
        self.generation = 0
        self.transition_state = 'HOST_ACTIVE'
        self.runtime_lock = asyncio.Lock()
        self.session_lease = None
        self.relay_owner = secrets.token_hex(16)
        self.modern_relay = False
        self.wallet_status_changed = asyncio.Event()
        self._armed = None
        self.provisional = self.provisional_task = None
        self.provisional_reason = None
        self._undelivered = None
        self._launch_deadline = None
        self.registered_generation = None
        self.restore_model = None
        self.intent_revision = 0
        self.codex_endpoint = None
        self.codex_exit = None
        self.codex_original = None
        self.codex_unavailable = None
        self.wallet_endpoint = None
        self.live_owner = None
        self.claude_owner = None
        from .host_quota import HostQuota
        self.quota = HostQuota(host, env)
        if host == 'codex':
            # Read routing labels only. Saved authentication is never read/copied.
            path = Path(env.get('CODEX_HOME', str(Path(env['HOME']) / '.codex'))) / 'config.toml'
            config = tomllib.loads(path.read_text()) if path.is_file() else {}
            self.original_provider = config.get('model_provider', 'openai')
            if not isinstance(self.original_provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', self.original_provider):
                raise IntegrationError('Custom provider configuration requires an explicit native restart.')
        self.launch_spec = HostLaunchSpec(host, executable, version, tuple(args), os.getcwd(),
                                          self.original_provider)

    @property
    def armed(self):
        return self._armed

    @armed.setter
    def armed(self, item):
        if self._armed is not item:
            self._armed = item
            self.wallet_status_changed.set()

    def active_intent(self):
        item = self.armed
        if item and (item.expires <= monotonic() or item.generation != self.generation
                     or item.identifier != self.identifier or item.source != self.mode):
            self.armed = None
            return None
        return item

    def require_arm(self):
        if self.host == 'codex' and self.codex_endpoint is not None:
            self.launch_spec.managed_codex_client(self.env)
            self.launch_spec.resume_arguments(self.identifier)
        else:
            if self.host == 'codex' and self.codex_unavailable:
                raise IntegrationError(self.codex_unavailable + ' Stay on host billing or start a fresh ov codex --wallet.')
            self.launch_spec.require_exit_resume(self.identifier, self.env)
        if (not self.modern_relay or self.session_lease is None or self.registered_generation != self.generation
                or not os.isatty(0) or not os.isatty(1)):
            raise IntegrationError('A registered native session in a current OpenVegas terminal is required. '
                                   'Restart the managed terminal; ordinary host billing is unchanged.')
        # Evaluate the same sanitizer before asking the user to leave their host.
        # This only builds a dictionary; no endpoint, credentials or grant needed.
        if self.host == 'claude':
            self.launch_spec.verify_direct_environment(self.env, native.child_environment('claude', '', '', 1, ''))

    async def check_live_source(self, *, preview=False):
        if self.host != 'codex':
            return
        _, policy = _codex_management()
        endpoint, identifier, child, gen = self.codex_endpoint, self.identifier, self.child, self.generation
        if not endpoint or child is None or child.returncode is not None:
            raise IntegrationError('Managed Codex source is not live; no switch armed.')
        if preview:
            # An active command hook may be observed here. This bounded preview
            # can refuse before even GET /options; it never arms a ticket.
            try:
                async with asyncio.timeout(1):
                    evidence = await policy.observe_intent(endpoint, identifier)
            except TimeoutError:
                raise IntegrationError('Codex policy preview timed out. Remain on the current route; '
                                       'no billing lookup or grant started.') from None
        else:
            evidence = await policy.capture_source(endpoint, identifier)
        if getattr(evidence, 'thread_id', None) != identifier or getattr(evidence, 'cwd', None) != self.launch_spec.cwd:
            raise IntegrationError('Managed Codex session or directory differs; no switch armed.')
        if ((endpoint, identifier, child, gen) != (self.codex_endpoint, self.identifier, self.child, self.generation)
                or child.returncode is not None):
            raise IntegrationError('Managed Codex source changed; no switch armed.')
        return getattr(evidence, 'model', None)

    def source_model(self, observed, hook_model=None):
        if self.host == 'claude' and not exact_model(hook_model):
            raise IntegrationError('Claude did not report the exact active model for this command '
                                   '(model unknown or changed). This chat stays on its current payer. '
                                   'Selecting /model alone cannot authorize a billing switch.')
        model = observed if self.host == 'codex' else hook_model
        require_same_model(model, model)
        if self.host == 'codex' and exact_model(hook_model):
            require_same_model(model, hook_model)
        if self.mode == 'v' and not (self.host == 'codex' and self.live_owner is not None):
            # Legacy exit/resume lacks the live transport's bound selection
            # proof. Never restore an older preference on that path.
            require_same_model(model, self.restore_model)
        return model

    def cancel_provisional(self):
        if self.provisional and self.provisional.guard is not None:
            self.provisional.guard.release()
        self.provisional = None
        self.provisional_reason = None
        task, self.provisional_task = self.provisional_task, None
        if task is not None and not task.done():
            task.cancel()
        return task

    async def verify_provisional(self, item):
        # The native hook has received its stop/ack. Backoff only paces reads;
        # only a successful current private-server inventory can arm this ticket.
        transferred = False
        try:
            # A brief settlement window for the just-stopped hook and native
            # idle cleanup; don't arm later because unrelated work finished.
            async with asyncio.timeout(min(4., max(0, item.expires - monotonic()))):
                while self.provisional is item and monotonic() < item.expires:
                    if (self.identifier != item.identifier or self.generation != item.generation
                            or self.mode != item.source or not self.modern_relay or self.session_lease is None
                            or self.child is None or self.child.returncode is not None):
                        return
                    try:
                        model = await self.check_live_source()
                    except IntegrationError as exc:
                        if self.provisional is item:
                            self.provisional_reason = str(exc)
                        await asyncio.sleep(.25)
                        continue
                    if (self.provisional is item and self.armed is None and monotonic() < item.expires):
                        require_same_model(item.host_model, model)
                        self.require_arm()
                        if item.guard is not None and self.live_owner is not item.guard.owner:
                            raise IntegrationError('Live payer owner changed; no switch armed.')
                        if self.live_owner is not None:
                            self.live_owner.begin(item)
                            transferred = True
                        else:
                            self.armed = item
                        self.provisional_reason = None
                    return
        except IntegrationError as exc:
            if self.provisional is item:
                self.provisional_reason = str(exc)
                self.local_error = True
        except (OSError, TimeoutError):
            pass  # The terminal status never advertises a provisional ticket as armed.
        finally:
            if item.guard is not None and not transferred:
                item.guard.release()
            if self.provisional is item:
                self.provisional = self.provisional_task = None
                if item.guard is not None and not transferred and self.live_owner is item.guard.owner:
                    self.local_error = True
                    self.emit('Native idle or ownership check did not pass. Payer unchanged; no input replayed.')

    def provisional_finished(self, item):
        # Cancellation before the first coroutine step skips its finally block.
        if self.provisional is item:
            self.provisional = self.provisional_task = None
            if item.guard is not None:
                item.guard.release()

    def request_intent(self, target, *, host_model=None, guard=None):
        if target == self.mode:
            owner = self.live_owner
            if owner is not None:
                released = owner.intent is not None and owner.intent.release()
                if owner.task and not owner.task.done() and not released:
                    raise IntegrationError('Native switch already fenced. Wait for payer confirmation.')
            self.intent_revision += 1
            self.armed = None
            self.cancel_provisional()
            return 'No switch armed. Staying on ' + ('host billing.' if self.mode == 'host' else '$V.')
        self.require_arm()
        require_same_model(host_model, host_model)
        if guard is not None and self.live_owner is not guard.owner:
            raise IntegrationError('Live payer owner changed; no switch armed.')
        if self.live_owner is not None:
            self.live_owner.begin(ExitIntent(self.mode, target, self.identifier, self.generation,
                                            monotonic() + ARM_SECONDS, host_model, guard))
            return 'Checking the native payer transition. No input is replayed.'
        if self.active_intent() is None:
            if self.provisional:
                require_same_model(self.provisional.host_model, host_model)
            self.armed = self.provisional or ExitIntent(self.mode, target, self.identifier, self.generation,
                                                        monotonic() + ARM_SECONDS, host_model)
            self.cancel_provisional()
        else:
            require_same_model(self.armed.host_model, host_model)
        exit_step = ('confirm it was intentional (Codex reports only an exit code)'
                     if self.host == 'codex' else 'confirm in this terminal')
        return (f'Switch armed for up to {ARM_SECONDS}s. Finish or stop all work, then type native /exit '
                f'and {exit_step}. Use "openvegas use {self.mode}" to cancel. '
                'No billing changed; no prompt sent.')

    def bind_session(self, identifier):
        from .session_ownership import SessionLease
        if self.identifier not in (None, identifier):
            raise IntegrationError('Native session identity changed; no handoff started.')
        if self.session_lease is None:
            self.session_lease = SessionLease(self.host, identifier)
        self.identifier = identifier

    async def handle(self, reader, writer):
        with ExitStack() as guard_cleanup:
            try:
                await self._handle(reader, writer, guard_cleanup)
            except asyncio.CancelledError:
                writer.close()
                raise

    async def _handle(self, reader, writer, guard_cleanup):
        response = {'ok': False, 'message': 'Billing request rejected; no change.'}
        generation = self.generation
        installed = None
        provisional_ack = None
        claude_ack = False
        guard = None
        try:
            async with asyncio.timeout(3):
                data = json.loads(await reader.readline())
                if (type(data) is not dict or not isinstance(data.get('token'), str)
                        or not secrets.compare_digest(data['token'], self.token)
                        or data.get('host') not in (None, self.host)
                        or self.child is None or self.child.returncode is not None):
                    raise ValueError
                identifier = session_id(data.get('session_id'))
                if identifier is not None and self.identifier not in (None, identifier):
                    raise ValueError
                if data.get('action') == 'register':
                    if not identifier or self.identifier not in (None, identifier):
                        raise ValueError
                    self.bind_session(identifier)
                    self.registered_generation = generation
                    if self.mode == 'v':
                        self.v_child_ready = True
                    self.transition_state = 'MODE_COMMITTED'
                    self.quota.transcript(data.get('transcript_path'), identifier)
                    if self.mode == 'host' and self.original_model is None:
                        model = data.get('model')
                        if isinstance(model, str) and native._MODEL.fullmatch(model):
                            self.original_model = model
                    response = {'ok': True, 'message': 'Native billing command connected.'}
                elif data.get('action') == 'quota':
                    if self.host != 'claude' or not identifier or identifier != self.identifier:
                        raise ValueError
                    self.quota.observe(data.get('rate_limits'))
                    response = {'ok': True, 'message': 'Usage metadata received.'}
                elif data.get('action') == 'play':
                    from .local_controls import GAMES
                    if data.get('game') is not None and data['game'] not in GAMES:
                        raise ValueError
                    self.open_controls(game=data.get('game'), delay=.75)
                    # Hooks can be trusted after SessionStart. A human play
                    # command carries the same host-owned identity without AI.
                    if self.identifier is None and identifier is not None:
                        self.bind_session(identifier)
                        self.quota.transcript(data.get('transcript_path'), identifier)
                        model = data.get('model')
                        if self.mode == 'host' and isinstance(model, str) and native._MODEL.fullmatch(model):
                            self.original_model = model
                    response = {'ok': True, 'message': 'Opening OpenVegas. No AI credits used.'}
                elif data.get('action') in ('v', 'host') and self.claude_owner is not None:
                    if identifier != self.identifier or identifier is None:
                        raise IntegrationError('Claude control belongs to another native chat.')
                    claude_ack = await self.claude_owner.arm(data['action'])
                    response = {'ok': True, 'message': 'Changing billing in this chat.' if claude_ack
                                else 'Billing is already on the requested route.'}
                elif data.get('action') in ('v', 'host'):
                    # Codex may trust plugin hooks after SessionStart. The first
                    # exact prompt supplies the same host-owned identity fields.
                    if self.host == 'codex' and self.codex_endpoint is not None and identifier is None:
                        raise IntegrationError('Managed Codex control needs the current native session ID.')
                    if self.identifier is None and identifier is not None:
                        self.bind_session(identifier)
                        model = data.get('model')
                        if isinstance(model, str) and native._MODEL.fullmatch(model):
                            self.original_model = model
                    if identifier is not None and identifier != self.identifier:
                        raise ValueError
                    if identifier == self.identifier and identifier and generation == 1:
                        self.registered_generation = generation
                    if not self.identifier:
                        raise IntegrationError('Native session is not ready or a switch is already pending.')
                    target = data['action']
                    if (self.live_owner is not None and self.live_owner.task is not None
                            and not self.live_owner.task.done() and target != self.mode):
                        raise IntegrationError('Live switch already in progress. Wait for the confirmed payer.')
                    prior = self.active_intent()
                    revision = self.intent_revision
                    hook_model = data.get('model') if data.get('host') == self.host and identifier == self.identifier else None
                    trusted_model = None
                    existing = prior or self.provisional
                    if target != self.mode and existing is not None:
                        try:
                            trusted_model = self.source_model(existing.host_model, hook_model)
                            require_same_model(existing.host_model, trusted_model)
                        except IntegrationError:
                            if self.live_owner is None:
                                self.request_intent(self.mode)
                            raise
                    elif target != self.mode:
                        self.require_arm()
                        if self.local_task and not self.local_task.done():
                            raise IntegrationError('Close the local controls first; no switch armed.')
                        if self.live_owner is not None:
                            guard = self.live_owner.reserve(target)
                            guard_cleanup.callback(guard.release)
                        observed = await self.check_live_source(preview=True)
                        trusted_model = self.source_model(observed, hook_model)
                        if target == 'v' and self.live_owner is None:
                            await self.preflight_v(trusted_model)
                    if (generation != self.generation or revision != self.intent_revision
                            or (guard is not None and (self.live_owner is not guard.owner
                                                      or guard.owner.intent is not guard))
                            or not secrets.compare_digest(data['token'], self.token)):
                        raise IntegrationError('Switch cancelled or native session changed.')
                    if self.codex_endpoint is not None and target != self.mode and not prior:
                        self.require_arm()
                        if self.provisional is None:
                            self.provisional_reason = None
                            provisional_ack = self.provisional = ExitIntent(
                                self.mode, target, self.identifier, generation, monotonic() + ARM_SECONDS,
                                trusted_model, guard)
                        response = {'ok': True, 'message': 'Checking Codex after this hook exits. No switch armed '
                                    'yet. Wait for the wallet to show switch on exit, then type /exit '
                                    'and confirm it was intentional. '
                                    'Repeat this command to check. '
                                    f'Use "openvegas use {self.mode}" to cancel. No prompt or charge.'
                                    + (' Latest check: ' + self.provisional_reason if self.provisional_reason else '')}
                        if self.live_owner is not None:
                            response['message'] = ('Checking same-chat payer switch. Wait for native payer confirmation '
                                                   'before entering a new turn. No /exit or replay.')
                    else:
                        response = {'ok': True, 'message': self.request_intent(target, host_model=trusted_model)}
                    if prior is not self.armed:
                        installed = self.armed
                        self._undelivered = installed
                else:
                    raise ValueError
        except IntegrationError as exc:
            if data.get('action') == 'register' and self.session_lease is None:
                # This is only our newly launched child, never the other owner.
                self.pending = None
                if self.child is not None and self.child.returncode is None:
                    self.child.terminate()
            response['message'] = str(exc)
        except (OSError, ValueError, TimeoutError, TypeError):
            pass
        try:
            try:
                async with asyncio.timeout(3 if guard is not None else None):
                    writer.write(json.dumps(response).encode() + b'\n')
                    await writer.drain()
            finally:
                writer.close()
                async with asyncio.timeout(3 if guard is not None else None):
                    await writer.wait_closed()
        except BaseException:
            if claude_ack:
                await self.claude_owner.cancel_arm()
            if installed is not None and self.armed is installed:
                self.armed = None
            if provisional_ack is not None and self.provisional is provisional_ack:
                self.cancel_provisional()
            raise
        else:
            if claude_ack:
                self.claude_owner.begin()
            if provisional_ack is not None and self.provisional is provisional_ack:
                if (generation == self.generation and revision == self.intent_revision
                        and (guard is None or (guard.owner is self.live_owner and guard.owner.intent is guard))
                        and secrets.compare_digest(data['token'], self.token)):
                    self.provisional_task = asyncio.create_task(self.verify_provisional(provisional_ack))
                    if guard is not None:
                        self.provisional_task.add_done_callback(lambda _: self.provisional_finished(provisional_ack))
                    guard_cleanup.pop_all()  # Provisional ticket now owns this exact guard.
                else:
                    self.cancel_provisional()
        finally:
            if self._undelivered is installed:
                self._undelivered = None
                if installed is not None:
                    self.wallet_status_changed.set()

    def open_controls(self, *, menu=False, game=None, delay=0):
        from .terminal_handoff import SOCKET_ENV, TOKEN_ENV
        if not self.env.get(SOCKET_ENV) or not self.env.get(TOKEN_ENV):
            raise IntegrationError('OpenVegas local controls need the managed terminal. No model request or wager sent.')
        if (self.child is None or self.child.returncode is not None or self.pending or self.checking_v
                or (self.claude_owner is not None and self.claude_owner.target is not None)
                or (self.live_owner is not None and self.live_owner.task is not None and not self.live_owner.task.done())
                or (self.local_task and not self.local_task.done())):
            raise IntegrationError('A game or billing change is already open. No second action started.')
        self.local_error = False
        self.local_task = asyncio.create_task(self.run_controls(menu=menu, game=game, delay=delay))

    async def run_controls(self, *, menu, game, delay):
        from .local_controls import controls
        if self.claude_owner is not None:
            try:
                await asyncio.sleep(delay)
                action = await controls(self.mode, game=game, menu=menu,
                                        check_v=self.claude_owner.preflight)
                if action in ('use_v', 'use_host') and await self.claude_owner.arm(action[4:]):
                    self.claude_owner.begin()
            except (IntegrationError, OSError, ValueError, TimeoutError):
                self.local_error = True
            return
        revision, generation = self.intent_revision, self.generation
        checked_model = None
        guard = None
        async def check_v():
            nonlocal checked_model, guard
            if self.live_owner is not None:
                if revision != self.intent_revision or generation != self.generation:
                    raise IntegrationError('Local control is stale; reopen the menu.')
                self.require_arm()
                guard = self.live_owner.reserve('v')
            try:
                checked_model = await self.preflight_local_v()
            except BaseException:
                if guard is not None:
                    guard.release()
                    guard = None
                raise
        try:
            await asyncio.sleep(delay)
            action = await controls(self.mode, game=game, menu=menu, check_v=check_v)
            if (action in ('use_v', 'use_host') and self.intent_revision == revision
                    and self.generation == generation):
                model = None
                if action[4:] != self.mode:
                    self.require_arm()
                    if self.live_owner is not None and guard is None:
                        guard = self.live_owner.reserve(action[4:])
                    model = self.source_model(await self.check_live_source())
                    if action == 'use_v':
                        require_same_model(checked_model, model)
                    if self.intent_revision != revision or self.generation != generation:
                        return
                if guard is None:
                    self.request_intent(action[4:], host_model=model)
                else:
                    self.request_intent(action[4:], host_model=model, guard=guard)
                    guard = None  # RuntimeOwner.begin transferred cleanup to its task.
        except (IntegrationError, OSError, ValueError, TimeoutError):
            self.local_error = True  # No terminal bytes injected over the host.
        finally:
            if guard is not None:
                guard.release()

    def control_event(self, event):
        if event.get('control') == 'menu':
            try:
                self.open_controls(menu=True)
            except IntegrationError:
                pass  # Repeated key presses never open parallel games.

    def local_status(self):
        item = self.active_intent()
        return {'controls': True, 'local_error': self.local_error,
                **({'switch_on_exit': item.target} if item and self._undelivered is not item else {}),
                'quota_remaining': self.quota.remaining() if self.mode == 'host' else None}

    async def run_child(self, argv, env, *, staged=None):
        if self.runtime_lock.locked() or getattr(self, '_wallet_stage', None) is not staged:
            raise IntegrationError('A native runtime still owns this session; no second host started.')
        async with self.runtime_lock:
            from .session_ownership import GENERATION_ENV, OWNER_ENV, claim_relay
            from .terminal_handoff import close_writer
            staged_token = staged.consume(self, env) if staged is not None else None
            env = {**env, OWNER_ENV: self.relay_owner, GENERATION_ENV: str(self.generation + 1)}
            self.env.update({OWNER_ENV: env[OWNER_ENV], GENERATION_ENV: env[GENERATION_ENV]})
            lease = await claim_relay(env, allow_legacy=self.mode == 'host' and self.generation == 0)
            self.modern_relay = lease is not None
            if lease is None:
                if self.generation or self.mode != 'host':
                    raise IntegrationError('Modern relay ownership was lost; resume directly in the native host.')
                # Ordinary launch on an authenticated old relay keeps its old
                # protocol; it gains no ownership or safe-switch certification.
                for key in (OWNER_ENV, GENERATION_ENV):
                    env.pop(key, None)
                    self.env.pop(key, None)
            try:
                if staged is not None:
                    return await self._run_child_owned(argv, env, staged_token=staged_token)
                return await self._run_child_owned(argv, env)
            finally:
                self.modern_relay = False
                self.armed = None
                watcher = self.cancel_provisional()
                if watcher is not None:
                    await asyncio.gather(watcher, return_exceptions=True)
                if lease is not None:
                    await close_writer(lease)

    async def _run_child_owned(self, argv, env, *, staged_token=None):
        env = dict(env)
        if self.child is not None and self.child.returncode is None:
            raise IntegrationError('A native runtime still owns this session; no second host started.')
        self.generation += 1
        self.token = staged_token or secrets.token_urlsafe(32)
        self.env[TOKEN] = env[TOKEN] = self.token
        self.transition_state = 'ATTACH_VERIFIED_TARGET' if self.generation > 1 else 'HOST_ACTIVE'
        if (self.host == 'claude' and self.modern_relay and self.generation == 1
                and self.mode == 'host' and self.launch_spec.version == '2.1.322'
                and self.env.get('OPENVEGAS_CLAUDE_GENERATION_SWITCH', '1') == '1'
                and os.isatty(0) and os.isatty(1)):
            from .session_ownership import supports_relay_input
            if await supports_relay_input(env):
                from .claude_generation_host import ManagedClaude
                from .claude_generation_observer import ClaudeGenerationObserver
                try:
                    observer = ClaudeGenerationObserver()
                except (IntegrationError, OSError):
                    # No native process or payer mutation has occurred yet.
                    return await self._terminal_child(argv, env)
                env.update(observer.env)
                env.setdefault('CLAUDE_CODE_DISABLE_TERMINAL_TITLE', '1')
                self.claude_owner = ManagedClaude(self, env, observer)
                try:
                    return await self.claude_owner.run((*self.launch_spec.original_argv, *observer.argv), env)
                finally:
                    self.claude_owner = None
                    observer.close()
        if self.host == 'codex' and self.modern_relay:
            if self.mode == 'host':
                try:
                    client = self.launch_spec.managed_codex_client(self.env)
                    _codex_management()
                except IntegrationError:
                    if self.generation != 1:
                        raise
                else:
                    if self.generation == 1 and self.env.get('OPENVEGAS_CODEX_LIVE_SWITCH', '1') == '1':
                        from .codex_live_switch import RuntimeOwner
                        return await RuntimeOwner(self, env).run(client)
                    return await self._managed_codex_child(client, env)
            elif self.wallet_endpoint is not None:
                return await self._managed_codex_child(argv, env)
        return await self._terminal_child(argv, env)

    async def _managed_codex_child(self, argv, env):
        host_server, policy = _codex_management()
        saved = None
        try:
            async with AsyncExitStack() as stack:
                if self.mode == 'host':
                    try:
                        endpoint = await stack.enter_async_context(host_server(
                            self.executable, env, cwd=self.launch_spec.cwd))
                        if self.generation == 1:
                            async with asyncio.timeout(3):
                                await policy.check_source_config(endpoint, self.launch_spec.cwd)
                    except (IntegrationError, OSError, TimeoutError, ImportError) as exc:
                        if self.generation != 1:
                            raise
                        # The server may have started, but no TUI was attached. It
                        # must retire successfully before running the original argv.
                        await stack.aclose()
                        self.codex_unavailable = str(exc) if isinstance(exc, IntegrationError) else 'Managed Codex startup unavailable.'
                        self.emit(self.codex_unavailable + ' Starting the original host without handoff.')
                        return await self._terminal_child([self.executable, *self.launch_spec.original_argv], env)
                    if self.generation != 1:
                        if self.codex_original is None or self.codex_exit is None:
                            raise IntegrationError('Missing original Codex policy; no TUI resumed.')
                        snapshot = self.codex_exit[1]
                        policy.assert_same_policy(self.codex_original, snapshot)
                        await policy.verify_target(endpoint, snapshot, model=self.restore_model)
                        argv = self.launch_spec.managed_codex_client(
                            self.env, restore_id=self.identifier, restored_model=self.restore_model)
                    argv = [self.executable, '--remote', endpoint, *argv]
                else:
                    endpoint = self.wallet_endpoint
                self.codex_endpoint = endpoint
                self.codex_exit = None
                prior_child = self.child
                try:
                    code = await self._terminal_child(argv, env)
                    item = self.pending
                    if item is not None:
                        if (code or item.identifier != self.identifier or item.generation != self.generation
                                or self.child is None or self.child.returncode != 0):
                            raise IntegrationError('Native exit boundary changed; no handoff started.')
                        snapshot = await policy.capture_source(endpoint, self.identifier, after_exit=True)
                        if snapshot.thread_id != self.identifier or snapshot.cwd != self.launch_spec.cwd:
                            raise IntegrationError('Codex policy belongs to another session or directory.')
                        require_same_model(item.host_model, snapshot.model)
                        if self.mode == 'v':
                            policy.assert_same_policy(self.codex_original, snapshot)
                        saved = (item, snapshot)
                finally:
                    if (saved is None and self.child is not None and self.child is not prior_child
                            and self.child.returncode is not None):
                        # Refusal is never permission to kill a busy server. Retain
                        # its relay/session leases while it drains. Never re-arm.
                        self.pending = self.armed = None
                        await self._drain_codex_source(policy, endpoint)
        except BaseException:
            self.pending = None
            raise
        finally:
            self.codex_endpoint = None
        # Never authorize a successor while the source host server is closing.
        self.codex_exit = saved
        return code

    async def _drain_codex_source(self, policy, endpoint):
        def notice(message):
            self.emit(f'{message} No switch pending. To inspect the original runtime locally: '
                      f'codex --remote {endpoint}')
        await policy.wait_until_idle(endpoint, self.identifier, on_wait=notice)

    async def _terminal_child(self, argv, env):
        if self.host == 'claude':
            # Claude can generate a paid title even when UserPromptSubmit stops
            # a local command. Scope the supported opt-out to this owned child.
            env.setdefault('CLAUDE_CODE_DISABLE_TERMINAL_TITLE', '1')
        self.changed.clear()
        attributes = termios.tcgetattr(0) if os.isatty(0) else None
        wallet = None
        async def replace_wallet(mode):
            nonlocal wallet
            if wallet:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
                wallet = None
            if mode is None or self.env.get('OPENVEGAS_LOCAL_CONTROLS') == '0' or self.env.get('OPENVEGAS_CODEX_WALLET') == '0':
                return
            from .terminal_wallet import CredentialReader, monitor
            if self.wallet_credentials is None:
                self.wallet_credentials = CredentialReader()
            ready = asyncio.Event()
            wallet = asyncio.create_task(monitor(env, mode, ready=ready,
                                                 credentials=self.wallet_credentials,
                                                 control=self.control_event, status=self.local_status,
                                                 status_changed=self.wallet_status_changed))
            try:
                await asyncio.wait_for(ready.wait(), 3.5)
            except TimeoutError:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
                wallet = None
        await replace_wallet(self.mode)
        if self.live_owner is not None:
            self.live_owner.replace_wallet = replace_wallet
        try:
            if self.host == 'claude' and self.mode == 'host' and wallet is not None:
                from .host_quota import claude_settings
                argv = claude_settings(argv, env)
            if self._launch_deadline is not None and self._launch_deadline <= monotonic():
                raise IntegrationError('Exit handoff expired before launch. Resume explicitly in the native host.')
            self.child = await asyncio.create_subprocess_exec(*argv, env=env)
            if self.live_owner is not None:
                self.live_owner.bind(self.child.pid)
        except BaseException:
            if wallet:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
            raise
        finished = asyncio.create_task(self.child.wait())
        try:
            # Only the user exits the native UI. No switch request signals it.
            return await asyncio.shield(finished)
        except asyncio.CancelledError:
            self.pending = None
            self.armed = None
            if self.child.returncode is None:
                self.child.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(finished), 5)
                except TimeoutError:
                    self.child.kill()
                    await self.child.wait()
            raise
        finally:
            if self.local_task and not self.local_task.done():
                self.local_task.cancel()
                await asyncio.gather(self.local_task, return_exceptions=True)
            if wallet:
                wallet.cancel()
                await asyncio.gather(wallet, return_exceptions=True)
            if attributes is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, attributes)
                item = self.active_intent()
                if (item and self.child.returncode == 0) or self.mode == 'v':
                    # All native/HUD writers are stopped before the relay drains
                    # their queued output and acknowledges a clean screen.
                    from .terminal_wallet import handoff_screen
                    self.transition_state = 'DRAIN_AND_RELEASE'
                    await handoff_screen(env)
                    if (item and self.active_intent() is item and self._undelivered is not item
                            and self.child.returncode == 0 and self.modern_relay):
                        self.pending = item

    async def v_options(self, broker):
        if self.host == 'codex':
            native.validate_codex_wallet_handoff(
                self.launch_spec.resume_arguments(self.identifier), self.launch_spec.provider)
        device = broker.credentials.load()
        if not isinstance(device, str) or not device.startswith('ov_device_'):
            raise IntegrationError('Sign in to OpenVegas first; no provider account is required from you.')
        options = await broker.request('GET', '/native/options?host=' + self.host, token=device)
        if not isinstance(options, dict) or options.get('wallet_billing') is not True:
            raise IntegrationError('Direct wallet billing is not available on this backend yet. Billing unchanged.')
        live_wallet(options)
        if (not isinstance(options, dict) or options.get('host') != self.host or
                options.get('provider') != ('anthropic' if self.host == 'claude' else 'openai')):
            raise IntegrationError('The backend returned an invalid native route. Billing unchanged.')
        models = options.get('models')
        if not isinstance(models, list) or not models:
            raise IntegrationError('No reviewed direct API model is enabled on this backend. Billing unchanged.')
        for item in models:
            if (not isinstance(item, dict) or not isinstance(item.get('model'), str)
                    or not native._MODEL.fullmatch(item['model'])
                    or type(item.get('max_tokens')) is not int or not 1 <= item['max_tokens'] <= 128000):
                raise IntegrationError('Invalid model options. Billing unchanged.')
        if native._decimal(options.get('balance_v'), allow_zero=True) <= 0:
            raise IntegrationError('Your OpenVegas balance is empty; no spending session opened.')
        return options

    async def preflight_v(self, current_model):
        """Read availability while the native process is still alive; never grant spending."""
        if self.checking_v:
            raise IntegrationError('A billing availability check is already running. Billing unchanged.')
        self.checking_v = True
        previous_state = self.transition_state
        self.transition_state = 'PREFLIGHT_TARGET'
        child = self.child
        broker = None
        try:
            # Leave time for the hook's three-second socket response deadline.
            async with asyncio.timeout(2):
                broker = Broker()
                options = await self.v_options(broker)
                wallet_row(self.host, options, current_model)
        except TimeoutError:
            raise IntegrationError('The billing availability check timed out. '
                                   'Your session is unchanged; no credits spent.') from None
        finally:
            try:
                if broker is not None:
                    await asyncio.wait_for(broker.close(), .25)
            except (TimeoutError, OSError):
                pass
            finally:
                self.checking_v = False
                self.transition_state = previous_state
        if child is not self.child or child is None or child.returncode is not None:
            raise IntegrationError('The native session ended during the check. Billing unchanged.')
        return options

    async def preflight_local_v(self):
        if not self.identifier:
            raise IntegrationError(
                'The coding session is not connected yet. Review OpenVegas in /hooks, '
                'then restart the host in this same terminal. Games remain available here; '
                'no AI request or billing change was made.')
        self.require_arm()
        model = self.source_model(await self.check_live_source())
        if self.live_owner is None:
            await self.preflight_v(model)
        return model

    def check_exit_ticket(self, item, *, check_version=False):
        if (not isinstance(item, ExitIntent) or item.expires <= monotonic()
                or item.identifier != self.identifier or item.generation != self.generation
                or item.source != self.mode or self.session_lease is None
                or self.child is None or self.child.returncode != 0):
            raise IntegrationError('Exit handoff expired or session changed. No new host started.')
        if item.target == 'host':
            require_same_model(self.restore_model, item.host_model)
        if self.host == 'codex':
            self.launch_spec.managed_codex_client(self.env)
            if (self.codex_exit is None or self.codex_exit[0] is not item or self.codex_endpoint is not None):
                raise IntegrationError('No exclusive final Codex policy snapshot for this exit; no resume.')
        else:
            self.launch_spec.require_exit_resume(item.identifier, self.env)
        if check_version:
            info = native.detect(self.host)
            if (not info or info['version'] != self.launch_spec.version
                    or info['executable'] != self.launch_spec.executable):
                raise IntegrationError('Native executable or version changed. Resume directly on the original route.')

    async def check_background(self):
        if self.host == 'codex':
            # capture_source(..., after_exit=True) already checked the exclusive runtime.
            if self.codex_exit is None or self.codex_endpoint is not None:
                raise IntegrationError('No final Codex source inventory; no resume.')
            return
        # This lists Claude sessions, not descendants detached via a user's
        # literal ! shell. An empty array does not replace the human attestation.
        proc = await asyncio.create_subprocess_exec(self.executable, 'agents', '--json',
            env=self.env, cwd=self.launch_spec.cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024)
        try:
            async with asyncio.timeout(3):
                raw = bytearray()
                while len(raw) <= 128:
                    chunk = await proc.stdout.read(129 - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if any(c not in b'[] \t\r\n' for c in chunk):
                        break
                if len(raw) > 128 or not re.fullmatch(rb'\s*\[\s*\]\s*', raw) or await proc.wait() != 0:
                    raise ValueError
        except (TimeoutError, ValueError, OSError):
            raise IntegrationError('Cannot verify an empty native session inventory. Check claude agents; '
                                   'finish or stop background work yourself. No session resumed.') from None
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()  # Only the short-lived inventory probe, never the coding host.
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.communicate(), 1)
                except TimeoutError:
                    raise IntegrationError('Native inventory probe did not close; resume manually.') from None

    async def choose_v(self, ticket):
        self.v_child_started = False
        self.v_child_ready = False
        broker = Broker()
        try:
            options = await self.v_options(broker)
            if options.get('wallet_billing') is not True:
                raise IntegrationError('Direct wallet billing is not available on this backend yet. Billing unchanged.')
            balance = native._decimal(options.get('balance_v'), allow_zero=True)
            limit = None if live_wallet(options) else wallet_ceiling(balance)
            if balance <= 0 or limit == 0:
                raise IntegrationError('Your OpenVegas balance is empty; no spending session opened.')
            if self.host == 'codex':
                self.check_exit_ticket(ticket)
                require_same_model(ticket.host_model, self.codex_exit[1].model)
            chosen = wallet_row(self.host, options, ticket.host_model)
            async def runner(argv, env):
                self.check_exit_ticket(ticket, check_version=True)
                if argv[0] != self.executable:
                    raise IntegrationError('Native wallet executable changed. No child started.')
                if self.host == 'codex':
                    try:
                        endpoint = argv[argv.index('--remote') + 1]
                    except (ValueError, IndexError):
                        raise IntegrationError('Verified private wallet endpoint missing; no TUI started.') from None
                    if (not endpoint.startswith('unix:///') or argv != native.codex_resume_argv(
                            self.executable, endpoint, chosen['model'], self.launch_spec.resume_arguments(self.identifier))):
                        raise IntegrationError('Unexpected Codex wallet attach arguments; no TUI started.')
                else:
                    self.launch_spec.verify_direct_environment(self.env, env)
                await self.check_background()
                self.check_exit_ticket(ticket)
                self.restore_model = ticket.host_model
                if self.host == 'codex':
                    self.codex_original = self.codex_exit[1]
                    self.wallet_endpoint = endpoint
                self.mode = 'v'
                self.v_child_started = True
                try:
                    return await self.run_child(argv, env, staged=stage)
                finally:
                    self.wallet_endpoint = None
            def confirm_quote():
                # The explicit Use $V command is spending consent. native.launch
                # already validated the quote and calls this immediately before
                # POST /native/sessions. Expired consent cannot create a grant.
                self.check_exit_ticket(ticket)
                return True
            self.check_exit_ticket(ticket, check_version=True)
            from .staged_generation import stage_after_exit
            with stage_after_exit(self) as stage:
                return await native.launch(self.host, chosen['model'], limit, chosen['max_tokens'],
                    provider=options['provider'], broker=broker, emit=self.emit, runner=runner,
                    host_args=self.launch_spec.resume_arguments(self.identifier),
                    local_tools_only=self.host == 'codex', resume_provider=self.launch_spec.provider,
                    compact=True, yes=False, confirm=confirm_quote, wallet_mode=True,
                    control_environment=stage.environment(),
                    **({'codex_handoff_source': self.codex_exit[1]} if self.host == 'codex' else {}))
        finally:
            await broker.close()

    async def run(self):
        try:
            # Lock explicit resume identities before starting the native runtime.
            options = self.launch_spec.options
            for index, option in enumerate(options):
                parts = option.argv
                if parts[0] in {'resume', '--resume', '-r'}:
                    value = parts[1] if len(parts) > 1 else (options[index + 1].argv[0]
                            if index + 1 < len(options) else None)
                    if session_id(value):
                        self.bind_session(value)
            return await self._run_session()
        finally:
            if self.session_lease is not None:
                self.session_lease.close()
                self.session_lease = None

    async def _run_session(self):
        with tempfile.TemporaryDirectory(prefix='ov-use-', dir='/tmp') as directory:
            os.chmod(directory, 0o700)
            path = str(Path(directory) / 'switch.sock')
            server = await asyncio.start_unix_server(self.handle, path, limit=8192)
            os.chmod(path, 0o600)
            self.env.update({SOCKET: path, TOKEN: self.token})
            async with server:
                code = await self.run_child([self.executable, *self.launch_spec.original_argv], self.env)
                while self.pending:
                    ticket, self.pending = self.pending, None
                    try:
                        self.check_exit_ticket(ticket, check_version=True)
                        self._launch_deadline = ticket.expires
                        target = '$V' if ticket.target == 'v' else 'host billing'
                        question = ('Did you exit Codex intentionally with /exit?' if self.host == 'codex' else
                                    'Did you personally /exit, keep the armed model, finish all detached work '
                                    '(including ! shell jobs), and want to '
                                    f'resume this same personal, unmanaged session on {target}? '
                                    'If policy is managed, unknown, or changed in-session, answer No.')
                        if not click.confirm(question, default=False):
                            self.emit('Cancelled. No new session or prompt started.')
                            return code
                        self.check_exit_ticket(ticket)
                        await self.check_background()
                        self.check_exit_ticket(ticket)
                        if ticket.target == 'v':
                            code = await self.choose_v(ticket)
                        else:
                            self.mode = 'host'
                            argv = self.launch_spec.original_resume(self.identifier, self.restore_model)
                            self.emit('Resuming the same conversation on host billing; no prompt submitted.')
                            code = await self.run_child(argv, self.env)
                    except click.Abort:
                        self.pending = None
                        self.emit('Cancelled. No automatic fallback or prompt replay.')
                        return 1
                    except (IntegrationError, OSError) as exc:
                        self.pending = None  # A failed grant close invalidates any armed return.
                        self.emit(str(exc) if isinstance(exc, IntegrationError) else 'Native resume unavailable.')
                        self.emit('No automatic fallback. Check the previous route and grant status before resuming manually.')
                        return 1
                    finally:
                        self._launch_deadline = None
                return code


def main(host, args):
    env = dict(native.host_environment() or os.environ)
    # Noninteractive/admin invocations stay completely outside the supervisor.
    from .host_invocation import passthrough
    if not os.isatty(0) or passthrough(host, args):
        executable = shutil.which(host)
        if not executable:
            raise IntegrationError('Native executable unavailable.')
        os.execve(executable, [executable, *args], env)
    info = native.detect(host)
    if not info:
        raise IntegrationError('Native executable unavailable.')
    try:
        return asyncio.run(Supervisor(host, info['executable'], args, env, version=info['version']).run())
    except IntegrationError as exc:
        click.echo(str(exc), err=True)
        return 1
