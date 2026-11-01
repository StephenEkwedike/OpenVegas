-- Application-level migration journal for startup schema checks

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations(version) VALUES
  ('001_initial_schema'),
  ('002_enterprise_agent_casino'),
  ('003_inference_usage_accounts'),
  ('004_store_fulfillment'),
  ('005_agent_api_support'),
  ('006_mint_policy_audit'),
  ('007_casino_session_caps'),
  ('008_schema_migrations_and_readiness')
ON CONFLICT (version) DO NOTHING;
