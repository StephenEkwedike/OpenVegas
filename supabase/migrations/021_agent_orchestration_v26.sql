-- Agent orchestration runtime (v26): authoritative run state, replay, leases, and projection.

CREATE TABLE IF NOT EXISTS agent_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  state TEXT NOT NULL
    CHECK (state IN ('created','running','awaiting_approval','completed','failed','canceled','expired','interrupted')),
  version BIGINT NOT NULL DEFAULT 0,
  run_event_seq BIGINT NOT NULL DEFAULT 0,
  is_resumable BOOLEAN NOT NULL DEFAULT FALSE,
  state_reason_code TEXT
    CHECK (state_reason_code IN (
      'completed_success','completed_noop','failed_policy','failed_validation','failed_internal',
      'canceled_user','canceled_system','expired_timeout','expired_approval',
      'interrupted_worker_lost','interrupted_lease_expired','interrupted_reconciler','mutation_uncertain'
    )),
  state_entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_nonterminal_state TEXT
    CHECK (last_nonterminal_state IN ('created','running','awaiting_approval','interrupted')),
  cancel_requested_at TIMESTAMPTZ,
  cancel_disposition TEXT
    CHECK (cancel_disposition IN (
      'completed_before_cancel','canceled_by_user','canceled_by_system','cancel_rejected_terminal','cancel_superseded'
    )),
  expires_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_agent_runs_reason_required
    CHECK (
      (state IN ('completed','failed','canceled','expired','interrupted') AND state_reason_code IS NOT NULL)
      OR
      (state NOT IN ('completed','failed','canceled','expired','interrupted') AND state_reason_code IS NULL)
    ),
  CONSTRAINT ck_agent_runs_expired_not_resumable
    CHECK (state <> 'expired' OR is_resumable = FALSE),
  CONSTRAINT ck_agent_runs_interrupted_reason_domain
    CHECK (
      state <> 'interrupted'
      OR state_reason_code IN (
        'interrupted_worker_lost','interrupted_lease_expired','interrupted_reconciler','mutation_uncertain'
      )
    ),
  CONSTRAINT ck_agent_runs_version_nonnegative CHECK (version >= 0),
  CONSTRAINT ck_agent_runs_event_seq_nonnegative CHECK (run_event_seq >= 0)
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_user_updated
  ON agent_runs(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_run_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  run_version BIGINT NOT NULL,
  event_seq BIGINT NOT NULL,
  event_type TEXT NOT NULL,
  replay_class TEXT NOT NULL CHECK (replay_class IN ('durable','transient')),
  actor_id UUID,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, event_seq)
);

CREATE INDEX IF NOT EXISTS idx_agent_run_events_run_seq
  ON agent_run_events(run_id, event_seq);

CREATE TABLE IF NOT EXISTS agent_run_tool_calls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  run_version BIGINT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_class TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','started','succeeded','failed','timed_out','blocked')),
  commit_state TEXT NOT NULL DEFAULT 'not_applicable'
    CHECK (commit_state IN ('not_applicable','pending_commit','committed','commit_failed','commit_unknown')),
  recovery_policy TEXT NOT NULL DEFAULT 'not_resumable'
    CHECK (recovery_policy IN ('not_resumable','safe_resume_from_started','safe_replay_if_not_committed','manual_intervention_required')),
  approval_required BOOLEAN NOT NULL DEFAULT FALSE,
  approval_state_at_execution TEXT
    CHECK (approval_state_at_execution IN ('pending','approved','rejected','expired','revoked','superseded')),
  state_reason_code TEXT
    CHECK (state_reason_code IN (
      'completed_success','completed_noop','failed_policy','failed_validation','failed_internal',
      'expired_timeout','expired_approval','interrupted_worker_lost','interrupted_lease_expired','interrupted_reconciler',
      'mutation_uncertain','blocked_policy','blocked_missing_approval','timed_out_execution','commit_failed','commit_unknown'
    )),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  exit_code INT,
  timed_out BOOLEAN,
  duration_ms BIGINT,
  stdout_redacted TEXT,
  stderr_redacted TEXT,
  stdout_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  stderr_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  stdout_sha256 TEXT,
  stderr_sha256 TEXT,
  artifact_manifest_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_tool_started_has_timestamp
    CHECK (status <> 'started' OR started_at IS NOT NULL),
  CONSTRAINT ck_tool_terminal_has_finished
    CHECK (status NOT IN ('succeeded','failed','timed_out','blocked') OR finished_at IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_one_started_tool_per_run
  ON agent_run_tool_calls(run_id)
  WHERE status = 'started';

CREATE INDEX IF NOT EXISTS idx_agent_run_tool_calls_run_status
  ON agent_run_tool_calls(run_id, status);

CREATE TABLE IF NOT EXISTS agent_tool_approvals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  tool_call_id UUID NOT NULL REFERENCES agent_run_tool_calls(id) ON DELETE CASCADE,
  actor_id UUID NOT NULL,
  decision_actor_id UUID,
  decision_source TEXT,
  run_version_approved BIGINT NOT NULL,
  approval_context_hash TEXT NOT NULL,
  decision_state TEXT NOT NULL
    CHECK (decision_state IN ('pending','approved','consumed','expired','revoked','superseded')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expired_at TIMESTAMPTZ,
  consumed_at TIMESTAMPTZ,
  superseded_at TIMESTAMPTZ,
  CONSTRAINT ck_approval_consumed_at
    CHECK (
      (decision_state = 'consumed' AND consumed_at IS NOT NULL)
      OR
      (decision_state <> 'consumed')
    )
);

CREATE INDEX IF NOT EXISTS idx_agent_tool_approvals_run_tool_state
  ON agent_tool_approvals(run_id, tool_call_id, decision_state);

CREATE TABLE IF NOT EXISTS agent_run_holds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL UNIQUE REFERENCES agent_runs(id) ON DELETE CASCADE,
  hold_type TEXT NOT NULL CHECK (hold_type IN ('tool_execution','approval_gate','mutation_guard','external_commit')),
  reference_id TEXT NOT NULL UNIQUE,
  amount_v NUMERIC(24,6) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','settled','released')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_hold_amount_nonnegative CHECK (amount_v >= 0)
);

CREATE TABLE IF NOT EXISTS agent_run_mutation_leases (
  run_id UUID PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
  lease_holder TEXT NOT NULL,
  lease_token UUID NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_mutation_replays (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  tool_call_id UUID NULL REFERENCES agent_run_tool_calls(id) ON DELETE CASCADE,
  actor_id UUID NOT NULL,
  actor_role_class TEXT NOT NULL CHECK (actor_role_class IN ('user','admin','system','reconciler','worker')),
  scope TEXT NOT NULL CHECK (scope IN ('run_transition','approval_decision','cancel_request')),
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('processing','completed','failed')),
  response_status INT,
  content_type TEXT,
  response_body_text TEXT,
  response_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  response_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_replay_scope_toolcall_presence
    CHECK (
      (scope='approval_decision' AND tool_call_id IS NOT NULL)
      OR
      (scope IN ('run_transition','cancel_request') AND tool_call_id IS NULL)
    ),
  CONSTRAINT ck_replay_truncation_hash
    CHECK (
      (response_truncated = FALSE)
      OR
      (response_truncated = TRUE AND response_hash IS NOT NULL)
    ),
  CONSTRAINT ck_replay_completed_payload
    CHECK (
      status <> 'completed'
      OR
      (response_status IS NOT NULL AND content_type IS NOT NULL AND response_body_text IS NOT NULL)
    ),
  CONSTRAINT ck_replay_processing_no_response
    CHECK (
      status <> 'processing'
      OR
      (response_status IS NULL AND response_body_text IS NULL)
    ),
  CONSTRAINT ck_replay_content_type_json
    CHECK (content_type IS NULL OR content_type = 'application/json'),
  CONSTRAINT ck_replay_response_status_range
    CHECK (response_status IS NULL OR (response_status >= 100 AND response_status <= 599))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_replay_run_scope
  ON agent_mutation_replays(run_id, actor_id, actor_role_class, scope, idempotency_key)
  WHERE tool_call_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_replay_tool_scope
  ON agent_mutation_replays(run_id, tool_call_id, actor_id, actor_role_class, scope, idempotency_key)
  WHERE tool_call_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_replay_tool_scope_key
  ON agent_mutation_replays(run_id, tool_call_id, actor_id, scope, idempotency_key);

CREATE TABLE IF NOT EXISTS run_status_projection (
  run_id UUID PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
  run_version BIGINT NOT NULL,
  projection_version BIGINT NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('created','running','awaiting_approval','completed','failed','canceled','expired','interrupted')),
  summary_kind TEXT,
  summary_text TEXT,
  pending_approval_id UUID,
  state_reason_code TEXT,
  last_event_seq_consumed BIGINT NOT NULL DEFAULT 0,
  projected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_projection_last_event_seq_nonnegative CHECK (last_event_seq_consumed >= 0)
);

INSERT INTO schema_migrations(version)
VALUES ('021_agent_orchestration_v26')
ON CONFLICT (version) DO NOTHING;
