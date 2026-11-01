-- Runtime payloads are served only by authenticated backend routes, not PostgREST.
-- Keep the database-owner/service-role backend path; deny browser roles directly.
DO $$
DECLARE
  table_name TEXT;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'user_runtime_prefs', 'provider_credentials', 'inference_requests',
    'wrapper_reward_events', 'wallet_history_projection',
    'org_runtime_policies', 'context_retention_policies',
    'provider_threads', 'provider_thread_messages',
    'agent_runs', 'agent_run_events', 'agent_run_tool_calls',
    'agent_tool_approvals', 'agent_run_holds', 'agent_run_mutation_leases',
    'agent_mutation_replays', 'run_status_projection', 'agent_chat_turns',
    'chat_file_uploads', 'schema_migrations'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon, authenticated', table_name);
  END LOOP;
END $$;

-- CHECK treats NULL as passing. Require a non-NULL content type for new terminal
-- records, while preserving old incomplete history for separate reconciliation.
ALTER TABLE agent_run_tool_calls
  DROP CONSTRAINT IF EXISTS ck_tool_terminal_response_payload;
ALTER TABLE agent_run_tool_calls
  ADD CONSTRAINT ck_tool_terminal_response_payload CHECK (
    status NOT IN ('succeeded','failed','timed_out','blocked')
    OR (
      terminal_response_status IS NOT NULL
      AND terminal_response_content_type IS NOT NULL
      AND terminal_response_content_type = 'application/json'
      AND terminal_response_body_text IS NOT NULL
    )
  ) NOT VALID;

INSERT INTO schema_migrations(version)
VALUES ('038_private_runtime_rls')
ON CONFLICT (version) DO NOTHING;
