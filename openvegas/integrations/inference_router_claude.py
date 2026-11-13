"""Opt-in, same-process Claude router. No generation handoff or plugin gate.

The existing private observer supplies metadata and exact local commands only.
The session-owned HTTP listener remains the payer barrier across plugin reloads.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import stat
import termios
from dataclasses import dataclass
from pathlib import Path

from .billing_host import exact_model, session_id, wallet_row
from .broker import Broker
from .claude_generation_host import SWITCH_TIMEOUT, ManagedClaude
from .claude_generation_observer import ClaudeGenerationObserver
from .credentials import IntegrationError
from .inference_router_adapter import ProviderAdapter
from .inference_router_session import RouterSession, claude_wallet_callbacks

# This deliberately has no http.fetch, turn/tool inventories, prompt replay,
# ordinary-input hold. Unloading it cannot retarget the HTTP client.
_MODULE = r'''
const BILLING = __BILLING__;
async function billing($, target) {
  try {
    const result = await $.process.run(BILLING, { timeoutMs: 4500, stdin: JSON.stringify({
      hook_event_name: 'UserPromptSubmit', prompt: `openvegas use ${target}`,
      session_id: await $.session.id(), model: await $.session.model() }) });
    const reply = JSON.parse(result.stdout);
    $.ui.log(String(reply.stopReason ?? reply.reason ?? 'Billing request sent.').slice(0, 1000));
  } catch {
    $.ui.log('Local billing control unavailable. No billing command sent to a model.');
  }
}
export function register(on) {
  let editRevision = 0, putback = null;
  on('prompt.edit', ($, e, next) => {
    editRevision += 1;
    return next(e);
  });
  on('prompt.change', async ($, e, next) => {
    const result = await next(e);
    if (e.origin.kind === 'history' || e.origin.kind === 'undo') editRevision += 1;
    const owned = putback;
    if (e.origin.kind === 'engine' && owned?.text === e.text && owned.revision === editRevision) {
      putback = null;
      const current = await $.prompt.read();
      // Clear only the untouched native put-back of our exact local command.
      if (current.text === owned.text && owned.revision === editRevision)
        await $.prompt.fill({ text: '', cursor: 0, mode: 'replace' });
    }
    return result;
  });
  on('prompt.submit', async ($, e, next) => {
    const match = /^(?:\/|\$)?openvegas(?::openvegas)?[ \t]+use[ \t]+(v|host)$/i.exec(e.text.trim());
    if (!match || e.command || e.origin.kind !== 'composer') return next(e);
    putback = { text: e.text, revision: editRevision };
    await billing($, match[1].toLowerCase());
    return { text: e.text, origin: e.origin };
  });
  on('command.run', { command: 'openvegas' }, async ($, e, next) => {
    const match = /^use[ \t]+(v|host)$/i.exec((e.args ?? '').trim());
    if (!match) return next(e);
    await billing($, match[1].toLowerCase());
    return {};
  });
  on('session.start', async ($, e, next) => {
    const result = await next(e);
    if ((await $.session.version()).version !== '2.1.322') return result;
    const requestPath = await $.env.get('OV_BOUNDARY_CONTROL');
    const responsePath = await $.env.get('OV_BOUNDARY_RESPONSE');
    const generation = await $.env.get('OV_BOUNDARY_GENERATION');
    if (!requestPath || !responsePath || !generation) return result;
    await $.command.register({ name: 'openvegas', description: 'OpenVegas local controls',
      argumentHint: '[use v|use host]', immediate: true });
    let answered = null, observing = false;
    $.clock.every(50, async () => {
      if (observing) return;
      observing = true;
      try {
        const request = JSON.parse(await $.fs.read(requestPath));
        if (request.action !== 'metadata' || request.generation !== generation
            || !/^[a-f0-9]{32}$/.test(request.nonce ?? '') || request.nonce === answered) return;
        const model = await $.session.model();
        const metadata = { session_id: await $.session.id(), cwd: await $.session.cwd(), model,
          base_url: await $.env.get('ANTHROPIC_BASE_URL') };
        if (model !== await $.session.model()) return;
        await $.fs.write(responsePath, JSON.stringify({ generation, nonce: request.nonce, metadata }));
        answered = request.nonce;
      } finally { observing = false; }
    });
    return result;
  });
}
'''


@dataclass(frozen=True, repr=False)
class NativeMetadata:
    session_id: str
    cwd: str
    model: str
    base_url: str

    def __post_init__(self):
        if (session_id(self.session_id) is None or not exact_model(self.model)
                or not self.model.startswith('claude-')
                or not isinstance(self.cwd, str) or not os.path.isabs(self.cwd) or '\x00' in self.cwd
                or not isinstance(self.base_url, str)):
            raise IntegrationError('Exact native Claude metadata is unavailable; no model substituted.')


class ClaudeRouterObserver(ClaudeGenerationObserver):
    """Reuse the private plugin/IPC and installed command, not its task fence."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            hooks = json.loads((self.plugin_dir / 'hooks/hooks.json').read_text())
            command = hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
            module = _MODULE.replace('__BILLING__', json.dumps(shlex.split(command)))
            (self.plugin_dir / 'hooks/observer.js').write_text(module)
            self.generation = secrets.token_hex(16)
            self.env['OV_BOUNDARY_GENERATION'] = self.generation
        except BaseException:
            self.close()
            raise

    async def metadata(self, process):
        if self._closed or process is None or process.returncode is not None:
            raise IntegrationError('Native Claude metadata observer is unavailable; billing unchanged.')
        nonce = secrets.token_hex(16)
        try:
            async with self._lock, asyncio.timeout(SWITCH_TIMEOUT):
                self._write(self._request, {'action': 'metadata', 'generation': self.generation, 'nonce': nonce})
                while process.returncode is None:
                    try:
                        reply = self._read()
                    except json.JSONDecodeError:
                        reply = None
                    if (isinstance(reply, dict) and reply.get('nonce') == nonce
                            and reply.get('generation') == self.generation):
                        return NativeMetadata(**reply['metadata'])
                    await asyncio.sleep(.01)
        except (TimeoutError, OSError, ValueError, KeyError, TypeError):
            pass
        raise IntegrationError('Native Claude metadata observer is unavailable; billing unchanged.')


def _empty_remote_settings(path):
    """An empty native cache carries no policy; never ignore actual settings."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or not 0 < info.st_size <= 1024):
            return False
        raw = os.read(fd, 1025)
        after = os.fstat(fd)
        current = path.lstat()
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_size',
                  'st_mtime_ns', 'st_ctime_ns')
        return (all(getattr(info, key) == getattr(after, key) == getattr(current, key)
                    for key in fields)
                and len(raw) == info.st_size and json.loads(raw) == {})
    finally:
        os.close(fd)


def verify_settings(env, cwd):
    """Refuse unknown effective routing sources, without reading native auth."""
    if (not env.get('HOME') or any(key.startswith(('CLAUDE_CODE_MANAGED_', 'CLAUDE_CODE_POLICY_'))
                                   for key in env)):
        raise IntegrationError('Native profile location is unavailable; no router launch.')
    profile = Path(env.get('CLAUDE_CONFIG_DIR', str(Path(env['HOME']) / '.claude')))
    managed = [profile / name for name in ('remote-settings.json', 'managed-settings.json')]
    managed += [Path(path) for path in (
        '/Library/Application Support/ClaudeCode/managed-settings.json',
        '/Library/Application Support/ClaudeCode/managed-settings.d',
        '/Library/Managed Preferences/com.anthropic.claudecode.plist',
        '/etc/claude-code/managed-settings.json', '/etc/claude-code/managed-settings.d')]
    try:
        for path in managed:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            if path == profile / 'remote-settings.json' and _empty_remote_settings(path):
                continue
            raise IntegrationError('Managed native routing policy is unverified; no router launch.')
        sources = {profile / 'settings.json', profile / 'settings.local.json'}
        for parent in (Path(cwd), *Path(cwd).parents):
            sources.update(parent / '.claude' / name for name in ('settings.json', 'settings.local.json'))
        for path in sources:
            if not path.exists():
                continue
            if path.stat().st_size > 1024 * 1024:
                raise ValueError
            settings = json.loads(path.read_text())
            values = settings.get('env', {})
            if not isinstance(values, dict) or any(
                key == 'ANTHROPIC_CUSTOM_HEADERS' or key == 'OPENVEGAS_LOCAL_ROUTER_AUTH'
                or key.startswith('CLAUDE_CODE_USE_')
                or (key.startswith('ANTHROPIC_') and key.endswith('BASE_URL'))
                or key.lower() in {'http_proxy', 'https_proxy', 'all_proxy'} for key in values):
                raise ValueError
    except (OSError, ValueError, AttributeError):
        raise IntegrationError('Native settings routing is unverified; no router launch.') from None


class ManagedClaudeRouter(ManagedClaude):
    """Reuse the live wallet HUD, not ManagedClaude's generation switch methods."""

    def __init__(self, supervisor, env, observer):
        super().__init__(supervisor, env, observer)
        self.session = None
        self._model = None
        self._metadata = None
        self._process = None
        self._prepared = None
        self._metadata_error = False
        self.ending = False

    async def _observe(self):
        metadata = await self.observer.metadata(self._process)
        if (self.s.child is not self._process or self._process.returncode is not None
                or metadata.cwd != self.s.launch_spec.cwd
                or metadata.base_url != self.session._url
                or self.s.identifier not in (None, metadata.session_id)):
            raise IntegrationError('Native Claude routing/session metadata changed; no payer commit.')
        self.s.bind_session(metadata.session_id)
        self.s.registered_generation = self.s.generation
        return metadata

    async def _metadata_boundary(self, ticket):
        try:
            current = await self._observe()
            if self._metadata is None:
                self._metadata, self._model = current, current.model
            elif current != self._metadata:
                raise IntegrationError('Native Claude model changed; no payer commit.')
            return True
        except IntegrationError:
            self._metadata_error = True
            return False

    def _commit_guard(self, ticket):
        if (self.ending or self._metadata is None or self.s.child is not self._process
                or self._process.returncode is not None
                or self.s.identifier != self._metadata.session_id):
            raise IntegrationError('Native Claude ownership changed; no payer commit.')

    async def preflight(self):
        # Read-only local menu availability; never infer a model from launch args.
        current = await self._observe()
        broker = Broker()
        try:
            wallet_row('claude', await self.s.v_options(broker), current.model)
        finally:
            await broker.close()

    async def arm(self, target):
        if target not in ('host', 'v') or self.failed or self.ending or self.session is None:
            raise IntegrationError('Claude router is unavailable; no new payer committed.')
        if self.target is not None or (self.task and not self.task.done()):
            raise IntegrationError('A Claude billing change is already pending.')
        if target == self.s.mode:
            return False
        try:
            ticket = self.session.controls.reserve(target)
        except ValueError:
            raise IntegrationError('Claude router admission is already held; no switch started.') from None
        if ticket is None:
            raise IntegrationError('Claude router admission could not be reserved; no switch started.')
        self.target = target
        self._metadata = self._model = None
        self._metadata_error = False
        self._committed = False
        self._deadline = asyncio.get_running_loop().time() + SWITCH_TIMEOUT
        self._status('pending')
        return True

    def begin(self):
        if self.target is not None and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.change(self.target))

    async def cancel_arm(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.session.controls.cancel_reservation()
        self.target = None
        if not self.failed:
            self._status(None)

    async def change(self, target):
        try:
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.session.controls.cancel_reservation()
                raise IntegrationError('Billing command expired before admission.')
            self.session.controls.timeout = remaining
            result = await self.session.command('openvegas use ' + target)
            self.s.mode = result.billing
            if self.session.state.pending is not None:
                self.failed = 'Billing outcome is unconfirmed; local inference admission remains held.'
                self._status('blocked')
                self.s.emit(self.failed)
            elif result.changed:
                self._committed = True
                self.s.original_model = self._model
                self.s.intent_revision += 1
                self.s.transition_state = 'MODE_COMMITTED'
                self._status('refreshing')
                self.refresh_wallet(result.billing)
                self.s.emit('Native payer now ' + ('$V' if result.billing == 'v' else 'host')
                            + '; same Claude process and chat, no prompt submitted.')
            else:
                self._status(None)
                self.s.emit('Exact native model metadata is unavailable or changed; billing unchanged.'
                            if self._metadata_error else 'Billing unchanged; local switch refused. No prompt replayed.')
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - No native/private diagnostics.
            if self.session.state.pending is not None:
                self.failed = 'Billing outcome is unconfirmed; local inference admission remains held.'
                self._status('blocked')
                self.s.emit(self.failed)
            else:
                self._status(None)
                self.s.emit('Local billing switch unavailable; billing unchanged. No prompt replayed.')
        finally:
            self.target = None

    async def _close_grants(self):
        from .codex_billing_route import _settle

        if self.session is not None:
            await _settle(asyncio.create_task(self.session.controls.settle_callbacks()))
        if self._prepared is not None:
            for grant in self._prepared.retained_grants:
                if grant.state == 'issued':
                    await _settle(asyncio.create_task(grant.close()))

    async def run(self, argv, env):
        verify_settings(env, self.s.launch_spec.cwd)
        broker = Broker()
        attributes = termios.tcgetattr(0) if os.isatty(0) else None
        try:
            prepare, revoke = claude_wallet_callbacks(broker, active_model=lambda: self._model)
            self._prepared = prepare
            self.session = RouterSession(ProviderAdapter('claude', 'https://api.anthropic.com',
                broker.backend + '/integrations/native'), request_boundary=True,
                task_boundary=self._metadata_boundary, prepare_wallet=prepare, revoke_wallet=revoke,
                commit_guard=self._commit_guard, local_auth='header',
                status_changed=self.s.wallet_status_changed)
            async with self.session.launch(version=self.s.launch_spec.version, env=env, args=list(argv),
                                           effective_policy_verified=True) as (child_env, child_args):
                self._process = self.s.child = await asyncio.create_subprocess_exec(
                    self.s.executable, *child_args, env=child_env, cwd=self.s.launch_spec.cwd)
                self.refresh_wallet('host')
                try:
                    return await self._process.wait()
                finally:
                    self.ending = True
                    if self.s.local_task and not self.s.local_task.done():
                        self.s.local_task.cancel()
                        await asyncio.gather(self.s.local_task, return_exceptions=True)
                    if self.task and not self.task.done():
                        self.task.cancel()
                        await asyncio.gather(self.task, return_exceptions=True)
                    self.session.controls.cancel_reservation()
                    if self._process.returncode is None:
                        self._process.terminate()
                        try:
                            await asyncio.wait_for(self._process.wait(), 5)
                        except TimeoutError:
                            self._process.kill()
                            await self._process.wait()
                    await self.stop_wallet()
        finally:
            try:
                await self._close_grants()
            finally:
                try:
                    await broker.close()
                finally:
                    if attributes is not None:
                        termios.tcsetattr(0, termios.TCSADRAIN, attributes)
