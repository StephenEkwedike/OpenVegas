from __future__ import annotations

import asyncio

import httpx
import pytest

import openvegas.client as client_mod
from openvegas.client import APIError, OpenVegasClient
from openvegas.telemetry import get_metrics_snapshot, reset_metrics


def _resp(status: int, method: str, path: str, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload or {},
        request=httpx.Request(method, f"http://127.0.0.1:8000{path}"),
    )


@pytest.mark.asyncio
async def test_cli_startup_expired_access_valid_refresh_proactive_succeeds_no_prompt(
    monkeypatch: pytest.MonkeyPatch,
):
    session_state = {
        "access_token": "expired-access",
        "access_expires_at": 1,
        "refresh_token": "valid-refresh",
        "refresh_storage": "config",
    }
    monkeypatch.setattr(client_mod, "get_session", lambda: dict(session_state))
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: "expired-access")
    client = OpenVegasClient()

    refresh_calls: list[str] = []

    async def _fake_refresh(*, trigger: str):
        refresh_calls.append(trigger)
        session_state["access_token"] = "fresh-access"
        session_state["access_expires_at"] = 4_102_444_800
        client.token = "fresh-access"
        client._session_snapshot = dict(session_state)
        return "fresh-access"

    async def _fake_http(method: str, path: str, **_kwargs):
        assert client.token == "fresh-access"
        return _resp(200, method, path, {"ok": True})

    monkeypatch.setattr(client, "_refresh_single_flight", _fake_refresh)
    monkeypatch.setattr(client, "_do_http", _fake_http)

    out = await client._request("GET", "/wallet/balance")
    assert out == {"ok": True}
    assert refresh_calls == ["proactive"]


@pytest.mark.asyncio
async def test_401_triggers_single_retry_after_refresh(monkeypatch: pytest.MonkeyPatch):
    session_state = {"access_token": "a", "access_expires_at": 4_102_444_800}
    monkeypatch.setattr(client_mod, "get_session", lambda: dict(session_state))
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: "a")
    client = OpenVegasClient()

    refresh_calls: list[str] = []
    http_calls = {"n": 0}

    async def _fake_refresh(*, trigger: str):
        refresh_calls.append(trigger)
        client.token = "b"
        return "b"

    async def _fake_http(method: str, path: str, **_kwargs):
        http_calls["n"] += 1
        if http_calls["n"] == 1:
            return _resp(401, method, path, {"detail": "expired"})
        return _resp(200, method, path, {"ok": True})

    monkeypatch.setattr(client, "_refresh_single_flight", _fake_refresh)
    monkeypatch.setattr(client, "_do_http", _fake_http)
    out = await client._request("GET", "/wallet/balance")
    assert out == {"ok": True}
    assert refresh_calls == ["retry_401"]
    assert http_calls["n"] == 2


@pytest.mark.asyncio
async def test_refresh_rejected_clears_persisted_refresh_token(monkeypatch: pytest.MonkeyPatch):
    session_state = {"access_token": "a", "access_expires_at": 4_102_444_800}
    monkeypatch.setattr(client_mod, "get_session", lambda: dict(session_state))
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: "a")
    cleared = {"refresh": 0}
    monkeypatch.setattr(client_mod, "clear_persisted_refresh_token", lambda: cleared.__setitem__("refresh", cleared["refresh"] + 1))
    client = OpenVegasClient()

    async def _fake_refresh(*, trigger: str):
        raise client_mod.CliAuthError("refresh_rejected")

    async def _fake_http(method: str, path: str, **_kwargs):
        return _resp(401, method, path, {"detail": "expired"})

    monkeypatch.setattr(client, "_refresh_single_flight", _fake_refresh)
    monkeypatch.setattr(client, "_do_http", _fake_http)

    with pytest.raises(APIError) as exc:
        await client._request("GET", "/wallet/balance")
    assert exc.value.status == 401
    assert "Session expired" in exc.value.detail
    assert client.token is None
    assert cleared["refresh"] == 1


@pytest.mark.asyncio
async def test_refresh_malformed_does_not_clear_persisted_refresh(monkeypatch: pytest.MonkeyPatch):
    session_state = {"access_token": "a", "access_expires_at": 4_102_444_800}
    monkeypatch.setattr(client_mod, "get_session", lambda: dict(session_state))
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: "a")
    cleared = {"refresh": 0}
    monkeypatch.setattr(client_mod, "clear_persisted_refresh_token", lambda: cleared.__setitem__("refresh", cleared["refresh"] + 1))
    client = OpenVegasClient()

    async def _fake_refresh(*, trigger: str):
        raise ValueError("refresh_malformed")

    async def _fake_http(method: str, path: str, **_kwargs):
        return _resp(401, method, path, {"detail": "expired"})

    monkeypatch.setattr(client, "_refresh_single_flight", _fake_refresh)
    monkeypatch.setattr(client, "_do_http", _fake_http)

    with pytest.raises(APIError) as exc:
        await client._request("GET", "/wallet/balance")
    assert exc.value.status == 401
    assert "invalid payload" in exc.value.detail.lower()
    assert client.token is None
    assert cleared["refresh"] == 0


@pytest.mark.asyncio
async def test_proactive_refresh_failure_metric_is_cooldown_limited(monkeypatch: pytest.MonkeyPatch):
    reset_metrics()
    session_state = {
        "access_token": "expired",
        "access_expires_at": 1,
        "refresh_token": "r",
        "refresh_storage": "config",
    }
    monkeypatch.setattr(client_mod, "get_session", lambda: dict(session_state))
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: "expired")
    client = OpenVegasClient()
    client._proactive_fail_cooldown_sec = 9999.0

    async def _fail_refresh(*, trigger: str):
        raise RuntimeError("boom")

    async def _ok_http(method: str, path: str, **_kwargs):
        return _resp(200, method, path, {"ok": True})

    monkeypatch.setattr(client, "_refresh_single_flight", _fail_refresh)
    monkeypatch.setattr(client, "_do_http", _ok_http)

    await client._request("GET", "/wallet/balance")
    await client._request("GET", "/wallet/balance")
    key = "auth_refresh_attempt_total|outcome=failure,reason=refresh_preflight_failed,surface=cli,trigger=proactive"
    assert get_metrics_snapshot().get(key, 0) == 1


@pytest.mark.asyncio
async def test_refresh_single_flight_reuses_one_inflight_task(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(client_mod, "get_session", lambda: {})
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: None)
    client = OpenVegasClient()
    calls = {"n": 0}

    async def _slow_refresh(trigger: str):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return "tok"

    monkeypatch.setattr(client, "_refresh_once", _slow_refresh)
    a, b = await asyncio.gather(
        client._refresh_single_flight("retry_401"),
        client._refresh_single_flight("retry_401"),
    )
    assert a == "tok"
    assert b == "tok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_balance_bootstraps_wallet_once(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(client_mod, "get_session", lambda: {})
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: None)
    client = OpenVegasClient()
    calls: list[tuple[str, str]] = []

    async def _fake_request(method: str, path: str, **_kwargs):
        calls.append((method, path))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    await client.get_balance()
    await client.get_balance()

    assert calls == [
        ("POST", "/wallet/bootstrap"),
        ("GET", "/wallet/balance"),
        ("GET", "/wallet/balance"),
    ]


@pytest.mark.asyncio
async def test_retry_401_touchid_unlock_required_returns_clear_message(monkeypatch: pytest.MonkeyPatch):
    session_state = {
        "access_token": "a",
        "access_expires_at": 4_102_444_800,
        "refresh_storage": "platform_credential_store",
    }
    monkeypatch.setattr(client_mod, "get_session", lambda: dict(session_state))
    monkeypatch.setattr(client_mod, "get_bearer_token", lambda: "a")
    monkeypatch.setattr(client_mod, "require_touchid_unlock_for_refresh_storage", lambda _s: True)
    monkeypatch.setattr(client_mod, "request_touchid_unlock", lambda: False)
    client = OpenVegasClient()

    async def _fake_http(method: str, path: str, **_kwargs):
        return _resp(401, method, path, {"detail": "expired"})

    monkeypatch.setattr(client, "_do_http", _fake_http)

    with pytest.raises(APIError) as exc:
        await client._request("GET", "/wallet/balance")
    assert exc.value.status == 401
    assert "touch id unlock required" in exc.value.detail.lower()
