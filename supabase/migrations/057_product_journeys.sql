-- Optional, consented browser journeys. No content or payment information.
CREATE TABLE public.product_journeys (
    browser_id UUID PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment IN ('local','test','staging','production')),
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    registered_at TIMESTAMPTZ,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    UNIQUE (environment, user_id)
);
CREATE TABLE public.product_journey_events (
    id UUID PRIMARY KEY,
    browser_id UUID NOT NULL REFERENCES public.product_journeys(browser_id) ON DELETE CASCADE,
    event TEXT NOT NULL CHECK (event IN ('ov_first_open','ov_signup_completed','ov_returned')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_day DATE NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
    exported_at TIMESTAMPTZ,
    UNIQUE (browser_id, event, event_day)
);
CREATE INDEX product_journey_first_seen ON public.product_journeys(environment, first_seen);
CREATE INDEX product_journey_event_pending ON public.product_journey_events(occurred_at) WHERE exported_at IS NULL;
ALTER TABLE public.product_journeys ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.product_journey_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.product_journeys, public.product_journey_events FROM PUBLIC, anon, authenticated;
INSERT INTO public.schema_migrations(version) VALUES ('057_product_journeys') ON CONFLICT DO NOTHING;
