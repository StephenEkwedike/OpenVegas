"""Offline profile status coverage; no account, renderer or equipment writes."""
from __future__ import annotations

import ast
import asyncio
import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from rich.console import Console
from rich.panel import Panel

from openvegas import profile_identity as profile
from openvegas.client import APIError, OpenVegasClient

ROOT = Path(__file__).resolve().parents[2]
VALID = {
    "avatar_id": "ov_user_01", "avatar_palette": "warm",
    "dealer_skin_id": "ov_dealer_female_tux_blonde_v1", "theme": "dark",
}


class RawBody(httpx.AsyncByteStream):
    def __init__(self, chunks, before_read=None):
        self.chunks = chunks
        self.before_read = before_read
        self.closed = False
        self.read_count = 0

    async def __aiter__(self):
        if self.before_read:
            await self.before_read()
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def wire_response(status, *, json=None, content=None, headers=None):
    body = content if content is not None else __import__("json").dumps(json).encode()
    return httpx.Response(status, stream=RawBody([body]), headers=headers)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network or persistent configuration write forbidden")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr("openvegas.config.save_config", deny)


def test_packaged_registry_matches_reviewed_legacy_manifest():
    manifest = json.loads((ROOT / "ui/assets/avatar-manifest.json").read_text())
    assert profile.DEALERS == {row["id"]: row["name"] for row in manifest["dealer"]}
    assert profile.AVATARS == {
        row["id"]: (row["name"], frozenset(row["palettes"])) for row in manifest["users"]
    }
    assert profile.PALETTES == {row["id"]: row["name"] for row in manifest["palettes"]}


@pytest.mark.parametrize("value", [None, True, [], {}, "", "../../secret", "[red]", "\x1b[2J", "ov_user_99", "x" * 65])
def test_invalid_tokens_never_become_a_saved_default(value):
    for key in ("avatar_id", "avatar_palette", "dealer_skin_id"):
        with pytest.raises(ValueError):
            profile.ProfileIdentity.parse({**VALID, key: value})


def test_missing_and_cross_avatar_palette_are_rejected():
    with pytest.raises(ValueError):
        profile.ProfileIdentity.parse({"avatar_id": "ov_user_01"})
    with pytest.raises(ValueError):
        profile.ProfileIdentity.parse({**VALID, "avatar_palette": "neon"})


@pytest.mark.asyncio
async def test_packaged_status_has_no_cwd_registry_dependency(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("Repo registry accessed"))
    result = await profile.profile_status(SimpleNamespace(get_profile_preferences=AsyncMock(return_value=VALID)))
    assert "Classic Player" in result and "Victoria - Blonde" in result
    assert "Rendering and emote equipment unchanged" in result


@pytest.mark.asyncio
async def test_no_cache_between_accounts_or_after_failed_refresh():
    first = SimpleNamespace(get_profile_preferences=AsyncMock(side_effect=[VALID, APIError(401, "SECRET")]))
    second_payload = {**VALID, "avatar_id": "ov_user_02", "avatar_palette": "mono"}
    second = SimpleNamespace(get_profile_preferences=AsyncMock(return_value=second_payload))
    a, b = await asyncio.gather(profile.profile_status(first), profile.profile_status(second))
    assert "Classic Player" in a and "High Roller" not in a
    assert "High Roller" in b and "Classic Player" not in b
    later = await profile.profile_status(first)
    assert "unavailable" in later and "Classic Player" not in later and "SECRET" not in later
    assert first.get_profile_preferences.await_count == 2
    assert vars(first).keys() == {"get_profile_preferences"}


@pytest.mark.asyncio
async def test_unavailable_client_and_cancellation():
    assert "not supported by this client" in await profile.profile_status(object())
    getter = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await profile.profile_status(SimpleNamespace(get_profile_preferences=getter))


def client(http):
    instance = object.__new__(OpenVegasClient)
    instance.base_url = "https://offline.invalid"
    instance.token = "synthetic-auth"
    instance._http_client = http
    instance._request = AsyncMock(side_effect=AssertionError("Automatic auth flow forbidden"))
    instance._refresh_single_flight = AsyncMock(side_effect=AssertionError("Cosmetic auth refresh forbidden"))
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 302, 401, 403, 404, 405, 500, 501])
async def test_client_single_authenticated_get_no_redirect_refresh_or_write(status):
    requests = []

    def handle(request):
        requests.append(request)
        return wire_response(status, json=VALID if status == 200 else {"detail": "SECRET"},
                              headers={"location": "https://other.invalid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as http:
        instance = client(http)
        if status == 200:
            assert await instance.get_profile_preferences() == VALID
        else:
            with pytest.raises(APIError) as error:
                await instance.get_profile_preferences()
            assert error.value.status == status
            assert "SECRET" not in str(error.value)
        instance._request.assert_not_called()
        instance._refresh_single_flight.assert_not_called()
        assert instance.token == "synthetic-auth"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET" and request.url.path == "/ui/profile/preferences"
    assert request.headers["authorization"] == "Bearer synthetic-auth"
    assert request.content == b""
    assert all(t == profile.PROFILE_TIMEOUT_SECONDS for t in request.extensions["timeout"].values())


@pytest.mark.asyncio
async def test_unauthenticated_client_does_not_send_request():
    instance = object.__new__(OpenVegasClient)
    instance.token = None
    instance.base_url = "https://offline.invalid"
    instance._do_http = AsyncMock()
    assert "sign in" in await profile.profile_status(instance)
    instance._do_http.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b'not JSON SECRET', b'["SECRET"]'])
async def test_malformed_wire_response_is_nonfatal_and_redacted(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: wire_response(200, content=body))) as http:
        result = await profile.profile_status(client(http))
    assert "unavailable" in result and "SECRET" not in result


@pytest.mark.asyncio
async def test_timeout_cancels_read_without_auth_or_state_mutation(monkeypatch):
    monkeypatch.setattr(profile, "PROFILE_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def handle(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        instance = client(http)
        result = await profile.profile_status(instance)
        assert cancelled.is_set()
        assert "unavailable" in result
        assert instance.token == "synthetic-auth"
        instance._refresh_single_flight.assert_not_called()


def status_branch(client):
    """Compile the actual CLI /status branch without importing application startup."""
    tree = ast.parse((ROOT / "openvegas/cli.py").read_text())
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
               and ast.unparse(node.test) == "cmd == '/status'"]
    assert len(matches) == 1
    wrapper = ast.parse("async def run():\n    for cmd in ['/status']:\n        pass\n")
    wrapper.body[0].body[0].body = matches
    stream = io.StringIO()
    namespace = {
        "client": client, "console": Console(file=stream, width=240, color_system=None), "Panel": Panel,
        "_reasoning_status": lambda: "Reasoning unchanged", "_chat_capability": lambda _: True,
        "web_search_requested": False, "current_provider": "openai", "current_model": "fixture",
        "show_model_meta": True, "current_thread_id": None, "current_run_id": None,
        "current_run_version": 0, "workspace_root": "fixture", "plan_mode": False,
        "approval_mode": "ask", "verbose_tool_events": False, "last_web_search_used": False,
        "last_web_search_retry_without_tool": False, "voice_transcribe_requested": False,
        "last_voice_transcribe_effective": False, "last_voice_transcribe_used": False,
        "_mcp_feature_enabled": lambda: False, "pending_attachments": [],
    }
    exec(compile(ast.fix_missing_locations(wrapper), "trusted_cli_status", "exec"), namespace)  # noqa: S102 - Trusted production AST.
    return namespace["run"], stream


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status,expected", [
    (VALID, 200, "Saved profile identity"),
    ({**VALID, "avatar_id": ["SECRET"]}, 200, "invalid or unsupported saved preferences"),
    ({"detail": "SECRET"}, 401, "sign in"),
    ({"detail": "SECRET"}, 403, "sign in"),
    ({"detail": "SECRET"}, 404, "not supported by this backend"),
    ({"detail": "SECRET"}, 500, "could not be read"),
])
async def test_actual_cli_status_with_real_client_preserves_existing_status(payload, status, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: wire_response(status, json=payload))) as http:
        run, stream = status_branch(client(http))
        await run()
    output = stream.getvalue()
    assert expected in output
    assert "Chat Status" in output and "Reasoning unchanged" in output
    assert "SECRET" not in output
    if status != 200 or payload != VALID:
        assert "Classic Player" not in output and "Victoria" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,chunks,reads", [
    ({"content-length": "16385"}, [b"{}"], 0),
    ({"content-length": "-1"}, [b"{}"], 0),
    ({"content-length": "garbage"}, [b"{}"], 0),
    ({"content-length": "2"}, [b"x" * 16385], 1),
    ({"transfer-encoding": "chunked"}, [b"x" * 8192, b"x" * 8192, b"x", b"SECRET"], 3),
    ({"content-encoding": "gzip"}, [b"SECRET compressed bomb"], 0),
    ({"content-encoding": "br"}, [b"SECRET"], 0),
    ({}, [b"[" * 2000 + b"]" * 2000], 1),
    ({}, [b"\xffSECRET"], 1),
])
async def test_bounded_raw_response_rejected_neutrally(headers, chunks, reads):
    body = RawBody(chunks)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers=headers, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        run, stream = status_branch(client(http))
        await run()
    assert "unavailable" in stream.getvalue() and "SECRET" not in stream.getvalue()
    assert "Chat Status" in stream.getvalue()
    assert body.closed and body.read_count == reads
    assert len(requests) == 1 and requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["invalidate", "replace", "backend"])
@pytest.mark.parametrize("stage", ["headers", "body"])
async def test_same_client_identity_change_discards_old_response(mutation, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def pause():
        entered.set()
        await release.wait()

    body = RawBody([json.dumps(VALID).encode()], pause if stage == "body" else None)

    async def handle(request):
        requests.append(request)
        if stage == "headers":
            await pause()
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        instance = client(http)
        run, stream = status_branch(instance)
        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            if mutation == "backend":
                instance.base_url = "https://different.invalid"
            else:
                instance.token = None if mutation == "invalidate" else "other-account"
            release.set()
            await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert "unavailable" in stream.getvalue()
    assert "Saved profile" not in stream.getvalue() and "Classic Player" not in stream.getvalue()
    assert body.closed and len(requests) == 1
    assert requests[0].url.host == "offline.invalid"
    assert requests[0].headers["authorization"] == "Bearer synthetic-auth"


@pytest.mark.asyncio
async def test_cli_body_timeout_closes_stream_and_preserves_status(monkeypatch):
    monkeypatch.setattr(profile, "PROFILE_TIMEOUT_SECONDS", 0.01)

    async def stall():
        await asyncio.Event().wait()

    body = RawBody([json.dumps(VALID).encode()], stall)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=body)
    )) as http:
        run, stream = status_branch(client(http))
        await run()
    assert body.closed
    assert "unavailable" in stream.getvalue() and "Chat Status" in stream.getvalue()
    assert "Saved profile" not in stream.getvalue()


@pytest.mark.asyncio
async def test_exact_byte_cap_valid_json_is_accepted():
    encoded = json.dumps(VALID).encode()
    body = RawBody([encoded, b" " * (16384 - len(encoded))])
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-length": "16384"}, stream=body)
    )) as http:
        assert await client(http).get_profile_preferences() == VALID
    assert body.closed
