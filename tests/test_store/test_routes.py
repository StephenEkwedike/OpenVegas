"""HTTP contract and authentication checks without server startup or network."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt

import server.routes.store as routes
from openvegas.store.catalog import STORE_CATALOG
from server.middleware import auth


@pytest.fixture
def client(monkeypatch, service, approved):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "get_store_service", lambda: service)
    monkeypatch.setattr(auth, "_supabase_jwt_secret", lambda: "local-unit-test-signature-only")

    async def reject_fallback(token):
        raise HTTPException(401, "Invalid or expired token")

    monkeypatch.setattr(auth, "_validate_with_supabase", reject_fallback)
    with TestClient(app) as client:
        yield client


def headers(user="alice", *, expired=False):
    expires = datetime.now(UTC) + timedelta(minutes=-1 if expired else 10)
    token = jwt.encode(
        {"sub": user, "aud": "authenticated", "exp": expires},
        "local-unit-test-signature-only",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_public_catalog_shape_and_preview_allowlist(client):
    response = client.get("/store/emotes/catalog")
    assert response.status_code == 200
    items = response.json()["items"]
    item = next(i for i in items if i["id"] == "test_emote")
    assert item["category"] == "completion"
    assert item["pack_id"] == "openvegas.test-pack"
    assert item["preview_only"] is False
    assert item["cost_v"] == "2.5"
    assert not any(i["id"] == "victory_fireworks" for i in items)
    legacy = client.get("/store/list", headers=headers()).json()["items"]["victory_fireworks"]
    assert legacy["purchasable"] is False
    assert legacy["cost_v"] is None
    assert "private_storage_key" not in response.text
    assert client.get("/store/emotes/catalog/test_emote/preview").status_code == 200
    assert client.get("/store/emotes/catalog/ai_starter/preview").status_code == 404


def test_production_emotes_catalog_contains_six_unsellable_previews(client, monkeypatch):
    # Remove only the synthetic purchasable fixtures, restoring the actual catalog.
    monkeypatch.delitem(STORE_CATALOG, "test_emote")
    monkeypatch.delitem(STORE_CATALOG, "test_other")
    items = client.get("/store/emotes/catalog").json()["items"]
    assert {item["id"] for item in items} == {
        "openvegas.pixel-courier",
        "openvegas.beat-maker",
        "openvegas.visor-explorer",
        "openvegas.skyline-dunk",
        "openvegas.bicycle-finish",
        "openvegas.three-point-glow",
    }
    assert len(items) == 6
    assert sum(item["category"] == "completion" for item in items) == 3
    assert all(item["preview_only"] and not item["purchasable"] for item in items)
    assert all(item["cost_v"] is None and item["category"] in {"companion", "completion"} for item in items)
    legacy = client.get("/store/list", headers=headers()).json()["items"]
    assert {
        "theme_cyberpunk",
        "theme_retro",
        "horse_skin_unicorn",
        "victory_fireworks",
    } <= legacy.keys()


@pytest.mark.parametrize("slug", ["pixel-courier", "beat-maker", "visor-explorer"])
def test_coordinator_concepts_are_public_previews_never_purchasable(client, service, slug):
    sku = f"openvegas.{slug}"
    item = next(i for i in client.get("/store/emotes/catalog").json()["items"] if i["id"] == sku)
    assert item["pack_id"] == sku
    assert item["preview_url"] == f"/ui/assets/emotes/{slug}/sheet.png"
    assert item["preview_manifest_url"] == f"/ui/assets/emotes/{slug}/manifest.json"
    assert item["preview_only"] is True
    assert item["purchasable"] is False
    assert item["cost_v"] is None
    assert client.post("/store/buy", headers=headers(), json={"item_id": sku}).status_code == 409
    assert service.db.state["debits"] == []


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/store/emotes/owned", None),
        ("get", "/store/grants", None),
        ("get", "/store/list", None),
        ("post", "/store/buy", {"item_id": "test_emote"}),
        ("post", "/store/emotes/equip", {"item_id": "test_emote"}),
    ],
)
@pytest.mark.parametrize("token", ["missing", "expired", "invalid"])
def test_protected_endpoints_require_current_auth(client, method, path, body, token):
    credentials = (
        {}
        if token == "missing"
        else (
            headers(expired=True) if token == "expired" else {"Authorization": "Bearer fake-admin"}
        )
    )
    credentials["X-Admin-Preview"] = "true"
    response = client.request(method, path, json=body, headers=credentials)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "extra",
    [
        {"user_id": "bob"},
        {"cost_v": "0"},
        {"admin": True},
        {"pack_id": "other.pack"},
        {"entitlement": True},
        {"preview": True},
    ],
)
def test_client_cannot_override_price_identity_or_grant_ownership(client, service, extra):
    response = client.post("/store/buy", headers=headers(), json={"item_id": "test_emote", **extra})
    assert response.status_code == 422
    assert service.db.state["debits"] == []


def test_owned_and_equip_derive_account_and_slot_server_side(client):
    assert (
        client.post(
            "/store/emotes/equip", headers=headers(), json={"item_id": "test_emote"}
        ).status_code
        == 403
    )
    bought = client.post(
        "/store/buy", headers=headers(), json={"item_id": "test_emote", "idempotency_key": "key"}
    )
    assert bought.status_code == 200
    assert bought.json()["entitlement"]["effective_status"] == "active"
    equipped = client.post("/store/emotes/equip", headers=headers(), json={"item_id": "test_emote"})
    assert equipped.status_code == 200
    assert equipped.json()["slot"] == "completion"
    owned = client.get("/store/emotes/owned", headers=headers()).json()
    assert owned["equipped"] == {"completion": "test_emote"}
    assert client.get("/store/emotes/owned", headers=headers("bob")).json()["entitlements"] == []
    assert (
        client.post(
            "/store/emotes/equip", headers=headers("bob"), json={"item_id": "test_emote"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/store/emotes/equip", headers=headers(), json={"item_id": None, "slot": "completion"}
        ).status_code
        == 200
    )


def test_preview_only_purchase_denied_even_with_admin_header(client, service):
    response = client.post(
        "/store/buy",
        headers={**headers(), "X-Admin-Preview": "true"},
        json={"item_id": "victory_fireworks", "idempotency_key": "key"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "COSMETIC_PREVIEW_ONLY"
    assert service.db.state["debits"] == []


def test_expired_entitlement_and_key_conflict_status_codes(client, service):
    payload = {"item_id": "test_emote", "idempotency_key": "key"}
    assert client.post("/store/buy", headers=headers(), json=payload).status_code == 200
    assert (
        client.post(
            "/store/buy", headers=headers(), json={**payload, "item_id": "test_other"}
        ).status_code
        == 409
    )
    service.db.state["entitlements"]["alice", "test_emote"]["expires_at"] = datetime.now(
        UTC
    ) - timedelta(seconds=1)
    assert (
        client.post(
            "/store/emotes/equip", headers=headers(), json={"item_id": "test_emote"}
        ).status_code
        == 410
    )


def test_no_private_pack_download_surface(client):
    assert client.get("/store/emotes/test_emote/download", headers=headers()).status_code == 404


def test_old_list_and_ai_buy_contract(client):
    assert "ai_starter" in client.get("/store/list", headers=headers()).json()["items"]
    response = client.post("/store/buy", headers=headers(), json={"item_id": "ai_starter"})
    assert response.status_code == 200
    assert len(response.json()["grants"]) == 2
    assert response.json()["cost_v"] == "5.00"
    assert response.json()["idempotency_key"].startswith("cli-")
