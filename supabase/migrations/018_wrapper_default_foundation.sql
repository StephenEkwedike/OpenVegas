-- Wrapper-default foundation: canonical request identity, projection, and credential registry.

CREATE TABLE IF NOT EXISTS user_runtime_prefs (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  llm_mode TEXT NOT NULL DEFAULT 'wrapper' CHECK (llm_mode IN ('wrapper', 'byok')),
  conversation_mode TEXT NOT NULL DEFAULT 'persistent' CHECK (conversation_mode IN ('persistent', 'ephemeral')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS provider_credentials (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider TEXT NOT NULL,
  env TEXT NOT NULL,
  key_alias TEXT NOT NULL,
  key_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'unavailable', 'degraded', 'rotating', 'disabled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_provider_credentials_provider_env_status
  ON provider_credentials (provider, env, status, created_at DESC);

CREATE TABLE IF NOT EXISTS inference_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES auth.users(id),
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('processing', 'succeeded', 'failed')),
  inference_source TEXT NOT NULL CHECK (inference_source IN ('wrapper', 'byok')),
  wallet_funding_source TEXT NOT NULL CHECK (wallet_funding_source IN ('fiat_topup', 'promo', 'reward', 'demo', 'external')),
  final_charge_v NUMERIC(24,6),
  final_provider_cost_usd NUMERIC(24,6),
  response_status INT,
  response_body_text TEXT,
  provider_request_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status <> 'succeeded' OR response_body_text IS NOT NULL),
  UNIQUE (user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_inference_requests_status_updated
  ON inference_requests (status, updated_at DESC);

ALTER TABLE inference_usage
  ADD COLUMN IF NOT EXISTS request_id UUID REFERENCES inference_requests(id),
  ADD COLUMN IF NOT EXISTS inference_source TEXT,
  ADD COLUMN IF NOT EXISTS wallet_funding_source TEXT,
  ADD COLUMN IF NOT EXISTS billed_v_input_per_1m NUMERIC(24,6),
  ADD COLUMN IF NOT EXISTS billed_v_output_per_1m NUMERIC(24,6),
  ADD COLUMN IF NOT EXISTS billed_cost_input_per_1m NUMERIC(24,6),
  ADD COLUMN IF NOT EXISTS billed_cost_output_per_1m NUMERIC(24,6);

CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_usage_request_id
  ON inference_usage (request_id)
  WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS wrapper_reward_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  inference_usage_id UUID NOT NULL REFERENCES inference_usage(id),
  inference_source TEXT NOT NULL CHECK (inference_source IN ('wrapper', 'byok')),
  wallet_funding_source TEXT NOT NULL CHECK (wallet_funding_source IN ('fiat_topup', 'promo', 'reward', 'demo', 'external')),
  reward_v NUMERIC(24,6) NOT NULL CHECK (reward_v >= 0),
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (inference_usage_id)
);

CREATE TABLE IF NOT EXISTS wallet_history_projection (
  event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  request_id UUID REFERENCES inference_requests(id),
  event_type TEXT NOT NULL,
  display_amount_v NUMERIC(24,6) NOT NULL,
  display_status TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_wallet_history_projection_user_occurred
  ON wallet_history_projection (user_id, occurred_at DESC);

ALTER TABLE fiat_topups
  ADD COLUMN IF NOT EXISTS wallet_funding_source TEXT NOT NULL DEFAULT 'fiat_topup';

INSERT INTO schema_migrations(version)
VALUES ('018_wrapper_default_foundation')
ON CONFLICT (version) DO NOTHING;

