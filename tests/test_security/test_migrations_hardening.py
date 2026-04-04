from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_agent_balance_hardening_migration_exists_and_enforces_agent_nonnegative():
    sql = _read("supabase/migrations/011_agent_balance_hardening.sql")
    assert "ck_wallet_nonnegative_user_agent" in sql
    assert "account_id NOT LIKE 'agent:%'" in sql


def test_rls_hardening_migration_exists_and_hardens_sensitive_tables():
    sql = _read("supabase/migrations/012_rls_hardening.sql")
    assert "ALTER TABLE store_orders ENABLE ROW LEVEL SECURITY;" in sql
    assert "ALTER TABLE agent_tokens ENABLE ROW LEVEL SECURITY;" in sql
    assert "REVOKE ALL ON TABLE agent_tokens FROM anon, authenticated;" in sql


def test_startup_schema_requires_security_migrations():
    deps = _read("server/services/dependencies.py")
    assert 'require_migration_min(db, "011_agent_balance_hardening")' in deps
    assert 'require_migration_min(db, "012_rls_hardening")' in deps
    assert 'require_migration_min(db, "013_stripe_billing")' in deps
    assert 'require_migration_min(db, "014_demo_mode_isolation")' in deps
    assert 'require_migration_min(db, "015_demo_admin_autofund")' in deps
    assert 'require_migration_min(db, "016_human_casino")' in deps
    assert 'require_migration_min(db, "017_horse_quote_pricing")' in deps
    assert 'require_migration_min(db, "018_wrapper_default_foundation")' in deps
    assert 'require_migration_min(db, "019_inference_idempotency_and_holds")' in deps
    assert 'require_migration_min(db, "020_provider_context_threads")' in deps
    assert 'require_migration_min(db, "021_agent_orchestration_v26")' in deps
    assert 'require_migration_min(db, "022_agent_chat_tool_runtime_v30")' in deps
    assert 'require_migration_min(db, "025_agent_tool_heartbeat_column_v30_fix")' in deps
    assert 'require_migration_min(db, "026_agent_tool_cancelled_status_v30")' in deps
    assert 'require_migration_min(db, "027_agent_tool_result_payload_column_v30_fix")' in deps
    assert 'require_migration_min(db, "028_billing_topup_hardening")' in deps
    assert 'require_migration_min(db, "031_user_subscription_billing")' in deps
    assert 'require_migration_min(db, "032_wallet_bootstrap_and_continuation")' in deps
    assert 'require_migration_min(db, "033_avatar_preferences")' in deps
    assert 'require_migration_min(db, "036_profile_theme_preferences")' in deps
    assert 'require_migration_min(db, "037_chat_file_uploads")' in deps
    assert '"horse_quotes"' in deps
    assert '"horse_quote_idempotency"' in deps
    assert '"provider_credentials"' in deps
    assert '"inference_requests"' in deps
    assert '"wallet_history_projection"' in deps
    assert '"wrapper_reward_events"' in deps
    assert '"org_runtime_policies"' in deps
    assert '"context_retention_policies"' in deps
    assert '"user_starter_grants"' in deps
    assert '"user_continuation_credit"' in deps
    assert '"continuation_claim_idempotency"' in deps
    assert '"continuation_accounting_events"' in deps
    assert '"chat_file_uploads"' in deps
    assert '"profiles"' in deps
    assert '("fiat_topups", "mode")' in deps
    assert '("fiat_topups", "expires_at")' in deps
    assert '("fiat_topups", "manual_reconciliation_required")' in deps
    assert '("profiles", "avatar_id")' in deps
    assert '("profiles", "avatar_palette")' in deps
    assert '("profiles", "dealer_skin_id")' in deps
    assert '("profiles", "theme")' in deps
    assert '("chat_file_uploads", "content_bytes")' in deps
    assert '("chat_file_uploads", "status")' in deps
    assert '("chat_file_uploads", "expires_at")' in deps
    assert '"provider_threads"' in deps
    assert '"provider_thread_messages"' in deps
    assert '"agent_runs"' in deps
    assert '"agent_run_events"' in deps
    assert '"agent_run_tool_calls"' in deps
    assert '"agent_tool_approvals"' in deps
    assert '"agent_run_holds"' in deps
    assert '"agent_run_mutation_leases"' in deps
    assert '"agent_mutation_replays"' in deps
    assert '"run_status_projection"' in deps


def test_billing_migration_exists_and_hardens_dedupe_and_projection():
    sql = _read("supabase/migrations/013_stripe_billing.sql")
    assert "CREATE TABLE IF NOT EXISTS fiat_topups" in sql
    assert "CREATE TABLE IF NOT EXISTS stripe_webhook_events" in sql
    assert "uq_org_sponsorships_stripe_customer_id" in sql
    assert "ck_org_sponsorships_stripe_status" in sql


def test_demo_isolation_migration_exists_and_adds_game_history_flag():
    sql = _read("supabase/migrations/014_demo_mode_isolation.sql")
    assert "ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "idx_game_history_is_demo_created" in sql


def test_demo_autofund_migration_exists_and_seeds_demo_reserve():
    sql = _read("supabase/migrations/015_demo_admin_autofund.sql")
    assert "('demo_reserve', 0)" in sql
    assert "idx_ledger_demo_autofund_recent" in sql


def test_human_casino_migration_exists_and_enforces_uniques():
    sql = _read("supabase/migrations/016_human_casino.sql")
    assert "CREATE TABLE IF NOT EXISTS human_casino_sessions" in sql
    assert "CREATE TABLE IF NOT EXISTS human_casino_rounds" in sql
    assert "CREATE TABLE IF NOT EXISTS human_casino_idempotency" in sql
    assert "UNIQUE (round_id)" in sql  # payout + verification
    assert "UNIQUE (user_id, scope, idempotency_key)" in sql


def test_horse_quote_pricing_migration_exists_and_enforces_constraints():
    sql = _read("supabase/migrations/017_horse_quote_pricing.sql")
    assert "CREATE TABLE IF NOT EXISTS horse_quotes" in sql
    assert "CREATE TABLE IF NOT EXISTS horse_quote_idempotency" in sql
    assert "CHECK (budget_v >= 0)" in sql
    assert "consumed_at IS NULL AND consumed_game_id IS NULL" in sql
    assert "UNIQUE (user_id, scope, idempotency_key)" in sql


def test_wrapper_default_foundation_migration_exists_and_enforces_request_identity():
    sql = _read("supabase/migrations/018_wrapper_default_foundation.sql")
    assert "CREATE TABLE IF NOT EXISTS provider_credentials" in sql
    assert "CREATE TABLE IF NOT EXISTS inference_requests" in sql
    assert "CREATE TABLE IF NOT EXISTS wallet_history_projection" in sql
    assert "CREATE TABLE IF NOT EXISTS wrapper_reward_events" in sql
    assert "ADD COLUMN IF NOT EXISTS request_id UUID" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_usage_request_id" in sql
    assert "CHECK (status <> 'succeeded' OR response_body_text IS NOT NULL)" in sql


def test_inference_idempotency_holds_migration_exists_and_enforces_active_hold_uniqueness():
    sql = _read("supabase/migrations/019_inference_idempotency_and_holds.sql")
    assert "CREATE TABLE IF NOT EXISTS org_runtime_policies" in sql
    assert "CREATE TABLE IF NOT EXISTS context_retention_policies" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_preauth_request_id" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_inference_preauth_active_request" in sql
    assert "019_inference_idempotency_and_holds" in sql


def test_provider_context_threads_migration_exists_and_scopes_threads_per_provider():
    sql = _read("supabase/migrations/020_provider_context_threads.sql")
    assert "CREATE TABLE IF NOT EXISTS provider_threads" in sql
    assert "provider IN ('openai', 'anthropic', 'gemini')" in sql
    assert "thread_forked_from UUID REFERENCES provider_threads(id)" in sql
    assert "CREATE TABLE IF NOT EXISTS provider_thread_messages" in sql
    assert "020_provider_context_threads" in sql


def test_agent_orchestration_v26_migration_exists_and_enforces_replay_and_ordering_contracts():
    sql = _read("supabase/migrations/021_agent_orchestration_v26.sql")
    assert "CREATE TABLE IF NOT EXISTS agent_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_run_events" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_run_tool_calls" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_tool_approvals" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_run_holds" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_run_mutation_leases" in sql
    assert "CREATE TABLE IF NOT EXISTS agent_mutation_replays" in sql
    assert "CREATE TABLE IF NOT EXISTS run_status_projection" in sql
    assert "UNIQUE (run_id, event_seq)" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_replay_run_scope" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_replay_tool_scope" in sql
    assert "ck_replay_truncation_hash" in sql
    assert "ck_replay_completed_payload" in sql
    assert "ck_replay_content_type_json" in sql
    assert "ck_replay_response_status_range" in sql
    assert "ck_hold_amount_nonnegative" in sql
    assert "ck_agent_runs_version_nonnegative" in sql
    assert "ck_agent_runs_event_seq_nonnegative" in sql
    assert "ck_projection_last_event_seq_nonnegative" in sql
    assert "021_agent_orchestration_v26" in sql


def test_agent_chat_tool_runtime_v30_migration_exists_and_hardens_tool_callback_columns():
    sql = _read("supabase/migrations/022_agent_chat_tool_runtime_v30.sql")
    assert "ADD COLUMN IF NOT EXISTS runtime_session_id UUID" in sql
    assert "ADD COLUMN IF NOT EXISTS execution_token TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS request_payload_json JSONB" in sql
    assert "ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ" in sql
    assert "ADD COLUMN IF NOT EXISTS result_submission_hash TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS terminal_response_status INT" in sql
    assert "ck_tool_terminal_response_payload" in sql
    assert "ck_tool_terminal_response_hash" in sql
    assert "ck_tool_proposed_unclaimed" in sql
    assert "022_agent_chat_tool_runtime_v30" in sql


def test_agent_chat_tool_runtime_indexes_migration_exists():
    sql = _read("supabase/migrations/023_agent_chat_tool_runtime_indexes_v30.sql")
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_run_event_seq" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_tool_calls_run_status_token" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_mutation_leases_expires" in sql
    assert "023_agent_chat_tool_runtime_indexes_v30" in sql


def test_agent_chat_turns_optional_migration_exists():
    sql = _read("supabase/migrations/024_agent_chat_turns_optional_v30.sql")
    assert "CREATE TABLE IF NOT EXISTS agent_chat_turns" in sql
    assert "UNIQUE (run_id, turn_no)" in sql
    assert "024_agent_chat_turns_optional_v30" in sql


def test_agent_tool_heartbeat_column_fix_migration_exists():
    sql = _read("supabase/migrations/025_agent_tool_heartbeat_column_v30_fix.sql")
    assert "ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ" in sql
    assert "025_agent_tool_heartbeat_column_v30_fix" in sql


def test_agent_tool_cancelled_status_migration_exists_and_hardens_constraints():
    sql = _read("supabase/migrations/026_agent_tool_cancelled_status_v30.sql")
    assert "ck_tool_status_domain" in sql
    assert "'cancelled'" in sql
    assert "ck_tool_terminal_requires_finished" in sql
    assert "status NOT IN ('succeeded','failed','timed_out','blocked')" in sql
    assert "026_agent_tool_cancelled_status_v30" in sql


def test_agent_tool_result_payload_fix_migration_exists():
    sql = _read("supabase/migrations/027_agent_tool_result_payload_column_v30_fix.sql")
    assert "ADD COLUMN IF NOT EXISTS result_payload JSONB" in sql
    assert "027_agent_tool_result_payload_column_v30_fix" in sql


def test_billing_topup_hardening_migration_exists_and_adds_mode_expiry_reconciliation_fields():
    sql = _read("supabase/migrations/028_billing_topup_hardening.sql")
    assert "ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'stripe'" in sql
    assert "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS manual_reconciliation_required BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ALTER TYPE fiat_topup_status ADD VALUE 'expired'" in sql
    assert "ALTER TYPE fiat_topup_status ADD VALUE 'manual_reconciliation_required'" in sql
    assert "028_billing_topup_hardening" in sql


def test_user_subscription_billing_migration_exists_and_enforces_self_scope():
    sql = _read("supabase/migrations/031_user_subscription_billing.sql")
    assert "CREATE TABLE IF NOT EXISTS user_subscriptions" in sql
    assert "uq_user_subscriptions_stripe_customer_id" in sql
    assert "ck_user_subscriptions_status" in sql
    assert "ALTER TABLE user_subscriptions ENABLE ROW LEVEL SECURITY;" in sql
    assert "user_subscriptions_select_self" in sql
    assert "031_user_subscription_billing" in sql


def test_wallet_bootstrap_and_continuation_migration_exists_and_enforces_accounting_contracts():
    sql = _read("supabase/migrations/032_wallet_bootstrap_and_continuation.sql")
    assert "CREATE TABLE IF NOT EXISTS user_starter_grants" in sql
    assert "CREATE TABLE IF NOT EXISTS user_continuation_credit" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_continuation_active" in sql
    assert "(status = 'cancelled' AND outstanding_v = 0 AND repaid_at IS NULL)" in sql
    assert "CREATE TABLE IF NOT EXISTS continuation_claim_idempotency" in sql
    assert "CREATE TABLE IF NOT EXISTS continuation_accounting_events" in sql
    assert "event_type IN ('principal_repaid','principal_written_off')" in sql
    assert "032_wallet_bootstrap_and_continuation" in sql


def test_avatar_preferences_migration_exists_and_enforces_constraints():
    sql = _read("supabase/migrations/033_avatar_preferences.sql")
    assert "ADD COLUMN IF NOT EXISTS avatar_id TEXT DEFAULT 'ov_user_01'" in sql
    assert "ADD COLUMN IF NOT EXISTS avatar_palette TEXT DEFAULT 'default'" in sql
    assert "ADD COLUMN IF NOT EXISTS dealer_skin_id TEXT DEFAULT 'ov_dealer_female_tux_v1'" in sql
    assert "profiles_avatar_id_len" in sql
    assert "profiles_avatar_palette_len" in sql
    assert "profiles_dealer_skin_len" in sql
    assert "idx_profiles_avatar_lookup" in sql
    assert "033_avatar_preferences" in sql


def test_profile_theme_preferences_migration_exists_and_enforces_theme_domain():
    sql = _read("supabase/migrations/036_profile_theme_preferences.sql")
    assert "ADD COLUMN IF NOT EXISTS theme TEXT DEFAULT 'light'" in sql
    assert "ALTER COLUMN theme SET NOT NULL" in sql
    assert "profiles_theme_allowed" in sql
    assert "theme IN ('light', 'dark')" in sql
    assert "036_profile_theme_preferences" in sql


def test_chat_file_uploads_migration_exists_and_enforces_status_and_ownership_columns():
    sql = _read("supabase/migrations/037_chat_file_uploads.sql")
    assert "CREATE TABLE IF NOT EXISTS chat_file_uploads" in sql
    assert "REFERENCES auth.users(id) ON DELETE CASCADE" in sql
    assert "status IN ('pending', 'uploaded', 'expired', 'failed')" in sql
    assert "content_bytes BYTEA" in sql
    assert "idx_chat_file_uploads_user_status" in sql
    assert "037_chat_file_uploads" in sql


def test_callback_mutator_modules_do_not_touch_replay_helpers():
    callback_modules = [
        "openvegas/agent/mutators/tool_start.py",
        "openvegas/agent/mutators/tool_heartbeat.py",
        "openvegas/agent/mutators/tool_result.py",
        "openvegas/agent/mutators/tool_cancel.py",
        "openvegas/agent/tool_cas.py",
    ]
    for module in callback_modules:
        src = _read(module)
        assert "INSERT INTO agent_mutation_replays" not in src
        assert "UPDATE agent_mutation_replays" not in src
        assert "FROM agent_mutation_replays" not in src
        assert "from openvegas.agent.replay" not in src


def test_claimed_at_is_aligned_with_tool_start_cas():
    cas = _read("openvegas/agent/tool_cas.py")
    assert "SET status='started', claimed_at=now(), started_at=now(), last_heartbeat_at=now(), updated_at=now()" in cas


def test_ci_callback_replay_boundary_script_exists():
    script = _read("scripts/ci/check_callback_replay_boundary.py")
    assert "CALLBACK_MODULES" in script
    assert "tool_start.py" in script
    assert "tool_heartbeat.py" in script
    assert "tool_result.py" in script
    assert "tool_cancel.py" in script
    assert "agent_mutation_replays" in script


def test_ci_tool_request_immutability_script_exists():
    script = _read("scripts/ci/check_tool_request_immutability.py")
    assert "IMMUTABLE_COLUMNS" in script
    assert "request_payload_json" in script
    assert "payload_hash" in script
    assert "execution_token" in script
