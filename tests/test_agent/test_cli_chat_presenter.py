from __future__ import annotations

import io

from rich.console import Console

from openvegas.compact_uuid import encode_compact_uuid
import openvegas.tui.chat_renderer as chat_renderer
from openvegas.tui.chat_renderer import (
    render_assistant,
    render_status_bar,
    render_topup_hint,
    render_user_input,
)


def test_render_user_input_prefixes_with_single_chevron():
    console = Console(record=True, width=80, force_terminal=False)
    render_user_input(console, "hello world")
    out = console.export_text()
    assert "› hello world" in out


def test_render_assistant_uses_bullet_prefix_and_plain_text():
    console = Console(record=True, width=80, force_terminal=False)
    render_assistant(console, "### Header **bold** `code`")
    out = console.export_text()
    assert "• ### Header bold code" in out


def test_render_assistant_formats_markdown_table():
    console = Console(record=True, width=120, force_terminal=False)
    render_assistant(
        console,
        "\n".join(
            [
                "Top matches:",
                "| Address | Price | Beds | Baths |",
                "|---|---:|---:|---:|",
                "| 11711 W Rydalwater Ln | $322,900 | 3 | 2 |",
                "| 5806 Breezewood Dr | $374,900 | 3 | 1 |",
            ]
        ),
    )
    out = console.export_text()
    assert "Top matches:" in out
    assert "Address" in out
    assert "Price" in out
    assert "11711 W Rydalwater Ln" in out


def test_render_assistant_formats_markdown_table_narrow_fallback():
    console = Console(record=True, width=70, force_terminal=False)
    render_assistant(
        console,
        "\n".join(
            [
                "Top matches:",
                "| Address | Price | Beds | Baths |",
                "|---|---:|---:|---:|",
                "| 11711 W Rydalwater Ln | $322,900 | 3 | 2 |",
            ]
        ),
    )
    out = console.export_text()
    assert "Table (compact view)" in out
    assert "Address=11711 W Rydalwater Ln" in out


def test_render_status_bar_compact_line():
    console = Console(record=True, width=80, force_terminal=False)
    render_status_bar(console, "openai/gpt-4o-mini", "cost 0.01 $V", "~/repo")
    out = console.export_text()
    assert "openai/gpt-4o-mini · cost 0.01 $V · ~/repo" in out


def test_render_topup_hint_prints_low_balance_and_checkout_url():
    console = Console(record=True, width=120, force_terminal=False)
    render_topup_hint(
        console,
        {
            "balance_v": "123.000000",
            "suggested_topup_usd": "20.00",
            "checkout_url": "https://checkout.openvegas.local/topup/abc",
            "mode": "simulated",
            "payment_methods_display": ["Card", "PayPal"],
        },
    )
    out = console.export_text()
    assert "Low balance" in out
    assert "Suggested top-up: $20.00" in out
    assert "https://checkout.openvegas.local/topup/abc" in out


def test_render_topup_hint_prefers_short_status_url_for_qr(monkeypatch):
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    captured = {"value": ""}
    monkeypatch.setattr(chat_renderer.sys, "stdout", _TTY())
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(chat_renderer, "qr_width", lambda value, border=0: 10)

    def _fake_qr_half_block(value: str, border: int = 0) -> str:
        captured["value"] = value
        return "QRLINE1\nQRLINE2"

    monkeypatch.setattr(chat_renderer, "qr_half_block", _fake_qr_half_block)
    console = Console(file=io.StringIO(), record=True, width=120, force_terminal=False)
    render_topup_hint(
        console,
        {
            "topup_id": "123e4567-e89b-12d3-a456-426614174000",
            "checkout_url": "https://checkout.stripe.com/c/pay/cs_demo",
            "qr_value": "https://checkout.stripe.com/c/pay/cs_demo",
            "mode": "stripe",
        },
    )
    compact = encode_compact_uuid("123e4567-e89b-12d3-a456-426614174000")
    assert captured["value"] == f"http://127.0.0.1:8000/r/{compact}"
