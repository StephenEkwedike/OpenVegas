-- Default-off cosmetics-only referral foundation. No eligibility or grants seeded.
-- 051 is reserved by the TV worker. Requires 039_cosmetic_entitlements.
CREATE TABLE public.referral_identities (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id),
    identity_hash TEXT NOT NULL UNIQUE CHECK (identity_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.referral_invites (
    user_id UUID PRIMARY KEY REFERENCES public.referral_identities(user_id),
    code TEXT NOT NULL UNIQUE CHECK (code ~ '^[a-f0-9]{32}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.referral_attributions (
    invitee_id UUID PRIMARY KEY REFERENCES public.referral_identities(user_id),
    inviter_id UUID NOT NULL REFERENCES public.referral_invites(user_id),
    ticket_hash TEXT NOT NULL UNIQUE CHECK (ticket_hash ~ '^[0-9a-f]{64}$'),
    visited_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (inviter_id <> invitee_id),
    CHECK (visited_at <= created_at)
);
CREATE INDEX referral_attributions_inviter ON public.referral_attributions(inviter_id);
CREATE TABLE public.referral_sessions (
    invitee_id UUID PRIMARY KEY REFERENCES public.referral_attributions(invitee_id),
    request_id UUID NOT NULL UNIQUE REFERENCES public.inference_requests(id),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    qualified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
-- Explicit operator allowlist; does not alter the store's sale_enabled flag.
CREATE TABLE public.referral_eligible_items (
    item_id TEXT PRIMARY KEY CHECK (char_length(item_id) BETWEEN 1 AND 128),
    enabled BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE public.referral_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.referral_identities(user_id),
    kind TEXT NOT NULL CHECK (kind IN ('trial', 'reward')),
    item_id TEXT NOT NULL CHECK (char_length(item_id) BETWEEN 1 AND 128),
    idempotency_key UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ,
    UNIQUE (user_id, kind),
    UNIQUE (user_id, idempotency_key),
    UNIQUE (id, user_id, item_id),
    CHECK ((kind='trial' AND expires_at=created_at + interval '168 hours')
        OR (kind='reward' AND expires_at IS NULL)),
    CHECK (kind <> 'trial' OR expires_at IS NOT NULL)
);

ALTER TABLE public.cosmetic_entitlements ALTER COLUMN source_order_id DROP NOT NULL;
ALTER TABLE public.cosmetic_entitlements ADD COLUMN source_referral_grant_id UUID UNIQUE;
ALTER TABLE public.cosmetic_entitlements ADD CONSTRAINT cosmetic_entitlement_source
    CHECK (num_nonnulls(source_order_id, source_referral_grant_id) = 1);
ALTER TABLE public.cosmetic_entitlements ADD CONSTRAINT cosmetic_referral_source
    FOREIGN KEY (source_referral_grant_id, user_id, item_id)
    REFERENCES public.referral_grants(id, user_id, item_id);

CREATE FUNCTION public.guard_referral_grant() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status')
        OR (OLD.status='revoked' AND NEW.status <> 'revoked') THEN
        RAISE EXCEPTION 'Referral grant identity and duration are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER referral_grant_guard BEFORE UPDATE ON public.referral_grants
FOR EACH ROW EXECUTE FUNCTION public.guard_referral_grant();

CREATE FUNCTION public.guard_referral_entitlement() RETURNS TRIGGER
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
        -- An earned reward may replace a trial, never renew one or rewrite a purchase.
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
CREATE TRIGGER referral_entitlement_guard BEFORE INSERT OR UPDATE ON public.cosmetic_entitlements
FOR EACH ROW EXECUTE FUNCTION public.guard_referral_entitlement();

ALTER TABLE public.referral_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_attributions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_eligible_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.referral_grants ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.referral_identities, public.referral_invites, public.referral_attributions,
    public.referral_sessions, public.referral_eligible_items, public.referral_grants
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.guard_referral_grant(), public.guard_referral_entitlement()
    FROM PUBLIC, anon, authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        REVOKE ALL ON public.referral_identities, public.referral_invites, public.referral_attributions,
            public.referral_sessions, public.referral_eligible_items, public.referral_grants FROM service_role;
        GRANT SELECT, INSERT ON public.referral_identities, public.referral_invites,
            public.referral_attributions, public.referral_sessions, public.referral_grants TO service_role;
        GRANT SELECT ON public.referral_eligible_items TO service_role;
        GRANT UPDATE (status) ON public.referral_grants TO service_role;
    END IF;
END $$;
INSERT INTO public.schema_migrations(version) VALUES ('052_referral_emote_promo')
ON CONFLICT (version) DO NOTHING;
