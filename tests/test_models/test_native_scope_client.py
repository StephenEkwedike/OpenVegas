"""Native scope is opt-in, frozen across transport recovery and never authority."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from openvegas.agent.native_scope_client import NativeGenerationSession
from openvegas.client import OpenVegasClient, _native_scope_payload


@pytest.fixture
def scope():
    return {"run_id": str(uuid4()), "runtime_session_id": str(uuid4()),
            "expected_run_version": 1, "expected_valid_actions_signature": "sha256:" + "a" * 64}


def test_scope_is_a_snapshot_not_the_callers_mutable_dictionary(scope):
    value = _native_scope_payload(scope, provider="openrouter", key="key")
    assert value == scope and value is not scope
    scope["run_id"] = str(uuid4())
    assert value["run_id"] != scope["run_id"]


@pytest.mark.parametrize("provider,key", [("openai", "key"), ("openrouter", None),
                                         ("openrouter", ""), ("openrouter", " ")])
def test_explicit_identity_is_required(scope, provider, key):
    with pytest.raises(ValueError, match="explicit idempotency"):
        _native_scope_payload(scope, provider=provider, key=key)


@pytest.mark.parametrize("field,value", [
    ("user_id", str(uuid4())), ("owner_token", str(uuid4())), ("run_id", "wrong"),
    ("runtime_session_id", "wrong"), ("expected_run_version", True),
    ("expected_run_version", "1"), ("expected_run_version", -1),
    ("expected_valid_actions_signature", "sha256:"),
])
def test_scope_rejects_coercion_and_client_supplied_authority(scope, field, value):
    with pytest.raises(ValueError):
        _native_scope_payload({**scope, field: value}, provider="openrouter", key="key")


def test_one_generation_per_session_until_native_continuation_exists(scope):
    session = NativeGenerationSession()
    first = session.reserve(key="first", scope=scope)
    first["run_id"] = str(uuid4())
    replay = session.reserve(key="first", scope={**scope, "expected_run_version": 2})
    assert replay == scope
    with pytest.raises(ValueError, match="No further inference"):
        session.reserve(key="second", scope=scope)
    with pytest.raises(ValueError, match="No further inference"):
        session.reserve(key="second", scope={**scope, "run_id": str(uuid4())})
    assert session.reserve(key="first", scope=scope) == scope


@pytest.mark.parametrize("name", ["run_id", "runtime_session_id"])
def test_replay_cannot_be_rebound(scope, name):
    session = NativeGenerationSession()
    session.reserve(key="first", scope=scope)
    with pytest.raises(ValueError, match="cannot change"):
        session.reserve(key="first", scope={**scope, name: str(uuid4())})


@pytest.mark.asyncio
async def test_ask_forwards_scope_but_legacy_payload_has_no_new_field(scope):
    client = object.__new__(OpenVegasClient)
    client._request = AsyncMock(return_value={"text": "fixture"})
    await client.ask("one", "openrouter", "fixture/model", idempotency_key="first", native_scope=scope)
    assert client._request.call_args.kwargs["json"]["native_scope"] == scope
    await client.ask("one", "openrouter", "fixture/model", idempotency_key="first")
    assert "native_scope" not in client._request.call_args.kwargs["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_missing_key_stops_before_network(scope, stream):
    client = object.__new__(OpenVegasClient)
    client._request = AsyncMock()
    with pytest.raises(ValueError, match="explicit idempotency"):
        if stream:
            _ = [item async for item in client.ask_stream(
                "one", "openrouter", "fixture/model", native_scope=scope,
            )]
        else:
            await client.ask("one", "openrouter", "fixture/model", native_scope=scope)
    client._request.assert_not_called()


@pytest.mark.asyncio
async def test_stream_auth_refresh_preserves_original_key_and_scope(scope):
    original = dict(scope)
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            scope["run_id"] = str(uuid4())
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text='event: response.completed\ndata: {"text":"fixture"}\n\n')

    client = object.__new__(OpenVegasClient)
    client.base_url, client.token = "https://fixture.invalid", "fixture"
    client._refresh_single_flight = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client._http_client = http
        result = [item async for item in client.ask_stream(
            "one", "openrouter", "fixture/model", idempotency_key="fixed", native_scope=scope,
        )]
    assert result and len(payloads) == 2 and payloads[0] == payloads[1]
    assert payloads[0]["native_scope"] == original
    assert payloads[0]["idempotency_key"] == "fixed"
    client._refresh_single_flight.assert_awaited_once()
