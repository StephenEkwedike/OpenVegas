"""Operator-only recovery of a committed canonical text turn, never billing repair.

The gateway commits its response, usage, hold and ledger together. Only that
terminal outcome can justify restoring a missing conversation append. A failed
request, a released hold, elapsed time, or absence of usage does NOT prove that
an upstream provider did not bill. Those cases remain blocked.

No provider SDK, wallet writer, pricing calculator or retry path is imported.
Call inspect_turn in a read-only repeatable-read transaction. restore_turn owns
one transaction and uses the same thread -> request -> hold -> account lock order
as the existing writers. The database credential is the authorization boundary;
operator_id is an audit identity, not an authentication credential.

Requires the coordinator-reviewed private audit migration described by
AUDIT_SCHEMA_PROPOSAL below. This module never executes schema changes. Do not
store receipts in wallet_history_projection: its metadata is customer-visible.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from openvegas.gateway.conversation import (
    CanonicalConversation,
    validate_text,
)

ACTION = "restore_completed_exchange"
# Proposed contract only, not an installer. Coordinator owns the actual migration.
AUDIT_SCHEMA_PROPOSAL = """
CREATE TABLE public.inference_turn_reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    thread_id UUID NOT NULL REFERENCES public.provider_threads(id),
    request_id UUID NOT NULL REFERENCES public.inference_requests(id),
    operator_id UUID NOT NULL,
    plan_token TEXT NOT NULL CHECK (plan_token ~ '^[0-9a-f]{64}$'),
    details JSONB NOT NULL CHECK (jsonb_typeof(details) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, thread_id, request_id)
);
ALTER TABLE public.inference_turn_reconciliations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.inference_turn_reconciliations FROM anon, authenticated;
"""
_SCALE = Decimal("0.000001")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JSON = 6_020_000


class ReconciliationError(ValueError):
    """Static reason codes only: never include prompts, secrets or driver errors."""


def canonical_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
        if not parsed.int or str(parsed) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise ReconciliationError("INVALID_IDENTIFIER") from None


def _json(value: Any) -> dict:
    try:
        if isinstance(value, str):
            if len(value) > _MAX_JSON:
                raise ValueError
            value = json.loads(value)
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (ValueError, TypeError, RecursionError):
        raise ReconciliationError("INVALID_STORED_EVIDENCE") from None


def _money(value: Any) -> Decimal:
    try:
        if isinstance(value, (bool, float)) or len(str(value)) > 40:
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number < Decimal("1e18"):
            raise ValueError
        if number != number.quantize(_SCALE):
            raise ValueError
        return number.quantize(_SCALE)
    except (InvalidOperation, ValueError, TypeError):
        raise ReconciliationError("INVALID_STORED_AMOUNT") from None


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_payload_hash(
    history: CanonicalConversation, *, provider: str, model: str, prompt: str, max_tokens: int
) -> str:
    """Match AIGateway._payload_hash for the exact strict text-only request.

    The prompt is not persisted in inference_requests; caller-supplied recovery
    text is accepted only with this stored SHA-256 commitment, never by resemblance.
    A contract test compares this with the gateway to detect future schema drift.
    """
    validate_text(prompt)
    if type(max_tokens) is not int or not 1 <= max_tokens <= 2_147_483_647:
        raise ReconciliationError("INVALID_OUTPUT_BUDGET")
    return _digest(
        {
            "provider": provider,
            "model": model,
            "messages": history.messages() + [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "enable_tools": False,
            "enable_web_search": False,
            "strict_continuity": True,
        }
    )


@dataclass
class _Evidence:
    thread: dict
    records: list[dict]
    request: dict
    holds: list[dict]
    usage: list[dict]
    ledger: list[dict]
    escrow: dict | None
    charges: list[dict]
    receipts: list[dict]


async def _read(tx, user: str, thread: str, request: str, *, lock: bool) -> _Evidence:
    suffix = " FOR UPDATE" if lock else ""
    thread_row = await tx.fetchrow(
        "SELECT id, user_id, provider, model_id, expires_at FROM provider_threads "
        "WHERE id=$1::uuid AND user_id=$2::uuid" + suffix,
        thread,
        user,
    )
    if thread_row is None:
        raise ReconciliationError("SCOPED_TURN_NOT_FOUND")
    records = await tx.fetch(
        "SELECT id, role, content FROM provider_thread_messages "
        "WHERE thread_id=$1::uuid ORDER BY created_at, id LIMIT 2",
        thread,
    )
    row = await tx.fetchrow(
        "SELECT id, user_id, idempotency_key, payload_hash, status, inference_source, "
        "response_status, response_body_text, final_charge_v, final_provider_cost_usd, "
        "provider_request_id FROM inference_requests "
        "WHERE id=$1::uuid AND user_id=$2::uuid" + suffix,
        request,
        user,
    )
    if row is None:
        raise ReconciliationError("SCOPED_TURN_NOT_FOUND")
    holds = await tx.fetch(
        "SELECT id, request_id, user_id, account_id, provider, model_id, status, "
        "reserved_v, settled_v FROM inference_preauthorizations "
        "WHERE request_id=$1 LIMIT 2" + suffix,
        request,
    )
    reference = f"infer-preauth:{holds[0]['id']}" if len(holds) == 1 else ""
    escrow_id = f"escrow:{reference}"
    if lock:
        # Matches WalletService's sorted account locks, blocking conforming ledger
        # writers while the exact reference entries are verified. No balance writes.
        await tx.fetch(
            "SELECT account_id FROM wallet_accounts WHERE account_id=ANY($1::text[]) "
            "ORDER BY account_id FOR UPDATE",
            sorted({f"user:{user}", "store", escrow_id}),
        )
    ledger = await tx.fetch(
        "SELECT id, reference_id, entry_type, debit_account, credit_account, amount "
        "FROM ledger_entries WHERE reference_id=ANY($1::text[]) ORDER BY id LIMIT 65",
        [reference, reference + ":extra", request, request + ":extra"],
    )
    usage = await tx.fetch(
        "SELECT id, request_id, user_id, account_id, provider, model_id, input_tokens, "
        "output_tokens, v_cost, actual_cost_usd, inference_source FROM inference_usage "
        "WHERE request_id=$1::uuid LIMIT 2",
        request,
    )
    charges = await tx.fetch(
        "SELECT event_id, user_id, display_amount_v, display_status, metadata_json "
        "FROM wallet_history_projection WHERE request_id=$1::uuid "
        "AND event_type='ai_usage_charge' LIMIT 2",
        request,
    )
    try:
        receipts = await tx.fetch(
            "SELECT user_id, thread_id, request_id, operator_id, plan_token, details "
            "FROM inference_turn_reconciliations WHERE request_id=$1::uuid "
            "AND user_id=$2::uuid AND thread_id=$3::uuid LIMIT 2",
            request,
            user,
            thread,
        )
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "42P01":
            raise ReconciliationError("PRIVATE_AUDIT_MIGRATION_REQUIRED") from None
        raise
    escrow = await tx.fetchrow(
        "SELECT balance FROM wallet_accounts WHERE account_id=$1",
        escrow_id,
    )
    return _Evidence(
        dict(thread_row),
        [dict(r) for r in records],
        dict(row),
        [dict(r) for r in holds],
        [dict(r) for r in usage],
        [dict(r) for r in ledger],
        dict(escrow) if escrow else None,
        [dict(r) for r in charges],
        [dict(r) for r in receipts],
    )


def _verify_settlement(e: _Evidence, user: str, request: str) -> dict:
    if len(e.holds) != 1 or len(e.usage) != 1 or len(e.charges) != 1:
        raise ReconciliationError("INCOMPLETE_SETTLEMENT_EVIDENCE")
    hold, usage, charge_row = e.holds[0], e.usage[0], e.charges[0]
    account = f"user:{user}"
    for row in (hold, usage):
        if (
            str(row["user_id"]) != user
            or row["account_id"] != account
            or str(row["request_id"]) != request
            or row["provider"] != e.thread["provider"]
            or row["model_id"] != e.thread["model_id"]
        ):
            raise ReconciliationError("SETTLEMENT_SCOPE_MISMATCH")
    cost = _money(e.request["final_charge_v"])
    reserved = _money(hold["reserved_v"])
    if (
        e.request["inference_source"] != "wrapper"
        or usage["inference_source"] != "wrapper"
        or e.request["response_status"] != 200
        or hold["status"] != ("settled" if cost else "refunded")
        or _money(hold["settled_v"]) != cost
        or _money(usage["v_cost"]) != cost
        or _money(usage["actual_cost_usd"]) != _money(e.request["final_provider_cost_usd"])
    ):
        raise ReconciliationError("SETTLEMENT_MISMATCH")
    reference = f"infer-preauth:{canonical_uuid(str(hold['id']))}"
    escrow = f"escrow:{reference}"
    expected = {}
    for ref, kind, debit, credit, amount in (
        (reference, "reserve", account, escrow, reserved),
        (reference, "reserve_settle", escrow, "store", min(cost, reserved)),
        (reference, "reserve_refund", escrow, account, max(reserved - cost, Decimal(0))),
        (reference + ":extra", "redeem", account, "store", max(cost - reserved, Decimal(0))),
    ):
        if amount:
            expected[(ref, kind, debit, credit)] = amount
    observed = {}
    for row in e.ledger:
        key = tuple(
            row[k] for k in ("reference_id", "entry_type", "debit_account", "credit_account")
        )
        amount = _money(row["amount"])
        if key in observed or amount <= 0:
            raise ReconciliationError("LEDGER_MISMATCH")
        observed[key] = amount
    if observed != expected or (reserved and e.escrow is None):
        raise ReconciliationError("LEDGER_MISMATCH")
    if e.escrow is not None and _money(e.escrow["balance"]) != 0:
        raise ReconciliationError("ESCROW_NOT_CLOSED")
    metadata = _json(charge_row["metadata_json"])
    # Negative amounts are allowed only on this display projection, not ledger rows.
    if (
        str(charge_row["user_id"]) != user
        or str(charge_row["display_status"]) != "completed"
        or _money(-Decimal(str(charge_row["display_amount_v"]))) != cost
        or any(
            metadata.get(key) != usage[key]
            for key in ("provider", "model_id", "input_tokens", "output_tokens")
        )
    ):
        raise ReconciliationError("CHARGE_PROJECTION_MISMATCH")
    body = _json(e.request["response_body_text"])
    if (
        _money(body.get("v_cost")) != cost
        or _money(body.get("actual_cost_usd")) != _money(usage["actual_cost_usd"])
        or body.get("provider_request_id") != e.request["provider_request_id"]
    ):
        raise ReconciliationError("RESPONSE_SETTLEMENT_MISMATCH")
    for field in ("input_tokens", "output_tokens"):
        value = body.get(field)
        if type(value) is not int or value < 0 or value != usage[field]:
            raise ReconciliationError("RESPONSE_USAGE_MISMATCH")
    return body


def _receipt(e: _Evidence, user: str, thread: str, request: str) -> dict | None:
    if not e.receipts:
        return None
    if len(e.receipts) != 1:
        raise ReconciliationError("AUDIT_CONFLICT")
    row = e.receipts[0]
    metadata = _json(row["details"])
    if (
        str(row["user_id"]) != user
        or str(row["thread_id"]) != thread
        or str(row["request_id"]) != request
        or str(row["operator_id"]) != metadata.get("operator_id")
        or row["plan_token"] != metadata.get("plan_token")
        or metadata.get("version") != 1
        or metadata.get("action") != ACTION
        or metadata.get("thread_id") != thread
        or metadata.get("request_id") != request
        or any(
            not _HEX.fullmatch(str(metadata.get(key, "")))
            for key in ("plan_token", "before_revision", "after_revision")
        )
    ):
        raise ReconciliationError("AUDIT_CONFLICT")
    canonical_uuid(metadata.get("operator_id"))
    return metadata


def _evaluate(
    e: _Evidence, user: str, thread: str, request: str, *, prompt: str | None, max_tokens: int
) -> tuple[dict, CanonicalConversation | None]:
    report = {
        "user_id": user,
        "thread_id": thread,
        "request_id": request,
        "status": "blocked",
        "reason": "UNCONFIRMED_PROVIDER_OUTCOME",
        "request_status": e.request["status"],
        "can_restore": False,
        "wallet_mutation": False,
        "provider_called": False,
        "provider_invoice_verified": False,
        "settlement_verified": False,
    }
    receipt = _receipt(e, user, thread, request)
    if receipt:
        return {
            **report,
            "status": "already_reconciled",
            "reason": "HISTORICAL_RECEIPT",
            "plan_token": receipt["plan_token"],
            "revision": receipt["after_revision"],
        }, None
    if len(e.records) != 1 or e.records[0]["role"] != "system":
        raise ReconciliationError("UNSUPPORTED_HISTORY")
    content = _json(e.records[0]["content"])
    pending = content.get("pending")
    if pending is None:
        return {**report, "reason": "NO_PENDING_TURN"}, None
    if pending != e.request["idempotency_key"]:
        raise ReconciliationError("PENDING_REQUEST_MISMATCH")
    canonical_uuid(pending)
    history = CanonicalConversation.from_storage(
        {k: v for k, v in content.items() if k != "pending"}
    )
    if e.request["status"] != "succeeded":
        # In particular, "failed" + "voided" proves only local hold release.
        return report, None
    body = _verify_settlement(e, user, request)
    report.update(
        reason="ORIGINAL_PROMPT_REQUIRED",
        settlement_verified=True,
        final_charge_v=str(_money(e.request["final_charge_v"])),
    )
    expires = e.thread["expires_at"]
    if not isinstance(expires, datetime) or expires.tzinfo is None or expires <= datetime.now(UTC):
        return {**report, "reason": "THREAD_EXPIRED_OR_UNBOUNDED"}, None
    if (
        body.get("completion_status") != "complete"
        or body.get("tool_calls") != []
        or body.get("web_search_used") is not False
        or body.get("web_search_sources") != []
        or body.get("web_search_retry_without_tool") is not False
    ):
        return {**report, "reason": "RESPONSE_NOT_COMPLETE_PLAIN_TEXT"}, None
    validate_text(body.get("text"))
    if prompt is None:
        return report, None
    expected = request_payload_hash(
        history,
        provider=e.thread["provider"],
        model=e.thread["model_id"],
        prompt=prompt,
        max_tokens=max_tokens,
    )
    if expected != e.request["payload_hash"]:
        raise ReconciliationError("ORIGINAL_REQUEST_HASH_MISMATCH")
    restored = history.append(prompt, body["text"])
    # Bind the reviewed plan to all relevant persisted facts, not just the input.
    token = _digest(vars(e))
    return {
        **report,
        "status": "recoverable_completed",
        "reason": "COMMITTED_FACTS_VERIFIED",
        "can_restore": True,
        "plan_token": token,
        "before_revision": history.revision,
        "revision": restored.revision,
    }, restored


async def inspect_turn(
    tx,
    *,
    user_id: str,
    thread_id: str,
    request_id: str,
    prompt: str | None = None,
    max_tokens: int = 1024,
) -> dict:
    """No writes/locks; caller must supply a consistent read-only DB transaction."""
    user, thread, request = map(canonical_uuid, (user_id, thread_id, request_id))
    evidence = await _read(tx, user, thread, request, lock=False)
    report, _ = _evaluate(evidence, user, thread, request, prompt=prompt, max_tokens=max_tokens)
    return report


async def restore_turn(
    db,
    *,
    user_id: str,
    thread_id: str,
    request_id: str,
    operator_id: str,
    expected_plan: str,
    prompt: str,
    max_tokens: int,
) -> dict:
    """Restore one verified completed exchange and append a private audit receipt.

    Never reset processing/failed requests or alter their holds. An ambiguous commit
    must be inspected using the same scope; this function has no automatic retries.
    Concurrent matching calls serialize on the thread row and replay the receipt.
    """
    user, thread, request, operator = map(
        canonical_uuid, (user_id, thread_id, request_id, operator_id)
    )
    if not isinstance(expected_plan, str) or not _HEX.fullmatch(expected_plan):
        raise ReconciliationError("REVIEWED_PLAN_REQUIRED")
    async with db.transaction() as tx:
        evidence = await _read(tx, user, thread, request, lock=True)
        report, restored = _evaluate(
            evidence, user, thread, request, prompt=prompt, max_tokens=max_tokens
        )
        if report.get("plan_token") != expected_plan:
            raise ReconciliationError("REVIEWED_PLAN_CHANGED")
        if report["status"] == "already_reconciled":
            return {**report, "replayed": True}
        if not report["can_restore"] or restored is None:
            raise ReconciliationError("TURN_NOT_RECOVERABLE")
        message_id = canonical_uuid(str(evidence.records[0]["id"]))
        updated = await tx.execute(
            "UPDATE provider_thread_messages SET content=$2::jsonb "
            "WHERE id=$1::uuid AND thread_id=$3::uuid AND content=$4::jsonb",
            message_id,
            restored.to_json(),
            thread,
            json.dumps(_json(evidence.records[0]["content"])),
        )
        if updated != "UPDATE 1":
            raise ReconciliationError("HISTORY_CHANGED")
        await tx.execute(
            "UPDATE provider_threads SET last_used_at=now(), updated_at=now() "
            "WHERE id=$1::uuid AND user_id=$2::uuid",
            thread,
            user,
        )
        receipt = {
            "version": 1,
            "action": ACTION,
            "operator_id": operator,
            "thread_id": thread,
            "request_id": request,
            "plan_token": expected_plan,
            "before_revision": report["before_revision"],
            "after_revision": restored.revision,
        }
        await tx.execute(
            "INSERT INTO inference_turn_reconciliations "
            "(user_id,thread_id,request_id,operator_id,plan_token,details) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6::jsonb)",
            user,
            thread,
            request,
            operator,
            expected_plan,
            json.dumps(receipt, sort_keys=True),
        )
        return {**report, "status": "restored", "can_restore": False, "replayed": False}
