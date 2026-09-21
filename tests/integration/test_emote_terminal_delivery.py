"""Real local SQL and HTTP-to-renderer activation, synthetic signed payment only."""

import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt

from openvegas.emotes import manifest
from openvegas.emotes.remote import RemoteLibrary
from openvegas.emotes.resources import Catalog, PackRepository
from openvegas.emotes.selection import SelectionStore
from openvegas.emotes.transport import EmoteAPI
from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.service import StoreService
from openvegas.wallet.ledger import WalletService
from server.middleware import auth
from server.routes import store
from tests.integration.test_restoration_db import _billing, _checkout_event, _signed, _topup, _user


@pytest.mark.asyncio
async def test_real_db_topup_private_delivery_restore_and_revocation(
    database_factory, tmp_path, monkeypatch
):
    sku = "openvegas.delivery-fixture"
    root = tmp_path.resolve() / "private-fixtures"
    target = root / "delivery-fixture"
    target.mkdir(parents=True)
    public = Path(manifest.__file__).parent / "assets" / "pixel-courier"
    raw = json.loads((public / "manifest.json").read_bytes())
    raw.update(pack_id=sku, version="0.0.1")
    (target / "manifest.json").write_text(json.dumps(raw))
    (target / "sheet.png").write_bytes((public / "sheet.png").read_bytes())
    monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", str(root))
    monkeypatch.setitem(
        STORE_CATALOG,
        sku,
        {
            "name": "Synthetic fixture",
            "type": "cosmetic",
            "slot": "companion",
            "cost_v": Decimal("2.50"),
            "approval_status": "approved",
            "sale_enabled": True,
            "asset": {"pack_id": sku, "version": "0.0.1", "delivery_resource": "delivery-fixture"},
        },
    )
    async with database_factory() as sandbox:
        db = sandbox.db
        topup = await _topup(db)
        user = str(topup[0])
        await _billing(db).handle_webhook(**_signed(_checkout_event(topup)))
        service = StoreService(db, WalletService(db))
        await service.buy(user, sku, "delivery-once")
        await service.equip(user, "companion", sku)
        assert await service.wallet.get_balance(f"user:{user}") == Decimal("997.50")
        app = FastAPI()
        app.include_router(store.router)
        monkeypatch.setattr(store, "get_store_service", lambda: service)
        secret = "local-emote-delivery-integration-signature-only"
        monkeypatch.setattr(auth, "_supabase_jwt_secret", lambda: secret)

        def token(account):
            return jwt.encode(
                {"sub": account, "aud": "authenticated", "exp": int(time.time()) + 120},
                secret,
                algorithm="HS256",
            )

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            api = EmoteAPI(
                credential_reader=lambda: ("https://example.test", user, token(user)), http=http
            )
            state = SelectionStore(tmp_path / "new-device")
            library = RemoteLibrary(api, tmp_path / "cache", state)
            assert (await library.sync())["available"] == [sku]
            pack = state.load_selected(
                catalog=library.catalog(Catalog()), repository=library.repository(PackRepository())
            )
            assert pack.manifest.pack_id == sku and pack.frame(0).size == (64, 80)
            assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlements") == 1
            assert (
                await db.fetchval("SELECT count(*) FROM ledger_entries WHERE entry_type='redeem'")
                == 1
            )
            foreign = str(await _user(db))
            denied = await http.get(
                f"https://example.test/store/emotes/{sku}/pack",
                headers={"Authorization": f"Bearer {token(foreign)}"},
            )
            assert denied.status_code == 403 and "no-store" in denied.headers["cache-control"]
            async with db.transaction() as tx:
                await service._lock_scope(tx, "sku", user, sku)
                await tx.execute(
                    "UPDATE cosmetic_entitlements SET status='revoked', revoked_at=now(), revocation_reason='local_test' WHERE user_id=$1",
                    user,
                )
            await library.refresh()
            assert not library.authorize(sku) and state.read() is None
            library.close()
        assert await db.fetchval("SELECT count(*) FROM cosmetic_entitlement_events") == 2
