-- Permit an explicit, funded purchase to replace a trial/reward without losing
-- the canonical entitlement, equipment references or immutable grant history.
-- xmin is a subtransaction ID inside the wallet's SAVEPOINT. Stamp the top-level
-- transaction at insertion instead; existing rows intentionally remain NULL.
ALTER TABLE public.store_orders ADD COLUMN purchase_transaction_id xid8;
ALTER TABLE public.ledger_entries ADD COLUMN purchase_transaction_id xid8;
CREATE FUNCTION public.stamp_purchase_transaction() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.purchase_transaction_id := pg_current_xact_id();
    ELSIF NEW.purchase_transaction_id IS DISTINCT FROM OLD.purchase_transaction_id THEN
        RAISE EXCEPTION 'Purchase transaction provenance is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER store_order_transaction_stamp BEFORE INSERT OR UPDATE ON public.store_orders
FOR EACH ROW EXECUTE FUNCTION public.stamp_purchase_transaction();
CREATE TRIGGER ledger_transaction_stamp BEFORE INSERT OR UPDATE ON public.ledger_entries
FOR EACH ROW EXECUTE FUNCTION public.stamp_purchase_transaction();
REVOKE ALL ON FUNCTION public.stamp_purchase_transaction() FROM PUBLIC, anon, authenticated;

CREATE FUNCTION public.is_funded_referral_upgrade(
    old_row public.cosmetic_entitlements, new_row public.cosmetic_entitlements
) RETURNS BOOLEAN LANGUAGE sql SET search_path = pg_catalog, public AS $$
    SELECT old_row.source_order_id IS NULL
       AND old_row.source_referral_grant_id IS NOT NULL
       AND old_row.status = 'active' AND new_row.status = 'active'
       AND new_row.source_referral_grant_id IS NULL
       AND new_row.source_order_id IS NOT NULL AND new_row.expires_at IS NULL
       AND (new_row.id, new_row.user_id, new_row.item_id, new_row.slot,
            new_row.pack_id, new_row.acquired_version, new_row.created_at)
           IS NOT DISTINCT FROM
           (old_row.id, old_row.user_id, old_row.item_id, old_row.slot,
            old_row.pack_id, old_row.acquired_version, old_row.created_at)
       AND EXISTS (
           SELECT 1 FROM public.referral_grants g
           JOIN public.store_orders o ON o.id = new_row.source_order_id
           JOIN public.ledger_entries l ON l.reference_id = 'store:' || o.id::text
           WHERE g.id = old_row.source_referral_grant_id
             AND g.user_id = old_row.user_id AND g.item_id = old_row.item_id
             AND g.status = 'active'
             AND o.user_id = old_row.user_id AND o.item_id = old_row.item_id
             AND o.status = 'settled' AND o.cost_v > 0
             AND l.entry_type = 'redeem' AND l.amount = o.cost_v
             AND l.debit_account = 'user:' || old_row.user_id::text
             AND l.credit_account = 'store'
             AND o.purchase_transaction_id = pg_current_xact_id()
             AND l.purchase_transaction_id = pg_current_xact_id()
       );
$$;

CREATE OR REPLACE FUNCTION public.audit_cosmetic_entitlement() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.id, NEW.user_id, NEW.item_id, NEW.slot, NEW.pack_id,
            NEW.acquired_version, NEW.source_order_id, NEW.created_at)
           IS DISTINCT FROM
           (OLD.id, OLD.user_id, OLD.item_id, OLD.slot, OLD.pack_id,
            OLD.acquired_version, OLD.source_order_id, OLD.created_at)
           AND NOT public.is_funded_referral_upgrade(OLD, NEW) THEN
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

CREATE OR REPLACE FUNCTION public.guard_referral_entitlement() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE grant_row public.referral_grants;
BEGIN
    IF NEW.source_referral_grant_id IS NOT NULL THEN
        SELECT * INTO STRICT grant_row FROM public.referral_grants
            WHERE id=NEW.source_referral_grant_id AND user_id=NEW.user_id AND item_id=NEW.item_id;
        IF NEW.expires_at IS DISTINCT FROM grant_row.expires_at THEN
            RAISE EXCEPTION 'Referral expiry must match its authoritative grant';
        END IF;
    END IF;
    IF TG_OP='UPDATE' AND NEW.source_referral_grant_id IS DISTINCT FROM OLD.source_referral_grant_id THEN
        IF public.is_funded_referral_upgrade(OLD, NEW) THEN
            RETURN NEW;
        END IF;
        -- Preserve the existing earned-reward replacement; never renew a trial.
        IF OLD.source_order_id IS NOT NULL OR NEW.source_order_id IS NOT NULL
            OR OLD.status <> 'active' OR grant_row.kind IS DISTINCT FROM 'reward'
            OR grant_row.status IS DISTINCT FROM 'active'
            OR NOT EXISTS (SELECT 1 FROM public.referral_grants
                WHERE id=OLD.source_referral_grant_id AND kind='trial' AND status='active') THEN
            RAISE EXCEPTION 'Invalid referral entitlement source transition';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.is_funded_referral_upgrade(
    public.cosmetic_entitlements, public.cosmetic_entitlements
) FROM PUBLIC, anon, authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        GRANT EXECUTE ON FUNCTION public.is_funded_referral_upgrade(
            public.cosmetic_entitlements, public.cosmetic_entitlements
        ) TO service_role;
    END IF;
END $$;
INSERT INTO public.schema_migrations(version) VALUES ('053_referral_paid_upgrade')
ON CONFLICT (version) DO NOTHING;
