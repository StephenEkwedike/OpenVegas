-- Separate agent purchases from personal top-ups. Only the backend may access these tables.
ALTER TABLE organizations ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE agent_funding_orders (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES organizations(id),
    agent_id UUID NOT NULL REFERENCES agent_accounts(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    amount_usd NUMERIC(12,2) NOT NULL CHECK (amount_usd BETWEEN 10 AND 500),
    credit_v NUMERIC(20,6) NOT NULL CHECK (credit_v > 0),
    reversed_v NUMERIC(20,6) NOT NULL DEFAULT 0,
    debt_v NUMERIC(20,6) NOT NULL DEFAULT 0,
    livemode BOOLEAN NOT NULL,
    customer_id TEXT NOT NULL,
    payment_method_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('checkout','auto')),
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN
      ('created','checkout','pending','paid','failed','requires_action','unknown','expired','review')),
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    checkout_id TEXT UNIQUE,
    payment_intent_id TEXT UNIQUE,
    checkout_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,idempotency_key)
);
CREATE INDEX agent_funding_agent_history ON agent_funding_orders(agent_id,created_at DESC);
CREATE TABLE agent_auto_topups (
    agent_id UUID PRIMARY KEY REFERENCES agent_accounts(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    threshold_v NUMERIC(20,6) NOT NULL CHECK (threshold_v > 0 AND threshold_v <= 1000000),
    amount_usd NUMERIC(12,2) NOT NULL CHECK (amount_usd BETWEEN 10 AND 500),
    monthly_cap_usd NUMERIC(12,2) NOT NULL CHECK (monthly_cap_usd BETWEEN amount_usd AND 2000),
    customer_id TEXT NOT NULL,
    payment_method_id TEXT NOT NULL,
    livemode BOOLEAN NOT NULL,
    consent_version TEXT NOT NULL,
    consent_at TIMESTAMPTZ NOT NULL,
    last_attempt_at TIMESTAMPTZ,
    pause_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE agent_payment_adjustments (
    object_id TEXT PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES agent_funding_orders(id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    event_created BIGINT NOT NULL,
    charge_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    livemode BOOLEAN NOT NULL,
    needs_review BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE agent_funding_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_auto_topups ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_payment_adjustments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent_funding_orders,agent_auto_topups,agent_payment_adjustments FROM PUBLIC,anon,authenticated;
INSERT INTO schema_migrations(version) VALUES ('058_agent_funding') ON CONFLICT DO NOTHING;
