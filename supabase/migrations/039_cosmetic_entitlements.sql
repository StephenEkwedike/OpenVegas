-- Durable cosmetic ownership. Production application needs separate approval.
-- Depends on 004_store_fulfillment and the existing Supabase auth/role contract.
-- Backend PostgresDB uses one READ COMMITTED connection for the whole purchase.
-- Backend must run as table owner/service role; NEVER expose that role to clients.

CREATE UNIQUE INDEX store_orders_identity_user_item
    ON public.store_orders (id, user_id, item_id);

CREATE TABLE public.cosmetic_entitlements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    item_id TEXT NOT NULL CHECK (char_length(item_id) BETWEEN 1 AND 128),
    slot TEXT NOT NULL CHECK (slot IN ('theme','victory','horse_skin','companion','completion')),
    pack_id TEXT NOT NULL CHECK (
        pack_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$' AND position('..' in pack_id) = 0
    ),
    acquired_version TEXT NOT NULL CHECK (
        acquired_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
        AND position('..' in acquired_version) = 0
    ),
    source_order_id UUID NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revocation_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, item_id),
    UNIQUE (user_id, item_id, slot),
    FOREIGN KEY (source_order_id, user_id, item_id)
        REFERENCES public.store_orders (id, user_id, item_id),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (
        (status = 'active' AND revoked_at IS NULL AND revocation_reason IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL
            AND nullif(btrim(revocation_reason), '') IS NOT NULL)
    )
);

-- One preference per account/slot, not tied to an expiring local session/version.
-- FK forbids equipping another account's item or placing it in the wrong slot.
-- Expiration/revocation is checked on EVERY authoritative read/activation.
CREATE TABLE public.cosmetic_equipment (
    user_id UUID NOT NULL REFERENCES auth.users(id),
    slot TEXT NOT NULL,
    item_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, slot),
    FOREIGN KEY (user_id, item_id, slot)
        REFERENCES public.cosmetic_entitlements (user_id, item_id, slot)
);

-- Extra idempotency keys resolve to the canonical paid order, not $0 fake orders.
-- Original keys remain in store_orders for backwards compatibility.
CREATE TABLE public.store_purchase_requests (
    user_id UUID NOT NULL REFERENCES auth.users(id),
    idempotency_key TEXT NOT NULL CHECK (
        char_length(idempotency_key) BETWEEN 1 AND 128
        AND nullif(btrim(idempotency_key), '') IS NOT NULL
    ),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    source_order_id UUID NOT NULL,
    item_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, idempotency_key),
    FOREIGN KEY (source_order_id, user_id, item_id)
        REFERENCES public.store_orders (id, user_id, item_id)
);

CREATE TABLE public.cosmetic_entitlement_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entitlement_id UUID NOT NULL REFERENCES public.cosmetic_entitlements(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('granted', 'updated', 'revoked')),
    old_record JSONB,
    new_record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION public.audit_cosmetic_entitlement() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.id, NEW.user_id, NEW.item_id, NEW.slot, NEW.pack_id,
            NEW.acquired_version, NEW.source_order_id, NEW.created_at)
           IS DISTINCT FROM
           (OLD.id, OLD.user_id, OLD.item_id, OLD.slot, OLD.pack_id,
            OLD.acquired_version, OLD.source_order_id, OLD.created_at) THEN
            RAISE EXCEPTION 'Cosmetic entitlement identity is immutable';
        END IF;
        IF OLD.status = 'revoked' AND NEW.status <> 'revoked' THEN
            RAISE EXCEPTION 'Revoked entitlements require an explicit reconciliation policy';
        END IF;
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER cosmetic_entitlement_guard
BEFORE UPDATE ON public.cosmetic_entitlements
FOR EACH ROW EXECUTE FUNCTION public.audit_cosmetic_entitlement();

CREATE FUNCTION public.record_cosmetic_entitlement_event() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    INSERT INTO public.cosmetic_entitlement_events
        (entitlement_id, user_id, event_type, old_record, new_record)
    VALUES (
        NEW.id, NEW.user_id,
        CASE WHEN TG_OP = 'INSERT' THEN 'granted'
             WHEN NEW.status = 'revoked' AND OLD.status <> 'revoked' THEN 'revoked'
             ELSE 'updated' END,
        CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) ELSE NULL END,
        to_jsonb(NEW)
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER cosmetic_entitlement_audit
AFTER INSERT OR UPDATE ON public.cosmetic_entitlements
FOR EACH ROW EXECUTE FUNCTION public.record_cosmetic_entitlement_event();

CREATE INDEX cosmetic_entitlement_events_user_created
    ON public.cosmetic_entitlement_events (user_id, created_at DESC);

-- Backend-only routes, matching 038_private_runtime_rls. Deliberately no direct
-- browser SELECT/INSERT/UPDATE/DELETE policies; API responses filter by JWT user.
ALTER TABLE public.cosmetic_entitlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cosmetic_equipment ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.store_purchase_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cosmetic_entitlement_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.cosmetic_entitlements, public.cosmetic_equipment,
    public.store_purchase_requests, public.cosmetic_entitlement_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SEQUENCE public.cosmetic_entitlement_events_id_seq FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.audit_cosmetic_entitlement() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.record_cosmetic_entitlement_event() FROM PUBLIC, anon, authenticated;

-- Supabase's service_role may not exist in a minimal local PostgreSQL scaffold.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        -- Supabase default privileges may already grant ALL. Reset first so
        -- event/request history cannot be updated/deleted by this application role.
        REVOKE ALL ON public.cosmetic_entitlements, public.cosmetic_equipment,
            public.store_purchase_requests, public.cosmetic_entitlement_events FROM service_role;
        REVOKE ALL ON SEQUENCE public.cosmetic_entitlement_events_id_seq FROM service_role;
        GRANT SELECT, INSERT, UPDATE ON public.cosmetic_entitlements TO service_role;
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.cosmetic_equipment TO service_role;
        GRANT SELECT, INSERT ON public.store_purchase_requests,
            public.cosmetic_entitlement_events TO service_role;
        GRANT USAGE, SELECT ON SEQUENCE public.cosmetic_entitlement_events_id_seq TO service_role;
    END IF;
END $$;

-- No price/approval comes from client manifests. No automatic legacy backfill:
-- coordinator must reconcile previously fulfilled cosmetic orders against real
-- approved SKU/pack mappings, and compensate duplicates with append-only ledger
-- entries under a separate refund policy. Service refuses a second paid order
-- when legacy created/settled/fulfilled/reversed history needs reconciliation.
-- No new pack, approved art flag, price or administrator entitlement is seeded.
-- Startup readiness checks must add these four tables in coordinator-owned code.
--
-- Lock contract (all writers including future refund/revoke workers must follow):
-- Purchase: pg_advisory_xact_lock(hashtextextended(JSON compact array
--   ["store","request",user_id,idempotency_key], 0)), then the same lock for
--   ["store","sku",user_id,item_id], then order/entitlement row locks.
-- Equip: ["store","slot",user_id,slot], then ["store","sku",user_id,item_id].
-- Revoke/refund: acquire account-SKU scope BEFORE changing orders/entitlements;
-- never acquire slot/request scopes after SKU. User IDs must be canonical JWT
-- UUID strings. Hash collisions only serialize unrelated operations.
-- WalletService.redeem(tx=tx) reuses this exact transaction and wallet locking.
-- SQL uniqueness/FKs backstop application locks; no process-local lock reliance.
-- Do not change transaction isolation to REPEATABLE READ without retries/testing.
-- No private storage bucket, signed download, refunds or offline licenses here.
