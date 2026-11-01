-- Optional transcript convenience table for agent chat turns (v30)

CREATE TABLE IF NOT EXISTS agent_chat_turns (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  turn_no BIGINT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  content_json JSONB NOT NULL,
  tool_call_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, turn_no)
);

INSERT INTO schema_migrations(version)
VALUES ('024_agent_chat_turns_optional_v30')
ON CONFLICT (version) DO NOTHING;
