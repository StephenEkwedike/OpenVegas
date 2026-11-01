-- Backfill missing tool result payload column for runtime terminalization path.

ALTER TABLE agent_run_tool_calls
  ADD COLUMN IF NOT EXISTS result_payload JSONB;

UPDATE agent_run_tool_calls
SET result_payload='{}'::jsonb
WHERE result_payload IS NULL
  AND status IN ('succeeded','failed','timed_out','blocked')
  -- Avoid updating legacy rows that cannot meet the new terminal constraints.
  AND terminal_response_status IS NOT NULL
  AND terminal_response_content_type = 'application/json'
  AND terminal_response_body_text IS NOT NULL;

INSERT INTO schema_migrations(version)
VALUES ('027_agent_tool_result_payload_column_v30_fix')
ON CONFLICT (version) DO NOTHING;
