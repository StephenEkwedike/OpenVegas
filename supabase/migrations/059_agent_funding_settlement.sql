-- Receipt of payment is independent of a refund/dispute review state.
ALTER TABLE agent_funding_orders ADD COLUMN IF NOT EXISTS credited_at timestamptz;
UPDATE agent_funding_orders o SET credited_at=o.updated_at
WHERE credited_at IS NULL AND EXISTS (
    SELECT 1 FROM ledger_entries l
    WHERE l.reference_id='agent-funding:' || o.id::text AND l.entry_type='agent_fiat_topup'
);
ALTER TABLE agent_funding_orders ADD CONSTRAINT agent_funding_reversal_bounds
    CHECK (reversed_v >= 0 AND reversed_v <= credit_v AND debt_v >= 0 AND debt_v <= reversed_v);
INSERT INTO schema_migrations(version) VALUES ('059_agent_funding_settlement') ON CONFLICT DO NOTHING;
