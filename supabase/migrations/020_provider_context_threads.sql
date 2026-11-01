-- Provider-scoped context threads for wrapper shell.

CREATE TABLE IF NOT EXISTS provider_threads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic', 'gemini')),
  model_id TEXT NOT NULL,
  conversation_mode TEXT NOT NULL CHECK (conversation_mode IN ('persistent', 'ephemeral')),
  title TEXT,
  thread_forked_from UUID REFERENCES provider_threads(id),
  expires_at TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_provider_threads_user_provider_updated
  ON provider_threads (user_id, provider, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_threads_user_last_used
  ON provider_threads (user_id, last_used_at DESC);

CREATE TABLE IF NOT EXISTS provider_thread_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id UUID NOT NULL REFERENCES provider_threads(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
  content JSONB NOT NULL,
  token_count INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_provider_thread_messages_thread_created
  ON provider_thread_messages (thread_id, created_at ASC);

INSERT INTO schema_migrations(version)
VALUES ('020_provider_context_threads')
ON CONFLICT (version) DO NOTHING;
