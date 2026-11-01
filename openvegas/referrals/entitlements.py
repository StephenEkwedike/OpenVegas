"""Schema-aware cosmetic ownership queries, independent of promotion launch flags."""

from uuid import UUID


async def available(conn):
    rows = await conn.fetch("""SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid=to_regclass('public.cosmetic_entitlements')
          AND attname='source_referral_grant_id' AND NOT attisdropped
    ) AND to_regclass('public.referral_grants') IS NOT NULL AS referral_entitlements_available""")
    return bool(rows and rows[0]["referral_entitlements_available"] is True)


ENTITLEMENT_FROM = """
FROM public.cosmetic_entitlements e
LEFT JOIN public.store_orders o ON o.id=e.source_order_id AND o.user_id=e.user_id
LEFT JOIN public.referral_grants g ON g.id=e.source_referral_grant_id
    AND g.user_id=e.user_id AND g.item_id=e.item_id
"""
EFFECTIVE_STATUS = """CASE
WHEN e.status <> 'active' THEN e.status
WHEN e.source_order_id IS NOT NULL AND (o.id IS NULL OR o.status <> 'fulfilled') THEN 'revoked'
WHEN e.source_referral_grant_id IS NOT NULL AND (g.id IS NULL OR g.status <> 'active') THEN 'revoked'
WHEN e.expires_at IS NOT NULL AND e.expires_at <= clock_timestamp() THEN 'expired'
WHEN g.expires_at IS NOT NULL AND g.expires_at <= clock_timestamp() THEN 'expired'
ELSE 'active' END"""


async def find_entitlement(conn, user_id, item_id):
    user = UUID(str(user_id))
    row = await conn.fetchrow("""SELECT source_order_id,source_referral_grant_id
        FROM public.cosmetic_entitlements WHERE user_id=$1 AND item_id=$2 FOR UPDATE""", user, item_id)
    if not row:
        return None
    # Callers acquire the store SKU advisory lock first, including revocation writers.
    if row["source_order_id"]:
        await conn.fetchrow("SELECT id FROM public.store_orders WHERE id=$1 FOR UPDATE", row["source_order_id"])
    if row["source_referral_grant_id"]:
        await conn.fetchrow("SELECT id FROM public.referral_grants WHERE id=$1 FOR SHARE", row["source_referral_grant_id"])
    return await conn.fetchrow(
        f"SELECT e.*, {EFFECTIVE_STATUS} AS effective_status {ENTITLEMENT_FROM} "
        "WHERE e.user_id=$1 AND e.item_id=$2 FOR UPDATE OF e", user, item_id)


async def list_entitlements(conn, user_id):
    return await conn.fetch(
        f"SELECT e.*, clock_timestamp() AS lease_observed_at, {EFFECTIVE_STATUS} AS effective_status, "
        f"(q.item_id IS NOT NULL AND ({EFFECTIVE_STATUS})='active') AS equipped {ENTITLEMENT_FROM} "
        "LEFT JOIN public.cosmetic_equipment q ON q.user_id=e.user_id AND q.item_id=e.item_id "
        "WHERE e.user_id=$1 ORDER BY e.created_at DESC, e.item_id", UUID(str(user_id)))
