-- Avatar/dealer preferences persisted on profiles.

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS avatar_id TEXT DEFAULT 'ov_user_01',
  ADD COLUMN IF NOT EXISTS avatar_palette TEXT DEFAULT 'default',
  ADD COLUMN IF NOT EXISTS dealer_skin_id TEXT DEFAULT 'ov_dealer_female_tux_v1';

UPDATE profiles
SET
  avatar_id = COALESCE(NULLIF(trim(avatar_id), ''), 'ov_user_01'),
  avatar_palette = COALESCE(NULLIF(trim(avatar_palette), ''), 'default'),
  dealer_skin_id = COALESCE(NULLIF(trim(dealer_skin_id), ''), 'ov_dealer_female_tux_v1');

ALTER TABLE profiles
  ALTER COLUMN avatar_id SET NOT NULL,
  ALTER COLUMN avatar_palette SET NOT NULL,
  ALTER COLUMN dealer_skin_id SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'profiles_avatar_id_len'
  ) THEN
    ALTER TABLE profiles
      ADD CONSTRAINT profiles_avatar_id_len CHECK (char_length(avatar_id) BETWEEN 3 AND 64);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'profiles_avatar_palette_len'
  ) THEN
    ALTER TABLE profiles
      ADD CONSTRAINT profiles_avatar_palette_len CHECK (char_length(avatar_palette) BETWEEN 3 AND 32);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'profiles_dealer_skin_len'
  ) THEN
    ALTER TABLE profiles
      ADD CONSTRAINT profiles_dealer_skin_len CHECK (char_length(dealer_skin_id) BETWEEN 3 AND 64);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_profiles_avatar_lookup
  ON profiles (id, avatar_id, avatar_palette, dealer_skin_id);

INSERT INTO schema_migrations(version)
VALUES ('033_avatar_preferences')
ON CONFLICT (version) DO NOTHING;
