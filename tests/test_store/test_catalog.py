"""Release metadata gates for the server-owned cosmetic catalog."""

from copy import deepcopy
from decimal import Decimal

import pytest

from openvegas.store.catalog import STORE_CATALOG, cosmetic_purchasable, public_cosmetic
from openvegas.store.service import CosmeticUnavailable


def released_item(slot):
    return {
        "type": "cosmetic",
        "slot": slot,
        "approval_status": "approved",
        "sale_enabled": True,
        "artwork_approved": True,
        "native_compatibility_verified": True,
        "cost_v": Decimal("2.50"),
        "asset": {"pack_id": "openvegas.catalog-fixture", "version": "1.0.0"},
    }


@pytest.mark.parametrize("slot", ["companion", "completion"])
def test_consistent_release_metadata_is_purchasable(slot):
    item = released_item(slot)
    assert cosmetic_purchasable(item)
    public = public_cosmetic("test_release", item)
    assert public["purchasable"] is True
    assert public["availability"] == "available"
    assert public["cost_v"] == "2.50"


@pytest.mark.parametrize("slot", ["companion", "completion"])
@pytest.mark.parametrize("field", ["artwork_approved", "native_compatibility_verified"])
@pytest.mark.parametrize("value", [False, None, 0, 1, "false", "true", [], {}])
def test_release_metadata_requires_literal_true(slot, field, value):
    item = released_item(slot)
    item[field] = value
    assert not cosmetic_purchasable(item)
    public = public_cosmetic("test_release", item)
    assert public["purchasable"] is False
    assert public["availability"] == "preview_only"
    assert public["cost_v"] is None


@pytest.mark.parametrize("slot", ["companion", "completion"])
@pytest.mark.parametrize("missing", ["artwork_approved", "native_compatibility_verified"])
def test_partial_release_metadata_fails_closed(slot, missing):
    item = released_item(slot)
    del item[missing]
    assert not cosmetic_purchasable(item)


@pytest.mark.parametrize("slot", ["companion", "completion"])
def test_approval_without_both_release_flags_fails_closed(slot):
    item = released_item(slot)
    del item["artwork_approved"], item["native_compatibility_verified"]
    assert not cosmetic_purchasable(item)
    public = public_cosmetic("test_release", item)
    assert public["purchasable"] is False
    assert public["availability"] == "preview_only"
    assert public["cost_v"] is None


@pytest.mark.parametrize("slot", ["theme", "victory", "horse_skin"])
def test_other_cosmetic_slots_do_not_require_native_release_approval(slot):
    item = released_item(slot)
    item.update(artwork_approved=False, native_compatibility_verified=False)
    assert cosmetic_purchasable(item)
    del item["artwork_approved"], item["native_compatibility_verified"]
    assert cosmetic_purchasable(item)


@pytest.mark.parametrize("slot", ["companion", "completion"])
@pytest.mark.parametrize("change", [
    {"approval_status": "pending"}, {"sale_enabled": False}, {"sale_enabled": 1},
    {"asset": {"pack_id": "openvegas.catalog-fixture", "version": None}},
    {"cost_v": "NaN"}, {"type": "ai_pack"},
])
def test_release_flags_do_not_override_existing_purchase_gates(slot, change):
    item = released_item(slot)
    item.update(change)
    assert not cosmetic_purchasable(item)


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["companion", "completion"])
@pytest.mark.parametrize("artwork,native", [(False, True), (True, False), (False, False)])
async def test_contradictory_release_cannot_create_purchase(service, monkeypatch, slot, artwork, native):
    item = released_item(slot)
    item.update(artwork_approved=artwork, native_compatibility_verified=native)
    monkeypatch.setitem(STORE_CATALOG, "test_release", item)
    before = deepcopy(service.db.state)
    with pytest.raises(CosmeticUnavailable, match="^COSMETIC_PREVIEW_ONLY$"):
        await service.buy("alice", "test_release", "release-guard")
    assert service.db.state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["companion", "completion"])
@pytest.mark.parametrize("missing", [
    ("artwork_approved",), ("native_compatibility_verified",),
    ("artwork_approved", "native_compatibility_verified"),
])
async def test_missing_release_flags_cannot_create_purchase(service, monkeypatch, slot, missing):
    item = released_item(slot)
    for field in missing:
        del item[field]
    monkeypatch.setitem(STORE_CATALOG, "test_release", item)
    before = deepcopy(service.db.state)
    with pytest.raises(CosmeticUnavailable, match="^COSMETIC_PREVIEW_ONLY$"):
        await service.buy("alice", "test_release", "release-guard")
    assert service.db.state == before


def test_current_catalog_sales_stay_disabled():
    cosmetics = [item for item in STORE_CATALOG.values() if item["type"] == "cosmetic"]
    assert cosmetics
    assert all(item["sale_enabled"] is False for item in cosmetics)
    assert all(not cosmetic_purchasable(item) for item in cosmetics)
