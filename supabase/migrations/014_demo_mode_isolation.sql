-- Demo mode isolation for game history and reporting-safe defaults.

ALTER TABLE game_history
  ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_game_history_is_demo_created
  ON game_history(is_demo, created_at DESC);

INSERT INTO schema_migrations(version)
VALUES ('014_demo_mode_isolation')
ON CONFLICT (version) DO NOTHING;
