"""Codex-style minimal terminal chat renderer."""

from __future__ import annotations

import os
import re
import sys

from rich.console import Console
from rich.table import Table
from rich.style import Style
from rich.text import Text

from openvegas.compact_uuid import encode_compact_uuid
from openvegas.tui.qr_render import qr_half_block, qr_width


USER_BG = Style(bgcolor="grey23")
USER_PROMPT = Style(color="white", bold=True, bgcolor="grey23")
ASSISTANT_BULLET = Style(color="grey70")
ASSISTANT_TEXT = Style(color="white")
STATUS_BAR = Style(color="grey50")
DIM = Style(color="grey50")


_MD_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _split_markdown_table_blocks(payload: str) -> list[tuple[str, list[str]]]:
    """Split assistant text into `text` and `table` blocks for reusable rendering."""
    lines = str(payload or "").splitlines()
    if not lines:
        return []
    blocks: list[tuple[str, list[str]]] = []
    text_buf: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and (i + 1) < len(lines) and _MD_TABLE_SEPARATOR_RE.match(lines[i + 1] or ""):
            if text_buf:
                blocks.append(("text", text_buf))
                text_buf = []
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            blocks.append(("table", table_lines))
            continue
        text_buf.append(line)
        i += 1
    if text_buf:
        blocks.append(("text", text_buf))
    return blocks


def _parse_markdown_table(table_lines: list[str]) -> tuple[list[str], list[list[str]]]:
    def _split_row(raw: str) -> list[str]:
        row = str(raw or "").strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [cell.strip() for cell in row.split("|")]

    if len(table_lines) < 2:
        return [], []
    header = _split_row(table_lines[0])
    rows = [_split_row(row) for row in table_lines[2:]]
    return header, rows


def render_markdown_table(console: Console, table_lines: list[str]) -> bool:
    """Render markdown table as a rich table. Returns False when parsing fails."""
    header, rows = _parse_markdown_table(table_lines)
    if not header:
        return False
    if any(len(r) != len(header) for r in rows):
        return False
    # Narrow terminals degrade to simple bullet rows to avoid unreadable overflow.
    if int(getattr(console, "width", 120) or 120) < 90:
        console.print(Text("• Table (compact view):", style=ASSISTANT_BULLET))
        for row in rows:
            parts = [f"{header[idx]}={row[idx]}" for idx in range(min(len(header), len(row)))]
            console.print(Text(f"  - {'; '.join(parts)}", style=ASSISTANT_TEXT))
        return True
    table = Table(show_header=True, header_style="bold white")
    for col in header:
        justify = "right" if col.strip().lower() in {"price", "beds", "baths", "sqft"} else "left"
        table.add_column(col or " ", justify=justify, overflow="fold")
    for row in rows:
        table.add_row(*row)
    console.print(table)
    return True


def render_user_input(console: Console, text: str) -> None:
    """Render user message row with a subtle highlighted background."""
    line = Text()
    line.append("› ", style=USER_PROMPT)
    line.append(str(text or ""), style=USER_BG)
    line.pad_right(max(1, console.width))
    line.stylize(USER_BG)
    console.print(line)


def render_assistant(console: Console, text: str) -> None:
    """Render assistant response with plain text + markdown table formatting."""
    payload = str(text or "")
    if not payload:
        return
    blocks = _split_markdown_table_blocks(payload)
    if not blocks:
        blocks = [("text", payload.splitlines() or [payload])]
    first_text_line = True
    for block_type, lines in blocks:
        if block_type == "table":
            if not render_markdown_table(console, lines):
                for idx, line_text in enumerate(lines):
                    line = Text()
                    line.append("• " if first_text_line and idx == 0 else "  ", style=ASSISTANT_BULLET)
                    line.append(line_text, style=ASSISTANT_TEXT)
                    console.print(line)
                first_text_line = False
            continue
        for line_text in lines:
            line = Text()
            line.append("• " if first_text_line else "  ", style=ASSISTANT_BULLET)
            line.append(line_text, style=ASSISTANT_TEXT)
            console.print(line)
            first_text_line = False


def render_tool_event(console: Console, label: str, detail: str = "") -> None:
    """Render compact, dim tool activity line."""
    text = f"  ⟳ {label}" + (f" — {detail}" if detail else "")
    console.print(Text(text, style=DIM))


def render_tool_result(console: Console, label: str, status: str) -> None:
    text = f"  ⟳ {label} — {status}"
    console.print(Text(text, style=DIM))


def render_status_bar(console: Console, model: str, budget: str, workspace: str) -> None:
    parts = f"  {model} · {budget} · {workspace}"
    console.print(Text(parts, style=STATUS_BAR))


def render_topup_hint(console: Console, hint: dict[str, object]) -> None:
    """Render low-balance top-up hint in the same minimal CLI style."""
    checkout_url = str(hint.get("checkout_url") or "")
    suggested = str(hint.get("suggested_topup_usd") or "")
    balance_v = str(hint.get("balance_v") or "")
    methods = hint.get("payment_methods_display") or []
    mode = str(hint.get("mode") or "simulated")
    topup_id = str(hint.get("topup_id") or "").strip()
    app_base = str(os.getenv("APP_BASE_URL", "")).strip().rstrip("/")
    compact_topup = encode_compact_uuid(topup_id) if topup_id else None
    short_status = f"{app_base}/r/{compact_topup}" if (app_base and compact_topup) else ""
    qr_value = str(short_status or hint.get("qr_value") or checkout_url or "")

    console.print(Text("  ⚠ Low balance", style="yellow"))
    if balance_v:
        console.print(Text(f"  Balance: {balance_v} $V", style=ASSISTANT_TEXT))
    if suggested:
        console.print(Text(f"  Suggested top-up: ${suggested}", style=ASSISTANT_TEXT))
    if isinstance(methods, list) and methods:
        console.print(Text(f"  Methods: {', '.join(str(m) for m in methods)}", style=DIM))
    if mode == "simulated":
        console.print(Text("  [simulated checkout]", style=DIM))
    if checkout_url:
        console.print(Text(f"  -> {checkout_url}", style="cyan"))

    if qr_value and sys.stdout.isatty():
        try:
            width = qr_width(qr_value, border=0)
            if width + 4 <= console.width:
                for line in qr_half_block(qr_value, border=0).splitlines():
                    console.print(Text(f"    {line}", style=ASSISTANT_TEXT))
        except Exception:
            pass
