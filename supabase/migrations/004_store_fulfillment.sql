-- Store purchases + inference token grants

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'store_order_status') THEN
    CREATE TYPE store_order_status AS ENUM ('created', 'settled', 'fulfilled', 'failed', 'reversed');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS store_orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  item_id TEXT NOT NULL,
  cost_v NUMERIC(18,6) NOT NULL CHECK (cost_v >= 0),
  status store_order_status NOT NULL DEFAULT 'created',
  idempotency_key TEXT NOT NULL,
  idempotency_payload_hash TEXT NOT NULL,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_store_orders_user_created
  ON store_orders (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inference_token_grants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  source_order_id UUID NOT NULL REFERENCES store_orders(id),
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  tokens_total BIGINT NOT NULL CHECK (tokens_total >= 0),
  tokens_remaining BIGINT NOT NULL CHECK (tokens_remaining >= 0),
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_order_id, provider, model_id),
  CHECK (tokens_remaining <= tokens_total)
);

CREATE INDEX IF NOT EXISTS idx_grants_user_provider_model_created
  ON inference_token_grants (user_id, provider, model_id, created_at ASC);
