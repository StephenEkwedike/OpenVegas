"""Release rotation cannot leave a new debit or serve stale private bytes."""
from copy import deepcopy

import pytest

import openvegas.store.service as store
import server.routes.store as routes
from server.services import emote_delivery as delivery
from tests.test_store.test_delivery import (
    PACK_PATH,
    credentials,
)
from tests.test_store.test_delivery import client as client  # noqa: PLC0414
from tests.test_store.test_delivery import owned as owned  # noqa: PLC0414
from tests.test_store.test_delivery import pack_files as pack_files  # noqa: PLC0414


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["loader", "wallet", "policy"])
@pytest.mark.parametrize("change", ["pin", "root", "catalog", "artwork_approved",
                                    "native_compatibility_verified", "sale_enabled"])
async def test_release_rotation_rolls_back_new_purchase(service, approved, monkeypatch, phase, change):
    before = deepcopy(service.db.state)

    def rotate():
        if change == "pin":
            monkeypatch.setenv(delivery.RELEASE_PIN_ENV, "0" * 64)
        elif change == "root":
            monkeypatch.setenv("OPENVEGAS_EMOTE_PACK_ROOT", "/unconfigured/new-release")
        elif change == "catalog":
            approved["artwork_approved"] = False
        else:
            approved[change] = 1  # Dict equality alone considers True == 1.

    if phase == "loader":
        original = delivery.load_delivery_pack

        def load(*args):
            payload = original(*args)
            rotate()
            return payload

        monkeypatch.setattr(delivery, "load_delivery_pack", load)
    elif phase == "wallet":
        original = service.wallet.redeem

        async def redeem(*args, **kwargs):
            result = await original(*args, **kwargs)
            rotate()
            return result

        monkeypatch.setattr(service.wallet, "redeem", redeem)
    else:
        original = store.check_purchase_adjustments

        async def check(*args, **kwargs):
            await original(*args, **kwargs)
            rotate()

        monkeypatch.setattr(store, "check_purchase_adjustments", check)

    with pytest.raises(store.CosmeticUnavailable, match="^COSMETIC_DELIVERY_UNAVAILABLE$"):
        await service.buy("alice", "test_emote", "release-rotation")
    assert service.db.state == before
    assert not any(lock.locked() for lock in service.db.locks.values())


@pytest.mark.parametrize("change", ["pin", "root"])
def test_download_rechecks_configuration_after_worker_returns(owned, monkeypatch, change):
    original = routes.load_delivery_pack

    def load(*args):
        payload = original(*args)
        monkeypatch.setenv(
            delivery.RELEASE_PIN_ENV if change == "pin" else "OPENVEGAS_EMOTE_PACK_ROOT",
            "0" * 64 if change == "pin" else "/unconfigured/new-release",
        )
        return payload

    monkeypatch.setattr(routes, "load_delivery_pack", load)
    response = owned.get(PACK_PATH, headers=credentials())
    assert response.status_code == 503
    assert response.json() == {"detail": "COSMETIC_DELIVERY_UNAVAILABLE"}
    assert response.headers["cache-control"] == "private, no-store"
