-- Casino session runtime caps and TTL fields

ALTER TABLE casino_sessions
  ALTER COLUMN max_loss_v TYPE NUMERIC(18,6),
  ALTER COLUMN net_pnl_v TYPE NUMERIC(18,6);

ALTER TABLE casino_sessions
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS max_exposure_v NUMERIC(18,6) NOT NULL DEFAULT 100.000000 CHECK (max_exposure_v >= 0),
  ADD COLUMN IF NOT EXISTS unresolved_rounds INT NOT NULL DEFAULT 0 CHECK (unresolved_rounds >= 0);

CREATE INDEX IF NOT EXISTS idx_casino_sessions_agent_status_expires
  ON casino_sessions(agent_account_id, status, expires_at);
