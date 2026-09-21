from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest

from openvegas.emotes import transport
from openvegas.emotes.manifest import PackError

ACCOUNT = "9e7bb0cc-45a8-4e87-9bbb-8bce8b02c38d"


def reader():
    return "https://example.test", ACCOUNT, "synthetic-token"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:secret@example.test",
        "file:///tmp/pack",
        "https://example.test/api",
        "https://example.test/?query=1",
        "https://example.test/#x",
        "https://[broken",
        "https://example.test:bad",
        "",
        "http://192.168.0.1",
    ],
)
def test_rejects_unsafe_backend(url):
    with pytest.raises(PackError):
        transport.backend_scope(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://EXAMPLE.test:443/", "https://example.test"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:8000/", "http://[::1]:8000"),
        ("http://localhost:80", "http://localhost"),
    ],
)
def test_normalizes_scope(url, expected):
    assert transport.backend_scope(url) == expected


def test_credentials_never_hydrates_keychain(monkeypatch):
    from openvegas import config

    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": ACCOUNT, "exp": time.time() + 90}).encode()
    ).decode()
    token = "test." + payload + ".test"
    monkeypatch.setattr(config, "get_backend_url", lambda: "https://example.test")
    monkeypatch.setattr(config, "load_config", lambda: {"session": {"access_token": token}})
    monkeypatch.setattr(config, "get_session", lambda: pytest.fail("must not read keychain"))
    assert transport.credentials() == ("https://example.test", ACCOUNT, token)


@pytest.mark.parametrize("session", [{}, {"access_token": "broken"}, {"access_token": []}, None])
def test_bad_session_has_actionable_login_error(monkeypatch, session):
    from openvegas import config

    monkeypatch.setattr(config, "get_backend_url", lambda: "https://example.test")
    monkeypatch.setattr(config, "load_config", lambda: {"session": session})
    with pytest.raises(PackError, match="openvegas login"):
        transport.credentials()


@pytest.mark.asyncio
async def test_requests_same_origin_only_and_no_purchase():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = transport.EmoteAPI(credential_reader=reader, http=http)
        assert await api.owned() == {"ok": True}
        await api.pack("fixture.pack")
        await api.equip("fixture.pack")
        await api.equip(None)
    assert [r.url.path for r in requests] == [
        "/store/emotes/owned",
        "/store/emotes/fixture.pack/pack",
        "/store/emotes/equip",
        "/store/emotes/equip",
    ]
    assert json.loads(requests[2].content) == {"item_id": "fixture.pack", "slot": "companion"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 409, 410, 500, 503])
async def test_status_errors_do_not_echo_details_redirect_or_retry(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, json={"detail": "PRIVATE SECRET"}, headers={"Location": "https://evil.test"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as http:
        api = transport.EmoteAPI(credential_reader=reader, http=http)
        with pytest.raises(PackError) as exc:
            await api.owned()
        assert "PRIVATE" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mid_request_account_change_rejected():
    current = [reader()]

    def respond(request):
        current[0] = ("https://example.test", "other", "another-token")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = transport.EmoteAPI(credential_reader=lambda: current[0], http=http)
        with pytest.raises(PackError, match="changed"):
            await api.owned()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"{}", headers={"Content-Type": "text/html"}),
        httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "99999999"},
        ),
        httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "oops"},
        ),
        httpx.Response(200, content=b'{"x":1,"x":2}', headers={"Content-Type": "application/json"}),
        httpx.Response(200, json=[1, 2, 3]),
    ],
)
async def test_response_validation(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
        with pytest.raises(PackError):
            await transport.EmoteAPI(credential_reader=reader, http=http).owned()


@pytest.mark.asyncio
async def test_streamed_size_bound_without_length(monkeypatch):
    class Data(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(4):
                yield b"x" * 32768

    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 65536)
    response = httpx.Response(200, stream=Data(), headers={"Content-Type": "application/json"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as http:
        with pytest.raises(PackError, match="size"):
            await transport.EmoteAPI(credential_reader=reader, http=http).owned()


@pytest.mark.asyncio
async def test_whole_request_deadline(monkeypatch):
    async def respond(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    monkeypatch.setattr(transport, "REQUEST_TIMEOUT", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(PackError, match="unreachable"):
            await transport.EmoteAPI(credential_reader=reader, http=http).owned()


@pytest.mark.asyncio
async def test_no_requests_for_path_traversal():
    def respond(request):
        pytest.fail("unsafe path must not be requested")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = transport.EmoteAPI(credential_reader=reader, http=http)
        for item in ("../private", "/private", "good?admin=1", "good\x1b[2J"):
            with pytest.raises(PackError):
                await api.pack(item)
