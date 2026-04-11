"""Shared service dependencies for FastAPI routes.

Provides runtime DB/Redis initialization and feature-aware schema checks.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

from openvegas.fraud.engine import FraudEngine
from openvegas.gateway.catalog import ProviderCatalog
from openvegas.gateway.inference import AIGateway
from openvegas.mint.engine import MintService
from openvegas.payments.fake_gateway import FakeGateway
from openvegas.payments.service import BillingService
from openvegas.payments.stripe_gateway import StripeGateway
from openvegas.telemetry import emit_metric
from openvegas.wallet.ledger import WalletService
from server.services.file_uploads import FileUploadService
from server.services.llm_mode import LLMModeService
from server.services.code_exec import CodeExecService
from server.services.mcp_registry import MCPRegistryService
from server.services.provider_threads import ProviderThreadService
from server.services.realtime_relay import RealtimeRelayService


class _Placeholder:
    """Placeholder DB/Redis connection for isolated development/testing."""

    async def execute(self, *a, **kw):
        return "OK"

    async def fetch(self, *a, **kw):
        return []

    async def fetchrow(self, *a, **kw):
        return None

    async def fetchval(self, *a, **kw):
        return None

    def transaction(self):
        return _TxCtx(self)

    async def incr(self, *a):
        return 1

    async def expire(self, *a):
        return None

    async def incrbyfloat(self, *a):
        return 0.0

    async def sadd(self, *a):
        return None

    async def scard(self, *a):
        return 1

    async def ping(self):
        return True


class _TxCtx:
    def __init__(self, conn: Any):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _PoolTxCtx:
    def __init__(self, pool: Any):
        self.pool = pool
        self.conn = None
        self.tx = None

    async def __aenter__(self):
        self.conn = await self.pool.acquire()
        self.tx = self.conn.transaction()
        await self.tx.start()
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.tx is not None:
                if exc_type is None:
                    await self.tx.commit()
                else:
                    await self.tx.rollback()
        finally:
            if self.conn is not None:
                await self.pool.release(self.conn)
        return False


class PostgresDB:
    """Minimal adapter exposing execute/fetch/fetchrow/transaction over asyncpg pool."""

    def __init__(self, pool: Any):
        self.pool = pool

    async def execute(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    def transaction(self):
        return _PoolTxCtx(self.pool)


@dataclass
class FeatureFlags:
    store_enabled: bool
    inference_enabled: bool
    agent_runtime_enabled: bool
    human_casino_enabled: bool
    mint_audit_enabled: bool
    context_enabled: bool
    trusted_proxy_headers_enabled: bool
    files_enabled: bool


_db: Any = _Placeholder()
_redis: Any = _Placeholder()
_http_client: httpx.AsyncClient | None = None
_log = logging.getLogger(__name__)


def _env_enabled(name: str, default: str = "0") -> bool:
    raw = str(os.getenv(name, default) or "").strip().strip('"').strip("'").lower()
    return raw in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def current_flags() -> FeatureFlags:
    return FeatureFlags(
        store_enabled=_env_enabled("STORE_ENABLED", "1"),
        inference_enabled=_env_enabled("INFERENCE_ENABLED", "1"),
        agent_runtime_enabled=_env_enabled("AGENT_RUNTIME_ENABLED", "1"),
        human_casino_enabled=_env_enabled("CASINO_HUMAN_ENABLED", "0"),
        mint_audit_enabled=_env_enabled("MINT_AUDIT_ENABLED", "1"),
        context_enabled=_env_enabled("OPENVEGAS_CONTEXT_ENABLED", "0"),
        trusted_proxy_headers_enabled=_env_enabled("OPENVEGAS_TRUSTED_PROXY_HEADERS", "0"),
        files_enabled=_env_enabled("OPENVEGAS_ENABLE_FILES", "0"),
    )


def get_db():
    return _db


def get_redis():
    return _redis


def bind_http_client(client: httpx.AsyncClient | None) -> None:
    global _http_client
    _http_client = client


def get_http_client() -> httpx.AsyncClient | None:
    return _http_client


async def request_with_http_client(method: str, url: str, **kwargs) -> httpx.Response:
    client = get_http_client()
    if client is not None:
        return await client.request(method, url, **kwargs)
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as temp_client:
        return await temp_client.request(method, url, **kwargs)


async def require_tables(db: Any, tables: set[str]) -> None:
    for table in sorted(tables):
        row = await db.fetchrow(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
            """,
            table,
        )
        if not row:
            raise RuntimeError(f"Missing required table: {table}")


async def require_columns(db: Any, columns: set[tuple[str, str]]) -> None:
    for table, column in sorted(columns):
        row = await db.fetchrow(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
            """,
            table,
            column,
        )
        if not row:
            raise RuntimeError(f"Missing required column: {table}.{column}")


async def require_migration_min(db: Any, version: str) -> None:
    row = await db.fetchrow(
        "SELECT version FROM schema_migrations WHERE version = $1",
        version,
    )
    if not row:
        raise RuntimeError(
            f"Schema migration '{version}' not applied. Run Supabase migrations before startup."
        )


async def assert_schema_compatible(db: Any, flags: FeatureFlags) -> None:
    await require_tables(
        db,
        {
            "profiles",
            "wallet_accounts",
            "ledger_entries",
            "mint_challenges",
            "mint_events",
            "provider_catalog",
            "inference_usage",
            "schema_migrations",
        },
    )
    await require_migration_min(db, "008_schema_migrations_and_readiness")
    await require_migration_min(db, "011_agent_balance_hardening")
    await require_migration_min(db, "012_rls_hardening")
    await require_migration_min(db, "013_stripe_billing")
    await require_migration_min(db, "014_demo_mode_isolation")
    await require_migration_min(db, "015_demo_admin_autofund")
    await require_migration_min(db, "017_horse_quote_pricing")
    await require_migration_min(db, "018_wrapper_default_foundation")
    await require_migration_min(db, "019_inference_idempotency_and_holds")
    await require_migration_min(db, "028_billing_topup_hardening")
    await require_migration_min(db, "031_user_subscription_billing")
    await require_migration_min(db, "032_wallet_bootstrap_and_continuation")
    await require_migration_min(db, "033_avatar_preferences")
    await require_migration_min(db, "036_profile_theme_preferences")
    await require_migration_min(db, "037_chat_file_uploads")

    await require_tables(
        db,
        {
            "fiat_topups",
            "stripe_webhook_events",
            "horse_quotes",
            "horse_quote_idempotency",
            "provider_credentials",
            "inference_requests",
            "wallet_history_projection",
            "wrapper_reward_events",
            "org_runtime_policies",
            "context_retention_policies",
            "user_subscriptions",
            "user_starter_grants",
            "user_continuation_credit",
            "continuation_claim_idempotency",
            "continuation_accounting_events",
            "chat_file_uploads",
        },
    )
    await require_columns(
        db,
        {
            ("org_sponsorships", "stripe_subscription_status"),
            ("org_sponsorships", "has_active_subscription"),
            ("org_sponsorships", "cancel_at_period_end"),
            ("org_sponsorships", "current_period_end"),
            ("game_history", "is_demo"),
            ("fiat_topups", "mode"),
            ("fiat_topups", "expires_at"),
            ("fiat_topups", "manual_reconciliation_required"),
            ("fiat_topups", "manual_reconciliation_reason"),
            ("fiat_topups", "manual_reconciliation_marked_at"),
            ("profiles", "avatar_id"),
            ("profiles", "avatar_palette"),
            ("profiles", "dealer_skin_id"),
                ("profiles", "theme"),
                ("chat_file_uploads", "content_bytes"),
                ("chat_file_uploads", "status"),
                ("chat_file_uploads", "expires_at"),
            },
        )

    if flags.store_enabled:
        await require_migration_min(db, "009_inference_grant_usages_and_preauth")
        await require_tables(db, {"store_orders", "inference_token_grants", "inference_grant_usages"})

    if flags.inference_enabled:
        await require_tables(db, {"inference_preauthorizations", "inference_usage"})

    if flags.context_enabled:
        await require_migration_min(db, "020_provider_context_threads")
        await require_tables(db, {"provider_threads", "provider_thread_messages"})

    if flags.agent_runtime_enabled:
        await require_migration_min(db, "010_agent_session_events_and_precision")
        await require_migration_min(db, "021_agent_orchestration_v26")
        await require_migration_min(db, "022_agent_chat_tool_runtime_v30")
        await require_migration_min(db, "025_agent_tool_heartbeat_column_v30_fix")
        await require_migration_min(db, "026_agent_tool_cancelled_status_v30")
        await require_migration_min(db, "027_agent_tool_result_payload_column_v30_fix")
        await require_tables(db, {"agent_accounts", "agent_tokens", "agent_sessions", "agent_session_events"})
        await require_tables(
            db,
            {
                "agent_runs",
                "agent_run_events",
                "agent_run_tool_calls",
                "agent_tool_approvals",
                "agent_run_holds",
                "agent_run_mutation_leases",
                "agent_mutation_replays",
                "run_status_projection",
            },
        )
        await require_columns(
            db,
            {
                ("agent_runs", "workspace_root"),
                ("agent_runs", "workspace_fingerprint"),
                ("agent_runs", "git_root"),
                ("agent_runs", "runtime_session_id"),
                ("agent_run_tool_calls", "execution_token"),
                ("agent_run_tool_calls", "request_payload_json"),
                ("agent_run_tool_calls", "last_heartbeat_at"),
                ("agent_run_tool_calls", "result_submission_hash"),
                ("agent_run_tool_calls", "result_payload"),
                ("agent_run_tool_calls", "terminal_response_status"),
                ("agent_run_tool_calls", "terminal_response_body_text"),
            },
        )

    if flags.human_casino_enabled:
        await require_migration_min(db, "016_human_casino")
        await require_tables(
            db,
            {
                "human_casino_sessions",
                "human_casino_rounds",
                "human_casino_moves",
                "human_casino_payouts",
                "human_casino_verifications",
                "human_casino_idempotency",
            },
        )

    if flags.mint_audit_enabled:
        await require_columns(
            db,
            {
                ("mint_challenges", "purpose"),
                ("mint_challenges", "disclosure_version"),
                ("mint_challenges", "default_policy_version"),
            },
        )


async def assert_db_ready() -> None:
    db = get_db()
    await db.fetchrow("SELECT 1")


async def assert_redis_ready() -> None:
    r = get_redis()
    if hasattr(r, "ping"):
        pong = await r.ping()
        if pong is False:
            raise RuntimeError("Redis ping failed")


async def init_runtime_deps() -> None:
    """Initialize runtime DB/Redis dependencies when not in test mode."""
    global _db, _redis

    if os.getenv("OPENVEGAS_TEST_MODE", "0") == "1":
        _db = _Placeholder()
        _redis = _Placeholder()
        return

    jwt_secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
    if not jwt_secret:
        raise RuntimeError("SUPABASE_JWT_SECRET is required in runtime mode")

    database_url = os.getenv("DATABASE_URL", "").strip()
    redis_url = os.getenv("REDIS_URL", "").strip()

    if not database_url:
        _db = _Placeholder()
        _redis = _Placeholder()
        return

    import asyncpg

    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=10)
    _db = PostgresDB(pool)

    if redis_url:
        import redis.asyncio as redis

        _redis = redis.from_url(redis_url, decode_responses=True)
    else:
        _redis = _Placeholder()

    flags = current_flags()
    await assert_schema_compatible(_db, flags)
    if flags.human_casino_enabled:
        _log.info("human_casino=enabled schema_ready=true")
    else:
        _log.info("human_casino=disabled")


async def close_runtime_deps() -> None:
    global _db, _redis
    pool = getattr(_db, "pool", None)
    if pool is not None:
        await pool.close()

    if hasattr(_redis, "close"):
        close_fn = getattr(_redis, "close")
        res = close_fn()
        if hasattr(res, "__await__"):
            await res

    _db = _Placeholder()
    _redis = _Placeholder()


def get_wallet() -> WalletService:
    return WalletService(get_db())


def get_catalog() -> ProviderCatalog:
    return ProviderCatalog(get_db())


def get_gateway() -> AIGateway:
    return AIGateway(get_db(), get_wallet(), get_catalog(), http_client=get_http_client())


def get_llm_mode_service() -> LLMModeService:
    return LLMModeService(get_db())


def get_provider_thread_service() -> ProviderThreadService:
    return ProviderThreadService(get_db())


def get_file_upload_service() -> FileUploadService:
    return FileUploadService(get_db())


@lru_cache(maxsize=1)
def get_mcp_registry_service() -> MCPRegistryService:
    return MCPRegistryService()


@lru_cache(maxsize=1)
def get_code_exec_service() -> CodeExecService:
    return CodeExecService()


@lru_cache(maxsize=1)
def get_realtime_relay_service() -> RealtimeRelayService:
    return RealtimeRelayService()


def get_mint_service() -> MintService:
    return MintService(get_db(), get_wallet(), get_catalog())


def get_fraud_engine() -> FraudEngine:
    return FraudEngine(get_redis(), get_db())


def get_agent_service():
    from openvegas.agent.service import AgentService

    return AgentService(get_db(), get_wallet())


def get_agent_orchestration_service():
    from openvegas.agent.orchestration_service import AgentOrchestrationService

    return AgentOrchestrationService(get_db())


def get_boost_service():
    from openvegas.agent.boost import BoostService

    return BoostService(get_db(), get_wallet())


def get_casino_service():
    from openvegas.casino.service import CasinoService

    return CasinoService(get_db(), get_wallet())


def get_human_casino_service():
    from openvegas.casino.human_service import HumanCasinoService

    return HumanCasinoService(get_db(), get_wallet())


def get_org_service():
    from openvegas.enterprise.org_service import OrgService

    return OrgService(get_db())


def get_store_service():
    from openvegas.store.service import StoreService

    return StoreService(get_db(), get_wallet())


def get_billing_service() -> BillingService:
    return BillingService(get_db(), get_wallet(), _build_billing_gateway())


def _runtime_env_name() -> str:
    return str(os.getenv("OPENVEGAS_RUNTIME_ENV", os.getenv("ENV", "local"))).strip().lower()


def _is_production_env() -> bool:
    return _runtime_env_name() in {"prod", "production"}


def _stripe_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip())


def _build_billing_gateway():
    mode = str(os.getenv("OPENVEGAS_BILLING_PROVIDER", "hybrid")).strip().lower()
    if mode == "stripe":
        return StripeGateway()
    if mode == "simulated":
        return FakeGateway()

    # hybrid mode
    if _stripe_configured():
        try:
            return StripeGateway()
        except Exception:
            pass

    if _is_production_env() and os.getenv("OPENVEGAS_ALLOW_HYBRID_FAKE_IN_PROD", "0") != "1":
        raise RuntimeError("Hybrid billing resolved to fake in production without explicit allow override")
    emit_metric("billing_hybrid_resolved_to_fake_total", {"env": _runtime_env_name() or "unknown"})
    _log.warning("Hybrid billing resolved to simulated provider")
    return FakeGateway()
