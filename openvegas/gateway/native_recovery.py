"""Operator recovery of failed native billing, never a provider retry.

Required server-owned native_dispatch fields (persist before POST): account_id,
credential_sha256 = sha256(resolved supplier key), and budget = {token_digest:
integration_auth.digest(token), request_key: native request hash, reserved_v}.
Existing protocol/model/pricing/payload_hash/input_bound/output_bound stay frozen.
Persist the validated provider_request_id as soon as observed. Missing bindings
are not repairable with operator-supplied IDs, prices, receipts or bearer tokens.

No migration or second money ledger: WalletService settles the original escrow;
inference_usage and wallet_history_projection use the existing gateway schema.
The private request response_body_text gains native_recovery audit evidence in
the same transaction. The request stays failed: billing recovery cannot invent
successful output or allow redispatch. Redis is a retryable second phase; failure
retains allowance, never rolls back a committed debit or creates a new session.

Receipt API: https://openrouter.ai/docs/api/api-reference/generations/get-generation
Only exact canonical model IDs are supported (no guessed upstream ID aliases).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx

from openvegas.gateway.native_pricing import SCALE, amount, charge
from openvegas.gateway.providers import resolve_provider_api_key
from openvegas.wallet.ledger import WalletService

RECEIPT_URL = "https://openrouter.ai/api/v1/generation"
HEX = re.compile(r"[a-f0-9]{64}\Z")


class RecoveryError(ValueError):
    """Static reason codes only; never include secret-bearing driver/HTTP errors."""


def require(condition, code):
    if not condition:
        raise RecoveryError(code)


def identifier(value):
    try:
        require(
            isinstance(value, str) and str(UUID(value)) == value and UUID(value).int,
            "INVALID_IDENTIFIER",
        )
    except (ValueError, TypeError, AttributeError):
        raise RecoveryError("INVALID_IDENTIFIER") from None
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, "AMBIGUOUS_JSON")
            result[key] = item
        return result

    def invalid(_):
        raise RecoveryError("INVALID_JSON")

    try:
        require(
            isinstance(value, (str, bytes, bytearray)) and len(value) <= 6_020_000, "INVALID_JSON"
        )
        result = json.loads(
            value, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=invalid
        )
        require(type(result) is dict, "INVALID_JSON")
        return result
    except (ValueError, TypeError, RecursionError):
        raise RecoveryError("INVALID_JSON") from None


def money(value, *, scaled=False):
    try:
        require(not isinstance(value, (bool, float)) and len(str(value)) <= 40, "INVALID_AMOUNT")
        result = amount(value)
        require(not scaled or result == result.quantize(SCALE), "INVALID_AMOUNT")
        return result
    except (ValueError, ArithmeticError):
        raise RecoveryError("INVALID_AMOUNT") from None


async def _read(tx, user, request, *, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    row = await tx.fetchrow(
        "SELECT * FROM inference_requests WHERE id=$1::uuid AND user_id=$2::uuid" + suffix,
        request,
        user,
    )
    require(row is not None, "REQUEST_NOT_FOUND")
    holds = await tx.fetch(
        "SELECT * FROM inference_preauthorizations WHERE request_id=$1" + suffix, request
    )
    require(len(holds) == 1, "HOLD_NOT_UNIQUE")
    hold = dict(holds[0])
    ref = "infer-preauth:" + str(hold["id"])
    if lock:
        # Same request -> hold -> sorted accounts order as the gateway/wallet.
        await tx.fetch(
            "SELECT account_id FROM wallet_accounts WHERE account_id=ANY($1::text[]) "
            "ORDER BY account_id FOR NO KEY UPDATE",
            sorted(["user:" + user, "escrow:" + ref, "store"]),
        )
    ledger = await tx.fetch(
        "SELECT * FROM ledger_entries WHERE reference_id=ANY($1::text[]) ORDER BY id",
        [ref, ref + ":extra", request, request + ":extra"],
    )
    escrow = await tx.fetchrow(
        "SELECT balance FROM wallet_accounts WHERE account_id=$1", "escrow:" + ref
    )
    usage = await tx.fetch("SELECT * FROM inference_usage WHERE request_id=$1::uuid", request)
    history = await tx.fetch(
        "SELECT * FROM wallet_history_projection WHERE request_id=$1::uuid "
        "AND event_type='ai_usage_charge'",
        request,
    )
    owners = await tx.fetch(
        "SELECT id FROM inference_requests WHERE provider_request_id=$1", row["provider_request_id"]
    )
    return {
        "request": dict(row),
        "hold": hold,
        "ledger": [dict(r) for r in ledger],
        "escrow": dict(escrow) if escrow else None,
        "usage": [dict(r) for r in usage],
        "history": [dict(r) for r in history],
        "owners": [str(r["id"]) for r in owners],
    }


def _validate(state, user, request):
    row, hold = state["request"], state["hold"]
    require(
        str(row["id"]) == request
        and str(row["user_id"]) == user
        and row["status"] in {"failed", "processing"}
        and row.get("native_route_command_id") is None
        and row.get("inference_source") == "wrapper"
        and row.get("wallet_funding_source") == "external",
        "NOT_FAILED_HOST_REQUEST",
    )
    body = decode(row.get("response_body_text") or "{}")
    dispatch = body.get("native_dispatch")
    require(type(dispatch) is dict, "MISSING_DISPATCH_EVIDENCE")
    provider = dispatch.get("provider", "openrouter")
    require(provider in {"openrouter", "openai", "anthropic"}, "INVALID_PROVIDER")
    if row["status"] == "processing":
        # Longer than the gateway's 180-second dispatch/drain deadline. The row
        # lock and failed state fence any late finalizer out of this settlement.
        updated = row.get("updated_at")
        require(provider != "openrouter" and isinstance(updated, datetime)
                and updated.tzinfo is not None
                and updated < datetime.now(UTC) - timedelta(minutes=5), "REQUEST_STILL_ACTIVE")
    account = "user:" + user
    require(
        str(hold["user_id"]) == user
        and hold["account_id"] == account
        and str(hold["request_id"]) == request
        and hold["provider"] == provider
        and dispatch.get("account_id") == account
        and dispatch.get("model") == hold["model_id"]
        and dispatch.get("protocol") in {"messages", "responses"},
        "ASSOCIATION_MISMATCH",
    )
    require(
        HEX.fullmatch(row.get("payload_hash") or "")
        and dispatch.get("payload_hash") == row["payload_hash"],
        "PAYLOAD_MISMATCH",
    )
    provider_id = row.get("provider_request_id")
    require(
        isinstance(provider_id, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", provider_id)
        and not provider_id.startswith("sk-"),
        "MISSING_PROVIDER_ID",
    )
    require(
        body.get("provider_request_id", provider_id) == provider_id
        and state["owners"] == [request],
        "PROVIDER_ID_NOT_UNIQUE",
    )
    require(HEX.fullmatch(dispatch.get("credential_sha256") or ""), "MISSING_CREDENTIAL_BINDING")
    pricing = dispatch.get("pricing")
    require(
        type(pricing) is dict
        and pricing.get("model") == hold["model_id"]
        and pricing.get("policy") == ("verified_supplier_cost_plus_markup_v1" if provider == "openrouter"
                                      else "reviewed_direct_token_cost_plus_markup_v1"),
        "INVALID_FROZEN_PRICING",
    )
    if provider != "openrouter":
        from openvegas.gateway.native_direct import ENDPOINTS
        require(pricing.get("provider") == provider
                and dispatch["protocol"] == ENDPOINTS[provider][0]
                and pricing.get("native_transport") == dispatch["protocol"] + "_direct_v1"
                and dispatch.get("web_calls", 0) == 0, "INVALID_FROZEN_PRICING")
        _validate_receipt(body.get("native_receipt"), dispatch, provider_id)
    unsigned = {k: v for k, v in pricing.items() if k != "fingerprint"}
    expected = hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest()
    require(pricing.get("fingerprint") == expected, "INVALID_FROZEN_PRICING")
    require(
        money(pricing["v_per_supplier_usd"])
        == money(pricing["v_per_usd"]) * money(pricing["multiplier"])
        and money(pricing["v_per_usd"]) > 0
        and Decimal("1.20") <= money(pricing["multiplier"]) <= 3,
        "INVALID_FROZEN_PRICING",
    )
    for name in ("input_bound", "output_bound"):
        require(
            type(dispatch.get(name)) is int and 0 < dispatch[name] <= 2_147_483_647,
            "INVALID_USAGE_BOUND",
        )
    reserved = money(hold["reserved_v"], scaled=True)
    bound = (
        dispatch["input_bound"] * money(pricing["supplier_input_usd_per_million"])
        + dispatch["output_bound"] * money(pricing["supplier_output_usd_per_million"])
    ) / 1_000_000
    from openvegas.gateway.native_pricing import web_fee
    try:
        bound += web_fee(pricing, dispatch.get("web_calls", 0))
    except ValueError:
        raise RecoveryError("INVALID_FROZEN_WEB_PRICING") from None
    require(reserved > 0 and charge(bound, pricing) == reserved, "RESERVATION_MISMATCH")
    budget = dispatch.get("budget")
    key = row.get("idempotency_key", "").split(":")
    require(
        type(budget) is dict
        and HEX.fullmatch(budget.get("token_digest") or "")
        and HEX.fullmatch(budget.get("request_key") or "")
        and len(key) == 3
        and key[0] == "host"
        and key[2] == budget["request_key"]
        and key[1]
        and money(budget.get("reserved_v"), scaled=True) == reserved,
        "MISSING_BUDGET_BINDING",
    )
    recovered = body.get("native_recovery")
    if recovered is None:
        require(
            body.get("reconciliation_required") is True
            and body.get("wallet_hold_retained") is True
            and hold["status"] == "reserved"
            and money(hold["settled_v"]) == 0
            and row.get("final_charge_v") is None
            and row.get("final_provider_cost_usd") is None
            and not state["usage"]
            and not state["history"],
            "HOLD_NOT_UNSETTLED",
        )
        actual = Decimal(0)
    else:
        require(type(recovered) is dict and recovered.get("version") == 1, "INVALID_RECOVERY_AUDIT")
        identifier(recovered.get("operator_id"))
        require(HEX.fullmatch(recovered.get("plan_token") or ""), "INVALID_RECOVERY_AUDIT")
        receipt = _validate_receipt(recovered.get("receipt"), dispatch, provider_id)
        if provider != "openrouter":
            require(receipt == _validate_receipt(body.get("native_receipt"), dispatch, provider_id),
                    "RECOVERY_STATE_MISMATCH")
        actual = charge(receipt["total_cost"], pricing)
        require(
            actual <= reserved
            and recovered.get("receipt_sha256") == digest(receipt)
            and recovered.get("charge_v") == str(actual)
            and body.get("reconciliation_required") is False
            and body.get("wallet_hold_retained") is False
            and hold["status"] == ("settled" if actual else "refunded")
            and money(hold["settled_v"]) == actual
            and money(row["final_charge_v"]) == actual
            and money(row["final_provider_cost_usd"])
            == money(receipt["total_cost"]).quantize(SCALE),
            "RECOVERY_STATE_MISMATCH",
        )
        require(len(state["usage"]) == 1 and len(state["history"]) == 1, "RECOVERY_STATE_MISMATCH")
        usage, history = state["usage"][0], state["history"][0]
        require(
            str(usage["user_id"]) == user
            and usage["account_id"] == account
            and usage["provider"] == provider
            and usage["model_id"] == hold["model_id"]
            and usage["input_tokens"] == receipt["native_tokens_prompt"]
            and usage["output_tokens"] == receipt["native_tokens_completion"]
            and money(usage["v_cost"]) == actual
            and money(usage["actual_cost_usd"]) == money(receipt["total_cost"]).quantize(SCALE)
            and str(history["user_id"]) == user
            and Decimal(str(history["display_amount_v"])) == -actual,
            "RECOVERY_STATE_MISMATCH",
        )
    ref = "infer-preauth:" + str(hold["id"])
    escrow = "escrow:" + ref
    expected_entries = [("reserve", account, escrow, reserved)]
    if recovered is not None:
        if actual:
            expected_entries.append(("reserve_settle", escrow, "store", actual))
        if reserved > actual:
            expected_entries.append(("reserve_refund", escrow, account, reserved - actual))
    entries = []
    for entry in state["ledger"]:
        require(entry["reference_id"] == ref, "LEDGER_MISMATCH")
        entries.append(
            (
                entry["entry_type"],
                entry["debit_account"],
                entry["credit_account"],
                money(entry["amount"]),
            )
        )
    require(
        sorted(entries) == sorted(expected_entries)
        and state["escrow"] is not None
        and money(state["escrow"]["balance"]) == (0 if recovered is not None else reserved),
        "LEDGER_MISMATCH",
    )
    return body, dispatch, actual if recovered is not None else None


def _validate_receipt(data, dispatch, provider_id):
    if dispatch.get("provider", "openrouter") != "openrouter":
        from openvegas.gateway.native_receipt import validate
        try:
            return validate(data, dispatch, provider_id)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            raise RecoveryError("INVALID_DIRECT_RECEIPT") from None
    require(
        type(data) is dict
        and data.get("id") == provider_id
        and data.get("model") == dispatch["model"]
        and data.get("is_byok") is False,
        "RECEIPT_ASSOCIATION_MISMATCH",
    )
    require(
        data.get("finish_reason") in {"stop", "length", "tool_calls", "content_filter", "error"}
        and type(data.get("cancelled")) is bool,
        "RECEIPT_NOT_FINAL",
    )
    cost = money(data.get("total_cost"))
    for name, bound in (
        ("native_tokens_prompt", "input_bound"),
        ("native_tokens_completion", "output_bound"),
    ):
        require(
            type(data.get(name)) is int and 0 <= data[name] <= dispatch[bound],
            "INVALID_RECEIPT_USAGE",
        )
    # Store only metering facts, never echoed prompts, headers, or provider errors.
    return {
        "id": data["id"],
        "model": data["model"],
        "total_cost": str(cost),
        "is_byok": False,
        "finish_reason": data["finish_reason"],
        "cancelled": data["cancelled"],
        "native_tokens_prompt": data["native_tokens_prompt"],
        "native_tokens_completion": data["native_tokens_completion"],
    }


async def _receipt(db, dispatch, provider_id, transport):
    try:
        key = await resolve_provider_api_key(db, "openrouter")
        require(
            hashlib.sha256(key.encode()).hexdigest() == dispatch["credential_sha256"],
            "SUPPLIER_CREDENTIAL_CHANGED",
        )
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(
                transport=transport, timeout=15, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "GET",
                RECEIPT_URL,
                params={"id": provider_id},
                headers={"Authorization": "Bearer " + key},
            ) as response,
        ):
            require(response.status_code == 200, "RECEIPT_UNAVAILABLE")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                require(len(raw) <= 128_000, "RECEIPT_TOO_LARGE")
            require(key.encode() not in raw, "REFLECTED_CREDENTIAL")
        return _validate_receipt(decode(raw).get("data"), dispatch, provider_id)
    except RecoveryError:
        raise
    except Exception:  # noqa: BLE001 - supplier/credential exceptions can contain secrets
        raise RecoveryError("RECEIPT_UNAVAILABLE") from None


async def _budget(redis, dispatch, row, actual, *, apply):
    if redis is None:
        return "pending_redis_unavailable"
    from server.services.integration_billing import PREFIX

    binding = dispatch["budget"]
    key = PREFIX + binding["token_digest"]
    # Check identity + exact old reservation atomically; never create/extend keys.
    script = """
    if redis.call('EXISTS',KEYS[1])==0 then return 0 end
    local raw=redis.call('HGET',KEYS[1],'identity')
    if not raw then return -1 end
    local ok,identity=pcall(cjson.decode,raw)
    if not ok or identity.user_id~=ARGV[1] or identity.model~=ARGV[2]
       or identity.session_id~=ARGV[3] then return -1 end
    local old=redis.call('HGET',KEYS[1],ARGV[4])
    if old=='s:'..ARGV[6] then return 1 end
    if old~='r:'..ARGV[5] then return -1 end
    local used=tonumber(redis.call('HGET',KEYS[1],'used'))
    if not used or used<tonumber(ARGV[5]) then return -1 end
    if ARGV[7]=='1' then
      redis.call('HINCRBY',KEYS[1],'used',tonumber(ARGV[6])-tonumber(ARGV[5]))
      redis.call('HSET',KEYS[1],ARGV[4],'s:'..ARGV[6])
    end
    return 2
    """
    try:
        result = await redis.eval(
            script,
            1,
            key,
            str(row["user_id"]),
            dispatch["model"],
            row["idempotency_key"].split(":")[1],
            "req:" + binding["request_key"],
            int(money(binding["reserved_v"]) * 1_000_000),
            int(actual * 1_000_000),
            "1" if apply else "0",
        )
        return {
            0: "absent_not_recreated",
            1: "already_settled",
            2: "settled" if apply else "would_settle",
        }.get(result, "pending_budget_conflict")
    except Exception:  # noqa: BLE001 - preserve the committed wallet outcome without Redis secrets
        return "pending_redis_unavailable"


async def reconcile(
    db,
    *,
    user_id,
    request_id,
    apply=False,
    operator_id=None,
    expected_plan=None,
    redis=None,
    transport=None,
):
    """Dry-run by default. DB must expose transaction/fetchrow; no costs accepted.

    Production authorization is outside this module. The business console route
    requires admin MFA and an exact plan confirmation; it permits only offline
    direct receipts. CLI remains restricted to explicit local disposable DBs.
    transport is an offline HTTP testing seam.
    """
    user, request = identifier(user_id), identifier(request_id)
    if apply:
        identifier(operator_id)
        require(isinstance(expected_plan, str) and HEX.fullmatch(expected_plan), "PLAN_REQUIRED")
    async with db.transaction() as tx:
        await tx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        state = await _read(tx, user, request)
        body, dispatch, settled = _validate(state, user, request)
    if settled is not None:
        if apply:
            require(expected_plan == body["native_recovery"]["plan_token"], "PLAN_CHANGED")
        budget = await _budget(redis, dispatch, state["request"], settled, apply=apply)
        return {
            "status": "already_settled",
            "request_id": request,
            "charge_v": str(settled),
            "plan_token": body["native_recovery"]["plan_token"],
            "budget": budget,
        }
    receipt = (_validate_receipt(body.get("native_receipt"), dispatch, state["request"]["provider_request_id"])
               if dispatch.get("provider", "openrouter") != "openrouter"
               else await _receipt(db, dispatch, state["request"]["provider_request_id"], transport))
    actual = charge(receipt["total_cost"], dispatch["pricing"])
    require(actual <= money(state["hold"]["reserved_v"]), "ACTUAL_EXCEEDS_RESERVED")
    plan = digest({"state": state, "receipt": receipt})
    report = {
        "status": "would_settle",
        "request_id": request,
        "charge_v": str(actual),
        "supplier_cost_usd": receipt["total_cost"],
        "plan_token": plan,
    }
    if not apply:
        return {
            **report,
            "budget": await _budget(redis, dispatch, state["request"], actual, apply=False),
        }
    require(expected_plan == plan, "PLAN_CHANGED")
    async with db.transaction() as tx:
        current = await _read(tx, user, request, lock=True)
        _validate(current, user, request)
        require(digest({"state": current, "receipt": receipt}) == plan, "PLAN_CHANGED")
        hold = current["hold"]
        await WalletService(db).settle_reservation(
            account_id=hold["account_id"],
            reservation_ref="infer-preauth:" + str(hold["id"]),
            settle_amount=actual,
            tx=tx,
        )
        await tx.execute(
            "UPDATE inference_preauthorizations SET settled_v=$2,status=$3,updated_at=now() WHERE id=$1",
            hold["id"],
            actual,
            "settled" if actual else "refunded",
        )
        pricing = dispatch["pricing"]
        inp, out = (
            money(pricing[k])
            for k in ("supplier_input_usd_per_million", "supplier_output_usd_per_million")
        )
        ratio = money(pricing["v_per_supplier_usd"])
        await tx.execute(
            """INSERT INTO inference_usage
            (id,request_id,user_id,account_id,actor_type,provider,model_id,input_tokens,output_tokens,
             v_cost,actual_cost_usd,inference_source,wallet_funding_source,billed_v_input_per_1m,
             billed_v_output_per_1m,billed_cost_input_per_1m,billed_cost_output_per_1m)
            VALUES ($1,$2,$3,$4,'human',$14,$5,$6,$7,$8,$9,'wrapper','external',$10,$11,$12,$13)""",
            str(uuid4()),
            request,
            user,
            hold["account_id"],
            hold["model_id"],
            receipt["native_tokens_prompt"],
            receipt["native_tokens_completion"],
            actual,
            money(receipt["total_cost"]).quantize(SCALE),
            (inp * ratio).quantize(SCALE),
            (out * ratio).quantize(SCALE),
            inp.quantize(SCALE),
            out.quantize(SCALE),
            dispatch.get("provider", "openrouter"),
        )
        await tx.execute(
            """INSERT INTO wallet_history_projection
            (user_id,request_id,event_type,display_amount_v,display_status,occurred_at,metadata_json)
            VALUES ($1,$2,'ai_usage_charge',$3,'completed',now(),$4::jsonb)""",
            user,
            request,
            -actual,
            canonical(
                {
                    "provider": dispatch.get("provider", "openrouter"),
                    "model_id": hold["model_id"],
                    "billing_recovered": True,
                    "input_tokens": receipt["native_tokens_prompt"],
                    "output_tokens": receipt["native_tokens_completion"],
                }
            ),
        )
        body.update(
            reconciliation_required=False,
            wallet_hold_retained=False,
            native_recovery={
                "version": 1,
                "operator_id": operator_id,
                "plan_token": plan,
                "receipt": receipt,
                "receipt_sha256": digest(receipt),
                "charge_v": str(actual),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
        await tx.execute(
            """UPDATE inference_requests SET response_body_text=$2,final_charge_v=$3,
            final_provider_cost_usd=$4,status='failed',updated_at=now()
            WHERE id=$1 AND status IN ('failed','processing')""",
            request,
            canonical(body),
            actual,
            money(receipt["total_cost"]).quantize(SCALE),
        )
    return {
        **report,
        "status": "settled",
        "budget": await _budget(redis, dispatch, state["request"], actual, apply=True),
    }
