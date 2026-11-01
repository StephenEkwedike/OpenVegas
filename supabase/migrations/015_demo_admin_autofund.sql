-- Demo admin autofund support: demo reserve source + lookup index.

INSERT INTO wallet_accounts (account_id, balance)
VALUES ('demo_reserve', 0)
ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_ledger_demo_autofund_recent
  ON ledger_entries (credit_account, created_at DESC)
  WHERE entry_type = 'demo_autofund';

INSERT INTO schema_migrations(version)
VALUES ('015_demo_admin_autofund')
ON CONFLICT (version) DO NOTHING;
