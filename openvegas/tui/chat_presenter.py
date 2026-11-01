"""Rich chat rendering helpers for CLI output."""

from __future__ import annotations

import re

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from openvegas.tui.chat_theme import normalize_chat_style


def _strip_markdown_noise(text: str) -> str:
    out = text
    out = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
    out = re.sub(r"__([^_]+)__", r"\1", out)
    out = re.sub(r"\*([^*\n]+)\*", r"\1", out)
    out = re.sub(r"_([^_\n]+)_", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", out)
    return out


def normalize_markdown_for_cli(text: str, *, style: str = "codex") -> str:
    token = normalize_chat_style(style)
    out = str(text or "")
    out = (
        out.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    if token == "minimal":
        out = _strip_markdown_noise(out)
    return out.strip()


def render_assistant_message(console: Console, text: str, *, style: str = "codex") -> None:
    token = normalize_chat_style(style)
    normalized = normalize_markdown_for_cli(text, style=token)
    if not normalized:
        return
    if token == "raw":
        console.print(normalized)
        return
    if token == "minimal":
        console.print(Text(normalized))
        return
    console.print(Markdown(normalized))

