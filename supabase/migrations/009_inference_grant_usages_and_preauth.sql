-- Inference grant usage audit + V preauthorization lifecycle

CREATE TABLE IF NOT EXISTS inference_grant_usages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  grant_id UUID NOT NULL REFERENCES inference_token_grants(id),
  inference_usage_id UUID,
  request_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  tokens_used BIGINT NOT NULL CHECK (tokens_used > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inference_grant_usages_grant_created
  ON inference_grant_usages(grant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inference_preauthorizations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id TEXT NOT NULL REFERENCES wallet_accounts(account_id),
  user_id UUID REFERENCES auth.users(id),
  request_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  reserved_v NUMERIC(18,6) NOT NULL CHECK (reserved_v >= 0),
  settled_v NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (settled_v >= 0),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'refunded', 'voided')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (account_id, request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inference_preauth_user_request
  ON inference_preauthorizations(account_id, request_id);

INSERT INTO schema_migrations(version)
VALUES ('009_inference_grant_usages_and_preauth')
ON CONFLICT (version) DO NOTHING;
