-- Human casino sessions/rounds + scoped idempotency replay persistence

CREATE TABLE IF NOT EXISTS human_casino_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  max_loss_v NUMERIC(18,6) NOT NULL CHECK (max_loss_v >= 0),
  max_rounds INT NOT NULL DEFAULT 100 CHECK (max_rounds > 0),
  rounds_played INT NOT NULL DEFAULT 0 CHECK (rounds_played >= 0),
  net_pnl_v NUMERIC(18,6) NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'loss_capped', 'round_capped', 'closed')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_human_casino_sessions_user_status_expires
  ON human_casino_sessions(user_id, status, expires_at);

CREATE TABLE IF NOT EXISTS human_casino_rounds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES human_casino_sessions(id),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  game_code TEXT NOT NULL,
  wager_v NUMERIC(18,6) NOT NULL CHECK (wager_v > 0),
  state_json JSONB NOT NULL DEFAULT '{}',
  rng_commit TEXT NOT NULL,
  rng_reveal TEXT,
  client_seed TEXT NOT NULL,
  nonce INT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('created', 'awaiting_action', 'resolvable', 'resolved', 'expired', 'canceled')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_human_casino_rounds_session_status_started
  ON human_casino_rounds(session_id, status, started_at DESC);

CREATE TABLE IF NOT EXISTS human_casino_moves (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  round_id UUID NOT NULL REFERENCES human_casino_rounds(id),
  move_index INT NOT NULL,
  action TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (round_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS human_casino_payouts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  round_id UUID NOT NULL REFERENCES human_casino_rounds(id),
  wager_v NUMERIC(18,6) NOT NULL CHECK (wager_v >= 0),
  payout_v NUMERIC(18,6) NOT NULL CHECK (payout_v >= 0),
  net_v NUMERIC(18,6) NOT NULL,
  ledger_ref TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (round_id)
);

CREATE TABLE IF NOT EXISTS human_casino_verifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  round_id UUID NOT NULL REFERENCES human_casino_rounds(id),
  commit_hash TEXT NOT NULL,
  reveal_seed TEXT NOT NULL,
  client_seed TEXT NOT NULL,
  nonce INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (round_id)
);

CREATE TABLE IF NOT EXISTS human_casino_idempotency (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id),
  scope TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  response_status INT,
  response_body TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, scope, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_human_casino_idem_scope_created
  ON human_casino_idempotency(user_id, scope, created_at DESC);

ALTER TABLE human_casino_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_casino_rounds ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_casino_moves ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_casino_payouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_casino_verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_casino_idempotency ENABLE ROW LEVEL SECURITY;

INSERT INTO schema_migrations(version)
VALUES ('016_human_casino')
ON CONFLICT (version) DO NOTHING;
