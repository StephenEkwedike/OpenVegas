-- OpenRouter is a server-managed inference supplier, not a customer BYOK account.
-- Preserve existing threads, RLS, provider credentials and all wallet balances.
ALTER TABLE provider_threads
  DROP CONSTRAINT IF EXISTS provider_threads_provider_check;
ALTER TABLE provider_threads
  ADD CONSTRAINT provider_threads_provider_check
  CHECK (provider IN ('openai', 'anthropic', 'gemini', 'mistral', 'openrouter'));

ALTER TABLE profiles
  DROP CONSTRAINT IF EXISTS profiles_default_provider_check;
ALTER TABLE profiles
  ADD CONSTRAINT profiles_default_provider_check
  CHECK (default_provider IN ('openai', 'anthropic', 'gemini', 'mistral', 'openrouter'));

INSERT INTO schema_migrations(version)
VALUES ('041_openrouter_provider_context')
ON CONFLICT (version) DO NOTHING;
