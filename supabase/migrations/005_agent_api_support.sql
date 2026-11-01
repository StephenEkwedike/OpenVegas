-- Agent runtime hardening + session envelope tracking

ALTER TABLE agent_tokens
  ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES auth.users(id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_tokens_token_hash ON agent_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent_active
  ON agent_tokens(agent_account_id, expires_at)
  WHERE revoked_at IS NULL;

ALTER TABLE agent_sessions
  ALTER COLUMN envelope_v TYPE NUMERIC(18,6),
  ALTER COLUMN spent_v TYPE NUMERIC(18,6);

ALTER TABLE agent_sessions
  ADD COLUMN IF NOT EXISTS reserved_v NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (reserved_v >= 0),
  ADD COLUMN IF NOT EXISTS refunded_v NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (refunded_v >= 0),
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_agent_budget'
  ) THEN
    ALTER TABLE agent_sessions
      ADD CONSTRAINT ck_agent_budget
      CHECK (spent_v >= 0 AND spent_v + reserved_v <= envelope_v);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent_status_expires
  ON agent_sessions(agent_account_id, status, expires_at);

CREATE TABLE IF NOT EXISTS agent_session_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES agent_sessions(id),
  event_type TEXT NOT NULL CHECK (event_type IN ('reserve', 'settle', 'refund', 'expire', 'close')),
  amount_v NUMERIC(18,6) NOT NULL CHECK (amount_v >= 0),
  request_id TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_session_events_session_created
  ON agent_session_events(session_id, created_at DESC);
