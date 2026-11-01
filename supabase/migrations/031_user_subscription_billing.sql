-- User-level Stripe subscriptions for automatic monthly wallet top-ups.

CREATE TABLE IF NOT EXISTS user_subscriptions (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT UNIQUE,
  stripe_price_id TEXT,
  stripe_subscription_status TEXT NOT NULL DEFAULT 'inactive',
  has_active_subscription BOOLEAN NOT NULL DEFAULT FALSE,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
  current_period_end TIMESTAMPTZ,
  monthly_amount_usd NUMERIC(18,6),
  latest_invoice_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_subscriptions_stripe_customer_id
ON user_subscriptions(stripe_customer_id)
WHERE stripe_customer_id IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_user_subscriptions_status'
  ) THEN
    ALTER TABLE user_subscriptions
      ADD CONSTRAINT ck_user_subscriptions_status
      CHECK (
        stripe_subscription_status IN (
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

ALTER TABLE user_subscriptions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'user_subscriptions' AND policyname = 'user_subscriptions_select_self'
  ) THEN
    CREATE POLICY user_subscriptions_select_self ON user_subscriptions
      FOR SELECT USING (user_id = auth.uid());
  END IF;
END $$;

REVOKE ALL ON TABLE user_subscriptions FROM anon, authenticated;

INSERT INTO schema_migrations(version)
VALUES ('031_user_subscription_billing')
ON CONFLICT (version) DO NOTHING;

