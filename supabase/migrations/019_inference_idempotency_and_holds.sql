-- Inference idempotency + hold lifecycle hardening.

CREATE TABLE IF NOT EXISTS org_runtime_policies (
  org_id UUID PRIMARY KEY,
  byok_allowed BOOLEAN NOT NULL DEFAULT FALSE,
  wrapper_required BOOLEAN NOT NULL DEFAULT TRUE,
  context_persistence_allowed BOOLEAN NOT NULL DEFAULT TRUE,
  low_balance_gambling_prompt_allowed BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS context_retention_policies (
  policy_name TEXT PRIMARY KEY,
  ttl_hours INT NOT NULL CHECK (ttl_hours > 0),
  persist_by_default BOOLEAN NOT NULL,
  max_messages INT NOT NULL CHECK (max_messages > 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO context_retention_policies(policy_name, ttl_hours, persist_by_default, max_messages)
VALUES ('default', 72, TRUE, 200)
ON CONFLICT (policy_name) DO NOTHING;

-- Canonical one-hold-per-request invariant for active inference holds.
CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_preauth_request_id
  ON inference_preauthorizations (request_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_preauth_active_request
  ON inference_preauthorizations (request_id)
  WHERE status = 'reserved';

CREATE INDEX IF NOT EXISTS idx_inference_preauth_user_status_updated
  ON inference_preauthorizations (user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_inference_requests_user_created
  ON inference_requests (user_id, created_at DESC);

INSERT INTO schema_migrations(version)
VALUES ('019_inference_idempotency_and_holds')
ON CONFLICT (version) DO NOTHING;
