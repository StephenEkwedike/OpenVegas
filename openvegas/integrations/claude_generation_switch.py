"""Private retained-PTY Claude process generations. No public launcher wiring.

The outer authenticated relay lease is caller-owned and never changes here.
Hold acknowledgements precede source capture; supported composer restoration
precedes release. This module never reads a host token, rewrites a profile,
replays submitted prompts, or issues a provider/billing request itself.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .claude_http_policy import VERSION
from .claude_payer_boundary import (
    RuntimeSnapshot,
    WalletEpoch,
    resume_argv,
    retire_owned_group,
    spawn_terminal,
)
from .credentials import IntegrationError


def _require(condition):
    if not condition:
        raise IntegrationError('Claude generation handoff refused; input remains fenced.')


@dataclass(frozen=True, repr=False)
class ComposerDraft:
    """Native plaintext and cursor in UTF-16 code units, never PTY keystrokes."""

    text: str
    cursor: int

    def __post_init__(self):
        _require(isinstance(self.text, str) and type(self.cursor) is int)
        try:
            size = len(self.text.encode('utf-16-le')) // 2
            _require(size <= 1024 * 1024 and 0 <= self.cursor <= size)
            # A cursor inside a surrogate pair cannot round-trip through fill.
            self.text.encode('utf-16-le')[:self.cursor * 2].decode('utf-16-le')
        except UnicodeError:
            _require(False)


@dataclass(frozen=True, repr=False)
class Observation:
    generation: str
    nonce: str
    snapshot: RuntimeSnapshot
    draft: ComposerDraft
    composer_ready: bool = False
    pending: tuple = ()


@dataclass(frozen=True, repr=False)
class PendingPrompt:
    """User text intercepted before native queue admission, never an in-flight turn."""

    identifier: str
    text: str

    def __post_init__(self):
        import re

        _require(isinstance(self.identifier, str) and re.fullmatch(r'[a-f0-9]{32}', self.identifier)
                 and isinstance(self.text, str) and len(self.text.encode()) <= 1024 * 1024)


@dataclass(frozen=True, repr=False)
class PreparedWallet:
    """Caller-owned listener/grant. This owner never retargets or closes it."""

    base_url: str
    gate: WalletEpoch

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            _require(url.scheme in ('http', 'https') and url.hostname in ('127.0.0.1', 'localhost', '::1')
                     and url.port and not url.username and not url.password
                     and url.path in ('', '/') and not url.query and not url.fragment
                     and isinstance(self.gate, WalletEpoch))
        except (TypeError, ValueError):
            _require(False)


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    generation: str
    retirement_seconds: float
    ready_seconds: float
    snapshot: RuntimeSnapshot = field(repr=False)


class ClaudeRuntimeOwner:
    """Awaited local changes; caller supplies authenticated fence and observer.

    observer(process=..., generation=..., nonce=..., draft=None) captures state;
    draft=<ComposerDraft> restores through prompt.fill and acknowledges readback.
    input_fence(hold=..., nonce=...) must resolve only on an authenticated outer
    lease acknowledgement. No automatic release on an ambiguous transaction.
    All calls, including write_input, run on one event loop. Caller must relay
    output continuously, handle terminal resize, and retain outer input/lease.
    """

    def __init__(self, executable, original_args, original_env, cwd, *, observer,
                 input_fence, launch_prefix=(), stop_grace=.25, version=VERSION,
                 before_spawn=None, commit=None, refuse_input=None):
        _require(version == VERSION and callable(observer) and callable(input_fence)
                 and type(stop_grace) in (int, float) and 0 <= stop_grace <= 5)
        self._executable, self._args = executable, tuple(original_args)
        self._env, self._cwd = dict(original_env), cwd
        self._observer, self._fence = observer, input_fence
        self._before_spawn, self._commit = before_spawn, commit
        self._refuse_input = refuse_input
        self._prefix, self._grace = tuple(launch_prefix), stop_grace
        self._master, self._slave = os.openpty()
        os.set_blocking(self._master, False)
        self._lock = asyncio.Lock()
        self._loop = None
        self._process = None
        self._generation = None
        self._mode, self._state = 'host', 'new'
        self._snapshot = None
        self._wallet = None
        self._retired_wallets = []
        self._fence_nonce = None
        self._reserved_identity = None

    @property
    def master_fd(self):
        return self._master

    @property
    def mode(self):
        return self._mode

    @property
    def state(self):
        return self._state

    @property
    def process(self):
        return self._process

    @property
    def generation(self):
        return self._generation

    @property
    def retired_wallets(self):
        """Keep listeners/accounting alive for accepted old work; never lose them."""
        return tuple(self._retired_wallets)

    def _same_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        _require(loop is self._loop)

    def write_input(self, data: bytes):
        self._same_loop()
        _require(self._state == 'active' and isinstance(data, bytes)
                 and self._process is not None and self._process.returncode is None)
        return os.write(self._master, data)

    def read_output(self, maximum=65536):
        self._same_loop()
        _require(type(maximum) is int and 0 < maximum <= 1024 * 1024 and self._master >= 0)
        try:
            return os.read(self._master, maximum)
        except BlockingIOError:
            return None

    def resize(self, rows, cols, xpix=0, ypix=0):
        import fcntl
        import struct
        import termios

        _require(self._master >= 0 and all(type(v) is int and 0 <= v <= 65535
                                         for v in (rows, cols, xpix, ypix)) and rows and cols)
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, xpix, ypix))

    async def _hold(self):
        self._fence_nonce = secrets.token_hex(16)
        await self._fence(hold=True, nonce=self._fence_nonce)

    async def _release(self):
        await self._fence(hold=False, nonce=self._fence_nonce)
        self._fence_nonce = None
        self._state = 'active'

    async def _observe(self, *, draft=None, action='capture', pending=()):
        nonce = secrets.token_hex(16)
        async with asyncio.timeout(10):
            extra = {} if action == 'capture' and not pending else {'action': action, 'pending': pending}
            result = await self._observer(process=self._process, generation=self._generation,
                                          nonce=nonce, draft=draft, **extra)
        if action == 'refuse':
            _require(result is True and self._process.returncode is None)
            return result
        _require(type(result) is Observation and result.generation == self._generation
                 and result.nonce == nonce and type(result.snapshot) is RuntimeSnapshot
                 and type(result.draft) is ComposerDraft and self._process.returncode is None)
        _require(type(result.pending) is tuple and len(result.pending) <= 128
                 and all(type(item) is PendingPrompt for item in result.pending)
                 and len({item.identifier for item in result.pending}) == len(result.pending))
        if action == 'capture':
            _require(result.composer_ready is True)
        if draft is not None:
            _require(result.composer_ready is True and result.draft == draft)
        return result

    def _child_env(self, mode, wallet):
        env = dict(self._env)
        env['OV_BOUNDARY_GENERATION'] = self._generation
        if mode == 'v':
            _require(type(wallet) is PreparedWallet and not wallet.gate.status['sealed']
                     and not wallet.gate.status['uncertain'])
            _require(not any(env.get(name) not in (None, '', '0') for name in (
                'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                'CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST')))
            for name in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN',
                         'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR'):
                env.pop(name, None)
            env['ANTHROPIC_BASE_URL'] = wallet.base_url
            env['ANTHROPIC_AUTH_TOKEN'] = wallet.gate.credential
            for name in ('NO_PROXY', 'no_proxy'):
                env[name] = ','.join(filter(None, (env.get(name), '127.0.0.1,localhost,::1')))
        return env

    async def _launch(self, mode, snapshot=None, wallet=None):
        from .session_ownership import fence_fields
        from .terminal_handoff import SOCKET_ENV, TOKEN_ENV

        self._generation = secrets.token_hex(16)
        env = self._child_env(mode, wallet)
        outer = (fence_fields(env), env.get(SOCKET_ENV), env.get(TOKEN_ENV))
        if self._before_spawn is not None:
            _require(self._before_spawn(mode, self._generation, env) is None)
            _require(outer == (fence_fields(env), env.get(SOCKET_ENV), env.get(TOKEN_ENV)))
        argv = ((self._executable, *self._args) if snapshot is None else
                resume_argv(self._executable, self._args, snapshot))
        self._process = await spawn_terminal((*self._prefix, *argv), env=env,
            cwd=self._cwd if snapshot is None else snapshot.cwd, terminal_fd=self._slave)

    async def _cleanup_failed_child(self):
        from .codex_billing_route import _settle

        if self._process is not None and not self._process.retired:
            await _settle(asyncio.create_task(retire_owned_group(
                self._process, grace_seconds=self._grace)))

    async def start(self):
        """Launch ordinary host UX without fencing onboarding/login/trust input.

        Returns the owned process, not an observed snapshot. Fresh observation
        is required by capture/change only after reserve_change holds input.
        No initial observer timeout can interrupt an ordinary host turn.
        """
        self._same_loop()
        async with self._lock:
            _require(self._state == 'new')
            self._state = 'starting'
            try:
                await self._launch('host')
                self._state = 'active'
                return self._process
            except BaseException:
                self._state = 'failed'
                await self._cleanup_failed_child()
                raise

    async def _reserve_locked(self):
        _require(self._state == 'active' and self._process.returncode is None)
        self._state = 'reserving'
        try:
            await self._hold()
        except BaseException:
            self._state = 'failed'
            raise
        self._reserved_identity = (self._process, self._generation, self._mode, self._wallet)
        self._state = 'reserved'
        return self._fence_nonce

    async def reserve_change(self):
        """Await BEFORE acknowledging a local billing hook; closes early-Enter gap."""
        self._same_loop()
        async with self._lock:
            return await self._reserve_locked()

    async def capture(self):
        """Fresh supported observation under an acknowledged input reservation."""
        self._same_loop()
        async with self._lock:
            _require(self._state == 'reserved')
            return await self._observe()

    async def cancel_reserved(self):
        """Explicit pre-mutation refusal: keep verified old payer and release input."""
        self._same_loop()
        async with self._lock:
            _require(self._state == 'reserved' and self._reserved_identity == (
                self._process, self._generation, self._mode, self._wallet)
                and self._process.returncode is None)
            try:
                await self._observe(action='refuse')
                if self._refuse_input is not None:
                    self._refuse_input()
                await self._release()
            except BaseException:
                # A missing refusal acknowledgement is not release authority.
                self._state = 'failed'
                raise
            self._reserved_identity = None

    async def change(self, target, *, wallet=None, commit=None, check=None):
        self._same_loop()
        async with self._lock:
            _require(self._state in ('active', 'reserved') and target in ('host', 'v') and target != self._mode
                     and (type(wallet) is PreparedWallet if target == 'v' else wallet is None))
            if self._state == 'active':
                await self._reserve_locked()
            source = self._mode
            self._state = 'changing'
            began = time.monotonic()
            retired = committed = False
            check = check or (lambda: None)
            try:
                check()
                before = await self._observe()
                check()
                # Validate prompt-free resume before stopping the current child.
                resume_argv(self._executable, self._args, before.snapshot)
                if target == 'v':
                    self._child_env(target, wallet)
                    wallet.gate.hold_admission()
                if self._wallet is not None:
                    self._wallet.gate.seal()
                    self._wallet.gate.require_transport_drained()
                    self._retired_wallets.append(self._wallet)
                    self._wallet = None
                retired = True  # Stop uncertainty must never release old input.
                receipt = await retire_owned_group(self._process, grace_seconds=self._grace)
                check()
                await self._launch(target, before.snapshot, wallet)
                check()
                after = await self._observe(draft=before.draft, pending=before.pending)
                check()
                before.snapshot.require_same_runtime(after.snapshot)
                _require(after.pending == before.pending)
                callback = commit if commit is not None else self._commit
                if callback is not None:
                    await callback(target, after.snapshot)
                self._snapshot, self._wallet, self._mode = after.snapshot, wallet, target
                committed = True
                check()
                if wallet is not None:
                    wallet.gate.activate_admission()
                await self._observe(action='release')
                check()
                elapsed = time.monotonic() - began
                await self._release()
                self._reserved_identity = None
                return Transition(source, target, self._generation, receipt.elapsed_seconds,
                                  elapsed, after.snapshot)
            except BaseException:
                self._state = 'failed' if retired else 'reserved'
                if wallet is not None:
                    wallet.gate.seal()
                    if wallet is not self._wallet and wallet not in self._retired_wallets:
                        self._retired_wallets.append(wallet)
                if retired and not committed:
                    await self._cleanup_failed_child()
                # No speculative release/fallback. In particular, an uncertain
                # release can already have delivered input to the NEW payer.
                raise

    async def close(self, *, release_input=False):
        self._same_loop()
        async with self._lock:
            if self._state == 'closed':
                return
            _require(type(release_input) is bool)
            if release_input:
                _require(self._process is not None and self._process.returncode == 0)
            self._state = 'closing'
            if self._wallet is not None:
                self._wallet.gate.seal()
                self._retired_wallets.append(self._wallet)
                self._wallet = None
            try:
                try:
                    if self._fence_nonce is None:
                        await self._hold()
                finally:
                    await self._cleanup_failed_child()
                if release_input:
                    await self._fence(hold=False, nonce=self._fence_nonce)
                    self._fence_nonce = None
            finally:
                # Keep descriptors if process retirement itself was uncertain.
                if self._process is None or self._process.retired:
                    for fd in (self._master, self._slave):
                        os.close(fd)
                    self._master = self._slave = -1
                    self._state = 'closed'
