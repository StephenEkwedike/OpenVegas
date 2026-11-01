-- Billing top-up hardening: mode, expiry, manual reconciliation, status expansion.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'fiat_topup_status') THEN
    IF NOT EXISTS (
      SELECT 1
      FROM pg_enum
      WHERE enumtypid = 'fiat_topup_status'::regtype
        AND enumlabel = 'expired'
    ) THEN
      ALTER TYPE fiat_topup_status ADD VALUE 'expired';
    END IF;

    IF NOT EXISTS (
      SELECT 1
      FROM pg_enum
      WHERE enumtypid = 'fiat_topup_status'::regtype
        AND enumlabel = 'manual_reconciliation_required'
    ) THEN
      ALTER TYPE fiat_topup_status ADD VALUE 'manual_reconciliation_required';
    END IF;
  END IF;
END $$;

ALTER TABLE fiat_topups
  ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'stripe',
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS manual_reconciliation_required BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS manual_reconciliation_reason TEXT NULL,
  ADD COLUMN IF NOT EXISTS manual_reconciliation_marked_at TIMESTAMPTZ NULL;

UPDATE fiat_topups
SET mode = 'stripe'
WHERE mode IS NULL OR mode = '';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'ck_fiat_topups_mode_domain'
  ) THEN
    ALTER TABLE fiat_topups
      ADD CONSTRAINT ck_fiat_topups_mode_domain
      CHECK (mode IN ('stripe', 'simulated'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_fiat_topups_user_mode_status_expires
  ON fiat_topups(user_id, mode, status, expires_at DESC);

INSERT INTO schema_migrations(version)
VALUES ('028_billing_topup_hardening')
ON CONFLICT (version) DO NOTHING;
