-- Content-free opt-in pilot. Applying this does NOT enable collection or sales.
CREATE TABLE public.data_contribution_preferences (
    user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    opted_in boolean NOT NULL DEFAULT false,
    notice_version text NOT NULL CHECK (notice_version='metadata-pilot-2026-10-20'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.data_contribution_inventory (
    user_id uuid NOT NULL REFERENCES public.data_contribution_preferences(user_id) ON DELETE CASCADE,
    submission_id uuid NOT NULL,
    host text NOT NULL CHECK (host IN ('openvegas','claude','codex')),
    model_family text NOT NULL CHECK (model_family IN ('openai','anthropic','google','mistral','other','unknown')),
    task_type text NOT NULL CHECK (task_type IN ('bugfix','feature','refactor','tests','documentation','other')),
    language text NOT NULL CHECK (language IN ('python','javascript','typescript','rust','go','java','other','unknown')),
    outcome text NOT NULL CHECK (outcome IN ('accepted','rejected','abandoned','unknown')),
    test_result text NOT NULL CHECK (test_result IN ('passed','failed','not_run','unknown')),
    correction_count integer NOT NULL CHECK (correction_count BETWEEN 0 AND 100),
    duration_bucket text NOT NULL CHECK (duration_bucket IN ('under_1m','1_to_5m','5_to_30m','over_30m','unknown')),
    notice_version text NOT NULL CHECK (notice_version='metadata-pilot-2026-10-20'),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now()+interval '30 days',
    PRIMARY KEY(user_id,submission_id),
    CHECK(expires_at <= created_at+interval '30 days')
);
CREATE INDEX data_contribution_expiry ON public.data_contribution_inventory(expires_at);
ALTER TABLE public.data_contribution_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.data_contribution_inventory ENABLE ROW LEVEL SECURITY;
-- No browser/PostgREST direct writes or reads. Existing authenticated server only.
REVOKE ALL ON public.data_contribution_preferences, public.data_contribution_inventory FROM PUBLIC, anon, authenticated;
COMMENT ON TABLE public.data_contribution_inventory IS
    'Account-linked, participant-reported metadata; not anonymous, verified training data or licensed for resale.';
