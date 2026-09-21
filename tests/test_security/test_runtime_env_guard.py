from __future__ import annotations

import pytest

from server.services.dependencies import init_runtime_deps


@pytest.mark.asyncio
async def test_init_runtime_deps_requires_auth_configuration_in_runtime(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_TEST_MODE", "0")
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    # Runtime accepts either a legacy JWT secret or the public Auth API pair.
    # Clear both alternatives so test ordering/environment cannot mask the guard.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_JWT_SECRET"):
        await init_runtime_deps()
