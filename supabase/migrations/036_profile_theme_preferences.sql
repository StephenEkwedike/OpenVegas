-- Persist website theme preference on profiles for cross-device sync.

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS theme TEXT DEFAULT 'light';

UPDATE profiles
SET theme = COALESCE(NULLIF(lower(trim(theme)), ''), 'light');

ALTER TABLE profiles
  ALTER COLUMN theme SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'profiles_theme_allowed'
  ) THEN
    ALTER TABLE profiles
      ADD CONSTRAINT profiles_theme_allowed CHECK (theme IN ('light', 'dark'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_profiles_theme_lookup
  ON profiles (id, theme);

INSERT INTO schema_migrations(version)
VALUES ('036_profile_theme_preferences')
ON CONFLICT (version) DO NOTHING;
