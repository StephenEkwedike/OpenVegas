-- Keep informational cancellation metadata from hiding older payment failures.
ALTER TABLE public.tv_subscriptions ADD COLUMN last_metadata_event_created BIGINT
    NOT NULL DEFAULT 0 CHECK (last_metadata_event_created >= 0);
UPDATE public.tv_subscriptions SET last_metadata_event_created = last_event_created;
-- Existing timestamps mixed financial status with metadata. Their order cannot
-- be reconstructed safely: require fresh paid proof for previously usable rows.
UPDATE public.tv_subscriptions SET status = 'paused' WHERE status IN ('active', 'past_due');
INSERT INTO public.schema_migrations(version) VALUES ('054_tv_event_ordering')
ON CONFLICT (version) DO NOTHING;
