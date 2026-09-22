"""Durable HTTP-command replay, distinct from gateway payload idempotency.

No schema writes or provider calls. Requires the private inference_route_commands
table supplied by migration 044. Its original (authenticated user, client key)
row is a typed route envelope; the actual gateway request uses a reserved derived
key in inference_requests. Only gateway rows acquire holds/usage.
Gateway payload hashing and terminal result serialization remain unchanged.

Call begin after auth/rate limits and before thread creation/history materialization, with the complete
validated AskRequest model_dump(exclude={"idempotency_key"}, mode="json"). Return
claim.response immediately when present. Otherwise pass the derived key to the
gateway, construct the route response, and call complete with an async append(tx)
callback. That callback MUST use the supplied transaction for every history
write, never open/commit another transaction or perform network work. No-op
callbacks are appropriate for non-persistent requests. Do not append first.

Processing/failed/legacy envelopes are never timed out or automatically retried:
a crash after upstream billing but before route completion requires operator
reconciliation. Completed responses survive process restarts and review/upload
expiry, because replay returns stored output without sending attachment bytes.
Keep envelope rows at least as long as their gateway rows. All /ask and /stream
workers must use this helper; drain old workers before enabling it. This is not
cross-key conversation serialization or automatic recovery of incomplete turns.
abandon_before_dispatch is only for an original caller that KNOWS the gateway
was never entered; absence of a DB row alone does not prove no upstream charge.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from openvegas.contracts.errors import APIErrorCode, ContractError

MAX_COMMAND_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ENVELOPE_BYTES = MAX_RESPONSE_BYTES + 2048
MAX_KEY_BYTES = 200
GATEWAY_KEY_PREFIX = "__inference_route_gateway_v1__:"
_KIND = "inference_route_replay_v1"
_DEFAULTS = {
    "thread_id": None,
    "conversation_mode": None,
    "persist_context": True,
    "enable_tools": False,
    "enable_web_search": False,
    "attachments": [],
    "reasoning_effort": None,
}
_COMMAND_FIELDS = {"prompt", "provider", "model", *_DEFAULTS}
_ENVELOPE_FIELDS = {"kind", "state", "owner_token", "gateway_key"}


def _invalid() -> ContractError:
    return ContractError(APIErrorCode.INVALID_TRANSITION, "Invalid or oversized replay data.")


def _blocked() -> ContractError:
    return ContractError(
        APIErrorCode.HOLD_CONFLICT,
        "Prior inference command is incomplete or unverified; no retry was made. "
        "Reconciliation is required if the original attempt cannot finish.",
    )


def _uuid(value: Any) -> str:
    if type(value) is not str or len(value) != 36:
        raise _invalid()
    try:
        parsed = UUID(value)
        if not parsed.int or str(parsed) != value:
            raise ValueError
    except ValueError:
        raise _invalid() from None
    return value


def _key(value: Any) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_KEY_BYTES
        or value.startswith(GATEWAY_KEY_PREFIX)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise _invalid()
    try:
        if len(value.encode("utf-8")) > MAX_KEY_BYTES:
            raise _invalid()
    except UnicodeError:
        raise _invalid() from None
    return value


def _bounded_json(value: Any, limit: int) -> str:
    """Validate types/shape before serialization, bounding depth and allocation."""
    nodes, text_bytes = 0, 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes, text_bytes
        nodes += 1
        if depth > 16 or nodes > 32768:
            raise _invalid()
        kind = type(item)
        if kind is str:
            if len(item) > limit:
                raise _invalid()
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > limit:
                raise _invalid()
        elif kind is dict:
            if len(item) > 32768:
                raise _invalid()
            for key, child in item.items():
                if type(key) is not str:
                    raise _invalid()
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif kind is list:
            if len(item) > 32768:
                raise _invalid()
            for child in item:
                visit(child, depth + 1)
        elif kind is int:
            if not -(2**63) <= item < 2**63:
                raise _invalid()
        elif kind is float:
            if not math.isfinite(item):
                raise _invalid()
        elif item is not None and kind is not bool:
            raise _invalid()

    try:
        visit(value, 0)
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        if len(raw.encode("utf-8")) > limit:
            raise _invalid()
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None


def command_fingerprint(command: dict[str, Any]) -> str:
    """Hash all incoming command fields, not mutable server history or run IDs.

    Ask/stream share one logical command schema. Unknown fields fail closed so a
    future request option cannot silently disappear from the identity contract.
    Upload order and exact prompt bytes are significant; default omission is not.
    This does not authorize uploads, models, or thread access.
    """
    if (
        type(command) is not dict
        or len(command) > len(_COMMAND_FIELDS)
        or command.keys() - _COMMAND_FIELDS
    ):
        raise _invalid()
    value = {**_DEFAULTS, **command}
    for name in ("prompt", "provider", "model"):
        if type(value.get(name)) is not str:
            raise _invalid()
    for name in ("provider", "model"):
        if not value[name] or len(value[name]) > 300:
            raise _invalid()
    for name in ("persist_context", "enable_tools", "enable_web_search"):
        if type(value[name]) is not bool:
            raise _invalid()
    for name in ("thread_id", "conversation_mode", "reasoning_effort"):
        if value[name] is not None and (type(value[name]) is not str or len(value[name]) > 300):
            raise _invalid()
    files = value["attachments"]
    if type(files) is not list or len(files) > 8:
        raise _invalid()
    for file_id in files:
        _uuid(file_id)
    raw = _bounded_json({"schema": _KIND, "command": value}, MAX_COMMAND_BYTES)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gateway_key(user_id: str, key: str) -> str:
    raw = json.dumps([_KIND, user_id, key], separators=(",", ":"), ensure_ascii=True)
    return GATEWAY_KEY_PREFIX + hashlib.sha256(raw.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ReplayClaim:
    """Server-internal claim; never accept this object or its token from HTTP."""

    user_id: str
    request_id: str
    idempotency_key: str = field(repr=False)
    command_hash: str
    gateway_idempotency_key: str
    owner_token: str = field(repr=False)
    _response_json: str | None = field(default=None, repr=False)

    @property
    def response(self) -> dict[str, Any] | None:
        return json.loads(self._response_json) if self._response_json is not None else None


_READ = """
SELECT id, user_id, idempotency_key, payload_hash, status, response_status,
       CASE WHEN octet_length(response_body_text) <= $3
            THEN response_body_text ELSE NULL END AS response_body_text
FROM inference_route_commands WHERE user_id=$1::uuid AND idempotency_key=$2 FOR UPDATE
"""

_PRIOR_GATEWAY = """
SELECT id FROM inference_requests
WHERE user_id=$1::uuid AND idempotency_key=ANY($2::text[]) LIMIT 1
"""


def _envelope(row: Any, *, user_id: str, key: str, digest: str) -> dict[str, Any]:
    try:
        if (
            str(row["user_id"]) != user_id
            or row["idempotency_key"] != key
            or row["payload_hash"] != digest
        ):
            raise ContractError(
                APIErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key conflict: incoming command mismatch.",
            )
        _uuid(str(row["id"]))
        raw = row["response_body_text"]
        if type(raw) is not str or len(raw) > MAX_ENVELOPE_BYTES:
            raise _blocked()
        body = json.loads(raw)
        if type(body) is not dict:
            raise _blocked()
        _bounded_json(body, MAX_ENVELOPE_BYTES)
        _uuid(body.get("owner_token"))
        if body.get("kind") != _KIND or body.get("gateway_key") != _gateway_key(user_id, key):
            raise _blocked()
        if body.get("state") == "processing":
            if (
                body.keys() != _ENVELOPE_FIELDS
                or row["status"] != "processing"
                or row["response_status"] is not None
            ):
                raise _blocked()
        elif body.get("state") == "completed":
            if (
                body.keys() != _ENVELOPE_FIELDS | {"response", "gateway_request_id"}
                or row["status"] != "succeeded"
                or row["response_status"] != 200
                or type(body["response"]) is not dict
            ):
                raise _blocked()
            _uuid(body["gateway_request_id"])
            _bounded_json(body["response"], MAX_RESPONSE_BYTES)
        else:
            raise _blocked()
        return body
    except ContractError as exc:
        if exc.code == APIErrorCode.IDEMPOTENCY_CONFLICT:
            raise
        raise _blocked() from None
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise _blocked() from None


class InferenceReplayService:
    def __init__(self, db: Any):
        self.db = db

    async def begin(
        self, *, user_id: str, idempotency_key: str, command: dict[str, Any]
    ) -> ReplayClaim:
        user_id, key = _uuid(user_id), _key(idempotency_key)
        digest = command_fingerprint(command)
        request_id, owner_token = str(uuid4()), str(uuid4())
        gateway_key = _gateway_key(user_id, key)
        initial = {
            "kind": _KIND,
            "state": "processing",
            "owner_token": owner_token,
            "gateway_key": gateway_key,
        }
        async with self.db.transaction() as tx:
            inserted = await tx.fetchrow(
                """INSERT INTO inference_route_commands
                   (id,user_id,idempotency_key,payload_hash,status,response_body_text)
                   VALUES ($1::uuid,$2::uuid,$3,$4,'processing',$5)
                   ON CONFLICT (user_id,idempotency_key) DO NOTHING RETURNING id""",
                request_id,
                user_id,
                key,
                digest,
                _bounded_json(initial, MAX_ENVELOPE_BYTES),
            )
            row = await tx.fetchrow(_READ, user_id, key, MAX_ENVELOPE_BYTES)
            if row is None:
                raise _blocked()
            body = _envelope(row, user_id=user_id, key=key, digest=digest)
            if inserted:
                if str(row["id"]) != request_id or body != initial:
                    raise _blocked()
                # Legacy/raw keys and orphaned gateway rows cannot become new
                # paid work just because a route envelope is absent.
                if await tx.fetchrow(_PRIOR_GATEWAY, user_id, [key, gateway_key]):
                    raise _blocked()
            elif body["state"] != "completed":
                raise _blocked()
            response_json = (
                _bounded_json(body["response"], MAX_RESPONSE_BYTES)
                if body["state"] == "completed"
                else None
            )
            return ReplayClaim(
                user_id,
                str(row["id"]),
                key,
                digest,
                gateway_key,
                body["owner_token"],
                response_json,
            )

    async def complete(
        self,
        claim: ReplayClaim,
        *,
        gateway_request_id: str,
        response: dict[str, Any],
        append: Callable[[Any], Awaitable[None]],
    ) -> dict[str, Any]:
        """Atomically append once and cache the original route response.

        Retransmitting a completed claim returns its original response, ignoring
        newly computed run IDs/diagnostics. Callback errors and cancellation roll
        back both writes. A lost commit acknowledgement can be retried via begin.
        """
        if type(claim) is not ReplayClaim or type(response) is not dict or not callable(append):
            raise _invalid()
        _uuid(claim.user_id)
        _uuid(claim.request_id)
        _uuid(claim.owner_token)
        _key(claim.idempotency_key)
        _uuid(gateway_request_id)
        response_json = _bounded_json(response, MAX_RESPONSE_BYTES)
        # Snapshot the caller's mutable dict before the first suspension point.
        snapshot = json.loads(response_json)
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(_READ, claim.user_id, claim.idempotency_key, MAX_ENVELOPE_BYTES)
            if row is None:
                raise _blocked()
            body = _envelope(
                row, user_id=claim.user_id, key=claim.idempotency_key, digest=claim.command_hash
            )
            if (
                str(row["id"]) != claim.request_id
                or body["owner_token"] != claim.owner_token
                or body["gateway_key"] != claim.gateway_idempotency_key
            ):
                raise _blocked()
            if body["state"] == "completed":
                if body["gateway_request_id"] != gateway_request_id:
                    raise _blocked()
                return body["response"]
            # Settled gateway rows are terminal/immutable. Do not take their row
            # lock before the callback's thread lock (reconciliation uses the
            # opposite lock order). The envelope lock serializes route writers.
            gateway = await tx.fetchrow(
                """SELECT id, user_id, idempotency_key, status, response_status,
                          inference_source
                   FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid""",
                gateway_request_id,
                claim.user_id,
            )
            if (
                gateway is None
                or str(gateway["id"]) != gateway_request_id
                or str(gateway["user_id"]) != claim.user_id
                or gateway["idempotency_key"] != claim.gateway_idempotency_key
                or gateway["status"] != "succeeded"
                or gateway["response_status"] != 200
                or gateway["inference_source"] != "wrapper"
            ):
                raise _blocked()
            finished = {
                **body,
                "state": "completed",
                "gateway_request_id": gateway_request_id,
                "response": snapshot,
            }
            serialized = _bounded_json(finished, MAX_ENVELOPE_BYTES)
            await append(tx)
            updated = await tx.fetchrow(
                """UPDATE inference_route_commands
                   SET status='succeeded', response_status=200, response_body_text=$3,
                       updated_at=now()
                   WHERE id=$1::uuid AND user_id=$2::uuid AND status='processing'
                   RETURNING id""",
                claim.request_id,
                claim.user_id,
                serialized,
            )
            if updated is None or str(updated["id"]) != claim.request_id:
                raise _blocked()
        return snapshot

    async def abandon_before_dispatch(self, claim: ReplayClaim) -> bool:
        """Delete a proven preflight-only claim; never call after entering gateway.

        The original caller must know no infer/stream_infer invocation started,
        including a cancelled/background invocation. This is not an exception
        cleanup hook: a provider may have billed before its DB state is visible.
        A visible gateway row of ANY status, completed envelope, or token mismatch
        blocks removal. Returns False when a previous removal already committed.
        No stale takeover, row expiry, gateway mutation, or paid retry is performed.
        """
        if type(claim) is not ReplayClaim:
            raise _invalid()
        _uuid(claim.user_id)
        _uuid(claim.request_id)
        _uuid(claim.owner_token)
        _key(claim.idempotency_key)
        if claim.gateway_idempotency_key != _gateway_key(claim.user_id, claim.idempotency_key):
            raise _blocked()
        async with self.db.transaction() as tx:
            row = await tx.fetchrow(_READ, claim.user_id, claim.idempotency_key, MAX_ENVELOPE_BYTES)
            if await tx.fetchrow(
                _PRIOR_GATEWAY,
                claim.user_id,
                [claim.idempotency_key, claim.gateway_idempotency_key],
            ):
                raise _blocked()
            if row is None:
                return False
            body = _envelope(
                row,
                user_id=claim.user_id,
                key=claim.idempotency_key,
                digest=claim.command_hash,
            )
            if (
                str(row["id"]) != claim.request_id
                or body["owner_token"] != claim.owner_token
                or body["state"] != "processing"
            ):
                raise _blocked()
            removed = await tx.fetchrow(
                """DELETE FROM inference_route_commands
                   WHERE id=$1::uuid AND user_id=$2::uuid AND status='processing'
                   RETURNING id""",
                claim.request_id,
                claim.user_id,
            )
            if removed is None or str(removed["id"]) != claim.request_id:
                raise _blocked()
        return True
