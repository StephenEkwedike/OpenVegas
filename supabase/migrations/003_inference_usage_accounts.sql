-- Add account-aware inference usage fields for human + agent parity

ALTER TABLE inference_usage
  ADD COLUMN IF NOT EXISTS account_id TEXT,
  ADD COLUMN IF NOT EXISTS actor_type TEXT;

UPDATE inference_usage
SET account_id = 'user:' || user_id::text
WHERE account_id IS NULL;

UPDATE inference_usage
SET actor_type = 'human'
WHERE actor_type IS NULL;

ALTER TABLE inference_usage
  ALTER COLUMN account_id SET NOT NULL,
  ALTER COLUMN actor_type SET NOT NULL;

ALTER TABLE inference_usage
  ALTER COLUMN user_id DROP NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_inference_usage_actor_type'
  ) THEN
    ALTER TABLE inference_usage
      ADD CONSTRAINT ck_inference_usage_actor_type
      CHECK (actor_type IN ('human', 'agent'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_inference_usage_account_created
  ON inference_usage (account_id, created_at DESC);
