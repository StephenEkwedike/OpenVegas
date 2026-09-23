-- Private, monotonic per-run generation reservations. Old generations remain
-- explicitly unverified for continuation; never infer/backfill their history.
ALTER TABLE public.agent_runs ADD COLUMN native_history_revision BIGINT
    CHECK (native_history_revision >= 0);
ALTER TABLE public.inference_route_commands
    ADD COLUMN native_history_revision BIGINT CHECK (native_history_revision >= 0),
    ADD COLUMN previous_native_request_id UUID REFERENCES public.inference_requests(id),
    ADD CONSTRAINT native_revision_shape CHECK (COALESCE((
        (native_history_revision IS NULL AND previous_native_request_id IS NULL)
        OR (native_run_id IS NOT NULL AND native_history_revision = 0
            AND previous_native_request_id IS NULL)
        OR (native_run_id IS NOT NULL AND native_history_revision > 0
            AND previous_native_request_id IS NOT NULL)
    ), false));
CREATE UNIQUE INDEX inference_route_native_revision_uq
    ON public.inference_route_commands(native_run_id, COALESCE(native_history_revision, -1))
    WHERE native_run_id IS NOT NULL;
CREATE UNIQUE INDEX inference_route_native_previous_uq
    ON public.inference_route_commands(previous_native_request_id)
    WHERE previous_native_request_id IS NOT NULL;
-- The replacement fence exists before removing the first-generation-only index.
DROP INDEX public.inference_route_native_run_uq;
INSERT INTO schema_migrations(version) VALUES ('047_native_continuation_revisions')
ON CONFLICT (version) DO NOTHING;
