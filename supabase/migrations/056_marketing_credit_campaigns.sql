-- Operator-created, default-off campaigns. No seed funding or active campaigns.
CREATE TABLE public.marketing_credit_campaigns (
    id UUID PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT false,
    approved_by TEXT NOT NULL CHECK (length(btrim(approved_by)) > 0),
    approval_reference TEXT NOT NULL CHECK (length(btrim(approval_reference)) > 0),
    budget_v NUMERIC(18,6) NOT NULL CHECK (budget_v >= 50),
    reserved_v NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (reserved_v >= 0 AND reserved_v <= budget_v),
    ends_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE public.marketing_credit_codes (
    id UUID PRIMARY KEY,
    campaign_id UUID NOT NULL REFERENCES public.marketing_credit_campaigns(id),
    event_hash TEXT NOT NULL,
    recipient_hash TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (campaign_id, event_hash),
    UNIQUE (campaign_id, recipient_hash),
    CHECK (expires_at > issued_at AND expires_at <= issued_at + interval '7 days')
);
CREATE TABLE public.marketing_credit_redemptions (
    code_id UUID PRIMARY KEY REFERENCES public.marketing_credit_codes(id),
    campaign_id UUID NOT NULL REFERENCES public.marketing_credit_campaigns(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    identity_hash TEXT NOT NULL,
    amount_v NUMERIC(18,6) NOT NULL DEFAULT 50 CHECK (amount_v = 50),
    redeemed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, user_id),
    UNIQUE (campaign_id, identity_hash)
);
ALTER TABLE public.marketing_credit_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.marketing_credit_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.marketing_credit_redemptions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.marketing_credit_campaigns, public.marketing_credit_codes,
    public.marketing_credit_redemptions FROM PUBLIC, anon, authenticated;
INSERT INTO public.schema_migrations(version) VALUES ('056_marketing_credit_campaigns')
ON CONFLICT (version) DO NOTHING;
