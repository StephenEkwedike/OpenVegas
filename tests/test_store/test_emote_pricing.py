"""Owner-approved prices are not permission to open sales."""

from decimal import Decimal

import pytest

from openvegas.store.catalog import (
    STORE_CATALOG,
    cosmetic_price_v,
    cosmetic_purchasable,
    public_cosmetic,
)


def test_six_emotes_have_five_usd_price_but_remain_unsellable(monkeypatch):
    monkeypatch.setenv("V_PER_USD", "100")
    emotes = {sku: item for sku, item in STORE_CATALOG.items() if sku.startswith("openvegas.")}
    assert len(emotes) == 6
    for sku, item in emotes.items():
        assert item["artwork_approved"] is True
        assert item["native_compatibility_verified"] is False
        assert item["price_usd"] == Decimal("5.00")
        assert cosmetic_price_v(item) == Decimal(500)
        assert not cosmetic_purchasable(item)
        public = public_cosmetic(sku, item)
        assert public["cost_v"] is None
        assert Decimal(public["planned_cost_v"]) == 500
        assert public["planned_cost_usd"] == "5.00"


@pytest.mark.parametrize(
    "rate, expected", [
        ("100", "500"), ("200", "1000"), ("99.123456", "495.617280"),
        ("100.00000051", "500.000005"),
    ]
)
def test_server_rate_drives_price(monkeypatch, rate, expected):
    monkeypatch.setenv("V_PER_USD", rate)
    item = {"price_usd": Decimal(5), "cost_v": Decimal(1)}
    assert cosmetic_price_v(item) == Decimal(expected)


@pytest.mark.parametrize("rate", ["0", "-1", "NaN", "Infinity", "", "bad", "1e100", "0.00000001"])
def test_invalid_rate_fails_closed(monkeypatch, rate):
    monkeypatch.setenv("V_PER_USD", rate)
    assert cosmetic_price_v({"price_usd": Decimal(5)}) is None
    from openvegas.payments.service import BillingError, BillingService

    with pytest.raises(BillingError, match="Invalid V_PER_USD"):
        BillingService._v_per_usd()


def test_existing_v_priced_items_do_not_change(monkeypatch):
    monkeypatch.setenv("V_PER_USD", "bad")
    assert cosmetic_price_v({"cost_v": Decimal("2.50")}) == Decimal("2.50")


@pytest.mark.asyncio
async def test_purchase_debits_resolved_price_and_replay_keeps_original_price(
    service, approved, monkeypatch
):
    monkeypatch.setenv("V_PER_USD", "100")
    monkeypatch.setitem(STORE_CATALOG["test_emote"], "price_usd", Decimal(5))
    service.db.state["balances"]["user:alice"] = Decimal(1000)
    result = await service.buy("alice", "test_emote", "usd-price")
    assert result.cost_v == Decimal(500)
    monkeypatch.setenv("V_PER_USD", "200")
    replay = await service.buy("alice", "test_emote", "usd-price")
    assert replay.cost_v == Decimal(500)
    assert replay.replayed


def test_catalog_planned_price_cannot_be_submitted_as_discount():
    from pydantic import ValidationError

    from server.routes.store import StoreBuyRequest

    with pytest.raises(ValidationError):
        StoreBuyRequest(item_id="openvegas.pixel-courier", planned_cost_v="0", price_usd="0")
