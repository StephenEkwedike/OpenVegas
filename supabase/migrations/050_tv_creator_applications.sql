-- Review-only TV pitches. Applying this does not enable intake or paid playback.
CREATE TABLE public.tv_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    idempotency_key UUID NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 3 AND 100),
    channel_id TEXT NOT NULL CHECK (channel_id ~ '^[a-z0-9-]{1,48}$'),
    -- Count the exact canonical UTF-8 bytes stored by the backend. jsonb::text
    -- would insert whitespace and change this boundary relative to the app/hash.
    document_json TEXT NOT NULL CHECK (octet_length(document_json) BETWEEN 1 AND 1000000),
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'shortlisted', 'needs_changes', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_note TEXT CHECK (char_length(review_note) BETWEEN 1 AND 1000),
    reviewer_id UUID REFERENCES auth.users(id),
    reviewed_at TIMESTAMPTZ,
    UNIQUE (user_id, idempotency_key),
    CHECK ((status='submitted' AND review_note IS NULL AND reviewer_id IS NULL AND reviewed_at IS NULL)
        OR (status<>'submitted' AND review_note IS NOT NULL AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE INDEX tv_applications_user_created ON public.tv_applications (user_id, created_at DESC);
CREATE INDEX tv_applications_pending ON public.tv_applications (created_at) WHERE status='submitted';
ALTER TABLE public.tv_applications ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.tv_applications FROM PUBLIC, anon, authenticated;
-- Only the trusted backend performs scoped reads and transitions after authentication.
GRANT SELECT, INSERT ON public.tv_applications TO service_role;
GRANT UPDATE (status, review_note, reviewer_id, reviewed_at) ON public.tv_applications TO service_role;
