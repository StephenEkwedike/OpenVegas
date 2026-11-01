-- Tool status domain hardening for cancelled state and terminal checks (v30 follow-up).

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_run_tool_calls_status_check') THEN
    ALTER TABLE agent_run_tool_calls DROP CONSTRAINT agent_run_tool_calls_status_check;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_status_domain') THEN
    ALTER TABLE agent_run_tool_calls DROP CONSTRAINT ck_tool_status_domain;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_terminal_has_finished') THEN
    ALTER TABLE agent_run_tool_calls DROP CONSTRAINT ck_tool_terminal_has_finished;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_terminal_requires_finished') THEN
    ALTER TABLE agent_run_tool_calls DROP CONSTRAINT ck_tool_terminal_requires_finished;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_terminal_response_payload') THEN
    ALTER TABLE agent_run_tool_calls DROP CONSTRAINT ck_tool_terminal_response_payload;
  END IF;

  ALTER TABLE agent_run_tool_calls
    ADD CONSTRAINT ck_tool_status_domain
    CHECK (status IN ('proposed','started','succeeded','failed','timed_out','blocked','cancelled'));

  ALTER TABLE agent_run_tool_calls
    ADD CONSTRAINT ck_tool_terminal_requires_finished
    CHECK (
      status NOT IN ('succeeded','failed','timed_out','blocked','cancelled')
      OR finished_at IS NOT NULL
    );

  ALTER TABLE agent_run_tool_calls
    ADD CONSTRAINT ck_tool_terminal_response_payload
    CHECK (
      status NOT IN ('succeeded','failed','timed_out','blocked')
      OR (
        terminal_response_status IS NOT NULL
        AND terminal_response_content_type IS NOT NULL
        AND terminal_response_content_type = 'application/json'
        AND terminal_response_body_text IS NOT NULL
      )
    ) NOT VALID; -- Preserve pre-022 terminal history; enforce every new/updated row.
END $$;

INSERT INTO schema_migrations(version)
VALUES ('026_agent_tool_cancelled_status_v30')
ON CONFLICT (version) DO NOTHING;
