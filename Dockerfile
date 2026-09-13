FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENVEGAS_ROOT=/app \
    OPENVEGAS_RUNTIME_ENV=production \
    OPENVEGAS_TEST_MODE=0 \
    OPENVEGAS_DB_FAIL_OPEN=0 \
    OPENVEGAS_DOTENV_OVERRIDE=0 \
    PORT=8000

WORKDIR /app

RUN groupadd --system --gid 10001 openvegas \
    && useradd --system --uid 10001 --gid openvegas \
        --create-home --home-dir /home/openvegas openvegas

COPY pyproject.toml requirements.lock README.md ./
COPY openvegas/ ./openvegas/
COPY server/ ./server/
RUN python -m pip install --no-cache-dir -c requirements.lock ".[server]"

COPY ui/ ./ui/
COPY supabase/migrations/ ./supabase/migrations/
COPY scripts/ ./scripts/

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health/ready', timeout=8).close()"

# Database migrations are a separate, explicitly authorized operation.
CMD ["bash", "scripts/start.sh"]
