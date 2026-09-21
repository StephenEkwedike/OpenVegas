import httpx
import pytest
from fastapi import FastAPI

from server.routes import payments


@pytest.mark.asyncio
async def test_webhook_error_does_not_expose_private_provider_payload(monkeypatch):
    class Broken:
        async def handle_webhook(self, **kwargs):
            raise RuntimeError("private-provider-key-and-customer-payload")

    monkeypatch.setattr(payments, "get_billing_service", lambda: Broken())
    app = FastAPI()
    app.include_router(payments.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post("/billing/webhook/stripe", content=b"{}")
    assert response.status_code == 503
    assert response.json() == {"detail": "Unable to process Stripe webhook; retry later"}
