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
    assert '"horse_quotes"' in deps
    assert '"horse_quote_idempotency"' in deps
    assert '"provider_credentials"' in deps
    assert '"inference_requests"' in deps
    assert '"wallet_history_projection"' in deps
    assert '"wrapper_reward_events"' in deps
    assert '"org_runtime_policies"' in deps
    assert '"context_retention_policies"' in deps
    assert '"provider_threads"' in deps
    assert '"provider_thread_messages"' in deps


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
