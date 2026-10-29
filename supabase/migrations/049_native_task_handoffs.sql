-- Private task-boundary snapshots. No public history-import or execution grant.
CREATE TABLE public.native_task_handoffs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version = 1),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    source_run_id UUID NOT NULL REFERENCES public.agent_runs(id),
    source_runtime_session_id UUID NOT NULL,
    source_history_revision BIGINT NOT NULL CHECK (source_history_revision >= 0),
    source_request_id UUID NOT NULL REFERENCES public.inference_requests(id),
    source_scope_json TEXT NOT NULL CHECK (octet_length(source_scope_json) BETWEEN 1 AND 8192),
    workspace_json TEXT NOT NULL CHECK (octet_length(workspace_json) BETWEEN 1 AND 16384),
    document_json TEXT NOT NULL CHECK (octet_length(document_json) BETWEEN 1 AND 1000000),
    document_sha256 TEXT NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    target_json TEXT NOT NULL CHECK (octet_length(target_json) BETWEEN 1 AND 8192),
    review_fingerprint TEXT NOT NULL CHECK (review_fingerprint ~ '^[0-9a-f]{64}$'),
    review_expires_at TIMESTAMPTZ NOT NULL,
    prepare_key TEXT NOT NULL CHECK (prepare_key ~ '^[!-~]{1,200}$'),
    prepare_request_json TEXT NOT NULL CHECK (octet_length(prepare_request_json) BETWEEN 1 AND 32768),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    handoff_sha256 TEXT NOT NULL CHECK (handoff_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    destination_run_id UUID UNIQUE REFERENCES public.agent_runs(id),
    destination_runtime_session_id UUID,
    destination_scope_json TEXT CHECK (octet_length(destination_scope_json) BETWEEN 1 AND 8192),
    destination_workspace_json TEXT CHECK (octet_length(destination_workspace_json) BETWEEN 1 AND 16384),
    commit_key TEXT CHECK (commit_key ~ '^[!-~]{1,200}$'),
    committed_at TIMESTAMPTZ,
    first_route_command_id UUID UNIQUE REFERENCES public.inference_route_commands(id),
    first_request_id UUID UNIQUE REFERENCES public.inference_requests(id),
    consumed_at TIMESTAMPTZ,
    first_dispatch_json TEXT CHECK (octet_length(first_dispatch_json) BETWEEN 1 AND 8192),
    CHECK (first_dispatch_json IS NULL OR first_request_id IS NOT NULL),
    UNIQUE (user_id, prepare_key),
    UNIQUE (user_id, commit_key),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '10 minutes'
           AND expires_at <= review_expires_at),
    CHECK (destination_run_id IS NULL OR destination_run_id <> source_run_id),
    CHECK ((destination_run_id IS NULL AND destination_runtime_session_id IS NULL
            AND destination_scope_json IS NULL AND destination_workspace_json IS NULL
            AND commit_key IS NULL AND committed_at IS NULL)
        OR (destination_run_id IS NOT NULL AND destination_runtime_session_id IS NOT NULL
            AND destination_scope_json IS NOT NULL AND destination_workspace_json IS NOT NULL
            AND commit_key IS NOT NULL AND committed_at IS NOT NULL
            AND committed_at >= created_at AND committed_at < expires_at)),
    CHECK ((first_route_command_id IS NULL AND first_request_id IS NULL AND consumed_at IS NULL)
        OR (first_route_command_id IS NOT NULL AND first_request_id IS NOT NULL
            AND consumed_at IS NOT NULL AND committed_at IS NOT NULL
            AND consumed_at >= committed_at AND consumed_at < expires_at))
);
-- Multiple previews are possible, but a completed boundary cannot be forked by
-- competing confirmations. A fresh source revision needs a fresh snapshot.
CREATE UNIQUE INDEX native_task_handoff_source_consumption_uq
    ON public.native_task_handoffs(source_run_id, source_history_revision)
    WHERE destination_run_id IS NOT NULL;

CREATE FUNCTION public.guard_native_task_handoff_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['destination_run_id','destination_runtime_session_id',
        'destination_scope_json','destination_workspace_json','commit_key','committed_at',
        'first_route_command_id','first_request_id','consumed_at','first_dispatch_json']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['destination_run_id','destination_runtime_session_id',
        'destination_scope_json','destination_workspace_json','commit_key','committed_at',
        'first_route_command_id','first_request_id','consumed_at','first_dispatch_json'])
       OR (OLD.destination_run_id IS NOT NULL AND
           ROW(NEW.destination_run_id,NEW.destination_runtime_session_id,NEW.destination_scope_json,
               NEW.destination_workspace_json,NEW.commit_key,NEW.committed_at) IS DISTINCT FROM
           ROW(OLD.destination_run_id,OLD.destination_runtime_session_id,OLD.destination_scope_json,
               OLD.destination_workspace_json,OLD.commit_key,OLD.committed_at))
       OR (OLD.first_request_id IS NOT NULL AND
           ROW(NEW.first_route_command_id,NEW.first_request_id,NEW.consumed_at,NEW.first_dispatch_json) IS DISTINCT FROM
           ROW(OLD.first_route_command_id,OLD.first_request_id,OLD.consumed_at,OLD.first_dispatch_json)) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Native handoff is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_task_handoff_immutable BEFORE UPDATE ON public.native_task_handoffs
    FOR EACH ROW EXECUTE FUNCTION public.guard_native_task_handoff_update();
REVOKE ALL ON FUNCTION public.guard_native_task_handoff_update() FROM PUBLIC, anon, authenticated;
ALTER TABLE public.native_task_handoffs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.native_task_handoffs FROM PUBLIC, anon, authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
        REVOKE ALL ON public.native_task_handoffs FROM service_role;
        GRANT SELECT, INSERT ON public.native_task_handoffs TO service_role;
        GRANT UPDATE (destination_run_id,destination_runtime_session_id,destination_scope_json,
            destination_workspace_json,commit_key,committed_at,first_route_command_id,
            first_request_id,consumed_at,first_dispatch_json) ON public.native_task_handoffs TO service_role;
    END IF;
END $$;
-- A committed destination must not become an ordinary fresh run if a caller
-- omits its handoff or an operator disables the rollout gate after confirmation.
ALTER TABLE public.agent_runs ADD COLUMN native_handoff_id UUID UNIQUE
    REFERENCES public.native_task_handoffs(id);
CREATE FUNCTION public.guard_native_handoff_run_binding() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF OLD.native_handoff_id IS NOT NULL AND
       ROW(NEW.native_handoff_id, NEW.runtime_session_id, NEW.workspace_root,
           NEW.workspace_fingerprint, NEW.git_root) IS DISTINCT FROM
       ROW(OLD.native_handoff_id, OLD.runtime_session_id, OLD.workspace_root,
           OLD.workspace_fingerprint, OLD.git_root) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Native handoff run binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER native_handoff_run_binding_immutable BEFORE UPDATE ON public.agent_runs
    FOR EACH ROW EXECUTE FUNCTION public.guard_native_handoff_run_binding();
REVOKE ALL ON FUNCTION public.guard_native_handoff_run_binding() FROM PUBLIC, anon, authenticated;
INSERT INTO schema_migrations(version) VALUES ('049_native_task_handoffs')
ON CONFLICT (version) DO NOTHING;
