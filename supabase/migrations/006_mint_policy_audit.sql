-- Mint policy/disclosure auditability

ALTER TABLE mint_challenges
  ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'company'
    CHECK (purpose IN ('company', 'user')),
  ADD COLUMN IF NOT EXISTS disclosure_version TEXT NOT NULL DEFAULT 'v1',
  ADD COLUMN IF NOT EXISTS default_policy_version TEXT NOT NULL DEFAULT 'company_default_v1';

CREATE INDEX IF NOT EXISTS idx_mint_challenges_user_created
  ON mint_challenges(user_id, created_at DESC);
