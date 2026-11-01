-- Horse quote pricing (odds-locked) + replay-safe idempotency

CREATE TABLE IF NOT EXISTS horse_quotes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  bet_type TEXT NOT NULL CHECK (bet_type IN ('win', 'place', 'show')),
  budget_v NUMERIC(24,6) NOT NULL CHECK (budget_v >= 0),
  horses_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  board_hash TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  consumed_game_id UUID REFERENCES game_history(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (consumed_at IS NULL AND consumed_game_id IS NULL)
    OR (consumed_at IS NOT NULL AND consumed_game_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_horse_quotes_user_active_expires
  ON horse_quotes (user_id, expires_at DESC)
  WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_horse_quotes_user_created
  ON horse_quotes (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS horse_quote_idempotency (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  scope TEXT NOT NULL CHECK (scope IN ('quote_create', 'quote_play')),
  resource_id UUID REFERENCES horse_quotes(id),
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  response_status INT,
  response_body_text TEXT,
  response_content_type TEXT NOT NULL DEFAULT 'application/json',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, scope, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_horse_quote_idem_scope_created
  ON horse_quote_idempotency (user_id, scope, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_horse_quote_idem_resource_payload
  ON horse_quote_idempotency (user_id, scope, resource_id, payload_hash, created_at DESC);

ALTER TABLE horse_quotes ENABLE ROW LEVEL SECURITY;
ALTER TABLE horse_quote_idempotency ENABLE ROW LEVEL SECURITY;

INSERT INTO schema_migrations(version)
VALUES ('017_horse_quote_pricing')
ON CONFLICT (version) DO NOTHING;
