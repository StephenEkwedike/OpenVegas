-- Private original provider evidence, never a public history projection.
-- TEXT deliberately preserves JSON tokens, opaque signatures and call order.
CREATE TABLE public.native_generation_envelopes (
    request_id UUID PRIMARY KEY REFERENCES public.inference_requests(id),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    run_id UUID NOT NULL REFERENCES public.agent_runs(id),
    runtime_session_id UUID NOT NULL,
    route_command_id UUID NOT NULL UNIQUE REFERENCES public.inference_route_commands(id),
    provider TEXT NOT NULL CHECK (provider = 'openrouter'),
    model_id TEXT NOT NULL CHECK (octet_length(model_id) BETWEEN 1 AND 256),
    provider_request_id TEXT NOT NULL CHECK (octet_length(provider_request_id) BETWEEN 1 AND 256),
    response_model TEXT NOT NULL CHECK (octet_length(response_model) BETWEEN 1 AND 256),
    finish_reason TEXT NOT NULL CHECK (finish_reason IN ('stop','length','tool_calls')),
    assistant_message_json TEXT NOT NULL CHECK (octet_length(assistant_message_json) BETWEEN 1 AND 2000000),
    assistant_sha256 TEXT NOT NULL CHECK (assistant_sha256 ~ '^[0-9a-f]{64}$'),
    request_payload_json TEXT NOT NULL CHECK (octet_length(request_payload_json) BETWEEN 1 AND 1100000),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    history_inputs_json TEXT NOT NULL CHECK (octet_length(history_inputs_json) BETWEEN 1 AND 65536),
    inputs_sha256 TEXT NOT NULL CHECK (inputs_sha256 ~ '^[0-9a-f]{64}$'),
    public_binding TEXT NOT NULL CHECK (public_binding ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.native_generation_envelopes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.native_generation_envelopes FROM PUBLIC, anon, authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        REVOKE ALL ON public.native_generation_envelopes FROM service_role;
        GRANT SELECT,INSERT ON public.native_generation_envelopes TO service_role;
    END IF;
END $$;
INSERT INTO schema_migrations(version) VALUES ('046_native_generation_envelopes')
ON CONFLICT (version) DO NOTHING;
