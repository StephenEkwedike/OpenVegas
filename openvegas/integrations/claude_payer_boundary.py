"""Private process-generation primitives, NOT an enabled Claude payer switch.

A wallet generation has one destination for its entire lifetime. Its credential
and admission state belong to the supervisor, not a reloadable native mod.
Attempt IDs identify physical sends, never logical native operations. Ending a
process group does not prove that detached sessions or supplier work ended.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import UUID

import httpx

from .claude_http_policy import MAX_BODY, SAFE_HEADERS, WalletRoute, _body_identity
from .credentials import IntegrationError


def _require(condition):
    if not condition:
        raise IntegrationError('Claude process boundary refused; no payer fallback.')


@dataclass(frozen=True, repr=False)
class RuntimeSnapshot:
    """Metadata from a fresh supported native observation, not a drain proof.

    The adapter must bind this to its one-use control nonce and generation.
    Settings/tools hashes exclude secrets and must be recomputed after resume.
    An effort from an older turn is NOT a current-effort observation.
    """

    session_id: str
    cwd: str
    model: str
    effort: str | None
    settings_digest: str
    tools_digest: str

    def __post_init__(self):
        try:
            valid_id = isinstance(self.session_id, str) and str(UUID(self.session_id)) == self.session_id
        except ValueError:
            valid_id = False
        _require(valid_id and isinstance(self.cwd, str) and self.cwd.startswith('/')
                 and '\x00' not in self.cwd
                 and isinstance(self.model, str)
                 and re.fullmatch(r'claude-[a-zA-Z0-9._-]{3,150}', self.model)
                 and self.effort in (None, 'low', 'medium', 'high', 'xhigh', 'max')
                 and all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value)
                         for value in (self.settings_digest, self.tools_digest)))

    def require_same_runtime(self, resumed: RuntimeSnapshot):
        _require(type(resumed) is RuntimeSnapshot and self == resumed)


def resume_argv(executable: str, original: tuple[str, ...], snapshot: RuntimeSnapshot):
    """Build a candidate, not launch authority. Never replay a task or fork chat.

    Preserve supported permission/tool arguments verbatim. Replace stale startup
    model AND effort with captured native values. Refuse unknown syntax, not tools.
    """
    _require(type(snapshot) is RuntimeSnapshot and isinstance(executable, str)
             and executable.startswith('/') and '\x00' not in executable)
    value_options = {'--permission-mode', '--add-dir', '--plugin-dir', '--settings',
                     '--setting-sources', '--mcp-config', '--allowedTools', '--allowed-tools',
                     '--disallowedTools', '--disallowed-tools', '--tools', '--agent',
                     '--append-system-prompt', '--append-system-prompt-file',
                     '--system-prompt', '--system-prompt-file', '--teammate-mode'}
    switches = {'--strict-mcp-config', '--verbose', '--dangerously-skip-permissions',
                '--allow-dangerously-skip-permissions', '--chrome', '--no-chrome'}
    args, index = [], 0
    while index < len(original):
        arg = original[index]
        _require(isinstance(arg, str))
        key, equal, value = arg.partition('=')
        if key in {'--effort', '--model', '-m', '--resume', '-r'} | value_options:
            if not equal:
                _require(index + 1 < len(original) and isinstance(original[index + 1], str))
                value = original[index + 1]
                index += 1
            _require(bool(value) and not value.startswith('-') and '\x00' not in value)
            if key == '--effort':
                _require(value in ('low', 'medium', 'high', 'xhigh', 'max'))
            elif key in ('--resume', '-r'):
                _require(value == snapshot.session_id)
            elif key in value_options:
                args.extend((arg,) if equal else (arg, value))
            index += 1
            continue
        _require(arg in switches or arg in ('--continue', '-c'))
        if arg in ('--continue', '-c'):
            index += 1
            continue
        args.append(arg)
        index += 1
    # None delegates effort restoration to native --resume; it is not proof of
    # effort equality. Never fill it from a prior turn or stale startup option.
    return (executable, *args, '--resume', snapshot.session_id, '--model', snapshot.model,
            *(('--effort', snapshot.effort) if snapshot.effort is not None else ()))


@dataclass(frozen=True)
class GroupRetirement:
    """Only the pinned child's POSIX group. Deliberately not named a tree proof."""

    elapsed_seconds: float
    scope: str = field(default='owned-process-group-only', init=False)


async def retire_owned_group(process, *, grace_seconds=None):
    """Reuse cancellation-settled shutdown; accept no arbitrary PID/leader."""
    from .codex_billing_route import _retired_group, _settle, stop
    from .native_owned_process import OwnedProcess

    _require(isinstance(process, OwnedProcess))
    started = time.monotonic()
    if grace_seconds is None:
        await stop(process, process_group=process.pid)
    else:
        _require(type(grace_seconds) in (int, float) and 0 <= grace_seconds <= 5)

        async def retire():
            async with process.stop_lock:
                if process.retired:
                    return
                _ = process.returncode  # Retained child identity, not a numeric PID guess.
                if not await _retired_group(process.pid):
                    process.signal_group(signal.SIGTERM)
                    deadline = time.monotonic() + grace_seconds
                    while not await _retired_group(process.pid):
                        if time.monotonic() >= deadline:
                            process.signal_group(signal.SIGKILL)
                            async with asyncio.timeout(5):
                                while not await _retired_group(process.pid):
                                    await asyncio.sleep(.01)
                            break
                        await asyncio.sleep(.01)
                await asyncio.wait_for(process.wait(), 5)
                process.reap_retired_group()

        _, cancelled = await _settle(asyncio.create_task(retire()))
        if cancelled:
            raise asyncio.CancelledError
    _require(process.retired)
    return GroupRetirement(time.monotonic() - started)


async def spawn_terminal(argv, *, env, cwd, terminal_fd):
    """Pinned new-session TUI child on the supervisor's retained PTY slave.

    Extend the same private ownership factory, not an arbitrary PID adoption.
    Direct descriptors avoid a shell acquiring/changing the PTY job-control state.
    The parent must pause input forwarding until post-resume validation succeeds.
    """
    from . import native_owned_process as owned
    from .codex_billing_route import _settle

    _require(type(terminal_fd) is int and terminal_fd >= 0 and os.isatty(terminal_fd)
             and isinstance(cwd, str) and cwd.startswith('/')
             and isinstance(argv, (tuple, list)) and len(argv) > 0
             and all(isinstance(arg, str) and '\x00' not in arg for arg in argv)
             and argv[0].startswith('/'))
    owned._contract()
    command, environment = tuple(argv), dict(env)
    fd = os.dup(terminal_fd)

    def launch():
        try:
            owned._contract()
            return owned.OwnedProcess(subprocess.Popen(command, env=environment, cwd=cwd,
                stdin=fd, stdout=fd, stderr=fd, start_new_session=True), owned._KEY)
        finally:
            os.close(fd)

    process, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(launch)))
    if cancelled:
        await retire_owned_group(process)
        raise asyncio.CancelledError
    return process


@dataclass(frozen=True, repr=False)
class Attempt:
    epoch: str
    sequence: int


class WalletEpoch:
    """Wallet-only ASGI Messages gate, independently owned by a supervisor.

    No host URL, host credentials, redirect following, payer mutation, automatic
    retry or body-hash deduplication. Closing admission is irreversible. The
    hosting supervisor must keep this listener alive as a refusing endpoint
    until its child is retired. This alone does not confine a native process's
    other egress, replace a grant-close receipt, or prove upstream settlement.
    """

    def __init__(self, route: WalletRoute, *, transport=None, wall_clock=time.time):
        route.validate(wall_clock())
        self._route = route
        self._clock = wall_clock
        self._transport = transport
        self._credential = secrets.token_urlsafe(32)
        self._epoch = secrets.token_hex(16)
        self._sequence = 0
        self._sealed = False
        self._ready = False
        self._uncertain = False
        self._active: set[Attempt] = set()
        self._completed = 0

    @property
    def credential(self):
        """Ephemeral gateway-only child credential; never a subscription token."""
        return self._credential

    @property
    def status(self):
        return MappingProxyType({'sealed': self._sealed, 'uncertain': self._uncertain,
                                 'active': len(self._active), 'completed': self._completed})

    def seal(self):
        """No await: serialized with ASGI admission on the owning event loop."""
        self._sealed = True

    def hold_admission(self):
        _require(not self._sealed and not self._uncertain and not self._active)
        self._ready = False

    def activate_admission(self):
        _require(not self._sealed and not self._uncertain)
        self._ready = True

    def require_transport_drained(self):
        _require(self._sealed and not self._active and not self._uncertain)

    async def __call__(self, scope, receive, send):
        _require(scope.get('type') == 'http')
        # A bounded body read is still pre-admission. Recheck sealed/expiry after
        # reading, so a slow client cannot cross the generation's close barrier.
        raw_headers = scope.get('headers', ())
        headers = {}
        for key, value in raw_headers:
            key = key.lower()
            if key in headers:
                return await self._deny(send)
            headers[key] = value
        supplied = headers.get(b'authorization', b'')
        expected = ('Bearer ' + self._credential).encode()
        models = scope.get('method') == 'GET' and scope.get('path') == '/v1/models'
        messages = scope.get('method') == 'POST' and scope.get('path') == '/v1/messages'
        if (not hmac.compare_digest(supplied, expected) or b'x-api-key' in headers
                or self._sealed or self._uncertain or (not models and not self._ready)
                or not (models or messages)
                or scope.get('query_string', b'') not in (b'', b'beta=true')
                or headers.get(b'content-encoding', b'identity') != b'identity'):
            return await self._deny(send)
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                while True:
                    event = await receive()
                    if event['type'] == 'http.disconnect':
                        return
                    _require(event['type'] == 'http.request')
                    body.extend(event.get('body', b''))
                    _require(len(body) <= MAX_BODY)
                    if not event.get('more_body', False):
                        break
            self._route.validate(self._clock())
            _require(not self._sealed and not self._uncertain and len(self._active) < 128)
            if models:
                _require(not body)
            else:
                value, _ = _body_identity(bytes(body))
                _require(self._ready and value.get('model') in self._route.models)
        except (IntegrationError, TimeoutError):
            return await self._deny(send)

        self._sequence += 1
        attempt = Attempt(self._epoch, self._sequence)
        self._active.add(attempt)
        upstream_headers = {key.decode('ascii'): val.decode('latin-1')
                            for key, val in headers.items() if key in
                            {name.encode() for name in SAFE_HEADERS}}
        upstream_headers['authorization'] = 'Bearer ' + self._route.token
        # Nothing awaitable between validation/admission and setting ownership.
        started = False
        complete = False
        try:
            async with httpx.AsyncClient(transport=self._transport, trust_env=False,
                                         follow_redirects=False, timeout=60) as client:
                endpoint = self._route.endpoint
                if models:
                    endpoint = endpoint.removesuffix('/messages') + '/models'
                async with client.stream('GET' if models else 'POST', endpoint, headers=upstream_headers,
                                         content=bytes(body)) as response:
                    # Redirects may name an origin with a different payer. Do not
                    # let the native client follow them either.
                    if 300 <= response.status_code < 400:
                        raise IntegrationError('Wallet redirect refused.')
                    await send({'type': 'http.response.start', 'status': response.status_code,
                                'headers': [(b'content-type', response.headers.get(
                                    'content-type', 'application/json').encode('latin-1'))]})
                    started = True
                    async for chunk in response.aiter_bytes():
                        await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                complete = True
        except BaseException as exc:
            # Even cancellation or a connect error can follow a supplier send.
            # Never claim zero cost, replay, change payer, or unseal this epoch.
            self._uncertain = True
            self.seal()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            if not started:
                await self._deny(send)
            else:
                # Abort the downstream stream rather than manufacture success.
                raise IntegrationError('Wallet response incomplete; epoch sealed.') from None
        finally:
            self._active.remove(attempt)
            if complete:
                self._completed += 1

    @staticmethod
    async def _deny(send):
        await send({'type': 'http.response.start', 'status': 503,
                    'headers': [(b'content-type', b'application/json'),
                                (b'x-should-retry', b'false')]})
        await send({'type': 'http.response.body', 'body':
                    b'{"type":"error","error":{"type":"api_error",'
                    b'"message":"Wallet generation unavailable; no payer fallback."}}'})
