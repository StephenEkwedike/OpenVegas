-- Stripe billing support: fiat topups, webhook dedupe, and org subscription projection.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'fiat_topup_status') THEN
    CREATE TYPE fiat_topup_status AS ENUM (
      'created',
      'checkout_created',
      'paid',
      'failed',
      'reversed'
    );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS fiat_topups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  amount_usd NUMERIC(18,6) NOT NULL CHECK (amount_usd > 0),
  v_credit NUMERIC(18,6) NOT NULL CHECK (v_credit > 0),
  currency TEXT NOT NULL DEFAULT 'usd',
  status fiat_topup_status NOT NULL DEFAULT 'created',
  idempotency_key TEXT NOT NULL,
  idempotency_payload_hash TEXT NOT NULL,
  stripe_customer_id TEXT,
  stripe_checkout_session_id TEXT UNIQUE,
  stripe_checkout_url TEXT,
  stripe_payment_intent_id TEXT UNIQUE,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_fiat_topups_user_created
  ON fiat_topups(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE org_sponsorships
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT UNIQUE,
  ADD COLUMN IF NOT EXISTS stripe_price_id TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_status TEXT,
  ADD COLUMN IF NOT EXISTS has_active_subscription BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_sponsorships_stripe_customer_id
ON org_sponsorships(stripe_customer_id)
WHERE stripe_customer_id IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_org_sponsorships_stripe_status'
  ) THEN
    ALTER TABLE org_sponsorships
      ADD CONSTRAINT ck_org_sponsorships_stripe_status
      CHECK (
        stripe_subscription_status IS NULL OR stripe_subscription_status IN (
          'inactive',
          'incomplete',
          'incomplete_expired',
          'trialing',
          'active',
          'past_due',
          'canceled',
          'unpaid',
          'paused'
        )
      );
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_org_budget_ledger_source_ref
ON org_budget_ledger(source, reference_id)
WHERE reference_id IS NOT NULL;

ALTER TABLE fiat_topups ENABLE ROW LEVEL SECURITY;
ALTER TABLE stripe_webhook_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'fiat_topups' AND policyname = 'fiat_topups_select_self'
  ) THEN
    CREATE POLICY fiat_topups_select_self ON fiat_topups
      FOR SELECT USING (user_id = auth.uid());
  END IF;
END $$;

REVOKE ALL ON TABLE stripe_webhook_events FROM anon, authenticated;

INSERT INTO wallet_accounts (account_id, balance)
VALUES ('fiat_reserve', 0)
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations(version)
VALUES ('013_stripe_billing')
ON CONFLICT (version) DO NOTHING;

