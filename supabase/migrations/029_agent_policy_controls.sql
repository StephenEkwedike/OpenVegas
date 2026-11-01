-- Agent envelope/policy control fields for org-level admin governance.

ALTER TABLE org_policies
  ADD COLUMN IF NOT EXISTS agent_default_envelope_v NUMERIC(18,6) DEFAULT 25.000000,
  ADD COLUMN IF NOT EXISTS agent_max_envelope_v NUMERIC(18,6) DEFAULT 250.000000,
  ADD COLUMN IF NOT EXISTS agent_session_ttl_sec INT DEFAULT 1800,
  ADD COLUMN IF NOT EXISTS agent_infer_enabled BOOLEAN DEFAULT TRUE;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_org_policies_agent_envelope_bounds'
  ) THEN
    ALTER TABLE org_policies
      ADD CONSTRAINT ck_org_policies_agent_envelope_bounds
      CHECK (
        agent_default_envelope_v > 0
        AND agent_max_envelope_v > 0
        AND agent_default_envelope_v <= agent_max_envelope_v
      );
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_org_policies_agent_session_ttl_sec'
  ) THEN
    ALTER TABLE org_policies
      ADD CONSTRAINT ck_org_policies_agent_session_ttl_sec
      CHECK (agent_session_ttl_sec >= 60);
  END IF;
END $$;

INSERT INTO schema_migrations(version)
VALUES ('029_agent_policy_controls')
ON CONFLICT (version) DO NOTHING;
