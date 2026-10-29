-- Separate TV viewing rights from monthly wallet funding. Default-off application code.
CREATE TABLE public.tv_checkouts (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id),
  idempotency_key UUID NOT NULL,
  stripe_customer_id TEXT NOT NULL,
  stripe_price_id TEXT NOT NULL,
  stripe_session_id TEXT UNIQUE,
  checkout_url TEXT,
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','completed','expired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id,idempotency_key)
);
CREATE UNIQUE INDEX tv_one_pending_checkout ON public.tv_checkouts(user_id) WHERE state='pending';
CREATE TABLE public.tv_subscriptions (
  stripe_subscription_id TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id),
  checkout_id UUID NOT NULL UNIQUE REFERENCES public.tv_checkouts(id),
  stripe_customer_id TEXT NOT NULL,
  stripe_price_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','past_due','canceled','blocked','unpaid','paused')),
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
  last_event_created BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX tv_subscription_owner ON public.tv_subscriptions(user_id);
CREATE TABLE public.tv_paid_terms (
  invoice_id TEXT PRIMARY KEY,
  stripe_subscription_id TEXT NOT NULL REFERENCES public.tv_subscriptions(stripe_subscription_id),
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL CHECK (period_end > period_start),
  amount_cents INTEGER NOT NULL CHECK (amount_cents = 2000),
  charge_id TEXT,
  payment_intent_id TEXT,
  revoked BOOLEAN NOT NULL DEFAULT false,
  revoke_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE public.tv_invoice_revocations (
  invoice_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL CHECK (reason IN ('refund','dispute')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE public.tv_playback_leases (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id),
  idempotency_key UUID NOT NULL,
  payload_hash TEXT NOT NULL,
  episode_id TEXT NOT NULL,
  revision TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('payg','monthly')),
  hourly_v NUMERIC(24,6) NOT NULL CHECK (hourly_v > 0),
  reserved_v NUMERIC(24,6) NOT NULL CHECK (reserved_v >= 0),
  charged_v NUMERIC(24,6) NOT NULL DEFAULT 0 CHECK (charged_v >= 0 AND charged_v <= reserved_v),
  expires_at TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','settled','released')),
  receipt_id TEXT UNIQUE,
  delivered_seconds INTEGER CHECK (delivered_seconds BETWEEN 0 AND 60),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id,idempotency_key),
  CHECK (mode <> 'monthly' OR (reserved_v=0 AND charged_v=0))
);
CREATE INDEX tv_expired_reservations ON public.tv_playback_leases(expires_at) WHERE state='reserved';
ALTER TABLE public.tv_checkouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tv_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tv_paid_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tv_invoice_revocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tv_playback_leases ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.tv_checkouts, public.tv_subscriptions, public.tv_paid_terms, public.tv_invoice_revocations, public.tv_playback_leases FROM anon, authenticated;
INSERT INTO schema_migrations(version) VALUES ('051_tv_billing') ON CONFLICT (version) DO NOTHING;
