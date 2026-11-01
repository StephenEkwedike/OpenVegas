-- Chat attachment upload lifecycle backing store.

CREATE TABLE IF NOT EXISTS chat_file_uploads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
  sha256 TEXT NOT NULL CHECK (char_length(sha256) = 64),
  status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded', 'expired', 'failed')),
  content_bytes BYTEA,
  error_code TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_file_uploads_user_status
  ON chat_file_uploads (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_file_uploads_expires
  ON chat_file_uploads (expires_at);

CREATE INDEX IF NOT EXISTS idx_chat_file_uploads_sha
  ON chat_file_uploads (user_id, sha256, size_bytes);

INSERT INTO schema_migrations(version)
VALUES ('037_chat_file_uploads')
ON CONFLICT (version) DO NOTHING;
