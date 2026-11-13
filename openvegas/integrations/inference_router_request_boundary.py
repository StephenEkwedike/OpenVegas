"""Fixed-route admission, without claiming retirement of native local work.

The native agent owns tools, permissions, history and retries. The HTTP router
owns payer admission. This module only verifies the retained native connection,
its selected model and its fixed route; it never enumerates or kills tool work.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from websockets.protocol import State

from . import codex_handoff_policy as policy
from .codex_billing_route import _OWNED_SERVERS, _POLICY_CONTEXT
from .codex_live_transport import TransportRefused, TransportUncertain, _path, _send
from .codex_stable_transport import StableTransport
from .native_owned_process import OwnedProcess


async def route_config(endpoint, cwd, expected_url=None):
    """Inspect only routing; do not replace or relax native security settings."""
    async with policy._reader(endpoint) as call:
        config = (await call('config/read', {'cwd': cwd, 'includeLayers': True}))['config']
        if config.get('model_provider') not in (None, 'openai'):
            raise TransportRefused('Native provider differs from the retained route.')
        base = config.get('openai_base_url')
        if (expected_url is not None and base != expected_url
                or expected_url is None and base not in (None, '', 'https://api.openai.com/v1')):
            raise TransportRefused('Native endpoint differs from the retained route.')
        return config


async def observe_selection(endpoint, thread_id, cwd, expected_url):
    async with policy._reader(endpoint) as call:
        thread = (await call('thread/read', {'threadId': thread_id, 'includeTurns': False}))['thread']
        selected = policy._identity(thread, thread_id, for_intent=True)
        if selected.cwd != cwd:
            raise TransportRefused('Native working directory changed.')
        config = (await call('config/read', {'cwd': cwd, 'includeLayers': True}))['config']
        if (config.get('model_provider') not in (None, 'openai')
                or config.get('openai_base_url') != expected_url):
            raise TransportRefused('Native request route changed.')
        return selected


class RequestStableTransport(StableTransport):
    """Reuse the native input/cancellation relay, not the old work inventory."""

    request_boundary = True

    def assert_request_connection(self, generation):
        self._check(generation, 'frozen')
        if (self._dirty or not self._front_current() or self._upstream is None
                or self._upstream.state is not State.OPEN
                or self._up_task is None or self._up_task.done()
                or not os.path.samestat(self._inode, os.stat(_path(self._source_endpoint)))):
            raise TransportUncertain('The retained native connection changed.')

    async def resume_same_backend(self, expected_generation):
        async with self._lock:
            self.assert_request_connection(expected_generation)
            if self._pending is not None and self.require_cancel_ack:
                try:
                    async with asyncio.timeout(3):
                        await self._pending_observed.wait()
                except TimeoutError:
                    await self._reject_pending('Not sent: terminal cancellation acknowledgement unavailable.')
            self.assert_request_connection(expected_generation)
            self._proof = None
            self._generation += 1
            self._phase = 'open'
            pending, self._pending = self._pending, None
            cancelled, self._cancelled_pending = self._cancelled_pending, None
            self._pending_cancel = None
            self.pending_changed()
            if pending is None:
                return
            raw, key, admitted_generation, timer, deadline = pending
            timer.cancel()
            if admitted_generation != expected_generation or key in self._out:
                self._fail()
                raise TransportUncertain('Queued native input identity changed.')
            if cancelled is pending or asyncio.get_running_loop().time() >= deadline:
                await self._refuse(self._client, {'id': key}, 'Not sent: waiting question cancelled or expired.')
                return
            self._out[key] = 'turn/start'
            try:
                await _send(self._upstream, raw)
            except BaseException:
                self._fail()
                raise


@dataclass(frozen=True)
class _RequestReceipt:
    ticket: object
    stamp: tuple


class CodexRequestBoundary:
    """Prove route/selection under held input, not global process quiescence."""

    def __init__(self, transport, endpoint, thread_id, *, expected_url, expected_model=None):
        self.transport, self.endpoint, self.thread_id = transport, endpoint, thread_id
        self.expected_url, self.expected_model = expected_url, expected_model
        self.process = _OWNED_SERVERS.get(endpoint)
        self._receipt = None

    def _stamp(self):
        t = self.transport
        if (not isinstance(t, RequestStableTransport) or type(self.process) is not OwnedProcess
                or _OWNED_SERVERS.get(self.endpoint) is not self.process
                or self.process.returncode is not None or t._source_endpoint != self.endpoint
                or t._sid != self.thread_id):
            raise TransportRefused('Native session no longer owns this route.')
        t.assert_request_connection(t.generation)
        return (t.generation, t._epoch, t._client, t._upstream, t._up_task,
                t._instance, t._connections, t._intent_revision, self.process)

    async def __call__(self, ticket):
        self._receipt = None
        try:
            before = self._stamp()
            cwd = _POLICY_CONTEXT[self.endpoint]['cwd']
            selected = await observe_selection(self.endpoint, self.thread_id, cwd, self.expected_url)
            if self.expected_model is not None and selected.model != self.expected_model:
                return False
            if before != self._stamp():
                return False
            self._receipt = _RequestReceipt(ticket, before)
            return True
        except Exception:  # noqa: BLE001 - Metadata never enters user-facing errors.
            return False

    def assert_current(self, ticket):
        if (self._receipt is None or self._receipt.ticket is not ticket
                or self._receipt.stamp != self._stamp()):
            raise TransportRefused('The held request route changed before commit.')

    def status(self):
        return {'host': 'codex', 'boundary_scope': 'requests', 'verified': self._receipt is not None}
