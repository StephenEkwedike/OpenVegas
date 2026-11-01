-- Precision standardization + updated_at maintenance

ALTER TABLE wallet_accounts
  ALTER COLUMN balance TYPE NUMERIC(18,6);

ALTER TABLE ledger_entries
  ALTER COLUMN amount TYPE NUMERIC(18,6);

ALTER TABLE mint_events
  ALTER COLUMN v_credited TYPE NUMERIC(18,6);

ALTER TABLE inference_usage
  ALTER COLUMN v_cost TYPE NUMERIC(18,6);

ALTER TABLE game_history
  ALTER COLUMN bet_amount TYPE NUMERIC(18,6),
  ALTER COLUMN payout TYPE NUMERIC(18,6);

ALTER TABLE casino_rounds
  ALTER COLUMN wager_v TYPE NUMERIC(18,6);

ALTER TABLE casino_payouts
  ALTER COLUMN wager_v TYPE NUMERIC(18,6),
  ALTER COLUMN payout_v TYPE NUMERIC(18,6),
  ALTER COLUMN net_v TYPE NUMERIC(18,6);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_store_orders_updated_at ON store_orders;
CREATE TRIGGER trg_store_orders_updated_at
BEFORE UPDATE ON store_orders
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_inference_grants_updated_at ON inference_token_grants;
CREATE TRIGGER trg_inference_grants_updated_at
BEFORE UPDATE ON inference_token_grants
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_inference_preauth_updated_at ON inference_preauthorizations;
CREATE TRIGGER trg_inference_preauth_updated_at
BEFORE UPDATE ON inference_preauthorizations
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_store_orders_status_created
  ON store_orders(status, created_at DESC);

INSERT INTO schema_migrations(version)
VALUES ('010_agent_session_events_and_precision')
ON CONFLICT (version) DO NOTHING;
