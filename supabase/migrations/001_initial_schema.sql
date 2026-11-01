-- OpenVegas Initial Schema
-- Supabase Postgres + Auth + RLS

-- User profiles (extends Supabase auth.users)
CREATE TABLE profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'pro', 'whale')),
    default_provider TEXT DEFAULT 'openai' CHECK (default_provider IN ('openai', 'anthropic', 'gemini')),
    default_model_by_provider JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Wallet accounts (one per user + system accounts)
CREATE TABLE wallet_accounts (
    account_id TEXT PRIMARY KEY,
    balance NUMERIC(18,2) NOT NULL DEFAULT 0.00
        CHECK (
            account_id NOT LIKE 'user:%' OR balance >= 0
        ),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Double-entry ledger
CREATE TABLE ledger_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debit_account TEXT NOT NULL REFERENCES wallet_accounts(account_id),
    credit_account TEXT NOT NULL REFERENCES wallet_accounts(account_id),
    amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    entry_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (reference_id, entry_type, debit_account, credit_account)
);

-- Mint challenges (server-issued, single-use, expiring)
CREATE TABLE mint_challenges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    nonce TEXT UNIQUE NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('solo', 'split', 'sponsor')),
    task_prompt TEXT NOT NULL,
    company_prompt TEXT,
    target_cost_usd NUMERIC(10,4) NOT NULL,
    max_credit_v NUMERIC(18,2) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Mint events (verified burns)
CREATE TABLE mint_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_id UUID UNIQUE NOT NULL REFERENCES mint_challenges(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL,
    provider_request_id TEXT NOT NULL,
    input_tokens INT NOT NULL,
    output_tokens INT NOT NULL,
    burn_cost_usd NUMERIC(10,6) NOT NULL,
    v_credited NUMERIC(18,2) NOT NULL,
    response_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Provider catalog (centralized, hot-toggleable)
CREATE TABLE provider_catalog (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    cost_input_per_1m NUMERIC(10,4) NOT NULL,
    cost_output_per_1m NUMERIC(10,4) NOT NULL,
    v_price_input_per_1m NUMERIC(10,2) NOT NULL,
    v_price_output_per_1m NUMERIC(10,2) NOT NULL,
    max_tokens INT DEFAULT 4096,
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (provider, model_id)
);

-- AI inference usage log
CREATE TABLE inference_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INT NOT NULL,
    output_tokens INT NOT NULL,
    v_cost NUMERIC(18,2) NOT NULL,
    actual_cost_usd NUMERIC(10,6) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Fraud events / holds
CREATE TABLE fraud_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    event_type TEXT NOT NULL,
    details JSONB,
    resolved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Game history
CREATE TABLE game_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    game_type TEXT NOT NULL,
    bet_amount NUMERIC(18,2) NOT NULL,
    payout NUMERIC(18,2) NOT NULL,
    outcome_data JSONB NOT NULL,
    server_seed TEXT NOT NULL,
    server_seed_hash TEXT NOT NULL,
    client_seed TEXT NOT NULL,
    nonce INT NOT NULL,
    provably_fair BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

------------------------------------------------------------
-- Row-Level Security
------------------------------------------------------------

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE wallet_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE mint_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE mint_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE inference_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE game_history ENABLE ROW LEVEL SECURITY;

-- Profiles: users read/update own row only
CREATE POLICY profiles_select ON profiles
    FOR SELECT USING (auth.uid() = id);
CREATE POLICY profiles_update ON profiles
    FOR UPDATE USING (auth.uid() = id);

-- Wallet: users read own balance only
CREATE POLICY wallet_select ON wallet_accounts
    FOR SELECT USING (account_id = 'user:' || auth.uid()::text);

-- Ledger: users read own entries only
CREATE POLICY ledger_select ON ledger_entries
    FOR SELECT USING (
        debit_account = 'user:' || auth.uid()::text
        OR credit_account = 'user:' || auth.uid()::text
    );

-- Mint challenges: users read own only
CREATE POLICY mint_challenges_select ON mint_challenges
    FOR SELECT USING (user_id = auth.uid());

-- Mint events: users read own only
CREATE POLICY mint_events_select ON mint_events
    FOR SELECT USING (user_id = auth.uid());

-- Inference usage: users read own only
CREATE POLICY inference_usage_select ON inference_usage
    FOR SELECT USING (user_id = auth.uid());

-- Provider catalog: public read
CREATE POLICY catalog_public_read ON provider_catalog
    FOR SELECT USING (true);

-- Game history: users read own only
CREATE POLICY game_history_select ON game_history
    FOR SELECT USING (user_id = auth.uid());

------------------------------------------------------------
-- System accounts (seeded on deploy)
------------------------------------------------------------

INSERT INTO wallet_accounts (account_id, balance) VALUES
    ('mint_reserve', 0),
    ('house', 0),
    ('rake_revenue', 0),
    ('store', 0)
ON CONFLICT DO NOTHING;
