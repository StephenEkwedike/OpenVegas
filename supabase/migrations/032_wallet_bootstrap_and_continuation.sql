-- Wallet bootstrap starter grant + continuation credit accounting.

CREATE TABLE IF NOT EXISTS user_starter_grants (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  granted_amount_v NUMERIC(18,6) NOT NULL CHECK (granted_amount_v > 0),
  grant_version TEXT NOT NULL DEFAULT 'v1',
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE user_starter_grants ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE user_starter_grants FROM anon, authenticated;

CREATE TABLE IF NOT EXISTS user_continuation_credit (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  principal_v NUMERIC(18,6) NOT NULL CHECK (principal_v > 0),
  outstanding_v NUMERIC(18,6) NOT NULL CHECK (outstanding_v >= 0),
  status TEXT NOT NULL CHECK (status IN ('active','repaid','cancelled')),
  cooldown_until TIMESTAMPTZ,
  issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  repaid_at TIMESTAMPTZ,
  CHECK (
    (status = 'active' AND outstanding_v > 0 AND repaid_at IS NULL) OR
    (status = 'repaid' AND outstanding_v = 0 AND repaid_at IS NOT NULL) OR
    (status = 'cancelled' AND outstanding_v = 0 AND repaid_at IS NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_continuation_active
ON user_continuation_credit(user_id)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_user_continuation_user_issued
ON user_continuation_credit(user_id, issued_at DESC);

ALTER TABLE user_continuation_credit ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE user_continuation_credit FROM anon, authenticated;

CREATE TABLE IF NOT EXISTS continuation_claim_idempotency (
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
  payload_hash TEXT NOT NULL,
  response_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, idempotency_key)
);

ALTER TABLE continuation_claim_idempotency ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE continuation_claim_idempotency FROM anon, authenticated;

CREATE TABLE IF NOT EXISTS continuation_accounting_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  continuation_id UUID NOT NULL REFERENCES user_continuation_credit(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('principal_repaid','principal_written_off')),
  amount_v NUMERIC(18,6) NOT NULL CHECK (amount_v > 0),
  reason TEXT,
  actor TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_continuation_accounting_events_user_created
ON continuation_accounting_events(user_id, created_at DESC);

ALTER TABLE continuation_accounting_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE continuation_accounting_events FROM anon, authenticated;

INSERT INTO schema_migrations(version)
VALUES ('032_wallet_bootstrap_and_continuation')
ON CONFLICT (version) DO NOTHING;
