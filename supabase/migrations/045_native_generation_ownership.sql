-- Initial ownership only: no history revision or continuation is implied.
ALTER TABLE public.inference_route_commands
    ADD COLUMN native_run_id UUID REFERENCES public.agent_runs(id),
    ADD COLUMN native_scope JSONB,
    ADD COLUMN gateway_request_id UUID REFERENCES public.inference_requests(id),
    ADD CONSTRAINT native_scope_shape CHECK (
        (native_run_id IS NULL AND native_scope IS NULL AND gateway_request_id IS NULL)
        OR COALESCE((native_run_id IS NOT NULL AND native_scope IS NOT NULL
            AND jsonb_typeof(native_scope) = 'object'
            AND native_scope->'scope_version' = '1'::jsonb
            AND jsonb_typeof(native_scope->'registration') = 'object'
            AND native_scope->'scope'->>'run_id' = native_run_id::text
            AND octet_length(native_scope::text) <= 8192), false)
    );
CREATE UNIQUE INDEX inference_route_native_run_uq
    ON public.inference_route_commands(native_run_id) WHERE native_run_id IS NOT NULL;
CREATE UNIQUE INDEX inference_route_native_gateway_uq
    ON public.inference_route_commands(gateway_request_id) WHERE gateway_request_id IS NOT NULL;

-- Nullable markers keep pre-045 rows explicitly unverified. Never backfill.
ALTER TABLE public.agent_runs ADD COLUMN native_generation_claim_id UUID
    REFERENCES public.inference_route_commands(id) ON DELETE SET NULL;
ALTER TABLE public.inference_requests ADD COLUMN native_route_command_id UUID
    UNIQUE REFERENCES public.inference_route_commands(id);
-- All three tables retain their existing private RLS and grants.
INSERT INTO schema_migrations(version) VALUES ('045_native_generation_ownership')
ON CONFLICT (version) DO NOTHING;
