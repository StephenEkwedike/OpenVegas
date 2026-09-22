"""Logical command identity survives auth refresh and is not shared across turns."""

import json
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from openvegas.client import OpenVegasClient


@pytest.mark.asyncio
async def test_ask_generates_one_key_per_managed_command_and_preserves_supplied_key():
    client = object.__new__(OpenVegasClient)
    client._request = AsyncMock(return_value={"text": "fixture"})
    await client.ask("one", "openrouter", "fixture/model")
    first = client._request.call_args.kwargs["json"]["idempotency_key"]
    assert str(UUID(first)) == first
    await client.ask("two", "openrouter", "fixture/model")
    assert client._request.call_args.kwargs["json"]["idempotency_key"] != first
    await client.ask("one", "openrouter", "fixture/model", idempotency_key=first)
    assert client._request.call_args.kwargs["json"]["idempotency_key"] == first


@pytest.mark.asyncio
async def test_stream_auth_refresh_reuses_identical_command_key():
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text='event: response.completed\ndata: {"text":"fixture"}\n\n')

    client = object.__new__(OpenVegasClient)
    client.base_url = "https://fixture.invalid"
    client.token = "synthetic-fixture"
    client._refresh_single_flight = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client._http_client = http
        output = [item async for item in client.ask_stream("one", "openrouter", "fixture/model")]
    assert output
    assert len(payloads) == 2 and payloads[0] == payloads[1]
    assert str(UUID(payloads[0]["idempotency_key"])) == payloads[0]["idempotency_key"]
    client._refresh_single_flight.assert_awaited_once()
