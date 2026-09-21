-- Cash adjustments do NOT generate cash refunds or wallet ledger entries.
-- Coordinate store buy with lock_user_adjustments BEFORE its request/SKU locks.
-- Apply only on the authoritative backend; no client writes or inferred FIFO lots.
ALTER TABLE public.cosmetic_entitlements
    DROP CONSTRAINT cosmetic_entitlements_status_check;
ALTER TABLE public.cosmetic_entitlements
    DROP CONSTRAINT cosmetic_entitlements_check1;
ALTER TABLE public.cosmetic_entitlements ADD CONSTRAINT cosmetic_entitlements_status_check
    CHECK (status IN ('active', 'suspended', 'revoked'));
ALTER TABLE public.cosmetic_entitlements ADD CONSTRAINT cosmetic_entitlements_adjustment_state_check
    CHECK ((status IN ('active','suspended') AND revoked_at IS NULL AND revocation_reason IS NULL)
        OR (status='revoked' AND revoked_at IS NOT NULL AND nullif(btrim(revocation_reason),'') IS NOT NULL));
-- Existing immutable identity and irreversible-revocation trigger remains intact.

CREATE UNIQUE INDEX fiat_topups_identity_user ON public.fiat_topups(id,user_id);
CREATE TABLE public.stripe_emote_funding (
    order_id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    item_id TEXT NOT NULL,
    topup_id UUID NOT NULL,
    credit_entry_id UUID NOT NULL REFERENCES public.ledger_entries(id),
    debit_entry_id UUID NOT NULL UNIQUE REFERENCES public.ledger_entries(id),
    -- A proven LOWER BOUND, not a FIFO allocation or a claim of exact provenance.
    funded_v NUMERIC(18,6) NOT NULL CHECK (funded_v>0),
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(order_id,user_id,item_id) REFERENCES public.store_orders(id,user_id,item_id),
    FOREIGN KEY(topup_id,user_id) REFERENCES public.fiat_topups(id,user_id)
);
CREATE INDEX stripe_emote_funding_topup ON public.stripe_emote_funding(topup_id);

CREATE TABLE public.stripe_emote_adjustments (
    kind TEXT NOT NULL CHECK (kind IN ('refund','dispute','charge_refund')),
    object_id TEXT NOT NULL CHECK (char_length(object_id) BETWEEN 1 AND 255),
    topup_id UUID NOT NULL,
    user_id UUID NOT NULL,
    charge_id TEXT NOT NULL,
    livemode BOOLEAN NOT NULL,
    currency TEXT NOT NULL CHECK(currency='usd'),
    amount_minor BIGINT NOT NULL CHECK(amount_minor>=0),
    state TEXT NOT NULL,
    event_created BIGINT NOT NULL CHECK(event_created>0),
    event_id TEXT NOT NULL REFERENCES public.stripe_webhook_events(event_id),
    needs_review BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(kind,object_id),
    FOREIGN KEY(topup_id,user_id) REFERENCES public.fiat_topups(id,user_id)
);
CREATE INDEX stripe_emote_adjustments_topup ON public.stripe_emote_adjustments(topup_id);

CREATE TABLE public.stripe_emote_adjustment_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES public.stripe_webhook_events(event_id),
    topup_id UUID NOT NULL REFERENCES public.fiat_topups(id),
    outcome TEXT NOT NULL,
    facts JSONB NOT NULL CHECK(jsonb_typeof(facts)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Only suspensions owned by this policy may be automatically restored.
CREATE TABLE public.stripe_emote_suspensions (
    entitlement_id UUID PRIMARY KEY REFERENCES public.cosmetic_entitlements(id),
    event_id TEXT NOT NULL REFERENCES public.stripe_webhook_events(event_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION public.stripe_emote_history_immutable() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN RAISE EXCEPTION 'Stripe emote evidence/audit is append-only'; END;
$$;
CREATE TRIGGER stripe_emote_funding_immutable BEFORE UPDATE OR DELETE
    ON public.stripe_emote_funding FOR EACH ROW EXECUTE FUNCTION public.stripe_emote_history_immutable();
CREATE TRIGGER stripe_emote_audit_immutable BEFORE UPDATE OR DELETE
    ON public.stripe_emote_adjustment_audit FOR EACH ROW EXECUTE FUNCTION public.stripe_emote_history_immutable();

ALTER TABLE public.stripe_emote_funding ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stripe_emote_adjustments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stripe_emote_adjustment_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stripe_emote_suspensions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.stripe_emote_funding,public.stripe_emote_adjustments,
    public.stripe_emote_adjustment_audit,public.stripe_emote_suspensions FROM PUBLIC,anon,authenticated;
REVOKE ALL ON SEQUENCE public.stripe_emote_adjustment_audit_id_seq FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.stripe_emote_history_immutable() FROM PUBLIC,anon,authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        REVOKE ALL ON public.stripe_emote_funding,public.stripe_emote_adjustments,
            public.stripe_emote_adjustment_audit,public.stripe_emote_suspensions FROM service_role;
        GRANT SELECT,INSERT ON public.stripe_emote_funding,public.stripe_emote_adjustment_audit TO service_role;
        GRANT SELECT,INSERT,UPDATE ON public.stripe_emote_adjustments TO service_role;
        GRANT SELECT,INSERT,DELETE ON public.stripe_emote_suspensions TO service_role;
        GRANT USAGE,SELECT ON SEQUENCE public.stripe_emote_adjustment_audit_id_seq TO service_role;
    END IF;
END $$;
