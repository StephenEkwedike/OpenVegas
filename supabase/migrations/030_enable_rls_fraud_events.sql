-- Enable RLS hardening for fraud_events (public schema, PostgREST-exposed)

ALTER TABLE public.fraud_events ENABLE ROW LEVEL SECURITY;

-- Authenticated users can only access their own fraud events.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'fraud_events'
      AND policyname = 'fraud_events_owner_select'
  ) THEN
    CREATE POLICY fraud_events_owner_select
      ON public.fraud_events
      FOR SELECT
      TO authenticated
      USING ((SELECT auth.uid()) = user_id);
  END IF;
END $$;

-- Inserts are constrained to the caller's own user_id.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'fraud_events'
      AND policyname = 'fraud_events_owner_insert'
  ) THEN
    CREATE POLICY fraud_events_owner_insert
      ON public.fraud_events
      FOR INSERT
      TO authenticated
      WITH CHECK ((SELECT auth.uid()) = user_id);
  END IF;
END $$;

-- Updates/deletes are constrained to the caller's own rows.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'fraud_events'
      AND policyname = 'fraud_events_owner_update'
  ) THEN
    CREATE POLICY fraud_events_owner_update
      ON public.fraud_events
      FOR UPDATE
      TO authenticated
      USING ((SELECT auth.uid()) = user_id)
      WITH CHECK ((SELECT auth.uid()) = user_id);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'fraud_events'
      AND policyname = 'fraud_events_owner_delete'
  ) THEN
    CREATE POLICY fraud_events_owner_delete
      ON public.fraud_events
      FOR DELETE
      TO authenticated
      USING ((SELECT auth.uid()) = user_id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_fraud_events_user_id ON public.fraud_events(user_id);
