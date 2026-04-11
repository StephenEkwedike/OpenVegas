from __future__ import annotations

import httpx
import pytest

from openvegas.client import APIError, OpenVegasClient


class _FakePersistentClient:
    def __init__(self):
        self.request_calls: list[tuple[str, str, dict]] = []
        self.stream_calls: list[tuple[str, str, dict]] = []
        self.is_closed = False

    async def request(self, method: str, url: str, headers: dict | None = None, **kwargs):
        self.request_calls.append((method, url, {"headers": headers or {}, **kwargs}))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    def stream(self, method: str, url: str, headers: dict | None = None, **kwargs):
        self.stream_calls.append((method, url, {"headers": headers or {}, **kwargs}))
        return _FakeStreamResponse(method, url)

    async def aclose(self):
        self.is_closed = True


class _FakeStreamResponse:
    def __init__(self, method: str, url: str):
        self.status_code = 200
        self._request = httpx.Request(method, url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        yield "event: response.delta"
        yield 'data: {"run_id":"r1","turn_id":"t1","sequence_no":1,"payload":{"text":"hi"}}'
        yield "event: response.completed"
        yield 'data: {"run_id":"r1","turn_id":"t1","sequence_no":2,"payload":{"text":"hi"}}'


@pytest.mark.asyncio
async def test_request_network_error_maps_to_api_error(monkeypatch):
    client = OpenVegasClient()

    async def _boom(*_args, **_kwargs):
        raise httpx.ReadError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "request", _boom)

    with pytest.raises(APIError) as e:
        await client._request("GET", "/health/ready")
    assert e.value.status == 503
    assert "Backend request failed" in e.value.detail


@pytest.mark.asyncio
async def test_do_http_reuses_client_scoped_async_client():
    client = OpenVegasClient()
    fake = _FakePersistentClient()
    await client._http_client.aclose()
    client._http_client = fake  # type: ignore[assignment]

    first = await client._do_http("GET", "/health/live")
    second = await client._do_http("POST", "/wallet/bootstrap", json={"ok": True})

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(fake.request_calls) == 2
    assert fake.request_calls[0][1].endswith("/health/live")
    assert fake.request_calls[1][1].endswith("/wallet/bootstrap")
    await client.aclose()
    assert fake.is_closed is True


@pytest.mark.asyncio
async def test_ask_stream_reuses_client_scoped_async_client():
    client = OpenVegasClient()
    fake = _FakePersistentClient()
    await client._http_client.aclose()
    client._http_client = fake  # type: ignore[assignment]

    events = [
        event
        async for event in client.ask_stream(
            "hi",
            "openai",
            "gpt-5.4",
            enable_tools=False,
            enable_web_search=False,
        )
    ]

    assert len(fake.stream_calls) == 1
    assert fake.stream_calls[0][1].endswith("/inference/stream")
    assert [event["event"] for event in events] == ["response.delta", "response.completed"]
    await client.aclose()
    assert fake.is_closed is True
