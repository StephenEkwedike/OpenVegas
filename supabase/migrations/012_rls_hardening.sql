-- Harden RLS and privileges for sensitive enterprise/store/inference tables

ALTER TABLE store_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE inference_token_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE inference_grant_usages ENABLE ROW LEVEL SECURITY;
ALTER TABLE inference_preauthorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE boost_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE boost_submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_session_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'store_orders' AND policyname = 'store_orders_select_self'
  ) THEN
    CREATE POLICY store_orders_select_self ON store_orders
      FOR SELECT USING (user_id = auth.uid());
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'inference_token_grants' AND policyname = 'grants_select_self'
  ) THEN
    CREATE POLICY grants_select_self ON inference_token_grants
      FOR SELECT USING (user_id = auth.uid());
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'inference_preauthorizations' AND policyname = 'preauth_select_self'
  ) THEN
    CREATE POLICY preauth_select_self ON inference_preauthorizations
      FOR SELECT USING (user_id = auth.uid());
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'inference_grant_usages' AND policyname = 'grant_usages_select_self'
  ) THEN
    CREATE POLICY grant_usages_select_self ON inference_grant_usages
      FOR SELECT USING (
        EXISTS (
          SELECT 1
          FROM inference_token_grants g
          WHERE g.id = inference_grant_usages.grant_id
            AND g.user_id = auth.uid()
        )
      );
  END IF;
END $$;

REVOKE ALL ON TABLE agent_tokens FROM anon, authenticated;
REVOKE ALL ON TABLE boost_challenges FROM anon, authenticated;
REVOKE ALL ON TABLE boost_submissions FROM anon, authenticated;
REVOKE ALL ON TABLE agent_session_events FROM anon, authenticated;

INSERT INTO schema_migrations(version)
VALUES ('012_rls_hardening')
ON CONFLICT (version) DO NOTHING;
