"""HTTP -> private download -> CLI selection -> renderer, with synthetic accounts."""

import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt

from openvegas.emotes.controller import EmoteController
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.manifest import PackError
from openvegas.emotes.remote import RemoteLibrary
from openvegas.emotes.resources import Catalog, PackRepository
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.transport import EmoteAPI
from server.middleware import auth
from server.routes import store

SECRET = "synthetic-emote-flow-secret-not-a-real-credential"


def token(user):
    return jwt.encode(
        {"sub": user, "aud": "authenticated", "exp": int(time.time()) + 120},
        SECRET,
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_purchase_http_restore_new_device_animate_once_and_revoke(
    service, approved, tmp_path, monkeypatch
):
    approved["slot"] = "companion"
    user = str(uuid4())
    other = str(uuid4())
    app = FastAPI()
    app.include_router(store.router)
    monkeypatch.setattr(store, "get_store_service", lambda: service)
    monkeypatch.setattr(auth, "_supabase_jwt_secret", lambda: SECRET)
    credentials = lambda: ("https://example.test", user, token(user))
    state = SelectionStore(tmp_path / "first-device")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        headers = {"Authorization": f"Bearer {token(user)}"}
        bought = await http.post(
            "https://example.test/store/buy",
            headers=headers,
            json={"item_id": "test_emote", "idempotency_key": "test-once"},
        )
        assert bought.status_code == 200
        replay = await http.post(
            "https://example.test/store/buy",
            headers=headers,
            json={"item_id": "test_emote", "idempotency_key": "test-once"},
        )
        assert replay.json()["order_id"] == bought.json()["order_id"]
        assert len(service.db.state["debits"]) == 1

        api = EmoteAPI(credential_reader=credentials, http=http)
        library = RemoteLibrary(api, tmp_path / "cache", state, get_identity=api.identity)
        await library.equip("openvegas.test-pack")
        assert state.read() == "openvegas.test-pack"
        library.close()
        assert state.read() == "openvegas.test-pack"

        fresh_state = SelectionStore(tmp_path / "new-device")
        restored = RemoteLibrary(
            api, tmp_path / "new-cache", fresh_state, get_identity=api.identity
        )
        result = await restored.sync()
        assert result["available"] == ["openvegas.test-pack"]
        assert fresh_state.read() == "openvegas.test-pack"
        pack = fresh_state.load_selected(
            catalog=restored.catalog(Catalog()), repository=restored.repository(PackRepository())
        )
        now = [0.0]
        controller = EmoteController(pack, source="openvegas", session_id="s", clock=lambda: now[0])
        controller.handle(Event("openvegas", "s", "turn", "start", Phase.START, 1, 0))
        assert controller.current_state == "active"
        controller.handle(Event("openvegas", "s", "turn", "done", Phase.COMPLETE, 1, 1, "success"))
        assert controller.current_state == "complete"
        now[0] += 5
        controller.tick()
        assert controller.current_state == "idle"
        controller.handle(
            Event("openvegas", "s", "turn", "duplicate", Phase.COMPLETE, 1, 2, "success")
        )
        assert controller.current_state == "idle"

        # Copying a first account's saved pack and preferences cannot activate for another.
        outsider_api = EmoteAPI(
            credential_reader=lambda: ("https://example.test", other, token(other)), http=http
        )
        outsider = RemoteLibrary(
            outsider_api, tmp_path / "new-cache", state, get_identity=outsider_api.identity
        )
        assert (await outsider.sync())["available"] == []
        assert not outsider.authorize("openvegas.test-pack")
        forbidden = await http.get(
            "https://example.test/store/emotes/test_emote/pack",
            headers={"Authorization": f"Bearer {token(other)}"},
        )
        assert forbidden.status_code == 403
        assert "no-store" in forbidden.headers["cache-control"]

        entitlement = service.db.state["entitlements"][user, "test_emote"]
        entitlement.update(status="revoked", revoked_at=datetime.now(UTC), revocation_reason="test")
        await restored.refresh()
        assert not restored.authorize("openvegas.test-pack")
        assert fresh_state.read() is None
        with pytest.raises(PackError):
            restored.repository(PackRepository()).load(pack.manifest.pack_id)
        assert len(service.db.state["debits"]) == 1
        restored.close()
        outsider.close()


@pytest.mark.asyncio
async def test_reading_library_preserves_existing_preference(
    service, approved, tmp_path, monkeypatch
):
    approved["slot"] = "companion"
    user = str(uuid4())
    await service.buy(user, "test_emote", "key")
    await service.equip(user, "companion", "test_emote")
    app = FastAPI()
    app.include_router(store.router)
    monkeypatch.setattr(store, "get_store_service", lambda: service)
    monkeypatch.setattr(auth, "_supabase_jwt_secret", lambda: SECRET)
    state = SelectionStore(tmp_path / "state")
    state.write("openvegas.test-pack")
    before = state.revision()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        api = EmoteAPI(
            credential_reader=lambda: ("https://example.test", user, token(user)), http=http
        )
        library = RemoteLibrary(api, tmp_path / "cache", state)
        report = await library.refresh()
        assert report["owned"]["entitlements"][0]["activatable"] is True
        library.close()
    assert state.read() == "openvegas.test-pack" and state.revision() == before
