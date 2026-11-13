"""Source-owned task fences for the private fixed inference router.

The transport observes its native backend from connection startup, not a plugin
hook or HTTP-idle poll. Unknown/detachable work permanently disqualifies this
process from a same-process payer switch; normal native operation is unchanged.
This deliberately does not claim complete detached-work retirement for either
host, or use a process-group snapshot as historical ownership evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.client import unix_connect
from websockets.protocol import State

from . import codex_handoff_policy as policy
from .codex_billing_route import _OWNED_SERVERS
from .codex_live_transport import (
    _LIMITS,
    _READ,
    TransportRefused,
    TransportUncertain,
    _frame,
    _path,
)
from .codex_stable_transport import StableTransport
from .credentials import IntegrationError
from .native_owned_process import OwnedProcess

_PASSIVE_ITEMS = frozenset({'userMessage', 'agentMessage', 'reasoning', 'plan'})
_PASSIVE_NOTICES = frozenset({
    'thread/status/changed', 'thread/readState/changed', 'thread/name/updated',
    'thread/settings/updated', 'thread/tokenUsage/updated', 'thread/prediction/updated',
    'turn/diff/updated', 'turn/plan/updated', 'turn/moderationMetadata',
    'item/agentMessage/delta', 'item/plan/delta', 'item/reasoning/summaryTextDelta',
    'item/reasoning/summaryPartAdded', 'item/reasoning/textDelta',
    'account/rateLimits/updated',
    'model/verification', 'model/safetyBuffering/updated', 'warning',
    'guardianWarning', 'deprecationNotice', 'configWarning',
    'fuzzyFileSearch/sessionUpdated', 'fuzzyFileSearch/sessionCompleted',
})
_PASSIVE_CALLS = _READ | frozenset({
    'initialize', 'initialized', 'thread/loaded/list', 'thread/goal/get',
    'thread/queue/list', 'thread/attachment/list', 'thread/backgroundTerminals/list',
    'thread/list', 'server/diagnostics', 'hooks/list', 'mcpServerStatus/list',
    'thread/settings/update', 'thread/name/set', 'thread/readState/set',
    'account/bedrock/checkGovCloudRequirements',
})


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def _billing_hook_file(source):
    """Verify exact installer-generated Node hook code, not a reported hash.

    Node's inline program has no subprocess/provider API. Its only connection
    is the existing bounded authenticated local control socket. Read-only
    provenance checks never execute the command or inspect profile credentials.
    """
    from .use import MISSING

    path = Path(source)
    if not path.is_absolute() or path.name != 'hooks.json':
        raise ValueError('Unrecognized billing hook path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_size > 256 * 1024):
            raise ValueError('Unowned billing hook file')
        raw = stream.read(256 * 1024 + 1)
        after = os.fstat(stream.fileno())
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if identity(before) != identity(after) or len(raw) > 256 * 1024:
        raise ValueError('Billing hook file changed')
    data = json.loads(raw)
    if set(data) != {'hooks'} or set(data['hooks']) != {'SessionStart', 'UserPromptSubmit'}:
        raise ValueError('Additional native hook code')
    script = Path(__file__).with_name('assets').joinpath('billing-hook.js').read_text().replace(
        '__OPENVEGAS_MISSING__', json.dumps(MISSING))
    command = None
    for event in ('SessionStart', 'UserPromptSubmit'):
        group = data['hooks'][event]
        entry = group[0]['hooks'][0]
        if (len(group) != 1 or set(group[0]) != {'hooks'} or len(group[0]['hooks']) != 1
                or set(entry) != {'type', 'command', 'timeout'} or entry['type'] != 'command'
                or entry['timeout'] != 5):
            raise ValueError('Unrecognized native hook options')
        argv = shlex.split(entry['command'])
        if (len(argv) != 5 or not Path(argv[0]).is_absolute() or Path(argv[0]).name != 'node'
                or argv[1:] != ['-e', script, 'codex', '--billing-hook']
                or shlex.join(argv) != entry['command']
                or command not in (None, entry['command'])):
            raise ValueError('Native hook differs from installed OpenVegas artifact')
        node = Path(argv[0]).stat()
        if (not stat.S_ISREG(node.st_mode) or node.st_uid not in (0, os.getuid())
                or node.st_mode & 0o022 or not node.st_mode & 0o111):
            raise ValueError('Native hook runtime is not owned')
        command = entry['command']
    return command, hashlib.sha256(raw).hexdigest(), (before.st_dev, before.st_ino, before.st_mtime_ns)


def _known_hook_metadata(hook):
    command, digest, identity = _billing_hook_file(hook['sourcePath'])
    if (hook.get('source') not in ('plugin', 'user', 'project') or hook.get('handlerType') != 'command'
            or hook.get('eventName') not in ('sessionStart', 'userPromptSubmit')
            or hook.get('async', False) is not False or hook.get('command') != command
            or hook.get('timeoutSec') != 5 or hook.get('matcher') not in (None, '')
            or hook.get('trustStatus') not in ('trusted', 'managed')):
        raise ValueError('Native hook metadata does not match the installed artifact')
    return hook['sourcePath'], digest, identity


class CodexWorkLedger:
    """Metadata only from the one owned app-server transport, never transcripts.

    Completion of a foreground turn also retires its inline retry loop. A tool
    which can escape that loop is NOT cleared by turn/completed, cancel ACK,
    process-list emptiness, or a later successful turn. Existing generation
    implementations remain available for those unsupported conditions.
    """

    def __init__(self):
        self.revision = 0
        self.reason = None
        self.root = None
        self.initialized = False
        self.pending = {}
        self.starts = {}
        self.start_threads = {}
        self.helpers = {}
        self.closed_helpers = set()
        self.turns = set()
        self.admitted = set()
        self.items = set()
        self.completed = set()
        self.hooks = {}
        self.hook_files = {}

    def __repr__(self):
        return '<CodexWorkLedger private-native-metadata>'

    def block(self, reason):
        self.reason = self.reason or reason
        self.revision += 1

    def _root(self, sid):
        if sid in self.helpers:
            if sid in self.closed_helpers or self.helpers[sid]:
                self.block('retired_helper_restarted')
                return False
            return True
        if not _identifier(sid) or self.root not in (None, sid):
            self.block('additional_or_unknown_thread')
            return False
        self.root = sid
        return True

    def _thread(self, thread, *, owned_helper_start=False):
        sid = thread['id']
        if sid == self.root or self.root is None and thread.get('ephemeral') is not True:
            return self._root(sid)
        if (not _identifier(sid) or sid in self.closed_helpers
                or thread.get('ephemeral') is not True or thread.get('parentThreadId') is not None
                or thread.get('modelProvider') != 'openai' or thread.get('cliVersion') != '0.166.0'
                or thread.get('source') not in ('cli', 'vscode', 'appServer')
                or sid not in self.helpers and (not owned_helper_start or len(self.helpers) >= 128)):
            self.block('additional_or_unknown_thread')
            return False
        self.helpers.setdefault(sid, False)
        return True

    def _thread_notice(self, thread):
        sid = thread['id']
        if sid == self.root or sid in self.helpers or self.root is None:
            return self._thread(thread)
        candidates = [key for key, ephemeral in self.starts.items()
                      if ephemeral and key not in self.start_threads]
        if len(candidates) != 1:
            self.block('unowned_helper_announcement')
            return False
        self.start_threads[candidates[0]] = sid
        return self._thread(thread, owned_helper_start=True)

    def observe(self, raw, *, incoming):
        """Observe before forwarding; malformed observations never erase state."""
        self.revision += 1
        try:
            self._observe(_frame(raw), incoming=incoming)
        except (KeyError, TypeError, ValueError, OSError):
            self.block('malformed_native_event')

    def _observe(self, frame, *, incoming):
        method, key = frame.get('method'), frame.get('id')
        params = frame.get('params', {})
        if incoming:
            if not method:
                # A response to an approval/tool request is not task retirement.
                return
            if self.root is not None and (method.startswith((
                    'account/login', 'accountSession/', 'account/chatgptAuthTokens/',
                    'account/gatewayOAuth/', 'attestation/')) or method == 'account/logout'):
                self.block('native_authentication_changed')
            if method == 'account/bedrock/checkGovCloudRequirements' and params:
                self.block('untracked_native_source')
            if key is not None:
                if key in self.pending or len(self.pending) >= 256:
                    self.block('ambiguous_native_rpc')
                    return
                self.pending[key] = (method, params.get('threadId'))
                if method == 'thread/start':
                    self.starts[key] = params.get('ephemeral') is True
            if method in ('thread/start', 'thread/resume', 'turn/start', 'turn/steer', 'turn/interrupt'):
                if method != 'thread/start':
                    self._root(params.get('threadId'))
            elif method == 'thread/unsubscribe':
                if params.get('threadId') not in self.helpers or set(params) != {'threadId'}:
                    self.block('unowned_thread_unsubscribe')
            elif method not in _PASSIVE_CALLS:
                self.block('untracked_native_source')
            return
        if not method:
            operation = self.pending.pop(key, None)
            if operation is None:
                self.block('unmatched_native_response')
                return
            name, sid = operation
            owned_helper = self.starts.pop(key, False)
            announced = self.start_threads.pop(key, None)
            if 'error' in frame:
                # A rejection did not authorize another turn; already observed
                # starts remain active until their exact completion event.
                return
            result = frame.get('result', {})
            if name == 'initialize':
                version = result.get('userAgent', '')
                if not isinstance(version, str) or not version.split(' ')[0].endswith('/0.166.0'):
                    self.block('unsupported_native_version')
                else:
                    self.initialized = True
            elif name in ('thread/start', 'thread/resume'):
                if announced is not None and announced != result['thread']['id']:
                    self.block('helper_start_identity_mismatch')
                else:
                    self._thread(result['thread'], owned_helper_start=owned_helper)
            elif name == 'thread/unsubscribe' and sid in self.helpers:
                if result.get('status') not in ('notLoaded', 'notSubscribed', 'unsubscribed'):
                    self.block('unverified_native_unsubscribe')
                else:
                    self.helpers[sid] = True
            elif name == 'turn/start':
                identity = sid, result['turn']['id']
                if not _identifier(identity[1]):
                    self.block('unknown_turn_identity')
                elif identity not in self.completed:
                    self.admitted.add(identity)
            elif name == 'account/bedrock/checkGovCloudRequirements':
                if (result != {'isGovCloud': False, 'shouldWarn': False}
                        or any(type(value) is not bool for value in result.values())):
                    self.block('unexpected_native_auth_configuration')
            return
        if key is not None:
            self.block('native_tool_or_approval_owner')
            return
        if method == 'remoteControl/status/changed':
            # Exact 0.166.0 schema has disabled/connecting/connected/errored.
            # "Disconnected" or an error is not proof of disabled admission.
            if (params.get('status') != 'disabled' or params.get('environmentId') is not None
                    or set(params) - {'status', 'environmentId', 'installationId', 'serverName'}
                    or not isinstance(params.get('installationId'), str)
                    or not isinstance(params.get('serverName'), str)):
                self.block('unowned_remote_control')
        elif method == 'thread/settings/updated':
            if (not self._root(params['threadId'])
                    or params['threadSettings']['modelProvider'] != 'openai'):
                self.block('native_provider_changed')
        elif method in ('account/updated', 'account/login/completed'):
            if self.root is not None:
                self.block('native_authentication_changed')
        elif method in ('hook/started', 'hook/completed'):
            run = params['run']
            _, digest, identity = _billing_hook_file(run['sourcePath'])
            provenance = (run['sourcePath'], digest, identity, run['eventName'])
            if (not self._root(params['threadId']) or not _identifier(run['id'])
                    or run.get('source') not in ('plugin', 'user', 'project') or run['executionMode'] != 'sync'
                    or run['handlerType'] != 'command'
                    or run['eventName'] not in ('sessionStart', 'userPromptSubmit')):
                self.block('unowned_native_hook')
            elif method == 'hook/started':
                if run['id'] in self.hooks or len(self.hook_files) >= 256:
                    self.block('ambiguous_native_hook')
                else:
                    self.hooks[run['id']] = provenance
                    self.hook_files[run['sourcePath']] = (digest, identity)
            elif self.hooks.pop(run['id'], None) != provenance or run['status'] not in (
                    'completed', 'failed', 'blocked', 'stopped'):
                self.block('unverified_hook_completion')
        elif method == 'thread/started':
            self._thread_notice(params['thread'])
        elif method == 'thread/closed':
            sid = params['threadId']
            if (sid not in self.helpers or not self.helpers[sid]
                    or any(thread == sid for thread, _ in self.turns | self.admitted)
                    or any(thread == sid for thread, _, _ in self.items)):
                self.block('unverified_native_thread_close')
            else:
                self.closed_helpers.add(sid)
        elif method == 'turn/started':
            sid, turn = params['threadId'], params['turn']['id']
            if not self._root(sid) or not _identifier(turn):
                self.block('unknown_turn_identity')
                return
            if (sid, turn) in self.completed:
                self.block('retired_turn_restarted')
            if (sid, turn) not in self.turns | self.admitted and not any(
                    op == 'turn/start' and root == sid for op, root in self.pending.values()):
                self.block('unowned_turn_start')
            self.turns.add((sid, turn))
        elif method == 'turn/completed':
            identity = params['threadId'], params['turn']['id']
            if identity not in self.turns or params['turn']['status'] not in (
                    'completed', 'interrupted', 'failed'):
                self.block('unknown_turn_completion')
                return
            self.turns.remove(identity)
            self.admitted.discard(identity)
            if len(self.completed) >= 4096:
                self.block('task_ledger_capacity')
            else:
                self.completed.add(identity)
        elif method in ('item/started', 'item/completed'):
            item = params['item']
            identity = params['threadId'], params['turnId'], item['id']
            if (identity[:2] not in self.turns or not _identifier(identity[2])
                    or item['type'] not in _PASSIVE_ITEMS):
                self.block('detached_or_untracked_tool_work')
                return
            if method == 'item/started':
                if identity in self.items or len(self.items) >= 4096:
                    self.block('ambiguous_native_item')
                else:
                    self.items.add(identity)
            elif identity not in self.items:
                self.block('unowned_item_completion')
            else:
                self.items.remove(identity)
        elif method == 'error':
            # The native retry belongs to the still-active turn. An HTTP stream
            # ending or an interrupt ACK cannot retire that owner.
            if not self.turns:
                self.block('unowned_retry_or_error')
        elif method not in _PASSIVE_NOTICES:
            self.block('untracked_native_event')

    def ready(self, thread_id):
        return (self.initialized and self.root == thread_id and self.reason is None
                and not self.pending and not self.turns and not self.items and not self.admitted
                and not self.hooks and all(self.helpers.values()))

    def status(self):
        return {'reason': self.reason, 'revision': self.revision,
                'pending_calls': len(self.pending), 'active_turns': len(self.turns),
                'active_items': len(self.items), 'active_hooks': len(self.hooks),
                'helper_threads': len(self.helpers),
                'completed_turns': len(self.completed)}


class _ObservedSocket:
    def __init__(self, socket, work):
        self.socket, self.work = socket, work

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def send(self, raw):
        self.work.observe(raw, incoming=True)
        try:
            await self.socket.send(raw)
        except BaseException:
            self.work.block('uncertain_native_send')
            raise

    async def __aiter__(self):
        try:
            async for raw in self.socket:
                self.work.observe(raw, incoming=False)
                yield raw
        finally:
            self.work.block('native_event_source_ended')


class ObservedStableTransport(StableTransport):
    """Original transport fences plus lifetime-bound task metadata observation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.work = CodexWorkLedger()

    async def _connect_backend(self):
        # Keep the existing endpoint/epoch/front checks. Install observation
        # before making the socket visible to either request or event pump.
        epoch, phase, front, generation = self._epoch, self._phase, self._client, self._generation
        path, inode, endpoint = self._up, self._inode, self._source_endpoint
        socket = await unix_connect(path, uri='ws://localhost/', proxy=None, **_LIMITS)
        try:
            async with self._lock:
                if (epoch != self._epoch or generation != self._generation
                        or phase != self._phase or phase not in ('open', 'verifying')
                        or front is not self._client or not self._front_current()
                        or self._upstream is not None or endpoint != self._source_endpoint
                        or not os.path.samestat(inode, os.stat(_path(endpoint)))):
                    raise TransportUncertain('Native endpoint or front changed during connection.')
                upstream = _ObservedSocket(socket, self.work)
                self._upstream = upstream
                self._epoch += 1
                self._up_task = asyncio.create_task(self._backend(upstream, self._epoch))
        except BaseException:
            await socket.close()
            raise

    async def resume_same_backend(self, expected_generation):
        """After payer commit, refuse held input and reopen this exact backend.

        No resume RPC, process rotation, unsent-prompt transfer or replay. A
        failed acknowledgement leaves the source closed to further submissions.
        """
        async with self._lock:
            self._check(expected_generation, 'frozen')
            if (self._dirty or self._out or self._in or self._internal
                    or not self.work.ready(self._sid)
                    or not self._front_current() or self._upstream is None
                    or self._upstream.state is not State.OPEN
                    or self._up_task is None or self._up_task.done()
                    or not os.path.samestat(self._inode, os.stat(_path(self._source_endpoint)))):
                raise TransportUncertain('Committed payer cannot reopen an unverified native backend.')
            self._refusal_revision += 1
            await self._reject_pending('Billing changed; waiting question was not sent. Re-enter your question.')
            # _front may receive another frame while the refusal send awaits
            # and wait on this lock. Invalidate that arrival stamp as well.
            self._refusal_revision += 1
            if not self.work.ready(self._sid):
                raise TransportUncertain('Native work changed while releasing committed input.')
            self._proof = None
            self._generation += 1
            self._phase = 'open'


@dataclass(frozen=True, repr=False)
class _Receipt:
    ticket: object
    stamp: tuple
    selection: object = field(repr=False)


class CodexRouterBoundary:
    """Between-task verifier for a frozen observed private Codex app-server.

    Parent MUST call assert_current(ticket) synchronously in its payer-commit
    closure. The async result is not transferable or valid after admission opens.
    expected_url is the exact owned router URL used in the launch overlay.
    """

    def __init__(self, transport, endpoint, thread_id, *, expected_url, expected_model=None):
        if not isinstance(transport, ObservedStableTransport):
            raise IntegrationError('Fixed router requires source-owned native event observation.')
        parsed = urlsplit(expected_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise IntegrationError('Expected router endpoint is not a private loopback URL.')
        self.transport, self.endpoint, self.thread_id = transport, endpoint, thread_id
        self.expected_url = expected_url
        self.expected_model = expected_model
        self.process = _OWNED_SERVERS.get(endpoint)
        self._hook_pins = {}
        self._receipt = None
        self._reason = 'not_verified'
        self._stage = 'not_started'

    def __repr__(self):
        return '<CodexRouterBoundary private-owned-source>'

    def _stamp(self):
        t = self.transport
        for path, expected in self._hook_pins.items():
            _, digest, identity = _billing_hook_file(path)
            if (digest, identity) != expected:
                raise TransportRefused('Native billing hook artifact changed during the fence.')
        if (type(self.process) is not OwnedProcess
                or _OWNED_SERVERS.get(self.endpoint) is not self.process
                or self.process.returncode is not None or t.state != 'frozen'
                or t._source_endpoint != self.endpoint or t._sid != self.thread_id
                or t._dirty or t._intent is not None or t._out or t._in or t._internal
                or not t._front_current() or t._upstream is None
                or t._upstream.state is not State.OPEN
                or t._up_task is None or t._up_task.done()
                or not os.path.samestat(t._inode, os.stat(_path(self.endpoint)))
                or not t.work.ready(self.thread_id)):
            raise TransportRefused('Native task ownership is not a held, complete boundary.')
        return (t.generation, t._epoch, t._client, t._upstream, t._up_task,
                t._instance, t._connections, t.work.revision, t._intent_revision,
                t._refusal_revision, self.process)

    async def __call__(self, ticket):
        self._receipt = None
        self._hook_pins = {}
        self._reason = 'native_boundary_unverified'
        try:
            self._stage = 'source_fence'
            stamp = self._stamp()
            # Existing exact-version reader authenticates one owned process and
            # rejects starts/settings/queue changes during its scoped inspection.
            async with policy._reader(self.endpoint) as call:
                self._stage = 'native_idle_inventory'
                live = await policy._idle(call, self.thread_id)
                if self.expected_model is not None and live.model != self.expected_model:
                    raise TransportRefused('Native model changed since the local switch command.')
                self._stage = 'native_policy'
                effective, static = await policy._configuration(call, live.cwd, {'cwds': [live.cwd]})
                self._stage = 'owned_route'
                if effective.get('openai_base_url') != self.expected_url:
                    raise TransportRefused('Native route no longer matches the owned loopback endpoint.')
                self._stage = 'native_hooks'
                for scope in ({'cwds': [live.cwd]}, {'threadId': self.thread_id}):
                    hooks = (await call('hooks/list', scope))['data']
                    if len(hooks) != 1:
                        raise TransportRefused('Native hook work has no retained ownership contract.')
                    for hook in hooks[0]['hooks']:
                        if hook.get('enabled'):
                            path, digest, identity = _known_hook_metadata(hook)
                            self._hook_pins[path] = (digest, identity)
                for path, expected in self.transport.work.hook_files.items():
                    _, digest, identity = _billing_hook_file(path)
                    if (digest, identity) != expected:
                        raise TransportRefused('Observed native hook code changed after it ran.')
                self._stage = 'native_diagnostics'
                diagnostic = await call('server/diagnostics', {})
                gauges = {entry['name']: entry['value'] for entry in diagnostic['gauges']}
                expected = {'core.threads.live': 1, 'app.requests.in_flight': 1, 'app.requests.queued': 0}
                if (diagnostic['process']['id'] != self.process.pid
                        or len(gauges) != len(diagnostic['gauges'])
                        or any(type(value) is not int for value in gauges.values())
                        or any(gauges.get(key) != value for key, value in expected.items())
                        or any(value != 0 for key, value in gauges.items() if key not in expected)):
                    raise TransportRefused('Native server still owns active or unknown work.')
                # Supplemental current-child check only. History is protected by
                # the source ledger's permanent detached/tool uncertainty latch.
                self._stage = 'owned_process_inventory'
                if not await asyncio.to_thread(policy._retirement_process_alone, self.process.pid):
                    raise TransportRefused('Native child inventory is incomplete or active.')
                self._stage = 'final_native_inventory'
                if live != await policy._idle(call, self.thread_id):
                    raise TransportRefused('Native task identity changed during inspection.')
                _, again = await policy._configuration(call, live.cwd, {'cwds': [live.cwd]})
                if static != again or stamp != self._stamp():
                    raise TransportRefused('Native boundary changed during inspection.')
            self._stage = 'final_source_fence'
            if stamp != self._stamp():
                raise TransportRefused('Native boundary changed after inspection.')
            self._receipt = _Receipt(ticket, stamp, live)
            self._reason = None
            self._stage = 'verified'
            return True
        except asyncio.CancelledError:
            self._reason = 'verification_cancelled'
            raise
        except Exception:  # noqa: BLE001 - Native frames/config never enter diagnostic text.
            self._reason = self.transport.work.reason or 'native_boundary_unverified'
            return False

    def assert_current(self, ticket):
        receipt = self._receipt
        try:
            valid = receipt is not None and receipt.ticket is ticket and receipt.stamp == self._stamp()
        except Exception:  # noqa: BLE001 - Do not expose metadata/file paths at the commit boundary.
            valid = False
        if not valid:
            raise TransportRefused('No current source-owned boundary for this payer commit.') from None

    def status(self):
        verified = False
        if self._receipt is not None:
            try:
                self.assert_current(self._receipt.ticket)
                verified = True
            except TransportRefused:
                pass
        return {**self.transport.work.status(), 'host': 'codex',
                'verified': verified, 'reason': self._reason if not self._receipt or verified
                else 'boundary_receipt_stale', 'stage': self._stage}


class ClaudeRouterBoundary:
    """Do not upgrade reloadable observer metadata into permanent admission."""

    async def __call__(self, _ticket):
        return False

    def assert_current(self, _ticket):
        raise TransportRefused('Claude fixed-process work retirement is unverified; retain the generation path.')

    def status(self):
        return {'host': 'claude', 'verified': False,
                'reason': 'reloadable_observer_and_detached_work_unverified',
                'fallback': 'existing_generation_path'}
