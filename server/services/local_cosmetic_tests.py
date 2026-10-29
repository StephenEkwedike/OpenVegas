"""Explicit local-only test entitlements; never a purchase or production grant API."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from urllib.parse import urlsplit

from openvegas.emotes.provisioning import PACK_SLOTS
from openvegas.store.catalog import STORE_CATALOG
from openvegas.store.service import StoreService
from server.services import emote_delivery as delivery

FLAG = 'OPENVEGAS_LOCAL_COSMETIC_TESTS'
PREFIX = 'local-test.'
TEST_SKUS = tuple(sorted(PREFIX + pack.removeprefix('openvegas.') for pack in PACK_SLOTS))


def require_local_config(env):
    if env.get(FLAG) != '1' or env.get('OPENVEGAS_RUNTIME_ENV') != 'development':
        raise ValueError('Local cosmetic tests require explicit development opt-in')
    for key, port, schemes in (
        ('DATABASE_URL', 54322, {'postgresql', 'postgres'}),
        ('SUPABASE_URL', 54321, {'http'}),
    ):
        address = urlsplit(env.get(key, ''))
        if (address.scheme not in schemes or address.hostname not in
                {'127.0.0.1', 'localhost', '::1', 'host.docker.internal'} or
                address.port != port or address.query or address.fragment or
                (key == 'DATABASE_URL' and address.path != '/postgres')):
            raise ValueError('Local cosmetic tests require the isolated development stack')


def local_catalog():
    """Return an isolated catalog view; global pricing/sales approvals stay untouched."""
    if os.environ.get(FLAG) != '1':
        return None
    require_local_config(os.environ)
    root = delivery._private_location('unused').parent
    with delivery._directory(root) as fd:
        raw = delivery._read(fd, delivery.RELEASE_FILE, delivery.MAX_PRIVATE_RELEASE_BYTES)
    pin = os.environ.get(delivery.RELEASE_PIN_ENV, '')
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError('Local test artwork release does not match its configured pin')
    release = json.loads(raw)
    entries = release.get('packs', [])
    if {p.get('pack_id') for p in entries} != set(PACK_SLOTS):
        raise ValueError('Local test artwork requires the approved six-pack release')
    catalog = dict(STORE_CATALOG)
    for row in entries:
        asset = {key: row[key] for key in ('pack_id', 'version', 'delivery_resource')}
        verified = delivery._release_entry(raw, pin, asset)
        pack_id = row['pack_id']
        if verified['slot'] != PACK_SLOTS[pack_id]:
            raise ValueError('Local test artwork slot mismatch')
        sku = PREFIX + pack_id.removeprefix('openvegas.')
        item = deepcopy(STORE_CATALOG[pack_id])
        item.update(name='Local test: ' + item['name'],
                    description='Expiring local test grant. Not a purchase or production entitlement.',
                    approval_status='approved', sale_enabled=False,
                    native_compatibility_verified=False, asset=asset)
        catalog[sku] = item
    return catalog


async def grant_local_tests(store, user_id, *, revoke=False):
    """Seven-day grants, idempotent without renewal; existing equipment is preserved."""
    require_local_config(os.environ)
    user_id = str(uuid.UUID(user_id))
    email = await store.db.fetchval('SELECT email FROM auth.users WHERE id=$1', uuid.UUID(user_id))
    if not email or email.rsplit('@', 1)[-1] != 'local.test':
        raise ValueError('Test grants are restricted to existing @local.test accounts')
    items = TEST_SKUS
    if not revoke and any(k not in store.catalog for k in items):
        raise ValueError('Local test catalog is not configured')
    # Verify every real delivery before any write. Private bytes are never logged.
    if not revoke:
        for sku in items:
            delivery.load_delivery_pack(sku, store.catalog[sku]['asset'])
    async with store.db.transaction() as tx:
        for slot in ('companion', 'completion'):
            await store._lock_scope(tx, 'slot', user_id, slot)
        for sku in items:
            await store._lock_scope(tx, 'sku', user_id, sku)
            existing = await store._find_entitlement(tx, user_id, sku)
            if revoke:
                if existing and existing['effective_status'] != 'revoked':
                    await tx.execute("""UPDATE cosmetic_entitlements SET status='revoked',
                        revoked_at=now(), revocation_reason='local_test_removed'
                        WHERE user_id=$1 AND item_id=$2""", user_id, sku)
                await tx.execute('DELETE FROM cosmetic_equipment WHERE user_id=$1 AND item_id=$2', user_id, sku)
                continue
            if existing:
                store._require_active(existing)
                continue
            item = store.catalog[sku]
            key = 'local-animation-test:v1:' + sku
            payload = StoreService.canonical_payload_hash({'kind': 'local_test_grant', 'user': user_id,
                'sku': sku, 'release': os.environ[delivery.RELEASE_PIN_ENV]})
            order_id = await tx.fetchval("""INSERT INTO store_orders
                (user_id,item_id,cost_v,status,idempotency_key,idempotency_payload_hash)
                VALUES($1,$2,0,'fulfilled',$3,$4) RETURNING id""", user_id, sku, key, payload)
            await tx.execute("""INSERT INTO cosmetic_entitlements
                (user_id,item_id,slot,pack_id,acquired_version,source_order_id,expires_at)
                VALUES($1,$2,$3,$4,$5,$6,now()+interval '7 days')""",
                user_id, sku, item['slot'], item['asset']['pack_id'], item['asset']['version'], order_id)
        if not revoke:
            for slot, sku in (('companion','local-test.beat-maker'), ('completion','local-test.skyline-dunk')):
                await tx.execute("""INSERT INTO cosmetic_equipment(user_id,slot,item_id)
                    VALUES($1,$2,$3) ON CONFLICT(user_id,slot) DO NOTHING""", user_id, slot, sku)
    return await store.list_owned(user_id)
