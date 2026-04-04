from __future__ import annotations

from rich.console import Console

import pytest

from openvegas.tui.prompt_ui import InlinePromptUI


class _InlineClientSaved:
    def __init__(self):
        self.calls: list[str] = []

    async def get_saved_topup_payment_method(self):
        self.calls.append("saved_status")
        return {"available": True, "brand": "visa", "last4": "4242"}

    async def charge_saved_topup(self, amount):
        self.calls.append(f"charge:{amount}")
        return {"topup_id": "top_e2e_saved", "status": "paid"}

    async def create_topup_checkout(self, amount):
        self.calls.append(f"checkout:{amount}")
        return {"topup_id": "top_e2e_checkout", "status": "checkout_created", "checkout_url": "https://checkout.example"}


@pytest.mark.asyncio
async def test_inline_ui_deposit_saved_card_flow_e2e(monkeypatch):
    ui = InlinePromptUI(client=_InlineClientSaved(), console=Console(record=True))
    ui.state.action = "Deposit"
    ui.state.amount = "10"

    monkeypatch.setattr("openvegas.tui.prompt_ui.Confirm.ask", lambda *_args, **_kwargs: True)

    out = await ui.run_once()

    assert "Top-up ID: top_e2e_saved" in out
    assert "Status: paid" in out
    assert "Saved card charged successfully." in out
    assert ui.client.calls == ["saved_status", "charge:10"]
