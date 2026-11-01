-- Enforce non-negative balances for both user and agent wallet principals

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_wallet_nonnegative_user_agent'
  ) THEN
    ALTER TABLE wallet_accounts
      ADD CONSTRAINT ck_wallet_nonnegative_user_agent
      CHECK (
        account_id NOT LIKE 'user:%' AND account_id NOT LIKE 'agent:%'
        OR balance >= 0
      );
  END IF;
END $$;

INSERT INTO schema_migrations(version)
VALUES ('011_agent_balance_hardening')
ON CONFLICT (version) DO NOTHING;
