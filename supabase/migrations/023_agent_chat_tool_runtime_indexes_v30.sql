-- Agent chat tool runtime indexes (v30)

CREATE UNIQUE INDEX IF NOT EXISTS ux_run_event_seq
  ON agent_run_events(run_id, event_seq);

CREATE UNIQUE INDEX IF NOT EXISTS ux_one_started_tool_per_run
  ON agent_run_tool_calls(run_id)
  WHERE status='started';

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_status
  ON agent_run_tool_calls(run_id, status);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id_id
  ON agent_run_tool_calls(run_id, id);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_status_token
  ON agent_run_tool_calls(run_id, status, execution_token);

CREATE INDEX IF NOT EXISTS idx_mutation_leases_expires
  ON agent_run_mutation_leases(expires_at);

CREATE INDEX IF NOT EXISTS idx_replays_scope_key
  ON agent_mutation_replays(run_id, actor_id, actor_role_class, scope, idempotency_key);

INSERT INTO schema_migrations(version)
VALUES ('023_agent_chat_tool_runtime_indexes_v30')
ON CONFLICT (version) DO NOTHING;
