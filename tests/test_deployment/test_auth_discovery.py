import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvegas import auth
from openvegas.auth_config import public_auth_config
from server.routes import ui_auth


def legacy_key(role):
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


@pytest.mark.parametrize("claims", [None, [], "not-claims", 42])
def test_nonobject_auth_claims_fail_as_configuration_error(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="anonymous"):
        public_auth_config("https://auth.example.test", "header." + payload + ".signature")


@pytest.mark.parametrize("key", ["sb_secret_do_not_publish", legacy_key("service_role"), "", "bad.key"])
def test_private_or_malformed_keys_never_publish(key, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_PUBLIC_URL", raising=False)
    monkeypatch.setenv("SUPABASE_ANON_KEY", key)
    app = FastAPI()
    app.include_router(ui_auth.router)
    result = TestClient(app).get("/auth/config")
    assert result.status_code == 503
    if key:
        assert key not in result.text


def test_public_discovery_exposes_only_client_settings(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://host.docker.internal:54321")
    monkeypatch.setenv("SUPABASE_PUBLIC_URL", "http://127.0.0.1:54321")
    monkeypatch.setenv("SUPABASE_ANON_KEY", legacy_key("anon"))
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "private-do-not-publish")
    app = FastAPI()
    app.include_router(ui_auth.router)
    result = TestClient(app).get("/auth/config")
    assert result.status_code == 200
    assert set(result.json()) == {"supabase_url", "supabase_anon_key"}
    assert result.json()["supabase_url"] == "http://127.0.0.1:54321"
    assert "private-do-not-publish" not in result.text
    assert result.headers["cache-control"] == "no-store"


def test_new_cli_discovers_without_saved_supabase_settings(monkeypatch):
    monkeypatch.setattr(auth, "load_config", dict)
    monkeypatch.setattr(auth, "get_backend_url", lambda: "https://app.example.test")
    calls = []

    def get(url, **kwargs):
        assert kwargs["follow_redirects"] is False
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url),
                              json={"supabase_url": "https://auth.example.test", "supabase_anon_key": legacy_key("anon")})

    monkeypatch.setattr(auth.httpx, "get", get)
    monkeypatch.setattr(auth, "create_client", lambda url, key: (url, key))
    assert auth.SupabaseAuth().client == ("https://auth.example.test", legacy_key("anon"))
    assert calls == ["https://app.example.test/auth/config"]


def test_public_url_rejects_remote_plaintext():
    with pytest.raises(ValueError, match="HTTPS"):
        public_auth_config("http://auth.example.test", "sb_publishable_test")


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [None, "http://127.0.0.1:8000/ui/login?mode=signup"])
async def test_signup_redirect_uses_gotrue_query_parameter(monkeypatch, redirect):
    monkeypatch.setattr(ui_auth, "_supabase_cfg", lambda: ("https://auth.example.test", "public-test"))

    async def request(method, url, **kwargs):
        assert method == "POST" and url.endswith("/auth/v1/signup")
        assert kwargs["params"] == ({"redirect_to": redirect} if redirect else {})
        assert set(kwargs["json"]) == {"email", "password"}
        return httpx.Response(200, json={"id": "synthetic"})

    monkeypatch.setattr(ui_auth, "request_with_http_client", request)
    assert await ui_auth._supabase_signup(email="local@test.invalid", password="synthetic-test",
                                         email_redirect_to=redirect) == {"id": "synthetic"}
