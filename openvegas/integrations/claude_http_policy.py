"""Request-level Claude payer policy; not an enabled launcher or an auth proxy.

The 2.1.322 native HTTP hook keeps credentials opaque and the TUI alive. Its
adapter must pass host requests through unchanged and rewrite only wallet
requests. A response-header acknowledgement is NOT a completed-stream receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from .credentials import IntegrationError

VERSION = '2.1.322'
MAX_BODY = 4 * 1024 * 1024
MAX_OPERATIONS = 100000
SAFE_HEADERS = frozenset({'accept', 'content-type', 'anthropic-version', 'anthropic-beta'})


def _require(condition):
    if not condition:
        raise IntegrationError('Claude request routing could not be verified. Nothing was sent; no payer fallback.')


def _body_identity(body):
    def object_fields(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Ambiguous JSON object')
        return value

    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')

    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=object_fields,
                           parse_constant=invalid_constant)
        _require(isinstance(value, dict))
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return value, hashlib.sha256(canonical.encode()).digest()
    except (ValueError, TypeError, RecursionError):
        _require(False)


def _endpoint(value):
    _require(isinstance(value, str) and value.isascii() and len(value) <= 2048
             and not any(ord(c) <= 32 or c in '\\' for c in value))
    try:
        url = urlsplit(value)
        # Match the Broker's explicit local-development backend contract.
        # Remote wallet credentials still require verified HTTPS.
        secure = url.scheme == 'https' or (
            url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'})
        _require(secure and url.hostname and not url.username
                 and not url.password and not url.fragment and not url.query
                 and url.path == '/integrations/native/v1/messages')
        _ = url.port
    except (ValueError, TypeError, AttributeError):
        _require(False)
    return value


@dataclass(frozen=True, repr=False)
class WalletRoute:
    endpoint: str
    token: str
    models: tuple[str, ...]
    expires_at: float

    def validate(self, now):
        _endpoint(self.endpoint)
        _require(isinstance(self.token, str)
                 and re.fullmatch(r'ov_native_[A-Za-z0-9_-]{16,128}', self.token)
                 and isinstance(self.models, tuple) and 1 <= len(self.models) <= 128
                 and all(isinstance(m, str) and re.fullmatch(r'[A-Za-z0-9._/-]{3,160}', m)
                         for m in self.models)
                 and len(set(self.models)) == len(self.models)
                 and type(self.expires_at) in (int, float) and now < self.expires_at < float('inf'))


@dataclass(frozen=True, repr=False)
class SwitchTicket:
    generation: int
    target: str


@dataclass(frozen=True, repr=False)
class Dispatch:
    mode: str
    generation: int
    operation_id: str
    endpoint: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'headers', MappingProxyType(dict(self.headers)))


@dataclass(repr=False)
class _Operation:
    dispatch: Dispatch
    created: float
    retry: int


class RequestRoutes:
    """Synchronous admission/commit linearization, owned by one controller.

    Native outer retries reset the SDK counter to zero. An equivalent body is
    therefore ambiguous, even at counter zero, and keeps its original ID only
    within the original payer generation. Cross-payer repeats are refused.
    This is conservative safety, NOT distinct-identical-intent certification.
    """

    def __init__(self, namespace, *, clock=time.monotonic, wall_clock=time.time):
        _require(isinstance(namespace, str) and re.fullmatch(r'[a-f0-9]{32}', namespace))
        self._namespace, self._clock, self._wall = namespace, clock, wall_clock
        self.generation, self.mode = 0, 'host'
        self._wallet = self._pending = None
        self._serial = 0
        self._operations = {}
        self._uncertain = False

    def prepare(self, target):
        _require(target in {'host', 'v'} and target != self.mode
                 and self._pending is None and not self._uncertain)
        self._pending = SwitchTicket(self.generation, target)
        return self._pending

    def commit(self, ticket, *, wallet=None, released=False):
        _require(ticket is self._pending and ticket is not None and not self._uncertain
                 and ticket.generation == self.generation)
        if ticket.target == 'v':
            _require(isinstance(wallet, WalletRoute))
            wallet.validate(self._wall())
        else:
            # The coordinator supplies this only after the exact grant's DELETE
            # acknowledgement. It must never infer it from a timer/HTTP headers.
            _require(released is True and wallet is None)
        self._wallet = wallet
        self.mode = ticket.target
        self.generation += 1
        self._pending = None
        # Retain only payer/operation tombstones, not closed grant capabilities.
        for op in self._operations.values():
            if op.dispatch.generation != self.generation:
                old = op.dispatch
                op.dispatch = Dispatch(old.mode, old.generation, old.operation_id)

    def abort(self, ticket, *, uncertain=False):
        _require(ticket is self._pending and ticket is not None)
        self._pending = None
        self._uncertain = bool(uncertain)

    def admit(self, *, path, method, body, retry_count, headers=None):
        _require(self._pending is None and not self._uncertain)
        _require(path in {'/v1/messages', '/v1/messages?beta=true'} and method == 'POST'
                 and isinstance(body, bytes) and 0 < len(body) <= MAX_BODY
                 and type(retry_count) is int and 0 <= retry_count <= 1000)
        now = self._clock()
        # URL aliases and JSON formatting cannot create a new payer identity.
        # This fingerprint never replaces or rewrites the original wire body.
        value, key = _body_identity(body)
        op = self._operations.get(key)
        if op is not None:
            _require(op.dispatch.generation == self.generation
                     and (retry_count == 0 or retry_count == op.retry + 1))
            if self.mode == 'v':
                self._wallet.validate(self._wall())
            op.retry = retry_count
            return op.dispatch
        # Do not expire payer tombstones by time: a delayed native retry can
        # outlive any chosen timeout. Fail closed at the bounded memory limit.
        _require(retry_count == 0 and len(self._operations) < MAX_OPERATIONS)
        self._serial += 1
        operation_id = f'ov.{self._namespace}.{self._serial}'
        if self.mode == 'host':
            dispatch = Dispatch('host', self.generation, operation_id)
        else:
            self._wallet.validate(self._wall())
            _require(value.get('model') in self._wallet.models)
            _require(headers is None or isinstance(headers, dict))
            clean = {}
            for name, value in (headers or {}).items():
                _require(isinstance(name, str) and isinstance(value, str))
                lower = name.lower()
                if lower in SAFE_HEADERS:
                    _require(lower not in clean and '\r' not in value and '\n' not in value
                             and value != '[redacted]')
                    clean[lower] = value
            clean['authorization'] = 'Bearer ' + self._wallet.token
            clean['idempotency-key'] = operation_id
            dispatch = Dispatch('v', self.generation, operation_id, self._wallet.endpoint, clean)
        self._operations[key] = _Operation(dispatch, now, retry_count)
        return dispatch
