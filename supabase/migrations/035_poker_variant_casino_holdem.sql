-- Update poker catalog metadata to casino hold'em for human-casino UX.

UPDATE casino_game_catalog
SET
  version = COALESCE(version, 1) + 1,
  rules_json = '{"variant":"casino_holdem","decision_points":1,"flow":"preflop+flop_visible_then_call_or_fold"}'::jsonb,
  payout_table_json = '{"call_win":2,"call_push":1,"call_loss":0,"fold":0}'::jsonb
WHERE game_code = 'poker';

INSERT INTO schema_migrations(version)
VALUES ('035_poker_variant_casino_holdem')
ON CONFLICT (version) DO NOTHING;
