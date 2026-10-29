-- Private, runtime-observed source evidence. This is not remote disk attestation.
CREATE TABLE public.native_mutation_preparations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    run_id UUID NOT NULL REFERENCES public.agent_runs(id),
    runtime_session_id UUID NOT NULL,
    native_inference_request_id UUID NOT NULL REFERENCES public.inference_requests(id),
    native_provider_call_id TEXT NOT NULL CHECK (octet_length(native_provider_call_id) BETWEEN 1 AND 256),
    original_ordinal INTEGER NOT NULL CHECK (original_ordinal >= 0),
    workspace_json TEXT NOT NULL CHECK (octet_length(workspace_json) BETWEEN 1 AND 16384),
    observed_source_json TEXT NOT NULL CHECK (octet_length(observed_source_json) BETWEEN 1 AND 262144),
    plan_json TEXT NOT NULL CHECK (octet_length(plan_json) BETWEEN 1 AND 524288),
    contract_sha256 TEXT NOT NULL CHECK (contract_sha256 ~ '^[0-9a-f]{64}$'),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL CHECK (octet_length(idempotency_key) BETWEEN 1 AND 200),
    tool_call_id UUID UNIQUE REFERENCES public.agent_run_tool_calls(id),
    approval_id UUID UNIQUE REFERENCES public.agent_tool_approvals(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '10 minutes'),
    CHECK (approval_id IS NULL OR tool_call_id IS NOT NULL),
    UNIQUE (user_id, idempotency_key),
    UNIQUE (native_inference_request_id, native_provider_call_id)
);
CREATE TABLE public.native_mutation_observations (
    tool_call_id UUID PRIMARY KEY REFERENCES public.agent_run_tool_calls(id),
    preparation_id UUID NOT NULL UNIQUE REFERENCES public.native_mutation_preparations(id),
    result_submission_sha256 TEXT NOT NULL CHECK (result_submission_sha256 ~ '^[0-9a-f]{64}$'),
    proof_json TEXT NOT NULL CHECK (octet_length(proof_json) BETWEEN 1 AND 16384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE public.native_mutation_approval_commands (
    user_id UUID NOT NULL REFERENCES auth.users(id),
    idempotency_key TEXT NOT NULL CHECK (octet_length(idempotency_key) BETWEEN 1 AND 200),
    preparation_id UUID NOT NULL UNIQUE REFERENCES public.native_mutation_preparations(id),
    tool_call_id UUID NOT NULL REFERENCES public.agent_run_tool_calls(id),
    approval_id UUID NOT NULL REFERENCES public.agent_tool_approvals(id),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    response_json TEXT NOT NULL CHECK (octet_length(response_json) BETWEEN 1 AND 16384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, idempotency_key)
);
ALTER TABLE public.native_mutation_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.native_mutation_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.native_mutation_approval_commands ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.native_mutation_preparations, public.native_mutation_observations, public.native_mutation_approval_commands
    FROM PUBLIC, anon, authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        REVOKE ALL ON public.native_mutation_preparations, public.native_mutation_observations, public.native_mutation_approval_commands FROM service_role;
        GRANT SELECT, INSERT ON public.native_mutation_preparations, public.native_mutation_observations, public.native_mutation_approval_commands TO service_role;
        GRANT UPDATE (tool_call_id, approval_id) ON public.native_mutation_preparations TO service_role;
    END IF;
END $$;
INSERT INTO schema_migrations(version) VALUES ('048_native_mutation_preparations')
ON CONFLICT (version) DO NOTHING;
