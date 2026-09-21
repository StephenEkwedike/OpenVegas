-- Add the already-supported managed Mistral adapter to persisted provider scopes.
-- Preserve all existing rows, ownership/RLS policies, credentials and balances.
ALTER TABLE provider_threads
  DROP CONSTRAINT IF EXISTS provider_threads_provider_check;
ALTER TABLE provider_threads
  ADD CONSTRAINT provider_threads_provider_check
  CHECK (provider IN ('openai', 'anthropic', 'gemini', 'mistral'));

ALTER TABLE profiles
  DROP CONSTRAINT IF EXISTS profiles_default_provider_check;
ALTER TABLE profiles
  ADD CONSTRAINT profiles_default_provider_check
  CHECK (default_provider IN ('openai', 'anthropic', 'gemini', 'mistral'));

INSERT INTO schema_migrations(version)
VALUES ('040_mistral_provider_context')
ON CONFLICT (version) DO NOTHING;
