from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.middleware.auth import get_current_user
from server.routes import inference as routes


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["ask", "stream"])
@pytest.mark.parametrize(
    "extra",
    [{"enable_tools": True}, {"enable_web_search": True}, {"attachments": ["local-file-fixture"]}],
)
async def test_gemini_rejects_before_thread_files_or_provider(monkeypatch, endpoint, extra):
    monkeypatch.setattr(
        routes, "get_fraud_engine", lambda: SimpleNamespace(check_inference=AsyncMock())
    )
    monkeypatch.setattr(
        routes,
        "get_llm_mode_service",
        lambda: SimpleNamespace(
            resolve_for_user=AsyncMock(
                return_value={"effective_mode": "wrapper", "conversation_mode": "persistent"}
            )
        ),
    )

    def forbidden():
        pytest.fail("Unsupported input reached thread/file/provider work")

    for name in ["get_provider_thread_service", "get_gateway", "get_file_upload_service"]:
        monkeypatch.setattr(routes, name, forbidden, raising=False)
    app = FastAPI()
    app.include_router(routes.router, prefix="/inference")
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "fixture"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/inference/" + endpoint,
            json={"provider": "gemini", "model": "gemini-2.5-flash", "prompt": "test", **extra},
        )
    if endpoint == "ask":
        assert response.status_code == 400
        assert "nothing was flattened or sent" in response.json()["detail"]
    else:
        assert response.status_code == 200
        assert "nothing was flattened or sent" in response.text
        assert "event: response.error" in response.text
