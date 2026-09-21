"""Isolated commerce checks. Synthetic approved assets/prices exist only in tests."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from openvegas.store.catalog import public_catalog, valid_pack_id
from openvegas.store.service import (
    CosmeticUnavailable,
    EntitlementDenied,
    EntitlementExpired,
    IdempotencyConflict,
    StoreError,
    StoreService,
)
from openvegas.wallet.ledger import InsufficientBalance


def test_initial_cosmetics_are_not_for_sale():
    for item in public_catalog(cosmetics_only=True).values():
        assert item["purchasable"] is False
        assert item["availability"] == "preview_only"
        assert item["cost_v"] is None


@pytest.mark.parametrize("pack", ["../pack", "a/pack", "A-pack", "", "a..b", "x" * 65, None])
def test_pack_id_rejects_unsafe_identifiers(pack):
    assert not valid_pack_id(pack)


def test_public_metadata_allowlist_and_authoritative_price(approved):
    item = public_catalog(cosmetics_only=True)["test_emote"]
    assert item["cost_v"] == "2.5"
    assert item["asset"]["pack_id"] == "openvegas.test-pack"
    assert item["asset"]["preview_url"].endswith("test.gif")
    assert "private_storage_key" not in str(item)
    assert "MUST_NOT_LEAK" not in str(item)
    assert "cost_v" not in item["asset"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/private.zip",
        "//example.invalid/a.png",
        "/ui/assets/emotes/previews/../private.png",
        "/ui/assets/emotes/previews/%2e%2e/a.png",
        "/ui/assets/emotes/packs/full.png",
        "/ui/assets/emotes/previews/a.png?token=secret",
    ],
)
def test_preview_never_exposes_private_or_remote_paths(approved, url):
    approved["asset"]["preview_url"] = url
    assert public_catalog(cosmetics_only=True)["test_emote"]["asset"]["preview_url"] is None


@pytest.mark.asyncio
async def test_purchase_is_durable_and_equips_on_new_service(service, approved):
    result = await service.buy("alice", "test_emote", "key")
    assert result.status == "fulfilled"
    assert result.entitlement["pack_id"] == "openvegas.test-pack"
    assert result.cost_v == Decimal("2.5")
    restored = StoreService(service.db, service.wallet)
    await restored.equip("alice", "completion", "test_emote")
    owned = await restored.list_owned("alice")
    assert owned["equipped"] == {"completion": "test_emote"}
    assert len(owned["entitlements"]) == 1
    assert "user_id" not in owned["entitlements"][0]
    assert len(service.db.state["debits"]) == 1
    assert await restored.list_owned("bob") == {"entitlements": [], "equipped": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_purchase_one_order_debit_entitlement(service, approved, same_key):
    results = await asyncio.gather(
        *(service.buy("alice", "test_emote", "same" if same_key else f"key-{i}") for i in range(20))
    )
    assert len({r.order_id for r in results}) == 1
    assert len(service.db.state["orders"]) == 1
    assert len(service.db.state["debits"]) == 1
    assert len(service.db.state["entitlements"]) == 1
    assert len(service.db.state["requests"]) == (0 if same_key else 19)


@pytest.mark.asyncio
async def test_alias_key_replay_and_conflict_are_durable(service, approved):
    original = await service.buy("alice", "test_emote", "one")
    duplicate = await service.buy("alice", "test_emote", "two")
    assert duplicate.already_owned
    assert duplicate.order_id == original.order_id
    restored = StoreService(service.db, service.wallet)
    replay = await restored.buy("alice", "test_emote", "two")
    assert replay.replayed
    assert replay.already_owned
    assert replay.state == "already_owned"
    assert replay.order_id == original.order_id
    with pytest.raises(IdempotencyConflict):
        await restored.buy("alice", "test_other", "two")
    assert len(service.db.state["debits"]) == 1


@pytest.mark.asyncio
async def test_same_key_different_sku_conflicts_and_account_isolation(service, approved):
    results = await asyncio.gather(
        service.buy("alice", "test_emote", "key"),
        service.buy("alice", "test_other", "key"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, IdempotencyConflict) for r in results) == 1
    await service.buy("bob", "test_emote", "key")
    assert len(service.db.state["debits"]) == 2
    with pytest.raises(EntitlementDenied):
        await service.equip("eve", "completion", "test_emote")
    await service.equip("bob", "completion", "test_emote")
    await service.equip("alice", "completion", None)
    assert (await service.list_owned("bob"))["equipped"] == {"completion": "test_emote"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "INSERT INTO cosmetic_entitlements",
        "UPDATE store_orders",
        "final_transition",
    ],
)
async def test_database_failure_rolls_back_order_wallet_and_ownership(service, approved, failure):
    service.db.fail = failure
    with pytest.raises(RuntimeError):
        await service.buy("alice", "test_emote", "key")
    assert service.db.state["orders"] == {}
    assert service.db.state["debits"] == []
    assert service.db.state["entitlements"] == {}
    service.db.fail = None
    await service.buy("alice", "test_emote", "key")
    assert len(service.db.state["debits"]) == 1


@pytest.mark.asyncio
async def test_insufficient_balance_is_atomic(service, approved):
    service.db.state["balances"]["user:alice"] = Decimal(0)
    with pytest.raises(InsufficientBalance):
        await service.buy("alice", "test_emote", "key")
    assert service.db.state["orders"] == {}
    assert service.db.state["entitlements"] == {}
    assert service.db.state["debits"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "revoked", "reversed", "settled"])
async def test_inactive_ownership_does_not_equip_or_repurchase(service, approved, change):
    result = await service.buy("alice", "test_emote", "one")
    await service.equip("alice", "completion", "test_emote")
    row = service.db.state["entitlements"]["alice", "test_emote"]
    error = EntitlementDenied
    if change == "expired":
        row["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
        error = EntitlementExpired
    elif change == "revoked":
        row["status"] = "revoked"
    else:
        service.db.state["orders"][result.order_id]["status"] = change
    with pytest.raises(error):
        await service.equip("alice", "completion", "test_emote")
    with pytest.raises(error):
        await service.buy("alice", "test_emote", "new-key")
    replay = await service.buy("alice", "test_emote", "one")
    assert replay.entitlement["effective_status"] != "active"
    assert (await service.list_owned("alice"))["equipped"] == {}
    assert len(service.db.state["debits"]) == 1


@pytest.mark.asyncio
async def test_unequip_and_replace_selection(service, approved):
    await service.buy("alice", "test_emote", "one")
    await service.buy("alice", "test_other", "two")
    await asyncio.gather(
        service.equip("alice", "completion", "test_emote"),
        service.equip("alice", "completion", "test_other"),
    )
    assert len((await service.list_owned("alice"))["equipped"]) == 1
    with pytest.raises(CosmeticUnavailable):
        await service.equip("alice", "theme", "test_emote")
    await service.equip("alice", "completion", None)
    assert (await service.list_owned("alice"))["equipped"] == {}


@pytest.mark.asyncio
async def test_ai_packs_remain_repeatable_and_replays_do_not_duplicate(service):
    one = await service.buy("alice", "ai_starter", "one")
    two = await service.buy("alice", "ai_starter", "two")
    replay = await service.buy("alice", "ai_starter", "one")
    assert one.order_id != two.order_id
    assert replay.order_id == one.order_id
    assert sum(g["tokens_total"] for g in one.grants) == 50_000
    assert len(one.grants) == 2
    assert len(service.db.state["debits"]) == 2
    assert service.db.state["entitlements"] == {}
    assert len(await service.list_grants("alice")) == 4
    assert await service.list_grants("bob") == []


@pytest.mark.asyncio
async def test_ai_same_key_concurrency_one_debit(service):
    results = await asyncio.gather(*(service.buy("alice", "ai_starter", "key") for _ in range(10)))
    assert len({r.order_id for r in results}) == 1
    assert len(service.db.state["debits"]) == 1
    assert len(service.db.state["grants"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_status", "pending"),
        ("sale_enabled", False),
        ("cost_v", None),
        ("cost_v", "NaN"),
        ("cost_v", "-1"),
        ("cost_v", "0.0000001"),
        ("asset", {}),
    ],
)
async def test_unapproved_or_invalid_assets_cannot_sell(service, approved, field, value):
    approved[field] = value
    with pytest.raises(CosmeticUnavailable):
        await service.buy("alice", "test_emote", "key")
    assert service.db.state["debits"] == []


@pytest.mark.asyncio
async def test_free_server_approved_item_does_not_write_zero_ledger_entry(service, approved):
    approved["cost_v"] = Decimal(0)
    result = await service.buy("alice", "test_emote", "key")
    assert result.entitlement["effective_status"] == "active"
    assert service.db.state["debits"] == []


@pytest.mark.asyncio
async def test_delisting_preserves_historical_replay_but_disables_equip(service, approved):
    await service.buy("alice", "test_emote", "key")
    await service.equip("alice", "completion", "test_emote")
    approved["approval_status"] = "pending"
    replay = await service.buy("alice", "test_emote", "key")
    assert replay.replayed
    assert (await service.list_owned("alice"))["equipped"] == {}
    with pytest.raises(CosmeticUnavailable):
        await service.equip("alice", "completion", "test_emote")


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", " ", "\n", "x" * 129])
async def test_invalid_idempotency_key_fails_before_transaction(service, approved, key):
    with pytest.raises(StoreError):
        await service.buy("alice", "test_emote", key)
    assert service.db.calls == []


@pytest.mark.asyncio
async def test_legacy_paid_cosmetic_without_entitlement_cannot_double_charge(service, approved):
    result = await service.buy("alice", "test_emote", "legacy-key")
    service.db.state["entitlements"].clear()
    with pytest.raises(CosmeticUnavailable, match="REQUIRES_RECONCILIATION"):
        await service.buy("alice", "test_emote", "new-key")
    assert len(service.db.state["debits"]) == 1
    replay = await service.buy("alice", "test_emote", "legacy-key")
    assert replay.order_id == result.order_id
    assert replay.entitlement is None


@pytest.mark.asyncio
@pytest.mark.parametrize("remap", ["pack_id", "slot", "type"])
async def test_catalog_remapping_does_not_upgrade_or_activate_another_pack(
    service, approved, remap
):
    await service.buy("alice", "test_emote", "key")
    await service.equip("alice", None, "test_emote")
    if remap == "pack_id":
        approved["asset"]["pack_id"] = "openvegas.another-premium-pack"
    else:
        approved[remap] = "theme" if remap == "slot" else "ai_pack"
    assert (await service.list_owned("alice"))["equipped"] == {}
    with pytest.raises(CosmeticUnavailable):
        await service.equip("alice", "completion", "test_emote")


@pytest.mark.asyncio
async def test_suspended_entitlement_is_not_activatable_or_deliverable(service, approved):
    await service.buy("alice", "test_emote", "one")
    await service.equip("alice", "completion", "test_emote")
    service.db.state["entitlements"]["alice", "test_emote"]["status"] = "suspended"
    owned = await service.list_owned("alice")
    assert owned["equipped"] == {}
    assert owned["entitlements"][0]["effective_status"] == "suspended"
    assert owned["entitlements"][0]["activatable"] is False
    with pytest.raises(EntitlementDenied):
        await service.equip("alice", "completion", "test_emote")
    with pytest.raises(EntitlementDenied):
        async with service.delivery_asset("alice", "test_emote"):
            pytest.fail("Suspended asset delivered")
    with pytest.raises(EntitlementDenied):
        await service.buy("alice", "test_emote", "another-key")
    assert len(service.db.state["debits"]) == 1


@pytest.mark.asyncio
async def test_payment_policy_failure_rolls_back_and_lock_precedes_sku(service, approved):
    import json

    service.db.state["cash_adjustment_blocked"] = True
    with pytest.raises(StoreError, match="PAYMENT_ADJUSTMENT"):
        await service.buy("alice", "test_emote", "one")
    assert service.db.state["orders"] == service.db.state["entitlements"] == {}
    assert service.db.state["debits"] == []
    locks = [
        json.loads(args[0]) for query, args in service.db.calls if "pg_advisory_xact_lock" in query
    ]
    assert locks[:3] == [
        ["stripe-emote", "alice"],
        ["store", "request", "alice", "one"],
        ["store", "sku", "alice", "test_emote"],
    ]
    assert any(query == "payment_policy_check" for query, _ in service.db.calls)
