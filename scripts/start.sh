#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8000}"
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
    echo "PORT must be an integer between 1 and 65535" >&2
    exit 1
fi

: "${DATABASE_URL:?DATABASE_URL must be configured before starting the API}"
if [[ ! "${SUPABASE_JWT_SECRET:-}" =~ [^[:space:]] &&
      ( ! "${SUPABASE_URL:-}" =~ [^[:space:]] || ! "${SUPABASE_ANON_KEY:-}" =~ [^[:space:]] ) ]]; then
    echo "SUPABASE_JWT_SECRET or both SUPABASE_URL and SUPABASE_ANON_KEY must be configured before starting the API" >&2
    exit 1
fi

if [[ "${OPENVEGAS_TEST_MODE:-0}" != "0" || "${OPENVEGAS_DB_FAIL_OPEN:-0}" != "0" ]]; then
    echo "The container requires OPENVEGAS_TEST_MODE=0 and OPENVEGAS_DB_FAIL_OPEN=0" >&2
    exit 1
fi

# Realtime and MCP state is process-local; keep exactly one API worker.
# Only trust forwarding proxies explicitly configured by the deployment owner.
exec uvicorn server.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    --timeout-keep-alive 65 \
    --log-level info
