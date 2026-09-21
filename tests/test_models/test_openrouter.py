"""Offline OpenRouter transport/catalog contracts. All credentials are synthetic."""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.gateway import openrouter, providers
from openvegas.gateway.catalog import ModelDisabled, ProviderCatalog, validate_catalog_entry
from openvegas.gateway.inference import AIGateway, InferenceRequest, InferenceResult

MODEL = "fixture/exact-model-20260901"
SYNTHETIC_CREDENTIAL = "synthetic-openrouter-test-credential"


def request(**changes):
    return InferenceRequest(
        **(
            {
                "account_id": "user:11111111-1111-4111-8111-111111111111",
                "provider": "openrouter",
                "model": MODEL,
                "max_tokens": 64,
                "messages": [
                    {"role": "system", "content": "Use concise prose."},
                    {"role": "user", "content": "First"},
                    {"role": "assistant", "content": "Answer"},
                    {"role": "user", "content": "Next"},
                ],
            }
            | changes
        )
    )


def catalog_row(**changes):
    return {
        "provider": "openrouter",
        "model_id": MODEL,
        "enabled": True,
        "max_tokens": 1024,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "v_price_input_per_1m": "10",
        "v_price_output_per_1m": "20",
        **changes,
    }


def capabilities(**changes):
    return {"context_window_tokens": 8192, "tools": True, **changes}


def review(**changes):
    now = datetime.now(UTC)
    return {
        "reviewed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "account_access": True,
        "completion_chat": True,
        "context_window_tokens": 8192,
        "cost_input_per_1m": "1",
        "cost_output_per_1m": "2",
        "capabilities": {"tools": True},
        **changes,
    }


def install_review(monkeypatch, value=None, model=MODEL):
    monkeypatch.setenv(
        "OPENVEGAS_MODEL_REVIEWS_JSON",
        json.dumps(
            {
                f"openrouter:{model}": review() if value is None else value,
            }
        ),
    )


def response():
    return {
        "id": "synthetic-receipt",
        "model": MODEL,
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "Answer"}}
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "cost": "0.000020",
        },
    }


def tool_response(arguments=None):
    body = response()
    body["choices"][0] = {
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-fixture",
                    "type": "function",
                    "function": {
                        "name": "call_local_tool",
                        "arguments": json.dumps(
                            arguments
                            or {
                                "tool_name": "Read",
                                "arguments": {"path": "README.md"},
                                "shell_mode": "read_only",
                                "timeout_sec": 30,
                            }
                        ),
                    },
                }
            ],
        },
    }
    return body


async def complete(req, client, **changes):
    return await openrouter.complete(
        req,
        SYNTHETIC_CREDENTIAL,
        **(
            {
                "model_config": catalog_row(),
                "capabilities": capabilities(),
                "parse_tool": AIGateway._parse_local_tool_call,
                "client": client,
            }
            | changes
        ),
    )


def test_exact_payload_preserves_roles_and_disables_fallback_and_plugins():
    req = request()
    original = copy.deepcopy(req.messages)
    payload = openrouter.build_payload(req, catalog_row(), capabilities())
    assert payload == {
        "model": MODEL,
        "messages": original,
        "max_tokens": 64,
        "stream": False,
        "transforms": [],
        "provider": {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
            "max_price": {"prompt": 1.0, "completion": 2.0, "request": 0},
        },
    }
    assert req.messages == original
    assert not ({"models", "route", "plugins", "reasoning"} & payload.keys())


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "openrouter/auto",
        "openrouter/free",
        "auto",
        "fixture/model-latest",
        "fixture/latest",
        "fixture/auto",
        "fixture/router-v2",
        "fixture/model:online",
        "fixture/model:free",
        "https://evil.invalid/model",
        "fixture/model?route=auto",
        "../model",
        "fixture/model/extra",
        "fixture/model\n",
    ],
)
def test_router_alias_and_injected_model_ids_are_rejected(model):
    assert not openrouter.valid_model(model)
    with pytest.raises(ContractError):
        openrouter.build_payload(request(model=model), catalog_row(), capabilities())


@pytest.mark.parametrize(
    "changes",
    [
        {"max_tokens": 0},
        {"max_tokens": -1},
        {"max_tokens": True},
        {"max_tokens": "64"},
        {"max_tokens": 1025},
        {"enable_web_search": True},
        {"messages": []},
        {"messages": None},
        {"messages": "raw prompt"},
        {"messages": [{"role": "tool", "content": "result"}]},
        {"messages": [{"role": "user", "content": [{"type": "image"}]}]},
        {"messages": [{"role": "user", "content": "x", "tool_calls": []}]},
        {"messages": [{"role": "user", "content": "x"}] * 201},
    ],
)
@pytest.mark.asyncio
async def test_bad_payload_is_refused_before_transport(changes):
    def forbidden(_):
        pytest.fail("Preflight failure reached transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ContractError):
            await complete(request(**changes), client)


def test_tool_definition_and_schema_are_in_reservation_bound():
    req = request(enable_tools=True)
    payload = openrouter.build_payload(req, catalog_row(), capabilities())
    assert payload["tools"] == [openrouter.local_tool_definition()]
    assert payload["tool_choice"] == "auto"
    assert openrouter.input_token_bound(req) > openrouter.input_token_bound(request())
    with pytest.raises(ContractError):
        openrouter.build_payload(req, catalog_row(), capabilities(tools=False))
    bound = openrouter.input_token_bound(req)
    with pytest.raises(ContractError):
        openrouter.build_payload(req, catalog_row(), capabilities(context_window_tokens=bound + 63))
    assert openrouter.build_payload(
        req, catalog_row(), capabilities(context_window_tokens=bound + 64)
    )


@pytest.mark.asyncio
async def test_exact_endpoint_per_request_credentials_no_client_mutation_or_native_sdk(monkeypatch):
    install_review(monkeypatch)
    entered, calls = asyncio.Event(), []

    async def handler(req):
        calls.append(req)
        if len(calls) == 2:
            entered.set()
        await entered.wait()
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=("unused", "unused")
    ) as client:
        before = dict(client.headers)
        gateway = AIGateway(None, None, None, client)
        for name in ("_call_openai", "_call_anthropic", "_call_gemini", "_call_mistral"):
            monkeypatch.setattr(
                gateway, name, AsyncMock(side_effect=AssertionError("Wrong provider"))
            )
        req = request()
        req._managed_model_config = catalog_row()
        results = await asyncio.gather(
            *(gateway._route_to_provider(req, key) for key in ("synthetic-left", "synthetic-right"))
        )
        assert all(result.text == "Answer" for result in results)
        assert dict(client.headers) == before and not client.is_closed
    assert len(calls) == 2
    assert {req.headers["authorization"] for req in calls} == {
        "Bearer synthetic-left",
        "Bearer synthetic-right",
    }
    for req in calls:
        assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert req.method == "POST" and not req.url.query
        assert req.headers["HTTP-Referer"] == "https://openvegas.ai"
        assert json.loads(req.content)["model"] == MODEL
        assert "synthetic-" not in req.content.decode()
        assert set(req.extensions["timeout"].values()) == {60}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 402, 403, 404, 422, 429, 500, 503])
async def test_http_failure_no_retry_redirect_fallback_or_error_echo(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status, text="private-upstream-detail", headers={"location": "https://evil.invalid"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ContractError) as error:
            await complete(request(), client)
    assert len(calls) == 1 and error.value.code == APIErrorCode.PROVIDER_UNAVAILABLE
    assert "private-upstream-detail" not in str(error.value)
    assert SYNTHETIC_CREDENTIAL not in str(error.value)
    if status == 402:
        assert "no automatic top-up" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "connection", "cancel"])
async def test_transport_failure_or_cancellation_makes_one_attempt(kind):
    calls = []

    def handler(req):
        calls.append(req)
        if kind == "cancel":
            raise asyncio.CancelledError
        cls = httpx.ReadTimeout if kind == "timeout" else httpx.ConnectError
        raise cls("private-transport-detail", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError if kind == "cancel" else ContractError) as error:
            await complete(request(), client)
    assert len(calls) == 1 and "private-transport-detail" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"null",
        b"{}",
        b'"text"',
        b"\xff",
        b"x" * (openrouter.MAX_RESPONSE_BYTES + 1),
    ],
    ids=["invalid-json", "array", "null", "empty-object", "string", "invalid-utf8", "oversized"],
)
async def test_malformed_or_oversized_response_is_sanitized(raw):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
    ) as client:
        with pytest.raises(ContractError):
            await complete(request(), client)


@pytest.mark.asyncio
async def test_deep_json_is_sanitized_not_recursion_error():
    raw = b"[" * 2000 + b"0" + b"]" * 2000
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
    ) as client:
        with pytest.raises(ContractError):
            await complete(request(), client)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_tokens", -1),
        ("prompt_tokens", True),
        ("prompt_tokens", "11"),
        ("prompt_tokens", 100000),
        ("completion_tokens", -1),
        ("completion_tokens", True),
        ("completion_tokens", 65),
        ("total_tokens", 999),
        ("cost", "NaN"),
        ("cost", "Infinity"),
        ("cost", "-1"),
        ("cost", "1"),
        ("cost", None),
    ],
)
def test_malformed_metering_cannot_be_settled(field, value):
    body = response()
    body["usage"][field] = value
    with pytest.raises((ValueError, TypeError)):
        openrouter.parse_response(body, request(), catalog_row(), AIGateway._parse_local_tool_call)


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "cost"])
def test_missing_metering_is_rejected(field):
    body = response()
    body["usage"].pop(field)
    with pytest.raises((KeyError, ValueError)):
        openrouter.parse_response(body, request(), catalog_row(), AIGateway._parse_local_tool_call)


def test_response_exact_model_and_only_explicit_reviewed_aliases():
    body = response()
    body["model"] = "fixture/not-selected"
    with pytest.raises(ValueError, match="routed model"):
        openrouter.parse_response(body, request(), catalog_row(), AIGateway._parse_local_tool_call)
    result = openrouter.parse_response(
        body,
        request(),
        catalog_row(response_model_ids=[body["model"]]),
        AIGateway._parse_local_tool_call,
    )
    assert result["text"] == "Answer"


def test_hidden_reasoning_and_provider_private_fields_never_escape():
    body = response()
    body["choices"][0]["message"].update(
        reasoning="private-thoughts", reasoning_details=[{"text": "secret"}]
    )
    body["provider"] = "private-vendor"
    result = openrouter.parse_response(
        body, request(), catalog_row(), AIGateway._parse_local_tool_call
    )
    assert result["completion_status"] == "complete"
    assert result["actual_cost_usd"] == Decimal("0.000020")
    assert not any(
        value in str(result) for value in ("private-thoughts", "private-vendor", "secret")
    )


@pytest.mark.parametrize("finish", ["length", "stop", "tool_calls", "content_filter", None])
def test_completion_status_is_not_invented(finish):
    body = response()
    body["choices"][0]["finish_reason"] = finish
    if finish in {"stop", "length"}:
        result = openrouter.parse_response(
            body, request(), catalog_row(), AIGateway._parse_local_tool_call
        )
        assert result["completion_status"] == ("complete" if finish == "stop" else "incomplete")
    else:
        with pytest.raises(ValueError):
            openrouter.parse_response(
                body, request(), catalog_row(), AIGateway._parse_local_tool_call
            )


def test_tools_are_requests_not_executed_and_require_opt_in():
    body = tool_response()
    with pytest.raises(ValueError):
        openrouter.parse_response(body, request(), catalog_row(), AIGateway._parse_local_tool_call)
    result = openrouter.parse_response(
        body, request(enable_tools=True), catalog_row(), AIGateway._parse_local_tool_call
    )
    assert result["completion_status"] == "incomplete"
    assert result["tool_calls"] == [
        {
            "tool_name": "Read",
            "arguments": {"path": "README.md"},
            "shell_mode": "read_only",
            "timeout_sec": 30,
        }
    ]


def test_valid_empty_tool_arguments_are_preserved_not_nested():
    body = tool_response({"tool_name": "List", "arguments": {}})
    result = openrouter.parse_response(
        body, request(enable_tools=True), catalog_row(), AIGateway._parse_local_tool_call
    )
    assert result["tool_calls"][0]["arguments"] == {}


@pytest.mark.parametrize("mutation", ["name", "type", "json", "too_many", "too_large", "not_list"])
@pytest.mark.asyncio
async def test_malformed_native_tools_are_sanitized_without_executing(mutation):
    body = tool_response()
    message = body["choices"][0]["message"]
    call = message["tool_calls"][0]
    if mutation == "name":
        call["function"]["name"] = "unapproved_remote_tool"
    elif mutation == "type":
        call["type"] = "computer_use"
    elif mutation == "json":
        call["function"]["arguments"] = "invalid-json-private-detail"
    elif mutation == "too_many":
        message["tool_calls"] *= 17
    elif mutation == "too_large":
        call["function"]["arguments"] = "x" * 32001
    else:
        message["tool_calls"] = {"function": "not-a-list"}
    parse_tool = AsyncMock(side_effect=AssertionError("Invalid tool must not reach parser"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ContractError) as error:
            await complete(request(enable_tools=True), client, parse_tool=parse_tool)
    parse_tool.assert_not_called()
    assert "invalid-json-private-detail" not in error.value.detail


@pytest.mark.asyncio
async def test_provider_error_inside_http_success_is_not_a_billable_completion():
    body = response()
    body["error"] = {"message": "private-provider-message", "code": 402}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ContractError) as error:
            await complete(request(), client)
    assert "private-provider-message" not in error.value.detail


@pytest.mark.parametrize(
    "arguments",
    [
        {"tool_name": "NotAnApprovedTool", "arguments": {}},
        {"tool_name": "Read", "arguments": "not an object"},
        {"tool_name": "Read", "arguments": {}, "timeout_sec": -1},
        {"tool_name": "Read", "arguments": {}, "timeout_sec": 301},
        {"tool_name": "Read", "arguments": {}, "timeout_sec": True},
        {"tool_name": "Read", "arguments": {}, "shell_mode": "unrestricted"},
    ],
)
def test_tool_response_must_satisfy_advertised_schema(arguments):
    with pytest.raises(ValueError):
        openrouter.parse_response(
            tool_response(arguments),
            request(enable_tools=True),
            catalog_row(),
            AIGateway._parse_local_tool_call,
        )


@pytest.mark.asyncio
async def test_adapter_requires_server_catalog_preflight():
    with pytest.raises(ContractError, match="preflight"):
        await AIGateway(None, None, None)._call_openrouter(request(), SYNTHETIC_CREDENTIAL)


@pytest.mark.parametrize(
    "change",
    [
        {"account_access": False},
        {"completion_chat": False},
        {"context_window_tokens": None},
        {"context_window_tokens": True},
        {"cost_input_per_1m": "2"},
        {"cost_output_per_1m": None},
        {"expires_at": "2000-01-01T00:00:00+00:00"},
    ],
)
def test_catalog_requires_fresh_exact_access_context_and_price_review(monkeypatch, change):
    install_review(monkeypatch, review(**change))
    with pytest.raises(ContractError):
        validate_catalog_entry("openrouter", MODEL, catalog_row())


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"cost_input_per_1m": "NaN"},
        {"v_price_output_per_1m": "-1"},
        {"max_tokens": True},
        {"max_tokens": 0},
    ],
)
def test_catalog_rejects_disabled_bad_price_and_unreviewed_budget(monkeypatch, change):
    install_review(monkeypatch)
    with pytest.raises((ContractError, ModelDisabled)):
        validate_catalog_entry("openrouter", MODEL, catalog_row(**change))


@pytest.mark.asyncio
async def test_production_registry_credential_only_and_no_secret_in_descriptor(monkeypatch):
    install_review(monkeypatch)
    monkeypatch.setenv("OPENVEGAS_RUNTIME_ENV", "production")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-fallback-must-not-be-used")
    state = {"credential": None}

    async def fetchrow(query, *args):
        if "provider_catalog" in query:
            assert args == ("openrouter", MODEL)
            return catalog_row()
        assert "provider_credentials" in query and args == ("openrouter", "production")
        assert "status = 'active'" in query
        return state["credential"]

    catalog = ProviderCatalog(SimpleNamespace(fetchrow=fetchrow))
    assert not (await catalog.describe_model("openrouter", MODEL))["available"]
    state["credential"] = {"key_alias": "OPENROUTER_SYNTHETIC_REVIEW_KEY"}
    monkeypatch.setenv("OPENROUTER_SYNTHETIC_REVIEW_KEY", SYNTHETIC_CREDENTIAL)
    descriptor = await catalog.validate_selection(
        "openrouter", MODEL, required_capabilities=["tools"]
    )
    assert descriptor["available"] and descriptor["availability"] == "configured_not_live_verified"
    assert SYNTHETIC_CREDENTIAL not in json.dumps(descriptor)
    assert "OPENROUTER_SYNTHETIC_REVIEW_KEY" not in json.dumps(descriptor)
    assert (
        not descriptor["capabilities"]["image_input"]
        and not descriptor["capabilities"]["web_search"]
    )


@pytest.mark.asyncio
async def test_preflight_rejects_before_reservation_or_credential_lookup(monkeypatch):
    install_review(monkeypatch)
    catalog = SimpleNamespace(get_model=AsyncMock(return_value=catalog_row()))
    gateway = AIGateway(None, None, catalog)
    gateway._resolve_provider_api_key = AsyncMock(side_effect=AssertionError("Too late"))
    with pytest.raises(ContractError):
        await gateway._prepare_inference_execution(request(enable_web_search=True))
    gateway._resolve_provider_api_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewed_aliases", [[], ["fixture/canonical-model-20260901"]])
async def test_reservation_includes_full_input_and_tool_schema(monkeypatch, reviewed_aliases):
    install_review(monkeypatch, review(response_model_ids=reviewed_aliases))
    row = catalog_row(response_model_ids=["fixture/unreviewed-catalog-alias"])
    tx = SimpleNamespace(fetchrow=AsyncMock(return_value=None), execute=AsyncMock())

    @asynccontextmanager
    async def transaction():
        yield tx

    wallet = SimpleNamespace(get_balance=AsyncMock(return_value=Decimal(100)), reserve=AsyncMock())
    gateway = AIGateway(
        SimpleNamespace(transaction=transaction),
        wallet,
        SimpleNamespace(get_model=AsyncMock(return_value=row)),
    )
    gateway._resolve_provider_api_key = AsyncMock(return_value=SYNTHETIC_CREDENTIAL)
    gateway._estimate_grant_cover_v = AsyncMock(return_value=Decimal(0))
    gateway._begin_inference_request = AsyncMock(return_value=("synthetic-request", None))
    req = request(enable_tools=True)
    req._managed_model_config = catalog_row(response_model_ids=["fixture/caller-alias"])
    context, replay = await gateway._prepare_inference_execution(req)
    bound = openrouter.input_token_bound(req)
    expected = ((Decimal(bound) * 10 + Decimal(64) * 20) / 1_000_000).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )
    assert replay is None and context.reserve_v == expected
    assert wallet.reserve.call_args.kwargs["amount"] == expected
    assert gateway._estimate_grant_cover_v.call_args.kwargs["estimated_total_tokens"] == bound + 64
    assert req._managed_model_config == catalog_row(response_model_ids=reviewed_aliases)
    assert row["response_model_ids"] == ["fixture/unreviewed-catalog-alias"]
    for rejected in ("fixture/unreviewed-catalog-alias", "fixture/caller-alias"):
        body = response()
        body["model"] = rejected
        with pytest.raises(ValueError, match="routed model"):
            openrouter.parse_response(
                body, req, req._managed_model_config, AIGateway._parse_local_tool_call
            )
    if reviewed_aliases:
        body = response()
        body["model"] = reviewed_aliases[0]
        assert (
            openrouter.parse_response(
                body, req, req._managed_model_config, AIGateway._parse_local_tool_call
            )["text"]
            == "Answer"
        )


@pytest.mark.parametrize(
    "aliases",
    [
        None,
        "fixture/alias",
        ["openrouter/auto"],
        ["fixture/latest"],
        ["fixture/one", "fixture/two", "fixture/three"],
    ],
)
@pytest.mark.asyncio
async def test_invalid_reviewed_aliases_fail_before_credential_reservation_or_transport(
    monkeypatch, aliases
):
    install_review(monkeypatch, review(response_model_ids=aliases))
    gateway = AIGateway(
        None, None, SimpleNamespace(get_model=AsyncMock(return_value=catalog_row()))
    )
    gateway._resolve_provider_api_key = AsyncMock(
        side_effect=AssertionError("Alias review failed too late")
    )
    with pytest.raises(ContractError):
        await gateway._prepare_inference_execution(request())
    gateway._resolve_provider_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_infer_uses_buffered_openrouter_once_and_finalizes_once(monkeypatch):
    install_review(monkeypatch)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = AIGateway(None, None, None, client)
        context = SimpleNamespace(provider_api_key=SYNTHETIC_CREDENTIAL)
        gateway._prepare_inference_execution = AsyncMock(return_value=(context, None))
        gateway._finalize_inference_execution = AsyncMock(
            side_effect=lambda ctx, req, result: result
        )
        gateway._cleanup_inference_after_failure = AsyncMock()
        req = request()
        req._managed_model_config = catalog_row(response_model_ids=[])
        events = [event async for event in gateway.stream_infer(req)]
    assert [event["type"] for event in events] == ["text_delta", "completed"]
    assert events[0]["text"] == "Answer"
    assert events[-1]["result"].completion_status == "complete"
    assert len(calls) == 1 and json.loads(calls[0].content)["stream"] is False
    gateway._finalize_inference_execution.assert_awaited_once()
    gateway._cleanup_inference_after_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_settlement_records_reported_discounted_usd_and_catalog_retail_v(monkeypatch):
    tx = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"status": "processing"}, {"status": "reserved"}]),
        execute=AsyncMock(),
    )

    @asynccontextmanager
    async def transaction():
        yield tx

    gateway = AIGateway(SimpleNamespace(transaction=transaction), None, None)
    gateway._settle_preauth = AsyncMock()
    context = SimpleNamespace(
        model_config=catalog_row(),
        user_id=None,
        request_id="request",
        preauth_id="hold",
        reservation_ref="infer-preauth:hold",
        reserve_v=Decimal(1),
        account_id="agent:fixture",
    )
    result = InferenceResult(
        "Answer", 11, 7, actual_cost_usd=Decimal("0.000003"), completion_status="complete"
    )
    finalized = await gateway._finalize_inference_execution(context, request(), result)
    assert finalized.actual_cost_usd == Decimal("0.000003")
    assert finalized.v_cost == Decimal("0.000250")
    usage = next(
        call.args
        for call in tx.execute.call_args_list
        if "INSERT INTO inference_usage" in call.args[0]
    )
    assert usage[10] == Decimal("0.000250") and usage[11] == Decimal("0.000003")


def test_registry_openrouter_is_managed_with_review_gated_tools(monkeypatch):
    descriptor = providers.get_provider("openrouter")
    assert descriptor.adapter_method == "_call_openrouter" and descriptor.requires_review
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", "{}")
    assert not providers.model_capabilities("openrouter", MODEL)["tools"]
    install_review(monkeypatch)
    caps = providers.model_capabilities("openrouter", MODEL)
    assert caps["tools"] and caps["streaming_mode"] == "buffered"
    assert not caps["reasoning_controls"] and not caps["web_search"] and not caps["image_input"]
