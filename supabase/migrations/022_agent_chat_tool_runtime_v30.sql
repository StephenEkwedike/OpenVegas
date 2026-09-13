-- Agent chat tool runtime hardening (v30)

ALTER TABLE agent_runs
  ADD COLUMN IF NOT EXISTS workspace_root TEXT,
  ADD COLUMN IF NOT EXISTS workspace_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS git_root TEXT,
  ADD COLUMN IF NOT EXISTS runtime_session_id UUID;

ALTER TABLE agent_run_tool_calls
  ADD COLUMN IF NOT EXISTS execution_token TEXT,
  ADD COLUMN IF NOT EXISTS request_payload_json JSONB,
  ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS result_submission_hash TEXT,
  ADD COLUMN IF NOT EXISTS requested_command_text TEXT,
  ADD COLUMN IF NOT EXISTS effective_command_text TEXT,
  ADD COLUMN IF NOT EXISTS shell_wrapper TEXT,
  ADD COLUMN IF NOT EXISTS execution_cwd TEXT,
  ADD COLUMN IF NOT EXISTS stdout TEXT,
  ADD COLUMN IF NOT EXISTS stderr TEXT,
  ADD COLUMN IF NOT EXISTS terminal_response_status INT,
  ADD COLUMN IF NOT EXISTS terminal_response_content_type TEXT,
  ADD COLUMN IF NOT EXISTS terminal_response_body_text TEXT,
  ADD COLUMN IF NOT EXISTS terminal_response_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS terminal_response_hash TEXT;

-- Presence / format hardening
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_agent_runs_runtime_session_present') THEN
    ALTER TABLE agent_runs
      ADD CONSTRAINT ck_agent_runs_runtime_session_present
      CHECK (runtime_session_id IS NULL OR runtime_session_id::text <> '');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_agent_runs_workspace_fingerprint_format') THEN
    ALTER TABLE agent_runs
      ADD CONSTRAINT ck_agent_runs_workspace_fingerprint_format
      CHECK (
        workspace_fingerprint IS NULL
        OR workspace_fingerprint ~ '^sha256:[0-9a-f]{64}$'
      );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_execution_token_present') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_execution_token_present
      CHECK (execution_token IS NULL OR execution_token <> '');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_payload_hash_present') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_payload_hash_present
      CHECK (payload_hash IS NULL OR payload_hash <> '');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_payload_hash_format') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_payload_hash_format
      CHECK (payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_result_submission_hash_present') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_result_submission_hash_present
      CHECK (result_submission_hash IS NULL OR result_submission_hash <> '');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_result_submission_hash_format') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_result_submission_hash_format
      CHECK (result_submission_hash IS NULL OR result_submission_hash ~ '^[0-9a-f]{64}$');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_started_requires_claim') THEN
    -- Pre-022 rows have no claim metadata. Preserve their history without inventing it;
    -- NOT VALID still enforces the constraint on every new or updated row.
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_started_requires_claim
      CHECK (status <> 'started' OR (claimed_at IS NOT NULL AND started_at IS NOT NULL)) NOT VALID;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_terminal_requires_finished') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_terminal_requires_finished
      CHECK (status NOT IN ('succeeded','failed','timed_out','blocked') OR finished_at IS NOT NULL);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_proposed_unclaimed') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_proposed_unclaimed
      CHECK (status <> 'proposed' OR (claimed_at IS NULL AND started_at IS NULL));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_terminal_response_payload') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_terminal_response_payload
      CHECK (
        status NOT IN ('succeeded','failed','timed_out','blocked')
        OR
        (
          terminal_response_status IS NOT NULL
          AND terminal_response_content_type IS NOT NULL
          AND terminal_response_content_type = 'application/json'
          AND terminal_response_body_text IS NOT NULL
        )
      ) NOT VALID;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tool_terminal_response_hash') THEN
    ALTER TABLE agent_run_tool_calls
      ADD CONSTRAINT ck_tool_terminal_response_hash
      CHECK (
        terminal_response_truncated = FALSE
        OR
        (terminal_response_truncated = TRUE AND terminal_response_hash IS NOT NULL)
      );
  END IF;
END $$;

INSERT INTO schema_migrations(version)
VALUES ('022_agent_chat_tool_runtime_v30')
ON CONFLICT (version) DO NOTHING;
