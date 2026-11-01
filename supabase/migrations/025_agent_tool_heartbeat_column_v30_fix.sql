-- v30 fix: required heartbeat column used by tool callback CAS paths.

ALTER TABLE agent_run_tool_calls
  ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;

INSERT INTO schema_migrations(version)
VALUES ('025_agent_tool_heartbeat_column_v30_fix')
ON CONFLICT (version) DO NOTHING;
