-- ============================================================
-- ENTERPRISE ORGS
-- ============================================================

CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    billing_customer_id TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'cancelled')),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE org_members (
    org_id UUID NOT NULL REFERENCES organizations(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);

CREATE TABLE org_sponsorships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    monthly_budget_usd NUMERIC(12,2) NOT NULL,
    refill_rule TEXT DEFAULT 'manual' CHECK (refill_rule IN ('manual', 'auto_stripe')),
    status TEXT DEFAULT 'active',
    renewed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE org_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID UNIQUE NOT NULL REFERENCES organizations(id),
    allowed_providers TEXT[] DEFAULT ARRAY['openai','anthropic','gemini'],
    allowed_models TEXT[] DEFAULT ARRAY[]::TEXT[],
    user_daily_cap_usd NUMERIC(10,2) DEFAULT 50.00,
    byok_fallback_enabled BOOLEAN DEFAULT FALSE,
    boost_enabled BOOLEAN DEFAULT TRUE,
    casino_enabled BOOLEAN DEFAULT FALSE,
    casino_agent_max_loss_v NUMERIC(18,2) DEFAULT 100.00,
    casino_round_max_wager_v NUMERIC(18,2) DEFAULT 10.00,
    casino_round_cooldown_ms INT DEFAULT 500,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE org_budget_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    source TEXT NOT NULL,
    delta_usd NUMERIC(12,4) NOT NULL,
    reference_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- AGENT SERVICE ACCOUNTS
-- ============================================================

CREATE TABLE agent_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'revoked')),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE agent_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_account_id UUID NOT NULL REFERENCES agent_accounts(id),
    scopes TEXT[] NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE agent_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_account_id UUID NOT NULL REFERENCES agent_accounts(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    envelope_v NUMERIC(18,2) NOT NULL,
    spent_v NUMERIC(18,2) DEFAULT 0,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'exhausted', 'closed')),
    started_at TIMESTAMPTZ DEFAULT now(),
    ended_at TIMESTAMPTZ
);

-- ============================================================
-- DETERMINISTIC BOOSTS
-- ============================================================

CREATE TABLE boost_challenges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    agent_session_id UUID NOT NULL REFERENCES agent_sessions(id),
    rubric_version TEXT NOT NULL,
    task_prompt TEXT NOT NULL,
    rubric_json JSONB NOT NULL,
    max_reward_v NUMERIC(18,2) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'submitted', 'scored', 'expired')),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE boost_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_id UUID UNIQUE NOT NULL REFERENCES boost_challenges(id),
    artifact_hash TEXT NOT NULL,
    artifact_text TEXT NOT NULL,
    score NUMERIC(5,2),
    reward_v NUMERIC(18,2),
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'scored', 'rewarded', 'rejected')),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- AGENT-ONLY CASINO
-- ============================================================

CREATE TABLE casino_game_catalog (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    game_code TEXT UNIQUE NOT NULL,
    version INT DEFAULT 1,
    rtp NUMERIC(5,4) NOT NULL,
    rules_json JSONB NOT NULL,
    payout_table_json JSONB NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE casino_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    agent_account_id UUID NOT NULL REFERENCES agent_accounts(id),
    agent_session_id UUID NOT NULL REFERENCES agent_sessions(id),
    max_loss_v NUMERIC(18,2) NOT NULL,
    max_rounds INT DEFAULT 100,
    rounds_played INT DEFAULT 0,
    net_pnl_v NUMERIC(18,2) DEFAULT 0,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'loss_capped', 'round_capped', 'closed')),
    started_at TIMESTAMPTZ DEFAULT now(),
    ended_at TIMESTAMPTZ
);

CREATE TABLE casino_rounds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES casino_sessions(id),
    game_code TEXT NOT NULL,
    wager_v NUMERIC(18,2) NOT NULL,
    state_json JSONB DEFAULT '{}',
    rng_commit TEXT NOT NULL,
    rng_reveal TEXT,
    client_seed TEXT NOT NULL,
    nonce INT NOT NULL,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'resolved')),
    started_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE casino_moves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    round_id UUID NOT NULL REFERENCES casino_rounds(id),
    move_index INT NOT NULL,
    action TEXT NOT NULL,
    payload_json JSONB DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (round_id, idempotency_key)
);

CREATE TABLE casino_payouts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    round_id UUID UNIQUE NOT NULL REFERENCES casino_rounds(id),
    wager_v NUMERIC(18,2) NOT NULL,
    payout_v NUMERIC(18,2) NOT NULL,
    net_v NUMERIC(18,2) NOT NULL,
    ledger_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE casino_verifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    round_id UUID UNIQUE NOT NULL REFERENCES casino_rounds(id),
    commit_hash TEXT NOT NULL,
    reveal_seed TEXT NOT NULL,
    client_seed TEXT NOT NULL,
    nonce INT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- SEED CASINO CATALOG
-- ============================================================

INSERT INTO casino_game_catalog (game_code, rtp, rules_json, payout_table_json) VALUES
('blackjack', 0.9950, '{"decks":1,"dealer_stands_on":17}', '{"blackjack":2.5,"win":2,"push":1}'),
('roulette', 0.9730, '{"type":"european","zeros":1}', '{"straight":36,"red":2,"black":2,"odd":2,"even":2}'),
('slots', 0.9500, '{"reels":3,"symbols":["7","BAR","CHERRY","LEMON","BELL","STAR"]}', '{"777":50,"BAR3":20,"BELL3":10,"STAR3":8,"CHERRY3":5,"CHERRY2":2}'),
('poker', 0.9540, '{"variant":"five_card_draw","draw_count":1}', '{"royal_flush":250,"straight_flush":50,"four_kind":25,"full_house":9,"flush":6,"straight":4,"three_kind":3,"two_pair":2,"jacks_better":1}'),
('baccarat', 0.9862, '{"decks":6,"commission":0.05}', '{"player_wins":2,"banker_wins":1.95,"tie":9}')
ON CONFLICT (game_code) DO NOTHING;

-- ============================================================
-- RLS POLICIES
-- ============================================================

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE casino_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE casino_rounds ENABLE ROW LEVEL SECURITY;
ALTER TABLE casino_game_catalog ENABLE ROW LEVEL SECURITY;

CREATE POLICY casino_catalog_public ON casino_game_catalog FOR SELECT USING (true);
