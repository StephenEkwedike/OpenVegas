-- HTTP command replay is not a billable inference. Keep its identity and cached
-- response separate from the gateway's immutable payload/settlement records.
CREATE TABLE public.inference_route_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL CHECK (octet_length(idempotency_key) BETWEEN 1 AND 200),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('processing', 'succeeded')),
    response_status INT,
    response_body_text TEXT NOT NULL CHECK (octet_length(response_body_text) <= 2099200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'processing' AND response_status IS NULL)
        OR (status = 'succeeded' AND response_status IS NOT NULL AND response_status = 200)),
    UNIQUE (user_id, idempotency_key)
);
ALTER TABLE public.inference_route_commands ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.inference_route_commands FROM PUBLIC, anon, authenticated;

INSERT INTO schema_migrations(version)
VALUES ('044_inference_route_commands')
ON CONFLICT (version) DO NOTHING;
