-- Private operator receipts; never expose prompt recovery or operator identity
-- through the customer-facing wallet history projection.
CREATE TABLE public.inference_turn_reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    thread_id UUID NOT NULL REFERENCES public.provider_threads(id),
    request_id UUID NOT NULL REFERENCES public.inference_requests(id),
    operator_id UUID NOT NULL,
    plan_token TEXT NOT NULL CHECK (plan_token ~ '^[0-9a-f]{64}$'),
    details JSONB NOT NULL CHECK (jsonb_typeof(details) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, thread_id, request_id)
);
ALTER TABLE public.inference_turn_reconciliations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.inference_turn_reconciliations FROM PUBLIC, anon, authenticated;

INSERT INTO schema_migrations(version)
VALUES ('042_inference_turn_reconciliation')
ON CONFLICT (version) DO NOTHING;
