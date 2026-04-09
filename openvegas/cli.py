"""OpenVegas CLI — Terminal Arcade for Developers."""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import logging
import mimetypes
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

try:  # Optional: richer in-line composer for chat input.
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.styles import Style as PromptStyle
except Exception:  # pragma: no cover - exercised by runtime envs without prompt_toolkit
    PromptSession = None
    InMemoryHistory = None
    KeyBindings = None
    MouseEventType = None
    PromptStyle = None

from openvegas import __version__
from openvegas.agent.local_tools import (
    ToolExecutionResult,
    execute_shell_run_streaming,
    execute_tool_request,
    extract_tool_instruction,
    workspace_fingerprint,
)
from openvegas.agent.runtime_contracts import ToolPolicyDecision, evaluate_tool_policy
from openvegas.agent.runtime_contracts import result_submission_hash as compute_result_submission_hash
from openvegas.agent.tool_cas import redact_hash_truncate
from openvegas.capabilities import resolve_capability
from openvegas.config import load_config, save_config
from openvegas.events import mk_event
from openvegas.ide.show_diff import (
    is_valid_show_diff_payload,
    normalize_show_diff_result,
    redact_show_diff_payload_shape,
)
from openvegas.telemetry import emit_metric
from openvegas.tui.approval_menu import (
    ApprovalDecision,
    SessionApprovalState,
    action_scope_for,
    apply_approval_decision,
    approval_rules_summary,
    choose_approval,
    should_auto_allow,
)
from openvegas.tui.chat_theme import (
    normalize_approval_ui,
    normalize_chat_style,
    normalize_tool_event_density,
)
from openvegas.tui.chat_renderer import (
    render_assistant,
    render_status_bar,
    render_topup_hint,
    render_tool_event,
    render_tool_result,
    render_user_input,
)
from openvegas.tui.avatar_state import map_lifecycle_event_to_state, map_tool_event_to_avatar_state
from openvegas.tui.dealer_panel import DealerPanel
from openvegas.tui.confetti import render_result_panel
from openvegas.tui.diff_reviewer import (
    ParsedUnifiedPatch,
    filter_patch_by_accepted_hunks as filter_patch_by_accepted_hunks_terminal,
    filtered_patch_footprint,
    is_valid_filtered_patch as is_valid_filtered_patch_terminal,
    parse_unified_patch as parse_unified_patch_terminal,
    review_patch_terminal,
)
from openvegas.tui.hints import verify_hint_for_result
from openvegas.tui.tool_event_renderer import describe_tool_action
from openvegas.tui.voice_button import VoiceButton
from openvegas.tui.icons import mic_icon

console = Console()

SUPPORTED_TOOL_NAMES = {
    "fs_list",
    "fs_read",
    "fs_search",
    "fs_apply_patch",
    "shell_run",
    "editor_open",
    "mcp_call",
}

EXTERNAL_TOOL_ALIASES = {
    "read": "fs_read",
    "search": "fs_search",
    "write": "fs_apply_patch",
    "findandreplace": "fs_apply_patch",
    "find_and_replace": "fs_apply_patch",
    "single_find_and_replace": "fs_apply_patch",
    "insertatend": "fs_apply_patch",
    "insert_at_end": "fs_apply_patch",
    "append_to_end": "fs_apply_patch",
    "bash": "shell_run",
    "list": "fs_list",
    "mcp": "mcp_call",
    "mcp_call": "mcp_call",
}
CANONICAL_EXTERNAL_TOOL_NAMES = tuple(sorted(EXTERNAL_TOOL_ALIASES.keys()))

FIND_REPLACE_TOOL_TOKENS = {
    "findandreplace",
    "find_and_replace",
    "single_find_and_replace",
}
INSERT_AT_END_TOOL_TOKENS = {
    "insertatend",
    "insert_at_end",
    "append_to_end",
}

RETRYABLE_MUTATION_ERRORS = {
    "stale_projection",
    "active_mutation_in_progress",
    "idempotency_conflict",
}
_VSCODE_DIFF_PROMPTED = False
_ENV_DEFAULTS_BOOTSTRAPPED = False
_FILE_SCAN_CACHE: dict[str, tuple[float, list[Path]]] = {}
_FILE_SCAN_CACHE_TTL_SEC = 2.5
_FILE_SCAN_CACHE_MAX_ENTRIES = max(8, int(os.getenv("OPENVEGAS_FILE_SCAN_CACHE_MAX_ENTRIES", "64")))


class LoopAction(str, Enum):
    FINALIZED = "finalized"
    CONTINUE = "continue"
    INTERCEPT = "intercept"


@dataclass
class ToolLoopState:
    tool_observations: list[dict[str, Any]] = field(default_factory=list)
    executed_tool_calls: dict[str, int] = field(default_factory=dict)
    streamed_tools_seen: dict[str, bool] = field(default_factory=dict)
    completion_criteria: CompletionCriteria | None = None
    pending_retry_tool_req: dict[str, Any] | None = None
    bridge_caps: dict[str, bool] = field(default_factory=lambda: {"connected": False, "show_diff": False})
    progress_fingerprint_prev: str | None = None
    unchanged_progress_iters: int = 0
    mutation_not_observed_iters: int = 0
    repeated_patch_failures: dict[str, int] = field(default_factory=dict)
    post_finalize_intercept_attempted: bool = False
    active_mutation_timeout_hit: bool = False
    active_mutation_observation_changed: bool = False


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_async(coro):
    """Run an async function from sync Click context."""
    return asyncio.run(coro)


def _is_simulated_checkout_url(url: str) -> bool:
    try:
        return urlparse(str(url or "")).netloc.lower() == "checkout.openvegas.local"
    except Exception:
        return False


def _drain_stdin_buffer(window_ms: int = 0) -> list[str]:
    """Drain unread stdin bytes that are already buffered.

    This prevents multiline paste leftovers from being consumed by the
    subsequent approval selector prompt.
    """
    if window_ms < 0:
        window_ms = 0
    try:
        if not sys.stdin.isatty():
            return []
        fd = sys.stdin.fileno()
    except Exception:
        return []

    deadline = time.monotonic() + (window_ms / 1000.0)
    chunks: list[bytes] = []
    while True:
        timeout = max(0.0, deadline - time.monotonic())
        if timeout <= 0.0 and window_ms > 0 and chunks:
            break
        if timeout <= 0.0 and window_ms == 0:
            timeout = 0.0
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except Exception:
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if window_ms == 0:
            # Immediate drain only for currently buffered bytes.
            continue
    if not chunks:
        return []
    text = b"".join(chunks).decode("utf-8", errors="ignore")
    return [ln.rstrip("\r") for ln in text.splitlines() if ln.rstrip("\r")]


def _load_openvegas_env_defaults_from_dotenv() -> None:
    """Load OPENVEGAS_* defaults from local .env without overriding exported env."""
    global _ENV_DEFAULTS_BOOTSTRAPPED
    if _ENV_DEFAULTS_BOOTSTRAPPED:
        return
    _ENV_DEFAULTS_BOOTSTRAPPED = True

    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    loaded: set[str] = set()
    for env_path in candidates:
        try:
            key = str(env_path.resolve())
        except Exception:
            key = str(env_path)
        if key in loaded:
            continue
        loaded.add(key)
        if not env_path.exists() or not env_path.is_file():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            env_name = name.strip()
            if not env_name.startswith("OPENVEGAS_"):
                continue
            if env_name in os.environ:
                continue
            os.environ[env_name] = value.strip().strip("'\"")


def _win_always_enabled() -> bool:
    raw = str(os.getenv("OPENVEGAS_WIN_ALWAYS", "")).strip()
    if raw:
        return raw.lower() in {"1", "true", "yes", "y", "on"}
    return str(os.getenv("OPENVEGAS_DEMO_ALWAYS_WIN_ENABLED", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _path_hint_candidates(msg: str) -> list[str]:
    text = (msg or "").strip()
    if not text:
        return []

    def _clean_candidate(raw: str) -> str:
        token = str(raw or "").strip()
        token = token.strip("`'\"")
        token = token.lstrip("([{")
        token = token.rstrip(":;,)]}!?")
        token = token.rstrip(".")
        return token.strip()

    def _looks_like_file_path(token: str) -> bool:
        t = str(token or "").strip()
        if not t:
            return False
        if "/" in t or t.startswith("."):
            return True
        return bool(re.search(r"\.[A-Za-z0-9_]{1,8}$", Path(t).name))

    candidates: list[str] = []
    for pat in (
        r'"([^"\n]+)"',
        r"'([^'\n]+)'",
        r"`([^`\n]+)`",
    ):
        for m in re.finditer(pat, text):
            token = _clean_candidate(m.group(1))
            if token and _looks_like_file_path(token):
                candidates.append(token)

    for candidate in re.findall(r"(/[^\s\"'`]+)", text):
        cleaned = _clean_candidate(candidate)
        if cleaned:
            candidates.append(cleaned)

    for token in re.findall(r"(?<!\w)([A-Za-z0-9_.\-/]+)(?!\w)", text):
        t = _clean_candidate(token)
        if not t:
            continue
        if t.endswith("."):
            continue
        if "/" not in t and "." in Path(t).name and not re.search(r"\.[A-Za-z0-9_]{1,8}$", Path(t).name):
            continue
        if "/" in t or t.startswith(".") or "." in Path(t).name:
            candidates.append(t)

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        c = candidate.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _merge_chat_prompt_and_buffered_lines(first: str, extras: list[str]) -> str:
    base = str(first or "").strip()
    if not extras:
        return base
    cleaned: list[str] = []
    for line in extras:
        token = str(line or "").strip()
        if not token:
            continue
        if token == base:
            continue
        if token.lower() in {"chat", "chat:"}:
            continue
        cleaned.append(token)
    if not cleaned:
        return base
    # Keep one coherent prompt line to avoid fragmented multi-line composition in TTY input.
    separator = " "
    if not base:
        return separator.join(cleaned).strip()
    return separator.join([base, *cleaned]).strip()


def _coalesce_prompt_text(raw: str) -> str:
    """Collapse multiline/pasted prompt text into one coherent chat turn."""
    text = str(raw or "")
    if not text:
        return ""
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    if not lines:
        return text.strip()
    return _merge_chat_prompt_and_buffered_lines(lines[0], lines[1:])


def _replace_nonbreaking_spaces(text: str) -> str:
    return str(text or "").replace("\u202f", " ").replace("\xa0", " ")


def _coalesce_live_prompt_text(raw: str) -> str:
    """Coalesce pasted multiline input while preserving single-line typing whitespace."""
    text = _replace_nonbreaking_spaces(str(raw or ""))
    if not text:
        return ""
    if "\n" not in text and "\r" not in text:
        return text
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    if not lines:
        return ""
    return _merge_chat_prompt_and_buffered_lines(lines[0], lines[1:])



def _insert_or_queue_voice_transcript(
    *,
    transcript: str,
    chat_prompt_session: Any | None,
    prompt_active: bool,
    pending_prefill: str | None,
) -> tuple[str | None, str, int]:
    token = str(transcript or "").strip()
    if not token:
        return pending_prefill, "none", 0

    if prompt_active and chat_prompt_session is not None:
        try:
            buf = chat_prompt_session.default_buffer
            current = str(getattr(buf, "text", "") or "")
            cursor = int(getattr(buf, "cursor_position", 0) or 0)
            cursor = max(0, min(len(current), cursor))
            prefix = current[:cursor]
            suffix = current[cursor:]
            needs_space = bool(prefix) and not prefix.endswith((" ", "\n", "\t"))
            inserted = f" {token}" if needs_space else token
            buf.text = f"{prefix}{inserted}{suffix}"
            buf.cursor_position = len(prefix) + len(inserted)
            app = getattr(chat_prompt_session, "app", None)
            if app is not None and hasattr(app, "invalidate"):
                app.invalidate()
            return pending_prefill, "live", len(token)
        except Exception:
            pass

    base = str(pending_prefill or "").strip()
    merged = f"{base} {token}".strip() if base else token
    return merged, "prefill", len(token)

def _wrap_token_with_attachment_marker(text: str, token: str) -> str:
    msg = str(text or "")
    needle = _normalize_space_chars(token)
    if not msg or not needle:
        return msg
    marker = _attachment_marker(needle)
    if marker in msg:
        return msg
    pattern = re.compile(rf"(?<!\{{){re.escape(needle)}(?!\}})", flags=re.IGNORECASE)
    return pattern.sub(marker, msg)


def _normalize_live_chat_input_text(raw: str) -> str:
    """Normalize in-composer chat text and annotate file-like mentions immediately."""
    text = _coalesce_live_prompt_text(raw)
    if not text:
        return ""
    if text.lstrip().startswith("/"):
        return text
    if _has_workspace_tooling_intent(text):
        return text

    out = text
    for token in _extract_filename_like_tokens(text):
        marker_token = _pick_attachment_marker_token(token)
        out = _wrap_token_with_attachment_marker(out, marker_token)
    for stem in _extract_screenshot_stems(text):
        out = _wrap_token_with_attachment_marker(out, stem)
    return out


def _pick_attachment_marker_token(token: str) -> str:
    raw = _normalize_space_chars(token)
    pieces = _split_compound_attachment_token(raw)
    if not pieces:
        return raw
    ext_pat = re.compile(
        r"\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?)$",
        flags=re.IGNORECASE,
    )
    candidates = [p for p in pieces if ext_pat.search(str(p).strip())]
    if not candidates:
        return raw

    first_word_stop = {"in", "s", "what", "this", "these", "that", "and", "or"}
    first_word_stop.update({"can", "you", "please", "tell", "show", "review", "check", "look", "see"})

    def _first_word(value: str) -> str:
        words = re.findall(r"[A-Za-z0-9_.-]+", str(value))
        if not words:
            return ""
        return words[0].lower()

    preferred = [c for c in candidates if _first_word(c) not in first_word_stop]
    pool = preferred or candidates
    pool.sort(key=lambda c: (len(re.findall(r"[A-Za-z0-9_.-]+", c)), len(c)), reverse=True)
    return str(pool[0]).strip()


def _path_hint_from_message(msg: str) -> str | None:
    candidates = _path_hint_candidates(msg)
    if not candidates:
        return None

    fallback_nonexistent: str | None = None
    for c in candidates:
        p = Path(c)
        if p.exists():
            return str(p)
        if not p.is_absolute():
            local = Path.cwd() / p
            if local.exists():
                return str(p)
        # Keep a deterministic fallback even when file does not exist yet.
        if fallback_nonexistent is None and ("/" in c or c.startswith(".") or "." in p.name):
            fallback_nonexistent = c
    return fallback_nonexistent


def _path_hints_from_message(msg: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for c in _path_hint_candidates(msg):
        p = Path(c)
        if p.exists():
            key = str(p.resolve())
        else:
            key = c
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    if len(out) <= 1:
        return out

    existing: list[str] = []
    for c in out:
        p = Path(c)
        if p.exists():
            existing.append(c)
            continue
        if not p.is_absolute():
            local = Path.cwd() / p
            if local.exists():
                existing.append(c)

    if len(existing) == 1:
        chosen = existing[0]
        chosen_name = Path(chosen).name.lower()
        chosen_path = str(Path(chosen)).replace("\\", "/").lower()
        collapsible = True
        for c in out:
            if c == chosen:
                continue
            token = str(c).strip().replace("\\", "/")
            token_l = token.lower()
            token_name = Path(token).name.lower()
            if token_name == chosen_name:
                continue
            token_suffix = token_l.lstrip("./")
            if token_suffix.startswith("/"):
                token_suffix = token_suffix[1:]
            if chosen_path.endswith("/" + token_suffix):
                continue
            collapsible = False
            break
        if collapsible:
            return [chosen]
    return out


def _search_pattern_hint_from_message(msg: str) -> str | None:
    text = (msg or "").strip()
    if not text:
        return None

    for pat in (
        r'"([^"\n]+)"',
        r"'([^'\n]+)'",
        r"`([^`\n]+)`",
        r"“([^”\n]+)”",
        r"‘([^’\n]+)’",
    ):
        m = re.search(pat, text)
        if m and m.group(1).strip():
            return m.group(1).strip()

    m = re.search(
        r"\b(?:search|find|grep|look\s+for)\b(?:\s+for)?\s+([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        raw = m.group(1).strip()
        raw = re.split(
            r"\b(?:across|in|within|under|inside|from|on)\b",
            raw,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" :;,.")
        if raw:
            return raw

    stop = {
        "search",
        "find",
        "grep",
        "across",
        "within",
        "repo",
        "repository",
        "codebase",
        "summarize",
        "where",
        "used",
        "talked",
        "about",
        "file",
        "this",
        "here",
        "what",
        "is",
        "the",
        "and",
    }
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.:-]{2,}", text):
        t = token.strip(".,:;")
        low = t.lower()
        if low in stop:
            continue
        if t.startswith("/"):
            continue
        return t
    return None


def _shell_command_hint_from_message(msg: str) -> str | None:
    text = (msg or "").strip()
    if not text:
        return None

    for pat in (r'"([^"\n]+)"', r"'([^'\n]+)'", r"`([^`\n]+)`"):
        m = re.search(pat, text)
        if m and m.group(1).strip():
            return m.group(1).strip()

    m = re.search(
        r"\b(?:run|execute)\s+(?:a\s+)?shell\s+command\b[:\s]+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        raw = m.group(1).strip()
        raw = re.split(r"\b(and|then)\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip(" :;,.")
        if raw:
            return raw
    return None


def _rewrite_shell_command_for_env(command: str) -> tuple[str, str | None]:
    cmd = str(command or "").strip()
    if not cmd:
        return cmd, None
    if shutil.which("rg") is None and re.match(r"^\s*rg\b", cmd):
        rewritten = re.sub(r"^\s*rg\b", "grep -R -n", cmd, count=1)
        return rewritten, "rg_unavailable_rewritten_to_grep"
    return cmd, None


def _is_scrape_request(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    return "scrape" in msg or "scraping" in msg


def _is_scrape_refusal_text(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    markers = (
        "can't help scrape",
        "can’t help scrape",
        "cannot help scrape",
        "can't scrape",
        "can’t scrape",
        "bypass site restrictions",
        "bypass access controls",
    )
    return any(token in msg for token in markers)


def _rewrite_lookup_request_for_safe_web_search(text: str) -> str:
    msg = str(text or "").strip()
    if not msg:
        return msg
    rewritten = re.sub(r"\bscrap(?:e|ing)\b", "find", msg, flags=re.IGNORECASE)
    return (
        f"{rewritten}\n\n"
        "Interpretation: use lawful web search on publicly accessible pages and authorized sources; "
        "do not bypass restrictions."
    ).strip()


def _is_noncode_asset_reference(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    return bool(
        re.search(
            r"\b[\w.\-]+\.(pdf|png|jpg|jpeg|gif|webp|svg|heic|bmp|tiff|doc|docx|ppt|pptx|xls|xlsx)\b",
            msg,
            flags=re.IGNORECASE,
        )
    )


def _has_local_path_syntax(text: str) -> bool:
    msg = str(text or "")
    return bool(re.search(r"(^|\s)(/|\./|\.\./)", msg))


def _has_workspace_action_verb(text: str) -> bool:
    msg = str(text or "").lower()
    verbs = ("open", "read", "edit", "patch", "update", "search", "grep", "list", "find")
    return any(re.search(rf"\b{re.escape(v)}\b", msg) for v in verbs)


def _has_code_filename_reference(text: str) -> bool:
    msg = str(text or "")
    return bool(
        re.search(
            r"\b[\w.\-]+\.(py|ts|tsx|js|jsx|md|json|yaml|yml|sql|toml|txt|rs|go|java|kt|cpp|c|h)\b",
            msg,
            flags=re.IGNORECASE,
        )
    )


def _has_workspace_tooling_intent(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    if _has_patch_intent(msg):
        return True
    if _is_noncode_asset_reference(msg) and not _has_patch_intent(msg):
        return False

    workspace_markers = (
        "codebase",
        "repository",
        "repo",
        "workspace",
        "project files",
        "source code",
        "in this project",
        "in this repo",
        "search code",
        "list files",
        "run tests",
        "pytest",
        "grep",
        "ripgrep",
        "open file",
        "read file",
    )
    if any(token in msg for token in workspace_markers):
        return True

    # Filename mentions only imply workspace intent when combined with local path syntax or action verbs.
    if _has_code_filename_reference(msg) and (_has_local_path_syntax(msg) or _has_workspace_action_verb(msg)):
        return True
    return False


def _extract_inline_file_mentions(text: str, *, workspace_root: str) -> list[str]:
    msg = str(text or "")
    if not msg.strip():
        return []
    candidates = re.findall(r"[\w./\\ -]+\.[A-Za-z0-9]{2,6}", msg)
    if not candidates:
        return []
    out: list[str] = []
    seen: set[str] = set()
    root = Path(workspace_root).resolve()
    for raw in candidates:
        token = str(raw or "").strip().strip("\"'`")
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        path = Path(token).expanduser()
        if not path.is_absolute():
            path = (root / path).resolve()
        try:
            if path.exists() and path.is_file():
                out.append(path.name)
        except Exception:
            continue
        if len(out) >= 5:
            break
    return out


def _normalize_space_chars(text: str) -> str:
    return str(text or "").replace("\u202f", " ").replace("\xa0", " ").strip()


def _attachment_search_roots(workspace_root: str) -> list[Path]:
    out: list[Path] = []
    candidates: list[Path] = [
        Path(workspace_root).resolve(),
        Path.cwd().resolve(),
    ]
    candidates.extend(_quick_attachment_dirs())

    # Optional legacy broad scope (explicit opt-in only).
    include_home_scan = str(os.getenv("OPENVEGAS_CHAT_ATTACH_SEARCH_HOME", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if include_home_scan:
        candidates.append(Path.home())

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved.exists() and resolved not in out:
            out.append(resolved)
    return out


def _attachment_sensitive_block_prefixes() -> list[Path]:
    raw = str(
        os.getenv(
            "OPENVEGAS_CHAT_ATTACH_BLOCK_PATH_PREFIXES",
            "/etc,/proc,/sys,/dev,/private/etc,/private/var/run,/var/run,~/Library/Keychains",
        )
    ).strip()
    out: list[Path] = []
    for token in [p.strip() for p in raw.split(",") if p.strip()]:
        try:
            resolved = Path(token).expanduser().resolve()
        except Exception:
            continue
        out.append(resolved)
    return out


def _attachment_path_allowed(path: Path) -> bool:
    raw = str(os.getenv("OPENVEGAS_CHAT_ATTACH_BLOCK_SENSITIVE", "1")).strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return True
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for blocked in _attachment_sensitive_block_prefixes():
        if resolved == blocked or blocked in resolved.parents:
            return False
    return True


def _candidate_search_roots(workspace_root: str) -> list[Path]:
    # Backward-compatible alias.
    return _attachment_search_roots(workspace_root)


def _quick_attachment_dirs() -> list[Path]:
    out: list[Path] = []
    for candidate in [
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path.home() / "Documents",
    ]:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved.exists() and resolved not in out:
            out.append(resolved)
    return out


def _set_file_scan_cache(cache_key: str, now_mono: float, files: list[Path]) -> None:
    _FILE_SCAN_CACHE[cache_key] = (now_mono, list(files))
    if len(_FILE_SCAN_CACHE) <= _FILE_SCAN_CACHE_MAX_ENTRIES:
        return
    # Keep freshest entries only; dict preserves insertion order.
    stale_count = len(_FILE_SCAN_CACHE) - _FILE_SCAN_CACHE_MAX_ENTRIES
    for key in list(_FILE_SCAN_CACHE.keys())[:stale_count]:
        _FILE_SCAN_CACHE.pop(key, None)


def _iter_files_limited(root: Path, *, max_depth: int = 2, max_files: int = 3000) -> list[Path]:
    """Return a bounded recursive file listing under root for fuzzy attachment lookup."""
    cache_key = f"{root.resolve()}::{int(max_depth)}::{int(max_files)}"
    now_mono = time.monotonic()
    cached = _FILE_SCAN_CACHE.get(cache_key)
    if cached and (now_mono - cached[0]) <= _FILE_SCAN_CACHE_TTL_SEC:
        return list(cached[1])

    out: list[Path] = []
    try:
        root_resolved = root.resolve()
        base_depth = len(root_resolved.parts)
    except Exception:
        return out

    ignored_dirs = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".idea",
    }
    try:
        for dirpath, dirnames, filenames in os.walk(root_resolved, topdown=True):
            current_dir = Path(dirpath)
            depth = len(current_dir.parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in ignored_dirs]

            for filename in filenames:
                if len(out) >= max_files:
                    break
                try:
                    out.append((current_dir / filename).resolve())
                except Exception:
                    continue
            if len(out) >= max_files:
                break
    except Exception:
        return out
    _set_file_scan_cache(cache_key, now_mono, out)
    return out


def _split_compound_attachment_token(token: str) -> list[str]:
    raw = _normalize_space_chars(token)
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _push(value: str) -> None:
        v = _normalize_space_chars(value).strip("\"'`").strip(" ,;:.!?")
        key = v.lower()
        if not v or key in seen:
            return
        seen.add(key)
        out.append(v)

    _push(raw)
    ext_match = re.search(
        r"(\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?))$",
        raw,
        flags=re.IGNORECASE,
    )
    if ext_match:
        ext = ext_match.group(1)
        stem_words = re.findall(r"[A-Za-z0-9_.-]+", raw[: -len(ext)])
        max_suffix_words = min(6, len(stem_words))
        for count in range(1, max_suffix_words + 1):
            _push(" ".join(stem_words[-count:]) + ext)
    for piece in re.split(r"\s+(?:and|or)\s+|[,;]", raw, flags=re.IGNORECASE):
        _push(piece)
    for match in re.findall(
        r"([A-Za-z0-9_.-]+(?:[ \t][A-Za-z0-9_.-]+){0,7}\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?))",
        raw,
        flags=re.IGNORECASE,
    ):
        _push(match)
    return out


def _resolve_attachment_token_path(token: str, *, workspace_root: str) -> str | None:
    variants = _split_compound_attachment_token(token)
    if not variants:
        return None
    roots = _attachment_search_roots(workspace_root)
    for value in variants:
        candidate_variants = [value, value.replace("\\ ", " ")]
        for candidate_value in candidate_variants:
            p = Path(candidate_value).expanduser()
            if p.is_absolute():
                try:
                    if p.exists() and p.is_file() and _attachment_path_allowed(p):
                        return str(p.resolve())
                except Exception:
                    continue
            for root in roots:
                try:
                    candidate = (root / candidate_value).resolve()
                except Exception:
                    continue
                try:
                    if candidate.exists() and candidate.is_file() and _attachment_path_allowed(candidate):
                        return str(candidate)
                except Exception:
                    continue

    # Fuzzy fallback: match by basename containment in bounded root listing.
    for raw in variants:
        raw_lc = raw.lower()
        for root in roots:
            exact_name_match: str | None = None
            for entry in _iter_files_limited(root, max_depth=2, max_files=3000):
                try:
                    name_lc = _normalize_space_chars(entry.name).lower()
                except Exception:
                    continue
                if not name_lc:
                    continue
                if name_lc == raw_lc:
                    if _attachment_path_allowed(entry):
                        exact_name_match = str(entry.resolve())
                    break
                if name_lc in raw_lc or raw_lc in name_lc:
                    if _attachment_path_allowed(entry):
                        return str(entry.resolve())
            if exact_name_match:
                return exact_name_match
    return None


def _read_clipboard_text() -> str:
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["pbpaste"],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                return str(proc.stdout or "").strip()
    except Exception:
        return ""
    return ""


def _clipboard_has_image() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        proc = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return False
        info = str(proc.stdout or "")
        return "«class PNGf»" in info or "«class TIFF»" in info or "picture" in info.lower()
    except Exception:
        return False


def _save_clipboard_image_to_file() -> str | None:
    """Best-effort clipboard-image export (macOS) modeled after Continue CLI."""
    if sys.platform != "darwin":
        return None
    timestamp = int(time.time() * 1000)
    tmp_path = Path("/tmp") / f"openvegas-clipboard-{timestamp}.png"
    script = (
        "set png_data to (the clipboard as «class PNGf»)\n"
        f'set file_ref to open for access POSIX file "{tmp_path}" with write permission\n'
        "try\n"
        "  set eof file_ref to 0\n"
        "  write png_data to file_ref\n"
        "on error errMsg number errNum\n"
        "  close access file_ref\n"
        "  error errMsg number errNum\n"
        "end try\n"
        "close access file_ref\n"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        if not tmp_path.exists() or not tmp_path.is_file():
            return None
        return str(tmp_path)
    except Exception:
        return None


def _extract_pasted_path_candidates(text: str) -> list[str]:
    msg = _normalize_space_chars(text)
    if not msg:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in msg.splitlines():
        token = _normalize_space_chars(line).strip("\"'`")
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _message_requests_attachment_analysis(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    markers = (
        "what do you see",
        "tell me what you see",
        "what's in",
        "what is in",
        "analyze this",
        "analyze these",
        "summarize this",
        "summarize these",
        "in this image",
        "in this screenshot",
        "in this pdf",
        "from this file",
        "transcribe this",
        "transcribe these",
        "speech to text",
        "audio",
        "voice note",
    )
    return any(token in msg for token in markers)


def _extract_filename_like_tokens(text: str) -> list[str]:
    msg = _normalize_space_chars(text)
    if not msg:
        return []
    out: list[str] = []
    seen: set[str] = set()
    patterns = [
        r"\{([^{}]+)\}",
        r'"([^"\n]+\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?|wav|mp3|m4a|ogg|flac|aac|webm))"',
        r"'([^'\n]+\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?|wav|mp3|m4a|ogg|flac|aac|webm))'",
        # Local paths with extension (absolute, relative, or home-prefixed).
        r"((?:~|/|\./|\.\./)[A-Za-z0-9 _./\\\-]{1,220}\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?|wav|mp3|m4a|ogg|flac|aac|webm))",
        # Basename with extension, capped token count to avoid swallowing full sentences.
        r"([A-Za-z0-9_.-]+(?:[ \t][A-Za-z0-9_.-]+){0,7}\.(?:pdf|png|jpe?g|gif|webp|svg|heic|bmp|tiff|txt|md|json|csv|docx?|pptx?|xlsx?|wav|mp3|m4a|ogg|flac|aac|webm))",
    ]
    for pat in patterns:
        for match in re.findall(pat, msg, flags=re.IGNORECASE):
            token = _normalize_space_chars(match)
            for part in _split_compound_attachment_token(token):
                token_lc = part.lower()
                if not part or token_lc in seen:
                    continue
                seen.add(token_lc)
                out.append(part)
    return out


def _extract_screenshot_stems(text: str) -> list[str]:
    msg = _normalize_space_chars(text)
    if not msg:
        return []
    pattern = r"(Screenshot\s+\d{4}-\d{2}-\d{2}\s+at\s+\d{1,2}\.\d{2}\.\d{2}(?:\s*[AP]M)?)"
    stems = [str(s).strip() for s in re.findall(pattern, msg, flags=re.IGNORECASE)]
    out: list[str] = []
    seen: set[str] = set()
    for stem in stems:
        key = stem.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(stem)
    return out


def _resolve_screenshot_stem_to_path(stem: str, *, workspace_root: str) -> str | None:
    token = _normalize_space_chars(stem)
    if not token:
        return None
    scan_roots = _attachment_search_roots(workspace_root)
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".bmp", ".tiff"}
    matches: list[str] = []
    for root in scan_roots:
        direct_hits: list[Path] = []
        try:
            direct_hits.extend(list(root.glob(f"{token}*")))
        except Exception:
            pass
        file_pool = [*direct_hits, *_iter_files_limited(root, max_depth=2, max_files=3000)]
        for path in file_pool:
            try:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in image_exts:
                    continue
                name = _normalize_space_chars(path.name)
                if token.lower() not in name.lower():
                    continue
                resolved = str(path.resolve())
                if resolved not in matches:
                    resolved = str(path.resolve())
                    matches.append(resolved)
            except Exception:
                continue
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Choose the freshest candidate when multiple screenshots share a prefix.
    matches.sort(
        key=lambda p: Path(p).stat().st_mtime if Path(p).exists() else 0.0,
        reverse=True,
    )
    return matches[0]


def _detect_auto_attach_paths(text: str, *, workspace_root: str, max_candidates: int = 5) -> tuple[list[str], list[str]]:
    resolved: list[str] = []
    unresolved: list[str] = []
    seen_paths: set[str] = set()
    for token in _extract_filename_like_tokens(text):
        path = _resolve_attachment_token_path(token, workspace_root=workspace_root)
        if path:
            if path not in seen_paths:
                seen_paths.add(path)
                resolved.append(path)
        else:
            unresolved.append(token)
        if len(resolved) >= max_candidates:
            break
    if len(resolved) < max_candidates:
        for stem in _extract_screenshot_stems(text):
            path = _resolve_screenshot_stem_to_path(stem, workspace_root=workspace_root)
            if path and path not in seen_paths:
                seen_paths.add(path)
                resolved.append(path)
            elif not path:
                unresolved.append(stem)
            if len(resolved) >= max_candidates:
                break
    uniq_unresolved: list[str] = []
    seen_unresolved: set[str] = set()
    for token in unresolved:
        key = _normalize_space_chars(token).lower()
        if key and key not in seen_unresolved:
            seen_unresolved.add(key)
            uniq_unresolved.append(token)
    return resolved, uniq_unresolved[:max_candidates]


async def _detect_auto_attach_paths_with_deadline(
    text: str,
    *,
    workspace_root: str,
    max_candidates: int = 5,
    deadline_ms: int = 500,
) -> tuple[list[str], list[str], bool]:
    clamped_ms = max(100, int(deadline_ms))
    try:
        paths, unresolved = await asyncio.wait_for(
            asyncio.to_thread(
                _detect_auto_attach_paths,
                text,
                workspace_root=workspace_root,
                max_candidates=max_candidates,
            ),
            timeout=clamped_ms / 1000.0,
        )
        return paths, unresolved, False
    except asyncio.TimeoutError:
        _tool_debug(f"auto-attach search exceeded {clamped_ms}ms; skipping")
        emit_metric("chat_attachment_resolve_timeout_total", {"deadline_ms": clamped_ms})
        unresolved = _extract_filename_like_tokens(text)[:max_candidates]
        return [], unresolved, True


def _has_web_request_signal(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    if re.search(r"https?://", msg):
        return True
    markers = (
        "web",
        "online",
        "internet",
        "search",
        "find",
        "look up",
        "latest",
        "current",
        "today",
    )
    return any(token in msg for token in markers)


def _is_local_attachment_analysis_request(text: str) -> bool:
    msg = str(text or "").strip().lower()
    if not msg:
        return False
    analysis_markers = (
        "what do you see in",
        "summarize",
        "analyze",
        "extract",
        "transcribe",
        "speech to text",
        "audio",
        "voice note",
        "this pdf",
        "this image",
        "screenshot",
        "attachment",
        "file",
    )
    return any(token in msg for token in analysis_markers)


def _should_enable_web_search_for_turn(text: str, *, has_uploaded_attachments: bool) -> bool:
    if not str(text or "").strip():
        return False
    if _has_workspace_tooling_intent(text):
        return False
    if has_uploaded_attachments and _is_local_attachment_analysis_request(text) and not _has_web_request_signal(text):
        return False
    return True


def _augment_web_search_prompt(text: str) -> str:
    msg = str(text or "").strip()
    if not msg:
        return msg
    lower = msg.lower()
    structured_result_verbs = ("find", "search", "look up", "compare", "list")
    if any(token in lower for token in structured_result_verbs):
        return (
            f"{msg}\n\n"
            "When using web search, prefer original source pages over aggregator landing pages. "
            "For each distinct result, include a source URL. "
            "Mark stale or unavailable pages explicitly."
        ).strip()
    return msg


def _parse_mcp_call_command(message: str) -> tuple[str, str, dict[str, Any], str | None]:
    raw = str(message or "").strip()
    prefix = "/mcp call"
    if not raw.lower().startswith(prefix):
        return "", "", {}, "usage: /mcp call <server_id> <tool> [json_args|k=v ...]"
    rest = raw[len(prefix):].strip()
    if not rest:
        return "", "", {}, "usage: /mcp call <server_id> <tool> [json_args|k=v ...]"

    parts = rest.split(maxsplit=2)
    if len(parts) < 2:
        return "", "", {}, "usage: /mcp call <server_id> <tool> [json_args|k=v ...]"

    server_id = str(parts[0] or "").strip()
    tool_name = str(parts[1] or "").strip()
    if not server_id or not tool_name:
        return "", "", {}, "usage: /mcp call <server_id> <tool> [json_args|k=v ...]"

    if len(parts) <= 2:
        return server_id, tool_name, {}, None

    args_raw = str(parts[2] or "").strip()
    if not args_raw:
        return server_id, tool_name, {}, None

    if args_raw.startswith("{"):
        try:
            parsed = json.loads(args_raw)
        except Exception:
            return "", "", {}, "invalid JSON args; expected object"
        if not isinstance(parsed, dict):
            return "", "", {}, "invalid JSON args; expected object"
        return server_id, tool_name, parsed, None

    out: dict[str, Any] = {}
    for token in shlex.split(args_raw):
        if "=" not in token:
            return "", "", {}, "invalid args; use JSON object or key=value pairs"
        key, value = token.split("=", 1)
        key = str(key or "").strip()
        if not key:
            return "", "", {}, "invalid args; empty key"
        val = value.strip()
        if val.lower() in {"true", "false"}:
            coerced: Any = val.lower() == "true"
        else:
            try:
                coerced = int(val)
            except Exception:
                try:
                    coerced = float(val)
                except Exception:
                    coerced = val
        out[key] = coerced
    return server_id, tool_name, out, None


def _coerce_nonempty_text(v: Any) -> str | None:
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return None


def _coerce_nonempty_text_preserve(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v
    return None


def _mutation_retry_backoff_sec(error_code: str, attempt: int) -> float:
    code = str(error_code or "")
    idx = max(0, int(attempt))
    if code == "stale_projection":
        return 0.0
    if code == "active_mutation_in_progress":
        return min(0.5, 0.12 * (idx + 1))
    if code == "idempotency_conflict":
        return min(0.4, 0.08 * (idx + 1))
    return 0.0


def _safe_workspace_resolve(workspace_root: str, raw_path: str) -> Path | None:
    root = Path(workspace_root).resolve()
    p = Path(raw_path)
    target = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if target == root or root in target.parents:
        return target
    return None


def _synthesize_patch_from_arguments(workspace_root: str, arguments: dict[str, Any]) -> str | None:
    path_val = None
    for key in ("path", "file_path", "filepath", "file", "target_path"):
        candidate = arguments.get(key)
        if isinstance(candidate, str) and candidate.strip():
            path_val = candidate.strip()
            break
        if isinstance(candidate, dict):
            nested = candidate.get("path")
            if isinstance(nested, str) and nested.strip():
                path_val = nested.strip()
                break
    if not path_val:
        return None

    new_text = None
    for key in ("new_content", "content", "after", "replacement", "updated_content", "text"):
        candidate = arguments.get(key)
        c = _coerce_nonempty_text(candidate)
        if c is not None:
            new_text = c
            break
    if new_text is None:
        return None

    target = _safe_workspace_resolve(workspace_root, path_val)
    if target is None:
        return None

    old_text: str = ""
    for key in ("old_content", "before", "original_content"):
        candidate = arguments.get(key)
        c = _coerce_nonempty_text(candidate)
        if c is not None:
            old_text = c
            break
    else:
        if target.exists() and target.is_file():
            try:
                old_text = target.read_text(encoding="utf-8")
            except Exception:
                return None

    root = Path(workspace_root).resolve()
    rel = str(target.relative_to(root))
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=rel,
            tofile=rel,
            lineterm="",
        )
    )
    if not diff_lines:
        return None
    patch = "".join(line + ("\n" if not line.endswith("\n") else "") for line in diff_lines)
    return patch


def _canonical_tool_name(name: str) -> str:
    token = (name or "").strip().lower().replace("-", "_")
    aliases = {
        **EXTERNAL_TOOL_ALIASES,
        "read_file": "fs_read",
        "file_read": "fs_read",
        "cat_file": "fs_read",
        "list_files": "fs_list",
        "ls": "fs_list",
        "find_files": "fs_list",
        "grep": "fs_search",
        "search_code": "fs_search",
        "ripgrep": "fs_search",
        "rg_search": "fs_search",
        "create_file": "fs_apply_patch",
        "fs_create": "fs_apply_patch",
        "write_file": "fs_apply_patch",
        "file_write": "fs_apply_patch",
        "fs_write": "fs_apply_patch",
        "edit_file": "fs_apply_patch",
        "run_command": "shell_run",
        "execute_command": "shell_run",
        "terminal_run": "shell_run",
        "open_file": "editor_open",
        "mcp_tool_call": "mcp_call",
    }
    return aliases.get(token, token)


def _tool_abi_mode() -> str:
    mode = os.getenv("OPENVEGAS_TOOL_ABI_MODE", "compat").strip().lower()
    if mode in {"strict", "compat"}:
        return mode
    return "compat"


def _terminal_diff_fallback_enabled() -> bool:
    raw = str(os.getenv("OPENVEGAS_TERMINAL_DIFF_FALLBACK", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _safe_rel_from_workspace(workspace_root: str, target: Path) -> str:
    root = Path(workspace_root).resolve()
    try:
        return str(target.relative_to(root))
    except Exception:
        return str(target)


def _read_existing_text_for_write(target: Path) -> tuple[str | None, str | None]:
    try:
        raw = target.read_bytes()
    except Exception as e:
        return None, f"Unable to read existing file: {e}"
    if b"\x00" in raw[:4096]:
        return None, "Existing file appears binary and cannot be rewritten via Write."
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "Existing file is not UTF-8 text and cannot be rewritten via Write."


def _build_unified_patch(*, old_text: str, new_text: str, rel_path: str) -> str | None:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=rel_path,
            tofile=rel_path,
            lineterm="",
        )
    )
    if not diff_lines:
        return None
    return "".join(line + ("\n" if not line.endswith("\n") else "") for line in diff_lines)


def _has_explicit_replace_wording(msg: str) -> bool:
    text = (msg or "").lower()
    if not text.strip():
        return False
    patch_with_file = bool(
        re.search(
            r"\bpatch\s+(?:`[^`]+`|'[^']+'|\"[^\"]+\"|[a-z0-9_./-]+)\s+with\b",
            text,
        )
    )
    return bool(
        re.search(
            r"\b(?:replace\s+all|replace\s+entire|replace\s+whole|rewrite\s+entire|rewrite\s+whole|overwrite|full\s+rewrite|replace\s+the\s+file)\b",
            text,
        )
    ) or patch_with_file


def _allow_full_replace_from_edit_intent(msg: str) -> bool:
    """Allow deterministic full-file replace when user clearly asked for an edit.

    This keeps strict explicit-replace wording support, while also allowing
    practical edit prompts that target a file directly or refer to "this file".
    """
    text = (msg or "").strip()
    if not text:
        return False
    if _has_explicit_replace_wording(text):
        return True
    lowered = text.lower()
    if not _has_patch_intent(lowered):
        return False
    if _has_explicit_file_target(lowered):
        return True
    if re.search(r"\b(?:this|the)\s+file\b", lowered):
        return True
    return False


def _has_explicit_replace_intent_from_arguments(arguments: dict[str, Any]) -> bool:
    raw_mode = str(arguments.get("write_mode") or arguments.get("mode") or "").strip().lower()
    operation_kind = str(arguments.get("operation_kind") or "").strip().lower()
    explicit_flag = arguments.get("explicit_replace_intent")
    if isinstance(explicit_flag, bool) and explicit_flag:
        return True
    if isinstance(arguments.get("replace_all"), bool) and bool(arguments.get("replace_all")):
        return True
    if raw_mode in {"replace", "full_replace", "replace_all"}:
        return True
    if operation_kind in {"full_replace", "replace_all"}:
        return True
    return False


def _validate_patch_safety(*, old_text: str, new_text: str, intent: str) -> tuple[bool, str | None]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    if intent == "append":
        old_norm = old_text.rstrip("\n")
        new_norm = new_text.rstrip("\n")
        if not new_norm.startswith(old_norm):
            return False, "append_intent_but_existing_content_modified"

    # NOTE: This is intentionally a coarse v1 guardrail heuristic, not semantic diff correctness.
    if intent in {"find_replace", "insert_before", "insert_after", "rewrite_section"}:
        deleted_count = sum(1 for line in old_lines if line not in new_lines)
        if len(old_lines) > 10 and deleted_count > (len(old_lines) * 0.5):
            return False, "targeted_edit_but_majority_replacement"

    if old_lines and not new_text.strip():
        return False, "edit_would_empty_file"

    return True, None


def _emit_intent_validator_result(*, intent: str, reason: str | None = None) -> None:
    token = str(intent or "unknown").strip() or "unknown"
    if reason is None:
        emit_metric("intent_validator_pass_total", {"intent": token})
        return

    r = str(reason or "unknown").strip() or "unknown"
    emit_metric("intent_validator_block_total", {"intent": token, "reason": r})
    if r == "append_intent_but_existing_content_modified":
        emit_metric("intent_append_rejected_replace_like_patch_total", {"intent": token})
    if r == "targeted_edit_but_majority_replacement":
        emit_metric("intent_replace_large_deletion_blocked_total", {"intent": token})
        if token == "append":
            emit_metric("intent_append_large_deletion_blocked_total", {"intent": token})
    if r == "intent_anchor_not_found":
        emit_metric("intent_anchor_not_found_total", {"intent": token})


def _find_all_exact_matches(haystack: str, needle: str) -> list[tuple[int, int]]:
    if needle == "":
        return []
    matches: list[tuple[int, int]] = []
    pos = 0
    while True:
        idx = haystack.find(needle, pos)
        if idx < 0:
            break
        matches.append((idx, idx + len(needle)))
        pos = idx + len(needle)
    return matches


def _replace_exact_matches(
    *,
    text: str,
    matches: list[tuple[int, int]],
    replacement: str,
    replace_all: bool,
) -> str:
    if not matches:
        return text
    if replace_all:
        out = text
        for start, end in reversed(matches):
            out = out[:start] + replacement + out[end:]
        return out
    start, end = matches[0]
    return text[:start] + replacement + text[end:]


def _resolve_and_read_target(
    *,
    workspace_root: str,
    path: str,
    tool_label: str,
    require_existing: bool,
) -> tuple[Path | None, str, bool, dict[str, Any] | None]:
    target = _safe_workspace_resolve(workspace_root, path)
    if target is None:
        return None, "", False, {
            "status": "blocked",
            "error": "workspace_path_out_of_bounds",
            "detail": f"{tool_label} target is outside workspace root.",
        }

    exists = target.exists()
    if exists and not target.is_file():
        return target, "", exists, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": f"{tool_label} target must be a file.",
        }
    if require_existing and (not exists or not target.is_file()):
        return target, "", exists, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": f"{tool_label} target must be an existing file.",
        }

    old_text = ""
    if exists:
        loaded, err = _read_existing_text_for_write(target)
        if err is not None:
            return target, "", exists, {
                "status": "blocked",
                "error": "binary_file_unsupported",
                "detail": err,
            }
        old_text = loaded or ""
    return target, old_text, exists, None


def _prepare_find_replace_patch(
    *,
    workspace_root: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = _coerce_nonempty_text(arguments.get("filepath")) or _coerce_nonempty_text(arguments.get("path"))
    if path is None:
        path = _deep_find_keyed_string(arguments, ("filepath", "path", "file_path", "file", "target_path"))
    old_string = _coerce_nonempty_text_preserve(arguments.get("old_string"))
    if old_string is None:
        old_string = _deep_find_keyed_string(arguments, ("old_string", "find", "old", "from", "needle"))
    new_string = arguments.get("new_string")
    if not isinstance(new_string, str):
        deep_new = _deep_find_keyed_string(arguments, ("new_string", "replacement", "replace", "to", "new"))
        new_string = deep_new if isinstance(deep_new, str) else None
    replace_all = bool(arguments.get("replace_all", False))

    if not path or old_string is None or new_string is None:
        return None, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": "FindAndReplace requires filepath, old_string, and new_string.",
        }
    if old_string == "":
        return None, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": "FindAndReplace old_string must not be empty.",
        }
    if old_string == new_string:
        return None, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": "FindAndReplace old_string and new_string must differ.",
        }

    target, old_text, _exists, resolve_err = _resolve_and_read_target(
        workspace_root=workspace_root,
        path=path,
        tool_label="FindAndReplace",
        require_existing=True,
    )
    if resolve_err is not None or target is None:
        return None, resolve_err
    matches = _find_all_exact_matches(old_text, old_string)
    if not matches:
        return None, {
            "status": "blocked",
            "error": "old_string_not_found",
            "detail": "FindAndReplace old_string not found in file.",
        }
    if (not replace_all) and len(matches) > 1:
        return None, {
            "status": "blocked",
            "error": "old_string_not_unique",
            "detail": "FindAndReplace old_string matched multiple regions; use replace_all or include more context.",
        }

    new_text = _replace_exact_matches(
        text=old_text,
        matches=matches,
        replacement=new_string,
        replace_all=replace_all,
    )
    ok, reason = _validate_patch_safety(old_text=old_text, new_text=new_text, intent="find_replace")
    if not ok:
        _emit_intent_validator_result(intent="find_replace", reason=str(reason or "unknown"))
        return None, {
            "status": "blocked",
            "error": str(reason or "targeted_edit_but_majority_replacement"),
            "detail": "FindAndReplace safety validation rejected generated patch.",
        }
    _emit_intent_validator_result(intent="find_replace")

    rel_path = _safe_rel_from_workspace(workspace_root, target)
    patch = _build_unified_patch(old_text=old_text, new_text=new_text, rel_path=rel_path)
    if patch is None:
        return None, {
            "status": "noop",
            "tool_name": "fs_apply_patch",
            "error": "no_change",
            "detail": "FindAndReplace produced no changes.",
        }
    return {
        "arguments": {"patch": patch, "path": rel_path},
        "meta": {
            "source": "find_replace_abi",
            "path": rel_path,
            "existing_file": True,
            "old_contents": old_text,
            "new_contents": new_text,
            "operation_kind": "find_replace",
            "selection_basis": "exact_string",
            "replace_all": bool(replace_all),
        },
    }, None


def _prepare_insert_at_end_patch(
    *,
    workspace_root: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    def _already_at_end(existing_text: str, append_text: str) -> bool:
        if not existing_text:
            return False
        candidate = append_text.rstrip("\n")
        if not candidate:
            return False
        return existing_text.rstrip("\n").endswith(candidate)

    path = _coerce_nonempty_text(arguments.get("filepath")) or _coerce_nonempty_text(arguments.get("path"))
    if path is None:
        path = _deep_find_keyed_string(arguments, ("filepath", "path", "file_path", "file", "target_path"))
    content: str | None = None
    raw_content = arguments.get("content")
    if isinstance(raw_content, str):
        content = raw_content
    if content is None:
        content = _deep_find_keyed_string(arguments, ("content", "new_content", "text", "value"))
    if not path or content is None:
        return None, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": "InsertAtEnd requires filepath and content.",
        }

    target, old_text, exists, resolve_err = _resolve_and_read_target(
        workspace_root=workspace_root,
        path=path,
        tool_label="InsertAtEnd",
        require_existing=False,
    )
    if resolve_err is not None or target is None:
        return None, resolve_err

    if _already_at_end(old_text, content):
        return None, {
            "status": "noop",
            "tool_name": "fs_apply_patch",
            "error": "no_change",
            "detail": "InsertAtEnd content already present at end of file.",
        }

    separator = ""
    if old_text and content and (not old_text.endswith("\n")):
        separator = "\n"
    new_text = f"{old_text}{separator}{content}"
    ok, reason = _validate_patch_safety(old_text=old_text, new_text=new_text, intent="append")
    if not ok:
        _emit_intent_validator_result(intent="append", reason=str(reason or "unknown"))
        return None, {
            "status": "blocked",
            "error": str(reason or "append_intent_but_existing_content_modified"),
            "detail": "InsertAtEnd safety validation rejected generated patch.",
        }
    _emit_intent_validator_result(intent="append")

    rel_path = _safe_rel_from_workspace(workspace_root, target)
    patch = _build_unified_patch(old_text=old_text, new_text=new_text, rel_path=rel_path)
    if patch is None:
        return None, {
            "status": "noop",
            "tool_name": "fs_apply_patch",
            "error": "no_change",
            "detail": "InsertAtEnd produced no changes.",
        }
    return {
        "arguments": {"patch": patch, "path": rel_path},
        "meta": {
            "source": "insert_at_end_abi",
            "path": rel_path,
            "append_content": content,
            "existing_file": bool(exists),
            "old_contents": old_text,
            "new_contents": new_text,
            "operation_kind": "append",
            "selection_basis": "eof",
        },
    }, None


def _split_unified_patch_hunks(patch_text: str) -> tuple[list[str], list[list[str]]]:
    parsed = parse_unified_patch_terminal(patch_text)
    if parsed.parse_error:
        return [], []
    header: list[str] = []
    hunks: list[list[str]] = []
    for item in parsed.files:
        header.extend(item.prelude_lines)
        header.append(item.old_header)
        header.append(item.new_header)
        header.extend(item.metadata_lines)
        for hunk in item.hunks:
            hunks.append([hunk.header_line, *hunk.body_lines])
    return header, hunks


def _filter_patch_by_accepted_hunks(patch_text: str, accepted_hunks: set[int]) -> str | None:
    filtered_text, _ = _filter_patch_by_accepted_hunks_with_parsed(patch_text, accepted_hunks)
    return filtered_text


def _filter_patch_by_accepted_hunks_with_parsed(
    patch_text: str,
    accepted_hunks: set[int],
) -> tuple[str | None, ParsedUnifiedPatch | None]:
    return filter_patch_by_accepted_hunks_terminal(str(patch_text or ""), set(int(x) for x in accepted_hunks))


def _is_valid_filtered_patch(
    patch_text: str,
    *,
    parsed_patch: ParsedUnifiedPatch | None = None,
) -> bool:
    parsed = parsed_patch if parsed_patch is not None else parse_unified_patch_terminal(str(patch_text or ""))
    return is_valid_filtered_patch_terminal(parsed)


@dataclass(frozen=True)
class CompletionCriteria:
    required_files: tuple[str, ...] = ()
    required_headings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required_nonempty_sections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    requires_mutation: bool = False

    @property
    def active(self) -> bool:
        return bool(
            self.required_files
            or self.required_headings
            or self.required_nonempty_sections
            or self.requires_mutation
        )


@dataclass(frozen=True)
class CompletionEvaluation:
    satisfied: bool
    missing: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class PatchHunk:
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    touched_lines: int


@dataclass(frozen=True)
class PatchScope:
    targets: tuple[str, ...]
    hunks: tuple[PatchHunk, ...]
    touched_per_file: dict[str, int]
    touched_total: int
    hunk_count_per_file: dict[str, int]


@dataclass(frozen=True)
class HunkPair:
    original: PatchHunk
    regenerated: PatchHunk
    anchor_distance: int


@dataclass(frozen=True)
class HunkPairing:
    pairs: tuple[HunkPair, ...]
    deterministic: bool
    unmatched_original: int
    unmatched_regenerated: int


@dataclass(frozen=True)
class PairQuality:
    max_anchor_distance: int
    total_anchor_distance: int
    is_partial: bool


class AttachmentState(str, Enum):
    ATTACHED = "attached"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


ATTACHMENT_ALLOWED_TRANSITIONS: dict[AttachmentState, set[AttachmentState]] = {
    AttachmentState.ATTACHED: {AttachmentState.UPLOADING, AttachmentState.UNSUPPORTED, AttachmentState.FAILED},
    AttachmentState.UPLOADING: {AttachmentState.UPLOADED, AttachmentState.FAILED, AttachmentState.UNSUPPORTED},
    AttachmentState.UPLOADED: set(),
    AttachmentState.FAILED: set(),
    AttachmentState.UNSUPPORTED: set(),
}


@dataclass
class PendingAttachment:
    local_id: str
    path: str
    name: str
    mime_type: str
    size_bytes: int
    sha256: str
    state: AttachmentState = AttachmentState.ATTACHED
    remote_file_id: str | None = None
    error: str | None = None


def _can_attachment_transition(src: AttachmentState, dst: AttachmentState) -> bool:
    return dst in ATTACHMENT_ALLOWED_TRANSITIONS.get(src, set())


def _attachment_key(path: str, size_bytes: int, sha256: str) -> str:
    return f"{sha256}:{int(size_bytes)}:{str(path or '')}"


def _attachment_marker(name: str) -> str:
    return f"{{{str(name or '').strip()}}}"


def _format_composer_attachment_status_row(
    attachments: list[PendingAttachment],
    *,
    provider: str | None = None,
    model: str | None = None,
    max_markers: int = 4,
) -> str | None:
    if not attachments:
        return None
    image_count = sum(1 for att in attachments if _attachment_is_image(att))
    audio_count = sum(1 for att in attachments if _attachment_is_audio(att))
    file_count = max(0, len(attachments) - image_count - audio_count)
    parts: list[str] = []
    if image_count > 0:
        image_supported = True
        if provider and model:
            image_supported = resolve_capability(provider, model, "image_input")
        if image_supported:
            parts.append(f"🖼 {image_count} image(s)")
        else:
            parts.append(f"⚠ {image_count} image(s) unsupported")
    if audio_count > 0:
        stt_supported = True
        if provider and model:
            stt_supported = resolve_capability(provider, model, "speech_to_text")
        if stt_supported:
            parts.append(f"◉ {audio_count} audio file(s)")
        else:
            parts.append(f"⚠ {audio_count} audio file(s) unsupported")
    if file_count > 0:
        parts.append(f"📄 {file_count} file(s)")
    if not parts:
        markers = [_attachment_marker(att.name) for att in attachments[:max(1, int(max_markers))]]
        extra = len(attachments) - len(markers)
        suffix = f" +{extra}" if extra > 0 else ""
        return f"Attachments: {' '.join(markers)}{suffix}"
    return f"Attachments: {'  '.join(parts)}"


def _format_live_composer_status_row(
    *,
    draft_text: str,
    attachments: list[PendingAttachment],
    provider: str | None,
    model: str | None,
) -> str | None:
    base = _format_composer_attachment_status_row(
        attachments,
        provider=provider,
        model=model,
    )
    message = str(draft_text or "")
    if message.lstrip().startswith("/"):
        return base
    mentions = _extract_filename_like_tokens(message)
    if not mentions:
        return base
    markers = " ".join(_attachment_marker(_pick_attachment_marker_token(tok)) for tok in mentions[:2])
    if base:
        return f"{base}  |  candidates {markers}"
    return f"candidates {markers}"


def _attachment_is_image(att: PendingAttachment) -> bool:
    mime = str(att.mime_type or _sniff_mime_type(att.path)).strip().lower()
    return mime.startswith("image/")


def _attachment_is_audio(att: PendingAttachment) -> bool:
    mime = str(att.mime_type or _sniff_mime_type(att.path)).strip().lower()
    return mime.startswith("audio/")


def _preflight_filter_attachments_for_capabilities(
    pending_attachments: list[PendingAttachment],
    *,
    provider: str,
    model: str,
) -> tuple[list[PendingAttachment], int, bool]:
    if not pending_attachments:
        return list(pending_attachments), 0, False
    image_supported = resolve_capability(provider, model, "image_input")
    if image_supported:
        return list(pending_attachments), 0, False

    kept = [att for att in pending_attachments if not _attachment_is_image(att)]
    dropped = len(pending_attachments) - len(kept)
    blocked = dropped > 0 and not kept
    return kept, max(0, dropped), blocked


def _inject_attachment_markers_into_message(message: str, attachments: list[PendingAttachment]) -> str:
    text = str(message or "")
    if not text.strip() or not attachments:
        return text
    out = text
    appended: list[str] = []
    for att in attachments:
        marker = _attachment_marker(att.name)
        if marker in out:
            continue
        name_pat = re.escape(att.name)
        if re.search(name_pat, out, flags=re.IGNORECASE):
            out = re.sub(name_pat, marker, out, flags=re.IGNORECASE)
            continue
        appended.append(marker)
    if appended:
        out = f"{out} {' '.join(appended)}".strip()
    return out


def _attachment_icon(mime_type: str, *, unicode_ok: bool) -> str:
    token = str(mime_type or "").lower()
    if token.startswith("image/"):
        return "🖼" if unicode_ok else "[IMG]"
    if token.startswith("audio/"):
        return mic_icon() if unicode_ok else "[AUDIO]"
    if token in {"application/pdf"}:
        return "📄" if unicode_ok else "[FILE]"
    return "📎" if unicode_ok else "[FILE]"


def _supports_unicode_output() -> bool:
    encoding = str(getattr(sys.stdout, "encoding", "") or "").lower()
    return bool(encoding) and "utf" in encoding


def _sniff_mime_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return str(guessed or "application/octet-stream")


def _mime_matches_pattern(mime_type: str, pattern: str) -> bool:
    token = str(mime_type or "").strip().lower()
    pat = str(pattern or "").strip().lower()
    if not token or not pat:
        return False
    if pat.endswith("/*"):
        return token.startswith(pat[:-1])
    return token == pat


def _chat_allowed_mime_patterns() -> list[str]:
    raw = str(
        os.getenv(
            "OPENVEGAS_CHAT_ALLOWED_MIME",
            "text/*,image/*,audio/*,application/pdf,application/json,application/xml,application/octet-stream",
        )
    ).strip()
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _is_chat_attachment_mime_allowed(mime_type: str) -> bool:
    patterns = _chat_allowed_mime_patterns()
    if not patterns:
        return True
    return any(_mime_matches_pattern(mime_type, pat) for pat in patterns)


def _is_likely_text_mime(mime_type: str) -> bool:
    token = str(mime_type or "").lower()
    return token.startswith("text/") or token in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/x-sh",
        "application/javascript",
        "application/typescript",
        "application/sql",
        "application/x-sql",
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_attachment_preview(path: str, *, max_chars: int) -> str:
    token = str(path or "").strip()
    if not token:
        return ""
    try:
        raw = Path(token).read_bytes()
    except Exception:
        return ""
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]..."


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _normalize_diff_path(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    return token


def _extract_required_files_from_message(user_message: str) -> list[str]:
    text = (user_message or "").strip()
    if not text:
        return []

    candidates: list[str] = []
    for pat in (r"`([^`\n]+)`", r'"([^"\n]+)"', r"'([^'\n]+)'"):
        for m in re.finditer(pat, text):
            token = m.group(1).strip()
            if "/" in token or "." in Path(token).name:
                candidates.append(token)

    for token in re.findall(r"(/[^\s\"'`]+)", text):
        candidates.append(token.strip())
    for token in re.findall(r"(?<!\w)([A-Za-z0-9_.\-/]+\.[A-Za-z0-9_]+)(?!\w)", text):
        candidates.append(token.strip())

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        c = candidate.strip(" \t\r\n:;,.")
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _extract_named_sections_from_message(user_message: str) -> list[str]:
    sections = _extract_sections_from_message(user_message)
    text = user_message or ""
    for pat in (
        r"\badd\s+a\s+[\"“]([^\"”\n]+)[\"”]\s+section\b",
        r"\badd\s+a\s+`([^`\n]+)`\s+section\b",
    ):
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            token = m.group(1).strip()
            if token:
                sections.append(token)
    out: list[str] = []
    seen: set[str] = set()
    for item in sections:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def _build_completion_criteria(user_message: str, *, planner_edit_intent: bool = False) -> CompletionCriteria:
    files = _extract_required_files_from_message(user_message)
    if not files:
        hints = _path_hints_from_message(user_message)
        if len(hints) == 1:
            files = [hints[0]]
    sections = _extract_named_sections_from_message(user_message)
    requires_mutation = (_has_patch_intent(user_message) or planner_edit_intent) and (
        bool(files) or _has_explicit_file_target(user_message)
    )
    if not files and not sections and not requires_mutation:
        return CompletionCriteria()

    required_files = tuple(files)
    required_headings: dict[str, tuple[str, ...]] = {}
    required_nonempty: dict[str, tuple[str, ...]] = {}
    if sections and files:
        primary = files[0]
        required_headings[primary] = tuple(sections)
        required_nonempty[primary] = tuple(sections)
    return CompletionCriteria(
        required_files=required_files,
        required_headings=required_headings,
        required_nonempty_sections=required_nonempty,
        requires_mutation=requires_mutation,
    )


def _resolve_under_workspace(workspace_root: str, path_token: str) -> Path | None:
    return _safe_workspace_resolve(workspace_root, path_token)


def _parse_markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _evaluate_completion_criteria(criteria: CompletionCriteria, workspace_root: str) -> CompletionEvaluation:
    if not criteria.active:
        return CompletionEvaluation(satisfied=True, missing=(), fingerprint="none")

    missing: list[str] = []
    file_state: dict[str, str] = {}
    sections_cache: dict[str, dict[str, str]] = {}

    for file_token in criteria.required_files:
        target = _resolve_under_workspace(workspace_root, file_token)
        if target is None:
            missing.append(f"{file_token}:out_of_bounds")
            continue
        if not target.exists() or not target.is_file():
            missing.append(f"{file_token}:missing")
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except Exception:
            missing.append(f"{file_token}:unreadable")
            continue
        file_state[file_token] = _sha256_hex(content.encode("utf-8"))
        sections_cache[file_token] = _parse_markdown_sections(content)

    for file_token, headings in criteria.required_headings.items():
        sections = sections_cache.get(file_token, {})
        section_keys = {k.lower(): k for k in sections.keys()}
        for heading in headings:
            if heading.lower() not in section_keys:
                missing.append(f"{file_token}:heading:{heading}")

    for file_token, headings in criteria.required_nonempty_sections.items():
        sections = sections_cache.get(file_token, {})
        section_keys = {k.lower(): k for k in sections.keys()}
        for heading in headings:
            key = section_keys.get(heading.lower())
            if not key:
                continue
            if not sections.get(key, "").strip():
                missing.append(f"{file_token}:empty:{heading}")

    fp_payload = {
        "missing": sorted(missing),
        "file_state": file_state,
    }
    fingerprint = _sha256_hex(json.dumps(fp_payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return CompletionEvaluation(
        satisfied=(len(missing) == 0),
        missing=tuple(sorted(missing)),
        fingerprint=fingerprint,
    )


def _parse_patch_scope(patch_text: str) -> PatchScope | None:
    text = str(patch_text or "")
    if not text.strip():
        return None
    lines = text.splitlines()
    current_file: str | None = None
    hunks: list[PatchHunk] = []
    touched_per_file: dict[str, int] = {}
    hunk_count_per_file: dict[str, int] = {}

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("+++ "):
            current_file = _normalize_diff_path(line[4:].strip())
            idx += 1
            continue
        if line.startswith("@@"):
            m = _HUNK_HEADER_RE.match(line)
            if not m or not current_file or current_file == "/dev/null":
                return None
            old_start = int(m.group(1))
            old_count = int(m.group(2) or "1")
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "1")
            touched = 0
            idx += 1
            while idx < len(lines) and not lines[idx].startswith("@@") and not lines[idx].startswith("--- ") and not lines[idx].startswith("+++ "):
                body_line = lines[idx]
                if body_line.startswith("+") or body_line.startswith("-"):
                    touched += 1
                idx += 1
            touched = max(1, touched)
            hunk = PatchHunk(
                file_path=current_file,
                old_start=old_start,
                old_count=max(1, old_count),
                new_start=new_start,
                new_count=max(1, new_count),
                touched_lines=touched,
            )
            hunks.append(hunk)
            touched_per_file[current_file] = touched_per_file.get(current_file, 0) + touched
            hunk_count_per_file[current_file] = hunk_count_per_file.get(current_file, 0) + 1
            continue
        idx += 1

    if not hunks:
        return None
    targets = tuple(sorted(touched_per_file.keys()))
    touched_total = sum(touched_per_file.values())
    return PatchScope(
        targets=targets,
        hunks=tuple(hunks),
        touched_per_file=touched_per_file,
        touched_total=touched_total,
        hunk_count_per_file=hunk_count_per_file,
    )


def _pair_hunks_by_file_order_nearest(original: PatchScope, regenerated: PatchScope) -> HunkPairing:
    orig_by_file: dict[str, list[PatchHunk]] = {}
    regen_by_file: dict[str, list[PatchHunk]] = {}
    for h in original.hunks:
        orig_by_file.setdefault(h.file_path, []).append(h)
    for h in regenerated.hunks:
        regen_by_file.setdefault(h.file_path, []).append(h)

    pairs: list[HunkPair] = []
    deterministic = True
    unmatched_original = 0
    unmatched_regenerated = 0

    for file_path, orig_hunks in orig_by_file.items():
        regen_hunks = regen_by_file.get(file_path, [])
        used: set[int] = set()
        for orig in orig_hunks:
            candidates: list[tuple[int, int, PatchHunk]] = []
            for idx, regen in enumerate(regen_hunks):
                if idx in used:
                    continue
                distance = abs(orig.old_start - regen.old_start)
                candidates.append((distance, idx, regen))
            if not candidates:
                unmatched_original += 1
                deterministic = False
                continue
            candidates.sort(key=lambda item: (item[0], item[1]))
            if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
                deterministic = False
            best_distance, best_idx, best_hunk = candidates[0]
            used.add(best_idx)
            pairs.append(HunkPair(original=orig, regenerated=best_hunk, anchor_distance=best_distance))
        unmatched_regenerated += max(0, len(regen_hunks) - len(used))

    for file_path, regen_hunks in regen_by_file.items():
        if file_path not in orig_by_file:
            unmatched_regenerated += len(regen_hunks)

    return HunkPairing(
        pairs=tuple(pairs),
        deterministic=deterministic,
        unmatched_original=unmatched_original,
        unmatched_regenerated=unmatched_regenerated,
    )


def _pairing_quality(pairing: HunkPairing) -> PairQuality:
    max_distance = max((p.anchor_distance for p in pairing.pairs), default=0)
    total_distance = sum(p.anchor_distance for p in pairing.pairs)
    is_partial = (len(pairing.pairs) == 0) or (pairing.unmatched_original > 0) or (not pairing.deterministic)
    return PairQuality(
        max_anchor_distance=max_distance,
        total_anchor_distance=total_distance,
        is_partial=is_partial,
    )


def _hunks_within_drift(pairing: HunkPairing, tolerance: int) -> bool:
    tol = max(0, int(tolerance))
    for pair in pairing.pairs:
        orig_end = pair.original.old_start + max(1, pair.original.old_count) - 1
        regen_end = pair.regenerated.old_start + max(1, pair.regenerated.old_count) - 1
        if abs(pair.original.old_start - pair.regenerated.old_start) > tol:
            return False
        if abs(orig_end - regen_end) > tol:
            return False
    return True


def _has_hunks_outside_original_regions(
    original: PatchScope,
    regenerated: PatchScope,
    *,
    tolerance: int,
) -> bool:
    tol = max(0, int(tolerance))
    original_by_file: dict[str, list[PatchHunk]] = {}
    for h in original.hunks:
        original_by_file.setdefault(h.file_path, []).append(h)

    for h in regenerated.hunks:
        originals = original_by_file.get(h.file_path, [])
        if not originals:
            return True
        regen_start = h.old_start
        regen_end = h.old_start + max(1, h.old_count) - 1
        inside_any = False
        for orig in originals:
            orig_start = orig.old_start - tol
            orig_end = orig.old_start + max(1, orig.old_count) - 1 + tol
            if regen_end >= orig_start and regen_start <= orig_end:
                inside_any = True
                break
        if not inside_any:
            return True
    return False


def _validate_patch_recovery_scope(
    *,
    original_patch: str,
    regenerated_patch: str,
    drift_tolerance_lines: int = 8,
    scope_multiplier: float = 2.0,
    absolute_slack_lines: int = 12,
    pair_max_anchor_distance_lines: int = 8,
    pair_quality_max_total_distance: int = 20,
) -> str | None:
    original = _parse_patch_scope(original_patch)
    regenerated = _parse_patch_scope(regenerated_patch)
    if original is None or regenerated is None:
        return "patch_recovery_scope_expansion"

    if set(original.targets) != set(regenerated.targets):
        return "patch_recovery_scope_expansion"

    pairing = _pair_hunks_by_file_order_nearest(original, regenerated)
    quality = _pairing_quality(pairing)
    if quality.is_partial:
        return "patch_recovery_scope_expansion"
    if quality.max_anchor_distance > max(0, int(pair_max_anchor_distance_lines)):
        return "patch_recovery_scope_expansion"
    if quality.total_anchor_distance > max(0, int(pair_quality_max_total_distance)):
        return "patch_recovery_scope_expansion"

    for file_path in original.targets:
        original_touched = int(original.touched_per_file.get(file_path, 0))
        regenerated_touched = int(regenerated.touched_per_file.get(file_path, 0))
        allowed = max(
            int(original_touched * float(scope_multiplier)),
            int(original_touched + int(absolute_slack_lines)),
        )
        if regenerated_touched > allowed:
            return "patch_recovery_scope_expansion"

    allowed_total = max(
        int(original.touched_total * float(scope_multiplier)),
        int(original.touched_total + int(absolute_slack_lines)),
    )
    if regenerated.touched_total > allowed_total:
        return "patch_recovery_scope_expansion"

    if not _hunks_within_drift(pairing, tolerance=int(drift_tolerance_lines)):
        return "patch_recovery_scope_expansion"

    if _has_hunks_outside_original_regions(
        original,
        regenerated,
        tolerance=int(drift_tolerance_lines),
    ):
        return "patch_recovery_scope_expansion"

    return None


def _truncate_text(text: str, limit: int = 800) -> str:
    raw = str(text or "")
    if len(raw) <= max(0, int(limit)):
        return raw
    return raw[: max(0, int(limit))] + "...<truncated>"


def _tool_result_reason_code(result: ToolExecutionResult) -> str:
    payload = result.result_payload if isinstance(result.result_payload, dict) else {}
    code = payload.get("patch_failure_code") or payload.get("reason_code") or payload.get("error")
    token = str(code or "").strip()
    return token or "tool_execution_failed"


def _patch_recovery_payload(
    *,
    reason_code: str,
    detail: str,
    original_outcome: ToolExecutionResult,
    retry_outcome: ToolExecutionResult | None,
    original_patch: str,
    regenerated_patch: str,
    scope_guard_rejected: bool = False,
    scope_guard_subreason: str | None = None,
) -> dict[str, Any]:
    original_payload = original_outcome.result_payload if isinstance(original_outcome.result_payload, dict) else {}
    retry_payload = (
        retry_outcome.result_payload
        if isinstance(retry_outcome, ToolExecutionResult) and isinstance(retry_outcome.result_payload, dict)
        else {}
    )
    original_scope = _parse_patch_scope(original_patch)
    regenerated_scope = _parse_patch_scope(regenerated_patch)
    target_files = sorted(
        set((original_scope.targets if original_scope is not None else ()))
        | set((regenerated_scope.targets if regenerated_scope is not None else ()))
    )
    return {
        "ok": False,
        "reason_code": reason_code,
        "detail": detail,
        "original_reason_code": str(
            original_payload.get("patch_failure_code")
            or original_payload.get("reason_code")
            or original_payload.get("error")
            or "tool_execution_failed"
        ),
        "retry_reason_code": (
            str(
                retry_payload.get("patch_failure_code")
                or retry_payload.get("reason_code")
                or retry_payload.get("error")
                or ""
            ).strip()
            or None
        ),
        "scope_guard_rejected": bool(scope_guard_rejected),
        "scope_guard_subreason": scope_guard_subreason,
        "target_files": target_files,
        "hunk_count": int(len(original_scope.hunks) if original_scope is not None else 0),
        "original_patch_sha256": _sha256_hex(str(original_patch).encode("utf-8")),
        "regenerated_patch_sha256": _sha256_hex(str(regenerated_patch).encode("utf-8")),
        "original_stdout": _truncate_text(original_outcome.stdout or ""),
        "original_stderr": _truncate_text(original_outcome.stderr or ""),
        "retry_stdout": _truncate_text((retry_outcome.stdout if retry_outcome is not None else "") or ""),
        "retry_stderr": _truncate_text((retry_outcome.stderr if retry_outcome is not None else "") or ""),
        "original_patch_diagnostics": (
            original_payload.get("patch_diagnostics") if isinstance(original_payload.get("patch_diagnostics"), dict) else None
        ),
        "retry_patch_diagnostics": (
            retry_payload.get("patch_diagnostics") if isinstance(retry_payload.get("patch_diagnostics"), dict) else None
        ),
    }


def _is_artifact_whole_file_fallback_target(*, rel_path: str, new_contents: str) -> bool:
    p = Path(str(rel_path or "").strip())
    if not p or p.is_absolute():
        return False
    # Keep fallback narrow: root-level artifact files only (never source tree writes).
    if len(p.parts) != 1 or any(part in {"..", "."} for part in p.parts):
        return False
    suffix = p.suffix.lower()
    allowed = {".md", ".txt", ".rst", ".log"}
    if suffix not in allowed:
        return False
    max_bytes = max(1024, int(os.getenv("OPENVEGAS_BOOTSTRAP_WRITE_MAX_BYTES", "262144")))
    return len((new_contents or "").encode("utf-8")) <= max_bytes


def _attempt_bootstrap_write_fallback(
    *,
    workspace_root: str,
    rel_path: str,
    new_contents: str,
    existing_file: bool,
) -> ToolExecutionResult | None:
    del existing_file
    if not _is_artifact_whole_file_fallback_target(
        rel_path=str(rel_path or ""),
        new_contents=str(new_contents or ""),
    ):
        return None
    target = _safe_workspace_resolve(workspace_root, str(rel_path or ""))
    if target is None:
        return ToolExecutionResult(
            "failed",
            {
                "ok": False,
                "reason_code": "workspace_path_out_of_bounds",
                "detail": "Bootstrap fallback target is outside workspace root.",
            },
            "",
            "",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(new_contents or ""), encoding="utf-8")
    except Exception as e:
        return ToolExecutionResult(
            "failed",
            {
                "ok": False,
                "reason_code": "patch_recovery_failed_bootstrap_fallback_exhausted",
                "detail": f"Bootstrap whole-file fallback failed: {e}",
            },
            "",
            "",
        )
    return ToolExecutionResult(
        "succeeded",
        {
            "ok": True,
            "fallback_mode": "bootstrap_whole_file",
            "path": str(rel_path or ""),
            "bytes_written": len(str(new_contents or "").encode("utf-8")),
            "detail": "Bootstrap whole-file fallback write succeeded.",
        },
        "",
        "",
    )


def _patch_failure_signature(
    *,
    arguments: dict[str, Any],
    write_meta: dict[str, Any] | None,
    outcome: ToolExecutionResult,
) -> str:
    patch_text = str(arguments.get("patch", ""))
    scope = _parse_patch_scope(patch_text)
    payload = outcome.result_payload if isinstance(outcome.result_payload, dict) else {}
    filtered_targets: list[str] = []
    filtered_hunk_total: int | None = None
    filtered_touched: dict[str, int] | None = None
    if isinstance(write_meta, dict):
        raw_targets = write_meta.get("filtered_target_files")
        if isinstance(raw_targets, list):
            filtered_targets = [str(x) for x in raw_targets if str(x).strip()]
        try:
            if write_meta.get("filtered_hunks_total") is not None:
                filtered_hunk_total = int(write_meta.get("filtered_hunks_total"))
        except Exception:
            filtered_hunk_total = None
        if isinstance(write_meta.get("filtered_touched_per_file"), dict):
            filtered_touched = {
                str(k): int(v)
                for k, v in dict(write_meta.get("filtered_touched_per_file")).items()
                if str(k).strip()
            }
    base = {
        "reason_code": _tool_result_reason_code(outcome),
        "target_files": (
            sorted(filtered_targets)
            if filtered_targets
            else (list(scope.targets) if scope is not None else [])
        ),
        "hunk_count": (
            int(filtered_hunk_total)
            if filtered_hunk_total is not None
            else int(len(scope.hunks) if scope is not None else 0)
        ),
        "patch_sha256": _sha256_hex(patch_text.encode("utf-8")),
        "stderr_sha256": _sha256_hex(str(outcome.stderr or "").encode("utf-8")),
    }
    if filtered_touched:
        base["touched_per_file"] = filtered_touched
    if isinstance(write_meta, dict):
        base["write_path"] = str(write_meta.get("path") or "")
        base["existing_file"] = bool(write_meta.get("existing_file"))
        new_contents = str(write_meta.get("new_contents") or "")
        base["new_contents_sha256"] = _sha256_hex(new_contents.encode("utf-8"))
    if isinstance(payload.get("patch_diagnostics"), dict):
        base["patch_failure_code"] = str(payload.get("patch_failure_code") or "")
    return _sha256_hex(json.dumps(base, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _prepare_write_patch(
    *,
    workspace_root: str,
    arguments: dict[str, Any],
    user_message: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = _coerce_nonempty_text(arguments.get("filepath")) or _coerce_nonempty_text(arguments.get("path"))
    if path is None:
        path = _deep_find_keyed_string(arguments, ("filepath", "path", "file_path", "file", "target_path"))
    content: str | None = None
    raw_content = arguments.get("content")
    if isinstance(raw_content, str):
        content = raw_content
    if content is None:
        raw_new_content = arguments.get("new_content")
        if isinstance(raw_new_content, str):
            content = raw_new_content
    if content is None:
        content = _deep_find_keyed_string(arguments, ("content", "new_content", "text", "replacement", "value"))

    if not path or content is None:
        return None, {
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": "Write requires filepath and content.",
        }

    target, old_text, exists, resolve_err = _resolve_and_read_target(
        workspace_root=workspace_root,
        path=path,
        tool_label="Write",
        require_existing=False,
    )
    if resolve_err is not None or target is None:
        return None, resolve_err

    rel_path = _safe_rel_from_workspace(workspace_root, target)
    raw_mode = str(arguments.get("write_mode") or arguments.get("mode") or "").strip().lower()
    append_mode = raw_mode in {"append", "append_bottom", "append_end"}
    explicit_replace_intent = _has_explicit_replace_intent_from_arguments(arguments) or _has_explicit_replace_wording(
        str(user_message or "")
    )

    if exists:
        if (not append_mode) and old_text == content:
            return None, {
                "status": "noop",
                "tool_name": "fs_apply_patch",
                "error": "no_change",
                "detail": "Write produced no changes.",
            }
        if (not append_mode) and (not explicit_replace_intent):
            _emit_intent_validator_result(
                intent="full_replace",
                reason="existing_file_replace_requires_explicit_intent",
            )
            return None, {
                "status": "blocked",
                "error": "existing_file_replace_requires_explicit_intent",
                "detail": "Existing-file Write(replace) requires explicit replace intent.",
            }

    new_text = content
    if append_mode and exists:
        separator = ""
        if old_text and content and (not old_text.endswith("\n")):
            separator = "\n"
        new_text = f"{old_text}{separator}{content}"

    safety_intent = "append" if append_mode else "full_replace"
    ok, reason = _validate_patch_safety(old_text=old_text, new_text=new_text, intent=safety_intent)
    if not ok:
        _emit_intent_validator_result(intent=safety_intent, reason=str(reason or "unknown"))
        return None, {
            "status": "blocked",
            "error": str(reason or "patch_safety_blocked"),
            "detail": "Write safety validation rejected generated patch.",
        }
    _emit_intent_validator_result(intent=safety_intent)

    patch = _build_unified_patch(old_text=old_text, new_text=new_text, rel_path=rel_path)
    if patch is None:
        return None, {
            "status": "noop",
            "tool_name": "fs_apply_patch",
            "error": "no_change",
            "detail": "Write produced no changes.",
        }

    prepared = {
        "patch": patch,
        "path": rel_path,
    }
    meta = {
        "source": "write_abi",
        "path": rel_path,
        "old_contents": old_text,
        "new_contents": new_text,
        "existing_file": bool(exists),
        "operation_kind": ("append" if append_mode else "full_replace"),
        "selection_basis": ("eof" if append_mode else "full_file"),
        "explicit_replace_intent": bool(explicit_replace_intent),
        "write_mode": ("append" if append_mode else "replace"),
    }
    return {"arguments": prepared, "meta": meta}, None


def _has_patch_intent(msg: str) -> bool:
    text = (msg or "").lower()
    # Explicit non-edit instructions should win.
    if re.search(
        r"\b(?:show|explain|example|sample|demonstrate)\b.*\b(?:do\s+not|don't|without)\s+\b(?:edit|modify|change|patch|write|apply)\b",
        text,
    ):
        return False
    if re.search(r"\b(?:do\s+not|don't|without)\s+\b(?:edit|modify|change|patch|write|apply)\b", text):
        return False

    edit_verbs = re.search(
        r"\b(patch|edit|modify|update|change|add|remove|delete|fix|refactor|implement|rewrite|replace|insert|append|rename|move|extract|create|make|write|generate|overwrite)\b",
        text,
    )
    read_only_verbs = re.search(
        r"\b(show|explain|read|describe|list|display|summarize|review|search|inspect|view|open)\b",
        text,
    )
    has_file_target = _has_explicit_file_target(msg)

    if edit_verbs:
        return True
    # File-targeted prompts default to edit intent unless explicitly read-only.
    if has_file_target:
        if read_only_verbs and not edit_verbs:
            return False
        return True
    return False


def _has_append_bottom_intent(msg: str) -> bool:
    text = (msg or "").lower()
    if not text.strip():
        return False
    if re.search(r"\b(?:do\s+not|don't|without)\s+\bappend\b", text):
        return False
    has_append_verb = bool(re.search(r"\b(?:append|add|insert)\b", text))
    has_bottom_loc = bool(
        re.search(
            r"\b(?:bottom|bottom\s+of(?:\s+the)?\s+file|end(?:\s+of(?:\s+the)?)?\s+file|end\s+of|at\s+end(?:\s+of)?|at\s+the\s+end(?:\s+of)?|to\s+end|to\s+the\s+end|eof)\b",
            text,
        )
    )
    has_file_ref = _has_explicit_file_target(text) or bool(re.search(r"\b(?:this|the)\s+file\b", text))
    if "append" in text and has_file_ref:
        return True
    return has_append_verb and has_bottom_loc and has_file_ref


def _is_patch_repeat_followup_intent(msg: str) -> bool:
    text = (msg or "").lower().strip()
    return bool(re.search(r"^(?:apply\s+)?(?:another(?:\s+one)?|one\s+more|again)\b", text))


def _has_explicit_file_target(msg: str) -> bool:
    text = (msg or "").strip()
    if not text:
        return False
    if re.search(r"(/[^\s\"'`]+)", text):
        return True
    # common file-like tokens
    if re.search(r"\b[\w.\-]+\.(py|ts|tsx|js|jsx|md|json|yaml|yml|sql|toml|txt|rs|go|java|kt|cpp|c|h)\b", text, flags=re.IGNORECASE):
        return True
    return False


def _is_temp_patch_smoke_intent(msg: str) -> bool:
    text = (msg or "").lower()
    return _has_patch_intent(text) and bool(re.search(r"\b(temp|temporary)\s+file\b", text))


def _is_patch_smoke_intent(msg: str) -> bool:
    text = (msg or "").lower().strip()
    if not _has_patch_intent(text):
        return False
    if _is_temp_patch_smoke_intent(text):
        return True
    if re.search(r"\b(another|one more)\s+patch\b", text) and not _has_explicit_file_target(text):
        return True
    if re.search(r"\btiny\s+patch\b", text) and not _has_explicit_file_target(text):
        return True
    return False


def _is_file_create_intent(msg: str) -> bool:
    text = (msg or "").lower().strip()
    has_create_verb = bool(re.search(r"\b(create|make|write|generate)\b", text))
    if not has_create_verb:
        return False
    if "file" in text:
        return True
    if _has_explicit_file_target(text):
        return True
    return False


def _extract_first_fenced_code_block(text: str) -> str | None:
    blocks = _extract_fenced_code_blocks(text)
    if not blocks:
        return None
    return blocks[0]


def _extract_fenced_code_blocks_with_lang(text: str) -> list[tuple[str, str]]:
    raw = str(text or "")
    if not raw.strip():
        return []
    out: list[tuple[str, str]] = []
    # Support both triple backticks and triple tildes.
    for m in re.finditer(r"(?P<fence>`{3,}|~{3,})(?P<lang>[^\n]*)\n(?P<body>.*?)(?P=fence)", raw, flags=re.DOTALL):
        lang = str(m.group("lang") or "").strip().lower()
        content = str(m.group("body") or "").strip("\n")
        if content.strip():
            out.append((lang, content))
    return out


def _extract_fenced_code_blocks(text: str) -> list[str]:
    return [content for _lang, content in _extract_fenced_code_blocks_with_lang(text)]


def _unfenced_code_extraction_enabled() -> bool:
    return os.getenv("OPENVEGAS_UNFENCED_CODE_EXTRACTION", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _extract_unfenced_code_like_region_for_target(text: str, target: str) -> str | None:
    raw = str(text or "")
    if not raw.strip():
        return None
    ext = Path(str(target or "")).suffix.lower()
    chunks = [chunk.strip("\n") for chunk in re.split(r"\n\s*\n", raw) if chunk.strip()]
    if not chunks:
        return None

    def _is_python_like(chunk: str) -> bool:
        py_signals = (
            r"^\s*def\s+\w+\s*\(",
            r"^\s*class\s+\w+\s*:",
            r"^\s*import\s+\w+",
            r"^\s*from\s+\S+\s+import\s+",
        )
        return any(re.search(pat, chunk, flags=re.MULTILINE) for pat in py_signals)

    ranked: list[tuple[int, int, int]] = []
    for idx, chunk in enumerate(chunks):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if len(lines) < 5:
            continue
        score = 0
        if re.search(r"^\s*(def|class|import|from|function|const|let|var)\b", chunk, flags=re.MULTILINE):
            score += 2
        if ext == ".py":
            if not _is_python_like(chunk):
                continue
            score += 4
        score += min(len(chunk) // 240, 3)
        if score >= 5:
            ranked.append((score, len(chunk), -idx))

    if not ranked:
        return None
    ranked.sort(reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == best[0] and ranked[1][1] == best[1]:
        return None
    return chunks[-best[2]]


def _read_observation_target_paths(tool_observations: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for obs in tool_observations:
        if str(obs.get("tool_name")) != "fs_read":
            continue
        if str(obs.get("result_status")) != "succeeded":
            continue
        payload = obs.get("result_payload")
        if not isinstance(payload, dict):
            continue
        path = str(payload.get("path") or "").strip()
        if not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _normalize_path_like_token(path: str) -> str:
    token = str(path or "").strip().replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    return token


def _paths_match_for_target(observed: str, target: str) -> bool:
    left = _normalize_path_like_token(observed)
    right = _normalize_path_like_token(target)
    if not left or not right:
        return False
    if left == right:
        return True
    if left.endswith("/" + right) or right.endswith("/" + left):
        return True
    return False


def _latest_read_observation_payload_for_target(
    tool_observations: list[dict[str, Any]],
    target: str,
) -> dict[str, Any] | None:
    for obs in reversed(tool_observations):
        if str(obs.get("tool_name")) != "fs_read":
            continue
        if str(obs.get("result_status")) != "succeeded":
            continue
        payload = obs.get("result_payload")
        if not isinstance(payload, dict):
            continue
        obs_path = str(payload.get("path") or "").strip()
        if not _paths_match_for_target(obs_path, target):
            continue
        return payload
    return None


def _derive_single_replace_from_old_and_new(old_text: str, new_text: str) -> tuple[str, str] | None:
    if old_text == new_text:
        return None
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    opcodes = difflib.SequenceMatcher(a=old_lines, b=new_lines).get_opcodes()
    changed = [(tag, i1, i2, j1, j2) for tag, i1, i2, j1, j2 in opcodes if tag != "equal"]
    if len(changed) == 1:
        _tag, i1, i2, j1, j2 = changed[0]
        old_mid = "".join(old_lines[i1:i2])
        new_mid = "".join(new_lines[j1:j2])
        if not old_mid:
            return None
        if old_text.count(old_mid) != 1:
            return None
        return old_mid, new_mid

    # Fallback: when structured diff decomposition is ambiguous, but we have
    # a concrete full old snapshot from fs_read, an exact full-snapshot replace
    # remains deterministic and fail-closed.
    if old_text.strip() and old_text.count(old_text) == 1:
        return old_text, new_text
    return None


def _lang_matches_target(lang: str, target: str) -> bool:
    token = (lang or "").strip().lower()
    ext = Path(str(target or "")).suffix.lower().lstrip(".")
    if not token or not ext:
        return False
    aliases = {
        "py": {"python", "py"},
        "js": {"javascript", "js", "node"},
        "ts": {"typescript", "ts"},
        "tsx": {"tsx", "typescriptreact"},
        "jsx": {"jsx", "javascriptreact"},
        "md": {"markdown", "md"},
        "json": {"json"},
        "yaml": {"yaml", "yml"},
        "yml": {"yaml", "yml"},
        "sql": {"sql"},
        "sh": {"bash", "shell", "sh", "zsh"},
        "toml": {"toml"},
        "go": {"go", "golang"},
        "rs": {"rust", "rs"},
    }
    return token in aliases.get(ext, {ext})


def _score_complete_file_block(content: str) -> int:
    text = str(content or "")
    score = max(0, len(text) // 200)
    hints = (
        r"^\s*import\s+",
        r"^\s*from\s+\S+\s+import\s+",
        r"^\s*def\s+\w+\s*\(",
        r"^\s*class\s+\w+",
        r"^\s*function\s+\w+\s*\(",
        r"^\s*(const|let|var)\s+\w+\s*=",
        r"^\s*export\s+",
        r"^\s*#include\s+",
        r"^\s*package\s+\w+",
        r"^\s*public\s+class\s+",
    )
    for pat in hints:
        if re.search(pat, text, flags=re.MULTILINE):
            score += 2
    return score


def _select_synth_code_block(
    *,
    blocks: list[tuple[str, str]],
    target: str | None,
) -> tuple[str | None, str | None]:
    if not blocks:
        return None, "zero_code_blocks"
    if len(blocks) == 1:
        return blocks[0][1], None

    if target:
        matched = [content for lang, content in blocks if _lang_matches_target(lang, target)]
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            blocks = [(lang, content) for lang, content in blocks if _lang_matches_target(lang, target)]

    ranked: list[tuple[int, int, int]] = []
    for idx, (_lang, content) in enumerate(blocks):
        ranked.append((_score_complete_file_block(content), len(content), -idx))
    ranked.sort(reverse=True)
    if not ranked:
        return None, "multiple_code_blocks_ambiguous"
    best = ranked[0]
    # Require deterministic winner. If top two tie on score+length, fail closed.
    if len(ranked) > 1 and ranked[1][0] == best[0] and ranked[1][1] == best[1]:
        return None, "multiple_code_blocks_ambiguous"
    chosen_index = -best[2]
    return blocks[chosen_index][1], None


def _should_synth_write_from_model_text(
    *,
    user_message: str,
    model_text: str,
    planner_edit_intent: bool,
) -> bool:
    targets = _path_hints_from_message(user_message)
    if len(targets) > 1:
        return False
    code_blocks = _extract_fenced_code_blocks_with_lang(model_text)
    content, _reason = _select_synth_code_block(
        blocks=code_blocks,
        target=(targets[0] if targets else None),
    )
    if not content:
        return False
    if not (_has_patch_intent(user_message) or planner_edit_intent):
        return False
    return True


def _latest_read_observation_path(tool_observations: list[dict[str, Any]]) -> str | None:
    for obs in reversed(tool_observations):
        if str(obs.get("tool_name")) != "fs_read":
            continue
        if str(obs.get("result_status")) != "succeeded":
            continue
        payload = obs.get("result_payload")
        if not isinstance(payload, dict):
            continue
        path = str(payload.get("path") or "").strip()
        if path:
            return path
    return None


def _extract_sections_from_message(msg: str) -> list[str]:
    text = (msg or "").strip()
    if not text:
        return []
    m = re.search(r"\bsections?\s*:\s*([^\n\r.]+)", text, flags=re.IGNORECASE)
    if not m:
        return []
    raw = m.group(1)
    parts = [p.strip() for p in re.split(r",|\band\b", raw, flags=re.IGNORECASE) if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        token = p.strip("`'\" ")
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _create_file_fallback_args(user_message: str) -> dict[str, Any]:
    target = _path_hint_from_message(user_message) or ".openvegas_tmp_patch.txt"
    sections = _extract_sections_from_message(user_message)
    if sections and str(target).lower().endswith(".md"):
        title = Path(str(target)).name.lstrip(".") or "temp"
        lines = [f"# {title}", ""]
        for section in sections:
            lines.extend([f"## {section}", ""])
        new_content = "\n".join(lines).rstrip() + "\n"
    elif sections:
        new_content = "\n".join([f"[{s}]" for s in sections]) + "\n"
    else:
        new_content = ""
    return {
        "path": target,
        "new_content": new_content,
    }


def _tool_debug_enabled() -> bool:
    return os.getenv("OPENVEGAS_TOOL_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _tool_debug(message: str) -> None:
    if not _tool_debug_enabled():
        return
    logging.getLogger("openvegas.tool_debug").debug(str(message))
    if os.getenv("OPENVEGAS_TOOL_DEBUG_CONSOLE", "").strip().lower() in {"1", "true", "yes", "on"}:
        console.print(f"[dim][tool-debug][/dim] {message}")


def _ide_bridge_trace_enabled() -> bool:
    return os.getenv("OPENVEGAS_IDE_BRIDGE_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def _ide_bridge_debug(message: str) -> None:
    if not _ide_bridge_trace_enabled():
        return
    logging.getLogger("openvegas.ide_bridge").debug(str(message))
    if os.getenv("OPENVEGAS_IDE_BRIDGE_TRACE_CONSOLE", "").strip().lower() in {"1", "true", "yes", "on"}:
        console.print(f"[dim][ide-bridge][/dim] {message}")


_INTERNAL_RESPONSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*prepended synthesized write tool call", re.IGNORECASE),
    re.compile(r"^\s*synth-write skipped;", re.IGNORECASE),
    re.compile(r"^\s*text-only gate pre-fallback:", re.IGNORECASE),
    re.compile(r"^\s*post-finalize intercept skipped;", re.IGNORECASE),
    re.compile(r"^\s*finalizing/continuing with text-only answer", re.IGNORECASE),
    re.compile(r"^\s*last_successful_tool=", re.IGNORECASE),
    re.compile(r"^\s*web:\s*requested=", re.IGNORECASE),
    re.compile(r"^\s*tokens:\s*in=", re.IGNORECASE),
)


def _sanitize_user_visible_response_text(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""
    out_lines: list[str] = []
    for line in raw.splitlines():
        token = str(line or "").strip()
        if token and any(p.search(token) for p in _INTERNAL_RESPONSE_PATTERNS):
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def _extension_is_ready() -> bool:
    code_bin = shutil.which("code")
    if not code_bin:
        return True
    ext_id = str(os.getenv("OPENVEGAS_VSCODE_DIFF_EXTENSION_ID", "")).strip().lower()
    if not ext_id:
        return True
    try:
        proc = subprocess.run(
            [code_bin, "--list-extensions"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return False
        installed = {ln.strip().lower() for ln in (proc.stdout or "").splitlines() if ln.strip()}
        return ext_id in installed
    except Exception:
        return False


def _run_extension_install() -> bool:
    code_bin = shutil.which("code")
    if not code_bin:
        return False
    target = str(
        os.getenv("OPENVEGAS_VSCODE_DIFF_EXTENSION_INSTALL_TARGET", "")
        or os.getenv("OPENVEGAS_VSCODE_DIFF_EXTENSION_ID", "")
    ).strip()
    if not target:
        return False
    try:
        proc = subprocess.run(
            [code_bin, "--install-extension", target, "--force"],
            check=False,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _maybe_prompt_vscode_extension_for_interactive_diff() -> None:
    global _VSCODE_DIFF_PROMPTED
    if _VSCODE_DIFF_PROMPTED:
        return
    if os.getenv("OPENVEGAS_VSCODE_DIFF_EXTENSION_PROMPT", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    if not sys.stdin.isatty():
        return
    if not shutil.which("code"):
        return
    cfg = load_config()
    if bool(cfg.get("skip_vscode_extension_prompt")):
        return
    if _extension_is_ready():
        return
    _VSCODE_DIFF_PROMPTED = True
    _drain_stdin_buffer(window_ms=0)
    raw = Prompt.ask(
        "Install OpenVegas VSCode interactive diff extension now? [Y/n/never]",
        default="Y",
    )
    _drain_stdin_buffer(window_ms=0)
    choice = str(raw or "").strip().lower()
    if choice in {"", "y", "yes"}:
        if not _run_extension_install():
            _tool_debug("vscode extension install skipped/failed")
        return
    if choice in {"n", "no"}:
        return
    if choice == "never":
        cfg["skip_vscode_extension_prompt"] = True
        save_config(cfg)
        return
    # fail closed on unexpected input
    return


def _promote_tool_call_for_patch_intent(
    *,
    user_message: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_observations: list[dict[str, Any]],
    force_patch_intent: bool = False,
) -> tuple[str, dict[str, Any]]:
    def _fallback_args() -> dict[str, Any]:
        suffix = uuid.uuid4().hex[:8]
        return {
            "path": ".openvegas_tmp_patch.txt",
            "new_content": f"openvegas temp patch applied {suffix}\n",
        }

    patch_smoke_intent = force_patch_intent or _is_patch_smoke_intent(user_message)
    create_file_intent = _is_file_create_intent(user_message)
    if not (patch_smoke_intent or create_file_intent):
        return tool_name, arguments
    already_patch_attempted = any(str(o.get("tool_name")) == "fs_apply_patch" for o in tool_observations)
    if already_patch_attempted:
        return tool_name, arguments
    if create_file_intent:
        if tool_name not in SUPPORTED_TOOL_NAMES:
            return "fs_apply_patch", _create_file_fallback_args(user_message)
        if tool_name in {"fs_list", "fs_read", "fs_search", "editor_open"}:
            return "fs_apply_patch", _create_file_fallback_args(user_message)
        return tool_name, arguments
    if tool_name not in SUPPORTED_TOOL_NAMES:
        return "fs_apply_patch", _fallback_args()
    if tool_name != "fs_list":
        return tool_name, arguments
    return "fs_apply_patch", _fallback_args()


def _synth_patch_tool_req_for_intent(
    *,
    user_message: str,
    tool_observations: list[dict[str, Any]],
    force_patch_intent: bool = False,
) -> dict[str, Any] | None:
    patch_smoke_intent = force_patch_intent or _is_patch_smoke_intent(user_message)
    create_file_intent = _is_file_create_intent(user_message)
    if not (patch_smoke_intent or create_file_intent):
        return None
    if any(str(o.get("tool_name")) == "fs_apply_patch" for o in tool_observations):
        return None
    if create_file_intent:
        args = _create_file_fallback_args(user_message)
        return {
            "type": "tool_call",
            "tool_name": "fs_apply_patch",
            "arguments": args,
            "shell_mode": "mutating",
            "timeout_sec": 30,
        }
    suffix = uuid.uuid4().hex[:8]
    return {
        "type": "tool_call",
        "tool_name": "fs_apply_patch",
        "arguments": {
            "path": ".openvegas_tmp_patch.txt",
            "new_content": f"openvegas temp patch applied {suffix}\n",
        },
        "shell_mode": "mutating",
        "timeout_sec": 30,
    }


def _comment_prefix_for_path(path: str) -> str:
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in {".py", ".sh", ".yaml", ".yml", ".toml", ".ini", ".env", ".rb", ".pl", ".r"}:
        return "#"
    if suffix in {".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs", ".swift", ".kt"}:
        return "//"
    if suffix in {".sql"}:
        return "--"
    if suffix in {".html", ".xml", ".md"}:
        return "<!--"
    return "#"


def _build_append_comment_from_intent(user_message: str, target: str) -> str | None:
    text = str(user_message or "").strip()
    lowered = text.lower()
    if "comment" not in lowered:
        return None
    quoted: str | None = None
    for pat in (r'"([^"\n]{1,200})"', r"'([^'\n]{1,200})'"):
        m = re.search(pat, text)
        if m:
            quoted = m.group(1).strip()
            break
    comment_body = quoted or "Commentaire ajoute a la fin du fichier."
    prefix = _comment_prefix_for_path(target)
    if prefix == "<!--":
        return f"<!-- {comment_body} -->\n"
    return f"{prefix} {comment_body}\n"


def _synth_write_tool_req_from_model_edit(
    *,
    user_message: str,
    model_text: str,
    tool_observations: list[dict[str, Any]],
    planner_edit_intent: bool = False,
) -> dict[str, Any] | None:
    def _skip(reason: str) -> None:
        emit_metric("tool_synth_write_skipped_total", {"reason": reason})
        _tool_debug(f"synth-write skipped; reason={reason}")

    if not (_has_patch_intent(user_message) or planner_edit_intent):
        _skip("no_edit_intent")
        return None

    targets = _path_hints_from_message(user_message)
    if len(targets) > 1:
        _skip("multiple_targets")
        return None

    target = targets[0] if targets else None
    if not target:
        observed_targets = _read_observation_target_paths(tool_observations)
        if len(observed_targets) > 1:
            _skip("multiple_targets")
            return None
        if len(observed_targets) == 1:
            target = observed_targets[0]
            _tool_debug(f"synth-write target fallback from fs_read observation: {target}")

    if not target:
        _skip("zero_targets")
        return None

    for obs in tool_observations:
        if str(obs.get("tool_name")) != "fs_apply_patch":
            continue
        # Suppress only after actual execution outcomes, not blocked/denied/noop.
        status = str(obs.get("result_status") or "").strip().lower()
        if status in {"succeeded", "failed"}:
            _skip("suppressed_prior_patch_result")
            return None

    # For append-comment prompts, prefer deterministic intent-derived content
    # over model-emitted full-file snippets to avoid replace-shaped fallbacks.
    if _has_append_bottom_intent(user_message):
        fallback_comment = _build_append_comment_from_intent(user_message, target)
        if fallback_comment:
            _tool_debug("synth-write used append-comment fallback from user intent (pre-codeblock)")
            return {
                "type": "tool_call",
                "tool_name": "InsertAtEnd",
                "arguments": {
                    "filepath": target,
                    "content": fallback_comment,
                    "operation_kind": "append",
                    "selection_basis": "eof",
                },
                "shell_mode": "mutating",
                "timeout_sec": 30,
            }

    code_blocks = _extract_fenced_code_blocks_with_lang(model_text)
    selected_content, selection_reason = _select_synth_code_block(
        blocks=code_blocks,
        target=target,
    )
    if not selected_content and _unfenced_code_extraction_enabled():
        selected_content = _extract_unfenced_code_like_region_for_target(model_text, target)
        if selected_content:
            selection_reason = None
            _tool_debug("synth-write used unfenced code extraction fallback")
    if not selected_content:
        if _has_append_bottom_intent(user_message):
            fallback_comment = _build_append_comment_from_intent(user_message, target)
            if fallback_comment:
                _tool_debug("synth-write used append-comment fallback from user intent")
                return {
                    "type": "tool_call",
                    "tool_name": "InsertAtEnd",
                    "arguments": {
                        "filepath": target,
                        "content": fallback_comment,
                        "operation_kind": "append",
                        "selection_basis": "eof",
                    },
                    "shell_mode": "mutating",
                    "timeout_sec": 30,
                }
        _skip(selection_reason or "content_missing")
        return None

    content = selected_content.strip("\n")
    if len(content.strip()) < 3:
        _skip("content_too_short")
        return None

    if _has_append_bottom_intent(user_message):
        return {
            "type": "tool_call",
            "tool_name": "InsertAtEnd",
            "arguments": {
                "filepath": target,
                "content": content,
                "operation_kind": "append",
                "selection_basis": "eof",
            },
            "shell_mode": "mutating",
            "timeout_sec": 30,
        }

    payload = _latest_read_observation_payload_for_target(tool_observations, target)
    current_text = str(payload.get("content") if isinstance(payload, dict) else "")
    replace_pair = _derive_single_replace_from_old_and_new(current_text, content) if current_text else None
    if replace_pair is not None:
        old_string, new_string = replace_pair
        return {
            "type": "tool_call",
            "tool_name": "FindAndReplace",
            "arguments": {
                "filepath": target,
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": False,
                "operation_kind": "find_replace",
                "selection_basis": "exact_string",
            },
            "shell_mode": "mutating",
            "timeout_sec": 30,
        }

    if not _allow_full_replace_from_edit_intent(user_message):
        _skip("existing_file_replace_requires_explicit_intent")
        return None

    return {
        "type": "tool_call",
        "tool_name": "Write",
        "arguments": {
            "filepath": target,
            "content": content,
            "write_mode": "replace",
            "explicit_replace_intent": True,
            "operation_kind": "full_replace",
            "selection_basis": "full_file",
        },
        "shell_mode": "mutating",
        "timeout_sec": 30,
    }


def _maybe_prepend_synth_write(
    *,
    tool_reqs: list[dict[str, Any]],
    user_message: str,
    model_text: str,
    planner_edit_intent: bool,
    tool_observations: list[dict[str, Any]],
    reason_if_empty: str,
    reason_if_non_mutating: str,
    debug_label: str,
    preprocess: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, dict[str, Any] | None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Prepend synthesized Write when edit intent exists and no mutating call remains."""
    had_any_candidates = bool(tool_reqs)
    has_mutating = any(_is_mutating_tool_candidate(req) for req in tool_reqs)
    _tool_debug(
        f"{debug_label}; had_any_candidates={had_any_candidates} "
        f"has_mutating={has_mutating} planner_edit_intent={planner_edit_intent}"
    )
    if has_mutating:
        return tool_reqs, [], False

    write_fallback = _synth_write_tool_req_from_model_edit(
        user_message=user_message,
        model_text=model_text,
        tool_observations=tool_observations,
        planner_edit_intent=planner_edit_intent,
    )
    if write_fallback is None:
        return tool_reqs, [], False

    synth_req: dict[str, Any] | None = write_fallback
    synth_errors: list[dict[str, Any]] = []
    if preprocess is not None:
        prepared, prep_error = preprocess(write_fallback)
        if prep_error is not None:
            synth_errors.append(prep_error)
            prep_reason = str(prep_error.get("error") or "preprocess_rejected")
            emit_metric("tool_synth_write_blocked_total", {"reason": prep_reason})
            emit_metric("preprocess_rejected_synth_write", {"reason": prep_reason})
        if prepared is None:
            return tool_reqs, synth_errors, False
        synth_req = prepared

    reason = reason_if_non_mutating if had_any_candidates else reason_if_empty
    out = list(tool_reqs)
    out.insert(0, synth_req)
    emit_metric("tool_synth_write_from_code_block_total", {"reason": reason})
    _tool_debug(f"{debug_label}; reason={reason}")
    return out, synth_errors, True


def _diagnose_synth_write_skip_reason(
    *,
    user_message: str,
    model_text: str,
    tool_observations: list[dict[str, Any]],
    planner_edit_intent: bool,
) -> str | None:
    if not (_has_patch_intent(user_message) or planner_edit_intent):
        return "no_edit_intent"
    targets = _path_hints_from_message(user_message)
    if len(targets) > 1:
        return "multiple_targets"
    target = targets[0] if targets else None
    if not target:
        observed_targets = _read_observation_target_paths(tool_observations)
        if len(observed_targets) > 1:
            return "multiple_targets"
        if len(observed_targets) == 1:
            target = observed_targets[0]
    if not target:
        return "zero_targets"
    for obs in tool_observations:
        if str(obs.get("tool_name")) != "fs_apply_patch":
            continue
        status = str(obs.get("result_status") or "").strip().lower()
        if status in {"succeeded", "failed"}:
            return "suppressed_prior_patch_result"
    if _has_append_bottom_intent(user_message):
        fallback_comment = _build_append_comment_from_intent(user_message, target)
        if fallback_comment:
            return None
    content, reason = _select_synth_code_block(
        blocks=_extract_fenced_code_blocks_with_lang(model_text),
        target=target,
    )
    if not content and _unfenced_code_extraction_enabled():
        content = _extract_unfenced_code_like_region_for_target(model_text, target)
        if content:
            reason = None
    if not content:
        return reason or "content_missing"
    if len(content.strip()) < 3:
        return "content_too_short"
    if _has_append_bottom_intent(user_message):
        return None
    if target:
        payload = _latest_read_observation_payload_for_target(tool_observations, target)
        current_text = str(payload.get("content") if isinstance(payload, dict) else "")
        if current_text and _derive_single_replace_from_old_and_new(current_text, content) is not None:
            return None
    if not _allow_full_replace_from_edit_intent(user_message):
        return "existing_file_replace_requires_explicit_intent"
    return None


def _deep_find_keyed_string(
    v: Any,
    keys: tuple[str, ...],
    depth: int = 0,
    *,
    _fallback_emitted: list[bool] | None = None,
) -> str | None:
    if depth > 4:
        return None
    if _fallback_emitted is None:
        _fallback_emitted = [False]

    def _emit_nested_fallback(hit_key: str) -> None:
        if depth <= 0 or _fallback_emitted[0]:
            return
        _fallback_emitted[0] = True
        emit_metric(
            "tool_argument_deep_fallback_total",
            {
                "depth": str(depth),
                "key": str(hit_key or ""),
            },
        )

    if isinstance(v, dict):
        for k in keys:
            if k in v:
                hit = _coerce_nonempty_text(v.get(k))
                if hit:
                    _emit_nested_fallback(k)
                    return hit
                hit = _deep_find_keyed_string(
                    v.get(k),
                    keys,
                    depth + 1,
                    _fallback_emitted=_fallback_emitted,
                )
                if hit:
                    return hit
        for child in v.values():
            hit = _deep_find_keyed_string(
                child,
                keys,
                depth + 1,
                _fallback_emitted=_fallback_emitted,
            )
            if hit:
                return hit
    elif isinstance(v, list):
        for child in v:
            hit = _deep_find_keyed_string(
                child,
                keys,
                depth + 1,
                _fallback_emitted=_fallback_emitted,
            )
            if hit:
                return hit
    return None


def _collect_tool_call_candidates(tool_calls_payload: Any, fallback_text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if isinstance(tool_calls_payload, list):
        for item in tool_calls_payload:
            if not isinstance(item, dict):
                continue
            if item.get("tool_name"):
                args = item.get("arguments", {})
                if isinstance(args, str):
                    try:
                        parsed_args = json.loads(args)
                        args = parsed_args if isinstance(parsed_args, dict) else {}
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                candidates.append(
                    {
                        "type": "tool_call",
                        "tool_name": item.get("tool_name"),
                        "arguments": args,
                        "shell_mode": item.get("shell_mode", "read_only"),
                        "timeout_sec": item.get("timeout_sec", 30),
                    }
                )
                continue

            fn = item.get("function")
            if not isinstance(fn, dict):
                continue
            fn_name = str(fn.get("name") or "").strip()
            raw_args = fn.get("arguments", {})
            parsed: dict[str, Any] = {}
            if isinstance(raw_args, str):
                try:
                    loaded = json.loads(raw_args)
                    parsed = loaded if isinstance(loaded, dict) else {}
                except Exception:
                    parsed = {}
            elif isinstance(raw_args, dict):
                parsed = raw_args
            tool_name = str(parsed.get("tool_name") or "").strip()
            if not tool_name and fn_name:
                tool_name = fn_name
            if not tool_name:
                continue
            args_obj = parsed.get("arguments", {})
            if not isinstance(args_obj, dict):
                args_obj = {}
            if not args_obj:
                # Native function-call payloads may place arguments at the top level.
                top_level = {k: v for k, v in parsed.items() if k not in {"tool_name", "shell_mode", "timeout_sec"}}
                if isinstance(top_level, dict) and top_level:
                    args_obj = top_level
            candidates.append(
                {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "arguments": args_obj,
                    "shell_mode": parsed.get("shell_mode", "read_only"),
                    "timeout_sec": parsed.get("timeout_sec", 30),
                }
            )

    if candidates:
        return candidates

    fallback_req, _ = extract_tool_instruction(fallback_text)
    if isinstance(fallback_req, dict):
        candidates.append(fallback_req)
    return candidates


def _is_mutating_tool_candidate(tool_req: dict[str, Any]) -> bool:
    raw = str(tool_req.get("tool_name", "")).strip()
    canonical = _canonical_tool_name(raw)
    lowered = raw.lower()
    if canonical == "fs_apply_patch" or lowered in {"write", "write_file", "file_write"}:
        return True
    if canonical == "shell_run":
        mode = str(tool_req.get("shell_mode", "read_only")).strip().lower()
        return mode in {"mutating", "exec"}
    return False


def _semantic_tool_signature(tool_name: str, arguments: dict[str, Any], shell_mode: str | None) -> str:
    name = str(tool_name).strip()
    args = arguments or {}
    if name in {"fs_read", "editor_open"}:
        key = {"tool_name": name, "path": str(args.get("path", "")).strip()}
    elif name == "fs_list":
        key = {
            "tool_name": name,
            "path": str(args.get("path", ".")).strip() or ".",
            "recursive": bool(args.get("recursive", False)),
        }
    elif name == "fs_search":
        key = {
            "tool_name": name,
            "path": str(args.get("path", ".")).strip() or ".",
            "pattern": str(args.get("pattern", "")).strip(),
        }
    elif name == "fs_apply_patch":
        key = {
            "tool_name": name,
            "patch": str(args.get("patch", "")).strip(),
        }
    elif name == "shell_run":
        key = {
            "tool_name": name,
            "shell_mode": str(shell_mode or "read_only"),
            "command": str(args.get("command", "")).strip(),
        }
    elif name == "mcp_call":
        key = {
            "tool_name": name,
            "server_id": str(args.get("server_id", "")).strip(),
            "tool": str(args.get("tool", "")).strip(),
            "arguments": args.get("arguments", {}),
        }
    else:
        key = {"tool_name": name, "arguments": args}
    return json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _preprocess_tool_request_for_runtime(
    *,
    tool_req: dict[str, Any],
    user_message: str,
    model_text: str,
    workspace_root: str,
    tool_observations: list[dict[str, Any]],
    force_patch_intent: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tool_name_raw = str(tool_req.get("tool_name", "")).strip()
    raw_token = tool_name_raw.lower().replace("-", "_")
    if tool_name_raw and _tool_abi_mode() == "strict":
        if raw_token not in CANONICAL_EXTERNAL_TOOL_NAMES:
            return None, {
                "tool_name": tool_name_raw,
                "status": "blocked",
                "error": "invalid_tool_arguments",
                "detail": (
                    "Non-canonical tool name is rejected in strict ABI mode. "
                    f"Use one of {list(CANONICAL_EXTERNAL_TOOL_NAMES)}."
                ),
            }
    tool_name = _canonical_tool_name(tool_name_raw)
    emit_metric(
        "tool_name_canonical_seen_total",
        {
            "tool_name_raw": tool_name_raw or "(missing)",
            "tool_name_canonical": tool_name or "(missing)",
        },
    )
    if tool_name_raw and tool_name_raw != tool_name:
        emit_metric(
            "tool_alias_rewrite_total",
            {
                "tool_name_raw": tool_name_raw,
                "tool_name_canonical": tool_name,
            },
        )
    arguments = tool_req.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed_args = json.loads(arguments)
            arguments = parsed_args if isinstance(parsed_args, dict) else {}
        except Exception:
            arguments = {}
    if not isinstance(arguments, dict):
        return None, {
            "tool_name": tool_name_raw or tool_name,
            "status": "blocked",
            "error": "invalid_tool_arguments",
            "detail": "Tool arguments must be an object.",
        }

    pre_promote_tool_name = tool_name
    tool_name, arguments = _promote_tool_call_for_patch_intent(
        user_message=user_message,
        tool_name=tool_name,
        arguments=dict(arguments),
        tool_observations=tool_observations,
        force_patch_intent=force_patch_intent,
    )
    if pre_promote_tool_name != tool_name:
        emit_metric(
            "tool_fallback_promotion_total",
            {"from_tool": pre_promote_tool_name, "to_tool": tool_name},
        )

    if tool_name not in SUPPORTED_TOOL_NAMES:
        return None, {
            "tool_name": tool_name_raw or tool_name,
            "status": "blocked",
            "error": "unknown_tool_name",
            "detail": f"Unsupported tool `{tool_name_raw}`; use one of {sorted(SUPPORTED_TOOL_NAMES)}.",
        }

    raw_tool_token = str(tool_name_raw or "").strip().lower().replace("-", "_")
    find_replace_like_request = (
        raw_tool_token in FIND_REPLACE_TOOL_TOKENS
        or (
            tool_name == "fs_apply_patch"
            and _coerce_nonempty_text(arguments.get("filepath")) is not None
            and isinstance(arguments.get("old_string"), str)
            and isinstance(arguments.get("new_string"), str)
            and _coerce_nonempty_text(arguments.get("patch")) is None
        )
    )
    insert_at_end_like_request = (
        raw_tool_token in INSERT_AT_END_TOOL_TOKENS
        or (
            tool_name == "fs_apply_patch"
            and str(arguments.get("operation_kind") or "").strip().lower() == "append"
            and _coerce_nonempty_text(arguments.get("filepath")) is not None
            and isinstance(arguments.get("content"), str)
            and _coerce_nonempty_text(arguments.get("patch")) is None
        )
    )
    write_like_request = (
        raw_tool_token in {"write", "write_file", "file_write"}
        or (
            tool_name == "fs_apply_patch"
            and _coerce_nonempty_text(arguments.get("filepath")) is not None
            and (
                isinstance(arguments.get("content"), str)
                or isinstance(arguments.get("new_content"), str)
            )
            and _coerce_nonempty_text(arguments.get("patch")) is None
            and not find_replace_like_request
            and not insert_at_end_like_request
        )
    )

    if find_replace_like_request:
        write_prepared, write_err = _prepare_find_replace_patch(
            workspace_root=workspace_root,
            arguments=arguments,
        )
        if write_err is not None:
            err = {
                "tool_name": tool_name,
                "status": write_err.get("status", "blocked"),
                "error": write_err.get("error", "invalid_tool_arguments"),
                "detail": write_err.get("detail", "FindAndReplace preprocessing failed."),
            }
            return None, err
        assert write_prepared is not None
        arguments = dict(write_prepared["arguments"])
        write_meta = dict(write_prepared["meta"])
    elif insert_at_end_like_request:
        write_prepared, write_err = _prepare_insert_at_end_patch(
            workspace_root=workspace_root,
            arguments=arguments,
        )
        if write_err is not None:
            err = {
                "tool_name": tool_name,
                "status": write_err.get("status", "blocked"),
                "error": write_err.get("error", "invalid_tool_arguments"),
                "detail": write_err.get("detail", "InsertAtEnd preprocessing failed."),
            }
            return None, err
        assert write_prepared is not None
        arguments = dict(write_prepared["arguments"])
        write_meta = dict(write_prepared["meta"])
    elif write_like_request:
        write_prepared, write_err = _prepare_write_patch(
            workspace_root=workspace_root,
            arguments=arguments,
            user_message=user_message,
        )
        if write_err is not None:
            err = {
                "tool_name": tool_name,
                "status": write_err.get("status", "blocked"),
                "error": write_err.get("error", "invalid_tool_arguments"),
                "detail": write_err.get("detail", "Write preprocessing failed."),
            }
            return None, err
        assert write_prepared is not None
        arguments = dict(write_prepared["arguments"])
        write_meta = dict(write_prepared["meta"])
    else:
        write_meta = None

    if tool_name in {"fs_read", "editor_open"}:
        if not (isinstance(arguments.get("path"), str) and str(arguments.get("path")).strip()):
            alias_path = _deep_find_keyed_string(
                arguments,
                ("path", "file_path", "filepath", "file", "target_path", "filename", "name"),
            )
            if alias_path:
                arguments["path"] = alias_path
        if not (isinstance(arguments.get("path"), str) and str(arguments.get("path")).strip()):
            hint = _path_hint_from_message(user_message) or _path_hint_from_message(model_text)
            if hint:
                arguments["path"] = hint

    if tool_name == "fs_search":
        pattern = arguments.get("pattern")
        if isinstance(pattern, dict):
            pattern = _deep_find_keyed_string(pattern, ("pattern", "query", "text", "value"))
        if not (isinstance(pattern, str) and pattern.strip()):
            alias_hit = _deep_find_keyed_string(
                arguments,
                ("pattern", "query", "term", "text", "keyword", "needle", "search", "search_term", "regex", "value"),
            )
            if alias_hit:
                pattern = alias_hit
        if not (isinstance(pattern, str) and pattern.strip()):
            pattern = _search_pattern_hint_from_message(user_message) or _search_pattern_hint_from_message(model_text)
        if isinstance(pattern, str) and pattern.strip():
            arguments["pattern"] = pattern.strip()
        if not (isinstance(arguments.get("path"), str) and str(arguments.get("path")).strip()):
            arguments["path"] = "."
        if not (isinstance(arguments.get("pattern"), str) and str(arguments.get("pattern")).strip()):
            return None, {
                "tool_name": tool_name,
                "status": "blocked",
                "error": "invalid_tool_arguments",
                "detail": "Unable to infer fs_search.pattern from model output or user request.",
            }
        # Bound search payload shape to avoid oversized result payload terminalization errors.
        try:
            max_files = int(arguments.get("max_files", 250))
        except Exception:
            max_files = 250
        try:
            max_matches = int(arguments.get("max_matches", 120))
        except Exception:
            max_matches = 120
        arguments["max_files"] = max(1, min(max_files, 500))
        arguments["max_matches"] = max(1, min(max_matches, 200))

    if tool_name == "fs_apply_patch":
        if not (isinstance(arguments.get("path"), str) and str(arguments.get("path")).strip()):
            path_hit = _deep_find_keyed_string(
                arguments,
                ("path", "file_path", "filepath", "target_path", "filename", "name"),
            )
            if path_hit:
                arguments["path"] = path_hit
            elif isinstance(arguments.get("file"), str) and str(arguments.get("file")).strip():
                arguments["path"] = str(arguments.get("file")).strip()
        # Preserve raw patch text exactly; trimming can alter final newline semantics.
        patch_text = _coerce_nonempty_text_preserve(arguments.get("patch"))
        if patch_text is None:
            patch_text = _deep_find_keyed_string(
                arguments,
                ("patch", "diff", "patch_text", "unified_diff", "changes", "edit", "edits", "text", "value"),
            )
        if patch_text is None:
            patch_text = _synthesize_patch_from_arguments(workspace_root, arguments)
        if patch_text is not None:
            arguments["patch"] = patch_text
        if not (isinstance(arguments.get("patch"), str) and str(arguments.get("patch")).strip()):
            return None, {
                "tool_name": tool_name,
                "status": "blocked",
                "error": "invalid_tool_arguments",
                "detail": "Unable to infer fs_apply_patch.patch from model output.",
            }

    if tool_name == "shell_run":
        command_text = _coerce_nonempty_text(arguments.get("command"))
        if command_text is None:
            command_text = _deep_find_keyed_string(
                arguments,
                ("command", "cmd", "shell_command", "script", "command_text", "text", "value"),
            )
        if command_text is None:
            command_text = _shell_command_hint_from_message(user_message) or _shell_command_hint_from_message(model_text)
        if command_text is not None:
            rewritten, rewrite_reason = _rewrite_shell_command_for_env(command_text)
            arguments["command"] = rewritten
            if rewrite_reason:
                emit_metric("tool_shell_rewrite_total", {"reason": rewrite_reason})
        if not (isinstance(arguments.get("command"), str) and str(arguments.get("command")).strip()):
            return None, {
                "tool_name": tool_name,
                "status": "blocked",
                "error": "invalid_tool_arguments",
                "detail": "Unable to infer shell_run.command from model output or user request.",
            }

    if tool_name == "mcp_call":
        server_id = _coerce_nonempty_text(arguments.get("server_id"))
        if server_id is None:
            server_id = _coerce_nonempty_text(
                _deep_find_keyed_string(arguments, ("server_id", "server", "mcp_server_id"))
            )
        if server_id is not None:
            arguments["server_id"] = server_id

        tool_label = _coerce_nonempty_text(arguments.get("tool"))
        if tool_label is None:
            tool_label = _coerce_nonempty_text(
                _deep_find_keyed_string(arguments, ("tool", "tool_name", "name"))
            )
        if tool_label is not None:
            arguments["tool"] = tool_label

        if not isinstance(arguments.get("arguments"), dict):
            alias_args = arguments.get("args")
            arguments["arguments"] = dict(alias_args) if isinstance(alias_args, dict) else {}

        if not (isinstance(arguments.get("server_id"), str) and str(arguments.get("server_id")).strip()):
            return None, {
                "tool_name": tool_name,
                "status": "blocked",
                "error": "invalid_tool_arguments",
                "detail": "mcp_call requires server_id.",
            }
        if not (isinstance(arguments.get("tool"), str) and str(arguments.get("tool")).strip()):
            return None, {
                "tool_name": tool_name,
                "status": "blocked",
                "error": "invalid_tool_arguments",
                "detail": "mcp_call requires tool.",
            }

    shell_mode = str(tool_req.get("shell_mode") or "read_only")
    timeout_sec = int(tool_req.get("timeout_sec", 30))
    prepared = {
        "type": "tool_call",
        "tool_name": tool_name,
        "arguments": arguments,
        "shell_mode": shell_mode,
        "timeout_sec": timeout_sec,
    }
    if write_meta is not None:
        prepared["_write_meta"] = write_meta
    return prepared, None


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__)
def cli():
    """OpenVegas -- Terminal Arcade for Developers"""
    pass


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--otp", is_flag=True, help="Use magic link (OTP) login")
def login(otp: bool):
    """Log in to OpenVegas."""
    _load_openvegas_env_defaults_from_dotenv()
    from openvegas.auth import SupabaseAuth, AuthError
    from openvegas.config import (
        get_session,
        load_refresh_from_platform_store,
        request_touchid_unlock,
        require_touchid_unlock_for_refresh_storage,
        touchid_enabled,
        touchid_supported,
    )

    try:
        auth = SupabaseAuth()
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        return

    if not otp:
        try:
            session = get_session()
            refresh_storage = str(session.get("refresh_storage", "") or "")
            has_keychain_refresh = bool(str(load_refresh_from_platform_store() or "").strip())
            should_try_touchid = bool(touchid_enabled() and touchid_supported() and (
                require_touchid_unlock_for_refresh_storage(refresh_storage) or has_keychain_refresh
            ))
            if should_try_touchid:
                console.print("[dim]Attempting Touch ID unlock...[/dim]")
                if request_touchid_unlock():
                    try:
                        auth.refresh_token()
                        console.print("[green]Unlocked with Touch ID.[/green]")
                        return
                    except Exception as e:
                        console.print(f"[yellow]Touch ID unlock failed ({e}); falling back to email/password.[/yellow]")
                else:
                    console.print("[yellow]Touch ID was unavailable or declined; falling back to email/password.[/yellow]")
            elif touchid_enabled() and not touchid_supported():
                console.print("[yellow]Touch ID enabled but unavailable on this setup (Keychain/keyring/LocalAuthentication unavailable); falling back to email/password.[/yellow]")
        except Exception:
            # Never block login on Touch ID preflight problems.
            pass

    email = Prompt.ask("Email")

    if otp:
        auth.login_with_otp(email)
        console.print("[green]Magic link sent! Check your email.[/green]")
    else:
        password = Prompt.ask("Password", password=True)
        try:
            result = auth.login_with_email(email, password)
            console.print(
                f"[green]Logged in as {result['email']}[/green]\n"
                f"[dim]user_id: {result.get('user_id', '')}[/dim]"
            )
        except Exception as e:
            console.print(f"[red]Login failed: {e}[/red]")


@cli.command("doctor-auth")
def doctor_auth():
    """Print one-line auth readiness diagnostics."""
    _load_openvegas_env_defaults_from_dotenv()
    import sys as _sys
    from openvegas.config import (
        get_session,
        load_refresh_from_platform_store,
        platform_keychain_available,
        touchid_enabled,
        touchid_supported,
    )

    session = get_session()
    refresh_storage = str(session.get("refresh_storage", "") or "")
    has_keychain_token = bool(str(load_refresh_from_platform_store() or "").strip())
    enabled = bool(touchid_enabled())
    keychain_ok = bool(platform_keychain_available())
    supported = bool(touchid_supported())
    ready = bool(enabled and supported and keychain_ok and has_keychain_token)

    console.print(
        "doctor-auth: "
        f"ready={'1' if ready else '0'} "
        f"touchid_enabled={'1' if enabled else '0'} "
        f"touchid_supported={'1' if supported else '0'} "
        f"keychain_available={'1' if keychain_ok else '0'} "
        f"keychain_token={'1' if has_keychain_token else '0'} "
        f"refresh_storage={refresh_storage or 'none'} "
        f"python={_sys.executable}"
    )


@cli.command("whoami")
def whoami():
    """Show current authenticated user from local session token."""
    _load_openvegas_env_defaults_from_dotenv()
    import base64
    import json as _json
    from openvegas.config import get_session

    session = get_session() or {}
    token = str(session.get("access_token", "") or "").strip()
    if not token:
        console.print("[yellow]Not logged in.[/yellow]")
        return

    email = ""
    user_id = ""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8", errors="ignore")
            claims = _json.loads(decoded)
            if isinstance(claims, dict):
                email = str(claims.get("email", "") or "")
                user_id = str(claims.get("sub", "") or "")
    except Exception:
        pass

    refresh_storage = str(session.get("refresh_storage", "") or "none")
    if not email and not user_id:
        console.print(f"whoami: refresh_storage={refresh_storage} email=unknown user_id=unknown")
        return

    console.print(
        f"whoami: refresh_storage={refresh_storage} "
        f"email={email or 'unknown'} user_id={user_id or 'unknown'}"
    )


@cli.command()
def signup():
    """Create a new OpenVegas account."""
    from openvegas.auth import SupabaseAuth, AuthError

    try:
        auth = SupabaseAuth()
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        return

    email = Prompt.ask("Email")
    password = Prompt.ask("Password", password=True)

    try:
        result = auth.signup(email, password)
        console.print(
            f"[green]Account created for {result['email']}[/green]\n"
            f"[dim]user_id: {result.get('user_id', '')}[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Signup failed: {e}[/red]")


@cli.command()
def logout():
    """Log out of OpenVegas."""
    from openvegas.auth import SupabaseAuth
    try:
        auth = SupabaseAuth()
        auth.logout()
    except Exception:
        from openvegas.config import clear_session
        clear_session()
    console.print("Logged out.")


@cli.command("quit")
def quit_session():
    """Quick-lock local session but keep refresh/keychain for Touch ID unlock."""
    from openvegas.config import clear_access_token_keep_refresh

    try:
        clear_access_token_keep_refresh()
    except Exception as e:
        console.print(f"[red]Unable to lock session: {e}[/red]")
        return

    console.print("Session locked. Use `openvegas login` to unlock with Touch ID.")


@cli.command()
def status():
    """Show balance, tier, and stats."""
    async def _status():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_balance()
            console.print(Panel(
                f"[bold]Balance:[/bold] {data.get('balance', '0.00')} $V\n"
                f"[bold]Tier:[/bold] {data.get('tier', 'free')}\n"
                f"[bold]Lifetime minted:[/bold] {data.get('lifetime_minted', '0.00')} $V\n"
                f"[bold]Lifetime won:[/bold] {data.get('lifetime_won', '0.00')} $V",
                title="OpenVegas Status",
                border_style="cyan",
            ))
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    run_async(_status())


# ---------------------------------------------------------------------------
# Wallet commands
# ---------------------------------------------------------------------------

@cli.command()
def balance():
    """Show your $V balance."""
    async def _balance():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_balance()
            console.print(f"[bold]{data.get('balance', '0.00')} $V[/bold]")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_balance())


@cli.command()
def history():
    """Show transaction history."""
    async def _history():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_billing_activity()
            entries = data.get("entries", [])
            if not entries:
                console.print("[dim]No transactions yet.[/dim]")
                return

            table = Table(title="Transaction History")
            table.add_column("Time", style="dim")
            table.add_column("Type")
            table.add_column("Amount", justify="right")
            table.add_column("Status")
            table.add_column("Reference")

            for entry in entries[:20]:
                kind = str(entry.get("type", "top_up"))
                if kind == "gameplay":
                    vv = str(entry.get("amount_v_2dp") or entry.get("amount_v") or "0.00")
                    try:
                        vnum = Decimal(vv)
                        amount = f"{'+' if vnum > 0 else ''}{vnum.quantize(Decimal('0.01'))} $V"
                    except Exception:
                        amount = f"{vv} $V"
                else:
                    usd = str(entry.get("amount_usd") or "0.00")
                    vv = str(entry.get("amount_v_2dp") or entry.get("amount_v") or "0.00")
                    amount = f"${usd} · +{vv} $V"
                table.add_row(
                    str(entry.get("time", ""))[:19].replace("T", " "),
                    kind,
                    amount,
                    str(entry.get("status", "")),
                    entry.get("reference_id", "")[:20],
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_history())


@cli.command()
@click.argument("amount")
@click.option("--saved/--no-saved", "use_saved", default=True, show_default=True, help="Attempt saved-card charge before checkout flow.")
def deposit(amount: str, use_saved: bool):
    """Buy $V with cash (returns Stripe checkout URL)."""
    async def _deposit():
        from openvegas.client import OpenVegasClient, APIError
        try:
            amt = Decimal(amount)
        except Exception:
            console.print("[red]Invalid amount. Example: openvegas deposit 10[/red]")
            return

        try:
            client = OpenVegasClient()
            if use_saved:
                try:
                    saved = await client.get_saved_topup_payment_method()
                except APIError:
                    saved = {"available": False}
                if bool(saved.get("available")):
                    brand = str(saved.get("brand") or "card").upper()
                    last4 = str(saved.get("last4") or "****")
                    prompt = f"Charge saved {brand} ••••{last4} for ${amt.quantize(Decimal('0.01'))}?"
                    if click.confirm(prompt, default=True):
                        with console.status("[bold cyan]Calculating top-up preview...[/bold cyan]", spinner="dots"):
                            preview = await client.preview_topup_checkout(amt)
                        console.print(
                            "[bold]Preview:[/bold] "
                            f"gross={preview.get('v_credit_gross')} $V, "
                            f"repay={preview.get('repay_v')} $V, "
                            f"net={preview.get('net_credit_v')} $V"
                        )
                        with console.status("[bold cyan]Charging saved card via Stripe...[/bold cyan]", spinner="dots"):
                            charged = await client.charge_saved_topup(amt)
                        console.print(f"[green]Top-up ID:[/green] {charged.get('topup_id')}")
                        console.print(f"[green]Status:[/green] {charged.get('status')}")
                        console.print("[green]Saved card charged successfully.[/green]")
                        return
            preview = await client.preview_topup_checkout(amt)
            console.print(
                "[bold]Preview:[/bold] "
                f"gross={preview.get('v_credit_gross')} $V, "
                f"repay={preview.get('repay_v')} $V, "
                f"net={preview.get('net_credit_v')} $V"
            )
            console.print(
                "[dim]Final repayment is computed at settlement and may differ "
                "if outstanding principal changes before payment completion.[/dim]"
            )
            if not click.confirm("Proceed to create checkout?", default=True):
                console.print("[yellow]Checkout cancelled.[/yellow]")
                return
            data = await client.create_topup_checkout(amt)
            console.print(f"[green]Top-up ID:[/green] {data.get('topup_id')}")
            console.print(f"[green]Status:[/green] {data.get('status')}")
            checkout_url = str(data.get("checkout_url") or "")
            if checkout_url:
                target_url = checkout_url
                if _is_simulated_checkout_url(checkout_url):
                    target_url = f"{str(client.base_url).rstrip('/')}/ui/payments"
                    console.print(
                        "[yellow]Backend returned simulated checkout URL. "
                        "Opening payments UI instead.[/yellow]"
                    )
                console.print(f"[bold cyan]Checkout URL:[/bold cyan] {checkout_url}")
                try:
                    opened = webbrowser.open(target_url, new=2)
                except Exception:
                    opened = False
                if opened:
                    console.print(f"[green]Opened in browser:[/green] {target_url}")
                else:
                    console.print(f"[yellow]Could not auto-open browser. Open manually:[/yellow] {target_url}")
            else:
                console.print("[yellow]No checkout URL returned. Try again or check deposit status.[/yellow]")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_deposit())


@cli.command("deposit-status")
@click.argument("topup_id")
def deposit_status(topup_id: str):
    """Check status of a Stripe top-up."""
    async def _status():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.get_topup_status(topup_id)
            console.print(
                f"[bold]Status:[/bold] {data.get('status')} | "
                f"[bold]Credit:[/bold] {data.get('v_credit', '0')} $V"
            )
            if data.get("checkout_url"):
                console.print(f"[dim]Checkout URL: {data['checkout_url']}[/dim]")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_status())


# ---------------------------------------------------------------------------
# Mint commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--amount", type=float, required=True, help="USD amount to burn")
@click.option(
    "--provider", type=click.Choice(["anthropic", "openai", "gemini"]), required=True
)
@click.option(
    "--mode", type=click.Choice(["solo", "split", "sponsor"]), default="solo"
)
def mint(amount: float, provider: str, mode: str):
    """Mint $V by burning LLM tokens (BYOK)."""
    from openvegas.config import get_provider_key

    api_key = get_provider_key(provider)
    if not api_key:
        console.print(
            f"[red]No API key for {provider}. Run: openvegas keys set {provider}[/red]"
        )
        return

    rates_display = {"solo": "standard rate", "split": "+8% $V bonus", "sponsor": "+15% $V bonus"}

    async def _mint():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()

            # 1. Get challenge
            challenge = await client.create_mint_challenge(amount, provider, mode)

            # 2. Show disclosure
            console.print(Panel(
                f"[bold]Mint Mode:[/bold] {mode.title()} Mint ({rates_display[mode]})\n"
                f"[bold]Provider:[/bold] {provider} ({challenge.get('model', '')})\n"
                f"[bold]Target burn ceiling:[/bold] up to ~${amount:.2f} on your account\n"
                f"[bold]Max $V credit cap:[/bold] {challenge.get('max_credit_v', '')} $V\n"
                f"[bold]Note:[/bold] actual burn depends on generated token usage and may be lower.\n"
                f"[bold]Your task:[/bold] {challenge.get('task_prompt', '')[:80]}...",
                title="OpenVegas Mint",
                border_style="green",
            ))

            if not Confirm.ask("Proceed with mint?"):
                console.print("[yellow]Mint cancelled.[/yellow]")
                return

            # 3. Send to backend for proxied mint
            console.print(
                "[dim]Sending key to server for proxied mint "
                "(key used once, never stored)...[/dim]"
            )

            result = await client.verify_mint(
                challenge["id"], challenge["nonce"],
                provider, challenge["model"], api_key,
            )

            console.print(
                f"[bold green]Minted {result['v_credited']} $V[/bold green] "
                f"(actual burn ~${float(result['cost_usd']):.4f} on {provider})"
            )

        except APIError as e:
            console.print(f"[red]Mint failed: {e.detail}[/red]")

    run_async(_mint())


# ---------------------------------------------------------------------------
# Keys management
# ---------------------------------------------------------------------------

@cli.group()
def keys():
    """Manage provider API keys."""
    pass


@keys.command("set")
@click.argument("provider", type=click.Choice(["anthropic", "openai", "gemini"]))
def keys_set(provider: str):
    """Set API key for a provider (stored locally)."""
    from openvegas.config import set_provider_key
    api_key = Prompt.ask(f"Enter {provider} API key", password=True)
    set_provider_key(provider, api_key)
    console.print(f"[green]{provider} API key saved to ~/.openvegas/config.json[/green]")


@keys.command("list")
def keys_list():
    """Show which providers have keys configured."""
    from openvegas.config import load_config
    config = load_config()
    providers = config.get("providers", {})
    for p in ["openai", "anthropic", "gemini"]:
        has_key = bool(providers.get(p, {}).get("api_key"))
        status = "[green]configured[/green]" if has_key else "[dim]not set[/dim]"
        console.print(f"  {p}: {status}")


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("game", type=click.Choice(["horse", "skillshot"]))
@click.option("--stake", type=float, required=True, help="Budget cap for horse ($V) or stake for other games")
@click.option("--horse", type=int, default=None, help="Horse number (horse racing only)")
@click.option(
    "--type", "bet_type",
    type=click.Choice(["win", "place", "show"]), default="win",
)
@click.option("--render/--no-render", default=True, help="Render terminal animation/reveal when available")
def play(
    game: str,
    stake: float,
    horse: int,
    bet_type: str,
    render: bool,
):
    """Play a game and wager $V."""
    _load_openvegas_env_defaults_from_dotenv()

    async def _play():
        import json
        import uuid
        from openvegas.casino.constants import min_game_wager_v
        from openvegas.client import OpenVegasClient, APIError
        from openvegas.games.base import GameResult
        from openvegas.games.horse_racing import HorseRacing
        from openvegas.games.skill_shot import SkillShotGame

        try:
            client = OpenVegasClient()
            force_win_mode = _win_always_enabled()
            balance_before_text = ""
            try:
                bal = await client.get_balance()
                bal_v = Decimal(str(bal.get("balance", "0")))
                balance_before_text = f"[dim]Balance before play: {bal_v.quantize(Decimal('0.01'))} $V[/dim]"
            except Exception:
                balance_before_text = ""

            min_wager = float(min_game_wager_v())
            if stake < min_wager:
                console.print(f"[red]Stake must be at least {min_wager:.2f} $V.[/red]")
                return

            if game == "horse":
                if stake <= 0:
                    console.print("[red]Stake must be greater than 0.[/red]")
                    return

                quote = await client.create_horse_quote(
                    bet_type=bet_type,
                    budget_v=Decimal(str(stake)),
                    idempotency_key=f"cli-horse-quote-{uuid.uuid4()}",
                )
                rows = list(quote.get("horses", []) or [])
                if not rows:
                    console.print("[red]No horses returned for quote.[/red]")
                    return

                table = Table(title=f"Horse Board ({bet_type})")
                table.add_column("#", justify="right")
                table.add_column("Horse")
                table.add_column("Odds", justify="right")
                table.add_column("Eff Mult", justify="right")
                table.add_column("Unit Price", justify="right")
                table.add_column("Max Units", justify="right")
                table.add_column("Debit", justify="right")
                table.add_column("Payout If Hit", justify="right")
                table.add_column("Selectable", justify="right")
                selectable_choices: list[str] = []
                for row in rows:
                    selectable = bool(row.get("selectable", False))
                    if selectable:
                        selectable_choices.append(str(row.get("number")))
                    table.add_row(
                        str(row.get("number", "")),
                        str(row.get("name", "")),
                        str(row.get("odds", "")),
                        str(row.get("effective_multiplier", "")),
                        str(row.get("unit_price_v", "")),
                        str(row.get("max_units", "")),
                        str(row.get("debit_v", "")),
                        str(row.get("payout_if_hit_v", "")),
                        "[green]yes[/green]" if selectable else "[red]no[/red]",
                    )
                console.print(table)

                if not selectable_choices:
                    console.print("[red]Budget too low for any horse position.[/red]")
                    return

                horse_choice = horse
                if horse_choice is None:
                    horse_choice = int(
                        Prompt.ask(
                            "Choose horse number",
                            choices=selectable_choices,
                            default=selectable_choices[0],
                        )
                    )
                selected = next((r for r in rows if int(r.get("number", -1)) == int(horse_choice)), None)
                if selected is None:
                    console.print("[red]Selected horse not in quote board.[/red]")
                    return
                if not bool(selected.get("selectable", False)):
                    console.print("[red]Selected horse is not selectable for this budget.[/red]")
                    return

                console.print(Panel(
                    f"[bold]Quote ID:[/bold] {quote.get('quote_id')}\n"
                    f"[bold]Budget:[/bold] {stake:.6f} $V\n"
                    f"[bold]Horse:[/bold] #{selected.get('number')} {selected.get('name')}\n"
                    f"[bold]Odds:[/bold] {selected.get('odds')}\n"
                    f"[bold]Debit:[/bold] {selected.get('debit_v')} $V\n"
                    f"[bold]Payout If Hit:[/bold] {selected.get('payout_if_hit_v')} $V\n"
                    f"[bold]Expires:[/bold] {quote.get('expires_at')}",
                    title="Horse Quote Review",
                    border_style="cyan",
                ))

                if not Confirm.ask("Proceed with quoted horse play?", default=True):
                    console.print("[yellow]Cancelled.[/yellow]")
                    return

                result = await client.play_horse_quote(
                    quote_id=str(quote.get("quote_id", "")),
                    horse=int(horse_choice),
                    idempotency_key=f"cli-horse-play-{uuid.uuid4()}",
                    demo_mode=force_win_mode,
                )
            else:
                bet = {"amount": stake, "type": bet_type}
                if force_win_mode:
                    result = await client.play_game_demo(game, bet)
                else:
                    result = await client.play_game(game, bet)

            net = Decimal(str(result.get("net", "0")))
            payout = Decimal(str(result.get("payout", "0")))
            bet_amount = Decimal(str(result.get("bet_amount", stake)))
            game_id = str(result.get("game_id", ""))

            if render:
                renderer_cls = {
                    "horse": HorseRacing,
                    "skillshot": SkillShotGame,
                }.get(game)
                if renderer_cls:
                    gr = GameResult(
                        game_id=game_id,
                        player_id="",
                        bet_amount=bet_amount,
                        payout=payout,
                        net=net,
                        outcome_data=result.get("outcome_data", {}) or {},
                        server_seed="",
                        server_seed_hash=str(result.get("server_seed_hash", "")),
                        client_seed="",
                        nonce=0,
                        provably_fair=bool(result.get("provably_fair", True)),
                    )
                    await renderer_cls().render(gr, console)

            result_lines: list[str] = []
            if net > 0:
                result_lines.append(f"[bold green]Won {payout} $V! (+{net} net)[/bold green]")
            else:
                result_lines.append(f"[red]Lost {bet_amount} $V.[/red]")

            if result.get("provably_fair"):
                result_lines.append(f"[dim]Verify: {verify_hint_for_result(game_id, False)}[/dim]")
            if balance_before_text:
                result_lines.append(balance_before_text)

            render_result_panel(
                console,
                "\n".join(result_lines),
                is_win=net > 0,
                animation_enabled=bool(load_config().get("animation", True)),
                title="Result",
            )

        except APIError as e:
            detail = str(e.detail)
            try:
                parsed = json.loads(detail)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict) and parsed.get("error"):
                console.print(f"[red]{parsed.get('error')}: {parsed.get('detail', detail)}[/red]")
            else:
                console.print(f"[red]{e.detail}[/red]")

    run_async(_play())


# ---------------------------------------------------------------------------
# AI Inference
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("prompt")
@click.option("--provider", default=None, help="Provider (openai/anthropic/gemini)")
@click.option("--model", default=None, help="Model ID")
def ask(prompt: str, provider: str | None, model: str | None):
    """Use $V for AI inference."""
    from openvegas.config import get_default_provider, get_default_model

    _load_openvegas_env_defaults_from_dotenv()

    if provider is None:
        provider = get_default_provider()
    if model is None:
        model = get_default_model(provider)

    async def _ask():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            result = await client.ask(prompt, provider, model)
            console.print(result.get("text", ""))
            console.print(
                f"\n[dim]Cost: {result.get('v_cost', '?')} $V | "
                f"Model: {provider}/{model}[/dim]"
            )
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_ask())


def _resolve_default_dealer_sprite_path(workspace_root: str) -> Path:
    env_path = str(os.getenv("OPENVEGAS_DEALER_SPRITE_PATH", "")).strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    candidates = [
        Path(workspace_root) / "ui" / "assets" / "sprites" / "dealers" / "ov_dealer_female_tux_v1.png",
        Path(__file__).resolve().parents[1] / "ui" / "assets" / "sprites" / "dealers" / "ov_dealer_female_tux_v1.png",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _build_cli_sprite_renderer(*, dealer_sprite: bool, workspace_root: str):
    if not dealer_sprite:
        return None
    try:
        from openvegas.tui.sprite_render import TerminalSpriteRenderer
    except Exception:
        emit_metric("avatar_sprite_render_fail_total", {"surface": "cli", "reason": "renderer_import_failed"})
        return None

    sprite_path = _resolve_default_dealer_sprite_path(workspace_root)
    renderer = TerminalSpriteRenderer(sprite_path)
    if not renderer.enabled():
        emit_metric(
            "avatar_sprite_render_fail_total",
            {"surface": "cli", "reason": str(getattr(renderer, "reason", "") or "renderer_disabled")},
        )
        console.print(
            "[dim]Dealer sprite unavailable "
            f"(reason={getattr(renderer, 'reason', 'unknown')}). "
            "Using unicode fallback.[/dim]"
        )
        return None
    console.print(f"[dim]Dealer sprite enabled: {renderer.path}[/dim]")
    return renderer


@cli.command()
@click.option("--provider", default=None, help="Provider (openai/anthropic/gemini)")
@click.option("--model", default=None, help="Model ID")
@click.option(
    "--dealer-sprite/--no-dealer-sprite",
    default=False,
    help="Enable truecolor dealer sprite rendering (defaults to emoji/unicode style).",
)
def chat(provider: str | None, model: str | None, dealer_sprite: bool):
    """OpenVegas conversational shell with slash commands and /ui handoff."""
    from openvegas.client import APIError, OpenVegasClient
    from openvegas.config import get_default_provider, get_default_model

    _load_openvegas_env_defaults_from_dotenv()

    current_provider = provider or get_default_provider()
    current_model = model or get_default_model(current_provider)
    current_thread_id: str | None = None
    current_run_id: str | None = None
    current_run_version: int = 0
    current_signature: str = "sha256:"
    runtime_session_id: str = str(uuid.uuid4())
    workspace_root = str(Path.cwd().resolve())
    workspace_git_root = workspace_root
    workspace_fp = workspace_fingerprint(workspace_root, workspace_git_root)
    plan_mode = False
    approval_mode = "ask"
    conversation_mode = "persistent"
    context_warning_emitted = False
    web_search_requested = True
    last_web_search_effective = False
    last_web_search_used = False
    last_web_search_retry_without_tool = False
    voice_transcribe_requested = True
    last_voice_transcribe_effective = False
    last_voice_transcribe_used = False
    voice_transcribe_model = str(os.getenv("OPENVEGAS_CHAT_SPEECH_MODEL", "gpt-4o-mini-transcribe")).strip() or "gpt-4o-mini-transcribe"
    voice_transcribe_language = str(os.getenv("OPENVEGAS_CHAT_SPEECH_LANGUAGE", "")).strip() or None
    pending_attachments: list[PendingAttachment] = []
    uploaded_attachment_cache: dict[str, str] = {}
    attachment_event_sequence = 0
    rendered_ui_event_keys: set[tuple[str, str, str, int]] = set()
    attachment_context_for_turn = ""
    voice_transcript_context_for_turn = ""
    attachment_markers_for_turn: list[str] = []
    attachment_file_ids_for_turn: list[str] = []
    voice_button = VoiceButton(console)
    pending_voice_prefill: str | None = None
    pending_voice_meta: dict[str, Any] | None = None
    prompt_input_active = False
    max_attachments_per_turn = max(1, min(20, int(os.getenv("OPENVEGAS_CHAT_MAX_ATTACHMENTS", "3"))))
    max_attachment_bytes = max(1024, int(os.getenv("OPENVEGAS_CHAT_MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024))))
    attachment_preview_max_chars = max(512, int(os.getenv("OPENVEGAS_CHAT_ATTACHMENT_PREVIEW_MAX_CHARS", "6000")))
    auto_attach_deadline_ms = max(100, int(os.getenv("OPENVEGAS_CHAT_ATTACH_RESOLVE_DEADLINE_MS", "500")))
    attach_search_home_enabled = str(os.getenv("OPENVEGAS_CHAT_ATTACH_SEARCH_HOME", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    auto_clipboard_image_attach = str(
        os.getenv("OPENVEGAS_CHAT_AUTO_CLIPBOARD_IMAGE_ATTACH", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}
    attachment_status_style = str(
        os.getenv("OPENVEGAS_CHAT_ATTACHMENT_STATUS_STYLE", "dim")
    ).strip().lower()
    last_attachment_status_row: str | None = None
    chat_transcript: list[dict[str, Any]] = []
    last_assistant_text_for_turn: str = ""
    unicode_ok = _supports_unicode_output()
    last_successful_tool: str | None = None
    client = OpenVegasClient()
    use_prompt_toolkit_chat = (
        str(os.getenv("OPENVEGAS_CHAT_PROMPT_TOOLKIT", "1")).strip().lower() in {"1", "true", "yes", "on"}
        and bool(getattr(sys.stdin, "isatty", lambda: False)())
        and bool(getattr(sys.stdout, "isatty", lambda: False)())
    )
    prompt_toolkit_unavailable_warned = False
    chat_prompt_session: Any | None = None
    chat_prompt_bindings: Any | None = None
    def _env_flag(name: str, default: str = "0") -> bool:
        return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}

    show_user_diagnostics = _env_flag("OPENVEGAS_CHAT_USER_DIAGNOSTICS", "0")
    show_model_meta = show_user_diagnostics and _env_flag("OPENVEGAS_CHAT_SHOW_MODEL_META", "0")
    show_token_usage = show_user_diagnostics and _env_flag("OPENVEGAS_CHAT_SHOW_TOKEN_USAGE", "0")
    show_web_diagnostics = show_user_diagnostics and _env_flag("OPENVEGAS_CHAT_SHOW_WEB_DIAGNOSTICS", "0")
    show_stream_status = show_user_diagnostics and _env_flag("OPENVEGAS_CHAT_SHOW_STREAM_STATUS", "0")
    show_user_echo = _env_flag("OPENVEGAS_CHAT_SHOW_USER_ECHO", "1")
    allow_model_switch = _env_flag("OPENVEGAS_CHAT_ALLOW_MODEL_SWITCH", "0")
    preferred_openai_models = [
        "gpt-5.4",
        "gpt-5.1",
        "gpt-5",
        "gpt-5.3-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex",
        "gpt-5.1-codex-mini",
    ]

    def _status_actor() -> str:
        return f"{current_provider}/{current_model}" if show_model_meta else "openvegas"

    def _pick_preferred_model(enabled_models: list[str], current: str) -> str:
        enabled = [str(m or "").strip() for m in enabled_models if str(m or "").strip()]
        if not enabled:
            return current
        if current in enabled and ("codex" in current.lower() or current in preferred_openai_models):
            return current
        enabled_lc = {m.lower(): m for m in enabled}
        for preferred in preferred_openai_models:
            chosen = enabled_lc.get(preferred.lower())
            if chosen:
                return chosen
        return enabled[0]

    cfg = load_config()
    verbose_tool_events = normalize_tool_event_density(str(cfg.get("tool_event_density", "compact"))) == "verbose"
    _ = cfg.get("chat_style", "codex")  # retained for backward compatibility only
    _ = cfg.get("approval_ui", "menu")  # retained for backward compatibility only
    session_approval = SessionApprovalState()
    dealer_enabled = str(os.getenv("OPENVEGAS_CLI_DEALER_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}
    cli_sprite_renderer = _build_cli_sprite_renderer(dealer_sprite=bool(dealer_sprite), workspace_root=workspace_root)
    dealer_panel = DealerPanel(
        console=console,
        enabled=dealer_enabled,
        label="openvegas",
        sprite_renderer=cli_sprite_renderer,
    )

    def _show_help() -> None:
        console.print("Chat Commands:")
        console.print("/help - show commands")
        if allow_model_switch:
            console.print("/provider <openai|anthropic|gemini> [model] - switch provider")
            console.print("/model <model_id> - switch model")
        console.print("/plan [on|off] - toggle plan mode (read-only intent)")
        console.print("/approve <ask|allow|exclude> - mutating tool approval mode")
        console.print("/style - deprecated (minimal style is always on)")
        console.print("/verbose-tools <on|off> - detailed tool event output")
        console.print("/approvals - show session approval overrides")
        console.print("/status - show current chat context")
        console.print("/web - show effective web search status (always on)")
        console.print("/voice - start/stop voice capture mode")
        console.print("/mcp <list|health|call> - MCP server list/health/tool call")
        console.print("/attach <path> - attach a file for the next turn")
        console.print("/paste - attach paths/images from clipboard")
        console.print("/detach <name|id> - remove one pending attachment")
        console.print("/clear-attachments - remove all pending attachments")
        console.print("/cancel-uploads - alias for /clear-attachments")
        console.print("/retry-failed - reset failed uploads and retry on next send")
        console.print("/attachments - list pending attachments")
        console.print("/legend - show icon/status legend")
        console.print("/export-transcript <path> - write transcript JSON (with attachment markers)")
        console.print("/tooling - show local tool runtime status")
        console.print("/ui - jump into game UI (blocked on pending orchestration state)")
        console.print("/exit - exit chat")

    def _mcp_feature_enabled() -> bool:
        return str(os.getenv("OPENVEGAS_ENABLE_MCP", "0")).strip().lower() in {"1", "true", "yes", "on"}

    def _icon(name: str) -> str:
        nerd_font = str(os.getenv("OPENVEGAS_CHAT_NERD_FONT", "auto")).strip().lower()
        use_nerd = nerd_font in {"1", "true", "yes", "on"}
        if nerd_font == "auto":
            term_program = str(os.getenv("TERM_PROGRAM", "")).lower()
            use_nerd = "warp" in term_program or "wezterm" in term_program
        if unicode_ok:
            if name == "voice":
                return mic_icon()
            if name == "actions":
                return "+"
            if name == "send":
                return "➤"
            if name == "attach":
                return "📎"
            if name == "paste":
                return "📋"
            if name == "web":
                return "🌐"
            if name == "mcp":
                return "🔌"
            if name == "quit":
                return "⏻"
            return "⚙"
        return {
            "voice": "[mic]",
            "actions": "[+]",
            "send": "[send]",
            "attach": "[attach]",
            "paste": "[paste]",
            "web": "[web]",
            "mcp": "[mcp]",
            "quit": "[quit]",
        }.get(name, "[tool]")

    def _prompt_action_row() -> str:
        voice_chip = f"◖{_icon('voice')}◗"
        if voice_button.is_recording:
            status = voice_button.label(include_hint=False)
            icon_token = _icon('voice')
            status_tail = str(status).replace(icon_token, "", 1).strip()
            if status_tail:
                voice_chip = f"{voice_chip} {status_tail}"
        return f"{voice_chip}  {_icon('actions')} Actions"

    def _render_action_hint() -> None:
        console.print(f"[dim]{_prompt_action_row()}[/dim]")

    def _show_legend() -> None:
        if unicode_ok:
            lines = [
                "🌐 web search",
                "📚 file search/retrieval",
                "🖼 image attachment/input",
                "mic speech-to-text (audio attachments)",
                "📄 file attachment/input",
                "📎 attachment status row",
                "⚙ tool lifecycle",
            ]
        else:
            lines = [
                "[WEB] web search",
                "[FILES] file search/retrieval",
                "[IMG] image attachment/input",
                "[AUDIO] speech-to-text (audio attachments)",
                "[FILE] file attachment/input",
                "[ATTACH] attachment status row",
                "[TOOL] tool lifecycle",
            ]
        console.print(Panel("\n".join(lines), title="Legend", border_style="cyan"))

    def _export_transcript(path_arg: str) -> bool:
        target = str(path_arg or "").strip()
        if not target:
            console.print("[red]Usage: /export-transcript <path>[/red]")
            return False
        out_path = Path(target).expanduser()
        if not out_path.is_absolute():
            out_path = (Path.cwd() / out_path).resolve()
        payload = {
            "version": 1,
            "provider": current_provider,
            "model": current_model,
            "thread_id": current_thread_id,
            "entries": list(chat_transcript),
        }
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            console.print(f"[red]Failed to export transcript: {exc}[/red]")
            return False
        console.print(f"[green]Transcript exported to {out_path}[/green]")
        return True

    def _render_ui_event(event_type: str, payload: dict[str, Any]) -> None:
        marker = str(payload.get("marker") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if event_type == "attachment_added":
            console.print(f"[dim]attached {marker}[/dim]")
            return
        if event_type == "attachment_removed":
            console.print(f"[dim]removed {marker}[/dim]")
            return
        if event_type == "upload_started":
            console.print(f"[dim]Uploading {marker}...[/dim]")
            return
        if event_type == "upload_succeeded":
            remote_id = str(payload.get("remote_file_id") or "").strip()
            suffix = f" (id={remote_id})" if remote_id else ""
            console.print(f"[dim]Uploaded {marker}{suffix}[/dim]")
            return
        if event_type == "upload_failed":
            console.print(f"[yellow]Upload failed {marker}: {reason or 'unknown error'}[/yellow]")
            return
        if event_type == "capability_unavailable":
            console.print(f"[yellow]{reason or 'Capability unavailable'} {marker}[/yellow]")
            return

    def _emit_attachment_event(event_type: str, payload: dict[str, Any]) -> None:
        nonlocal attachment_event_sequence
        attachment_event_sequence += 1
        envelope = mk_event(
            run_id=str(current_run_id or runtime_session_id),
            turn_id=str(current_thread_id or runtime_session_id),
            sequence_no=attachment_event_sequence,
            event_type=event_type,  # type: ignore[arg-type]
            payload=payload,
        )
        dedupe_key = (envelope.run_id, envelope.turn_id, envelope.type, envelope.sequence_no)
        if dedupe_key in rendered_ui_event_keys:
            return
        rendered_ui_event_keys.add(dedupe_key)
        _render_ui_event(envelope.type, envelope.payload)

    def _set_attachment_state(att: PendingAttachment, new_state: AttachmentState, *, error: str | None = None) -> None:
        if not _can_attachment_transition(att.state, new_state):
            return
        att.state = new_state
        att.error = (str(error).strip() or None) if error else None

    def _find_attachment(query: str) -> PendingAttachment | None:
        token = str(query or "").strip()
        if not token:
            return None
        token_lc = token.lower()
        for att in pending_attachments:
            if token == att.local_id:
                return att
            if token_lc == att.name.lower():
                return att
            if token_lc == Path(att.path).name.lower():
                return att
        return None

    def _attachment_label(att: PendingAttachment) -> str:
        icon = _attachment_icon(att.mime_type, unicode_ok=unicode_ok)
        return f"{icon} {_attachment_marker(att.name)}"

    def _render_attachment_status_row(*, force: bool = False) -> None:
        nonlocal last_attachment_status_row
        if not pending_attachments:
            last_attachment_status_row = None
            return
        max_markers = 6
        try:
            width = int(console.width or 120)
        except Exception:
            width = 120
        if width < 90:
            max_markers = 3
        markers = [_attachment_marker(att.name) for att in pending_attachments[:max_markers]]
        extra = len(pending_attachments) - len(markers)
        suffix = f" +{extra}" if extra > 0 else ""
        row = f"📎 Attachments: {' '.join(markers)}{suffix}"
        if row == last_attachment_status_row:
            return
        last_attachment_status_row = row
        console.print(f"  [dim]{row}[/dim]")

    def _human_bytes(num: int) -> str:
        value = float(max(0, int(num)))
        units = ["B", "KB", "MB", "GB"]
        idx = 0
        while value >= 1024.0 and idx < len(units) - 1:
            value /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(value)} {units[idx]}"
        return f"{value:.1f} {units[idx]}"

    def _render_upload_queue_preview() -> None:
        if not pending_attachments:
            return
        queue = Table(title="Attachment Upload Queue")
        queue.add_column("Attachment")
        queue.add_column("Type")
        queue.add_column("Size", justify="right")
        queue.add_column("State")
        queue.add_column("Preview")
        for att in pending_attachments:
            preview = "-"
            if _is_likely_text_mime(att.mime_type):
                snippet = _read_attachment_preview(att.path, max_chars=120).splitlines()
                preview = (snippet[0] if snippet else "").strip()[:80] or "[text]"
            queue.add_row(
                _attachment_marker(att.name),
                str(att.mime_type or "application/octet-stream"),
                _human_bytes(att.size_bytes),
                str(att.state.value),
                preview,
            )
        console.print(queue)

    def _attach_file(raw_path: str) -> bool:
        token = str(raw_path or "").strip()
        if not token:
            console.print("[red]Usage: /attach <path>[/red]")
            return False
        if len(pending_attachments) >= max_attachments_per_turn:
            console.print(f"[red]Cannot attach more than {max_attachments_per_turn} files.[/red]")
            return False
        resolved = Path(token).expanduser()
        if not resolved.is_absolute():
            resolved = (Path.cwd() / resolved).resolve()
        if not resolved.exists() or not resolved.is_file():
            console.print(f"[red]Attachment not found: {token}[/red]")
            return False
        try:
            size_bytes = int(resolved.stat().st_size)
        except Exception:
            console.print(f"[red]Unable to read attachment metadata: {token}[/red]")
            return False
        if size_bytes > max_attachment_bytes:
            console.print(
                f"[red]Attachment too large ({size_bytes} bytes). Max is {max_attachment_bytes} bytes.[/red]"
            )
            return False
        try:
            digest = _file_sha256(resolved)
        except Exception:
            console.print(f"[red]Unable to hash attachment: {token}[/red]")
            return False
        key = _attachment_key(str(resolved), size_bytes, digest)
        for existing in pending_attachments:
            existing_key = _attachment_key(existing.path, existing.size_bytes, existing.sha256)
            if existing_key == key:
                console.print(f"[yellow]Already attached {_attachment_marker(existing.name)}[/yellow]")
                return False
        mime_type = _sniff_mime_type(str(resolved))
        if not _is_chat_attachment_mime_allowed(mime_type):
            console.print(f"[red]Unsupported file type for chat attachments: {mime_type}[/red]")
            return False
        att = PendingAttachment(
            local_id=str(uuid.uuid4())[:8],
            path=str(resolved),
            name=resolved.name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=digest,
        )
        pending_attachments.append(att)
        _emit_attachment_event(
            "attachment_added",
            {"id": att.local_id, "marker": _attachment_marker(att.name)},
        )
        console.print(f"[dim]{_attachment_label(att)} attached[/dim]")
        return True

    def _paste_from_clipboard() -> int:
        """Attach clipboard image/path items and return attached count."""
        attached = 0
        image_path = _save_clipboard_image_to_file() if _clipboard_has_image() else None
        if image_path:
            if len(pending_attachments) < max_attachments_per_turn and _attach_file(image_path):
                attached += 1
            else:
                _emit_attachment_event(
                    "upload_failed",
                    {
                        "marker": _attachment_marker(Path(image_path).name),
                        "reason": "attachment limit reached",
                    },
                )

        clip_text = _read_clipboard_text()
        if clip_text:
            for token in _extract_pasted_path_candidates(clip_text):
                if len(pending_attachments) >= max_attachments_per_turn:
                    break
                resolved = _resolve_attachment_token_path(token, workspace_root=workspace_root)
                if resolved and _attach_file(resolved):
                    attached += 1
        return attached

    async def _prepare_attachments_for_turn() -> tuple[list[PendingAttachment], list[str], str, list[str]]:
        uploaded: list[PendingAttachment] = []
        markers: list[str] = []
        context_parts: list[str] = []
        file_ids: list[str] = []
        upload_concurrency = max(1, min(4, int(os.getenv("OPENVEGAS_CHAT_UPLOAD_CONCURRENCY", "2"))))
        upload_retry_max = max(0, min(3, int(os.getenv("OPENVEGAS_CHAT_UPLOAD_RETRY_MAX", "1"))))
        semaphore = asyncio.Semaphore(upload_concurrency)

        async def _upload_one(att: PendingAttachment) -> None:
            marker = _attachment_marker(att.name)
            if att.state == AttachmentState.UNSUPPORTED:
                return
            if att.state == AttachmentState.FAILED:
                _set_attachment_state(att, AttachmentState.ATTACHED)

            _emit_attachment_event("upload_started", {"id": att.local_id, "marker": marker})
            _set_attachment_state(att, AttachmentState.UPLOADING)

            file_key = _attachment_key(att.path, att.size_bytes, att.sha256)
            cached_remote = uploaded_attachment_cache.get(file_key)
            if cached_remote:
                att.remote_file_id = cached_remote
                _set_attachment_state(att, AttachmentState.UPLOADED)
                _emit_attachment_event(
                    "upload_succeeded",
                    {"id": att.local_id, "marker": marker, "remote_file_id": cached_remote},
                )
                return

            async with semaphore:
                reason = "upload failed"
                for attempt in range(upload_retry_max + 1):
                    try:
                        init_resp = await client.upload_init(
                            filename=att.name,
                            size_bytes=att.size_bytes,
                            mime_type=att.mime_type,
                            sha256_hex=att.sha256,
                        )
                        upload_id = str(init_resp.get("upload_id") or "").strip()
                        if not upload_id:
                            raise APIError(500, "Upload init did not return upload_id")
                        file_bytes = Path(att.path).read_bytes()
                        complete_resp = await client.upload_complete(
                            upload_id=upload_id,
                            content_base64=base64.b64encode(file_bytes).decode("ascii"),
                        )
                        remote_file_id = str(
                            complete_resp.get("file_id")
                            or complete_resp.get("upload_id")
                            or upload_id
                        ).strip()
                        if not remote_file_id:
                            raise APIError(500, "Upload complete did not return file_id")
                        uploaded_attachment_cache[file_key] = remote_file_id
                        att.remote_file_id = remote_file_id
                        _set_attachment_state(att, AttachmentState.UPLOADED)
                        _emit_attachment_event(
                            "upload_succeeded",
                            {"id": att.local_id, "marker": marker, "remote_file_id": remote_file_id},
                        )
                        return
                    except APIError as exc:
                        detail = str(exc.detail or "").strip()
                        code = str(exc.data.get("error") or "").strip() if isinstance(exc.data, dict) else ""
                        reason = detail or code or "upload failed"
                    except Exception as exc:
                        reason = str(exc or "").strip() or "upload failed"
                    if attempt < upload_retry_max:
                        emit_metric("chat_attachment_upload_retry_total", {"attempt": str(attempt + 1)})
                        console.print(f"[dim]Retrying upload {marker} ({attempt + 1}/{upload_retry_max})...[/dim]")
                        await asyncio.sleep(min(0.75, 0.25 * (attempt + 1)))
                        continue
                    _set_attachment_state(att, AttachmentState.FAILED, error=reason)
                    _emit_attachment_event(
                        "upload_failed",
                        {"id": att.local_id, "marker": marker, "reason": reason},
                    )
                    return

        candidates = [att for att in list(pending_attachments) if att.state != AttachmentState.UNSUPPORTED]
        markers.extend(_attachment_marker(att.name) for att in candidates)
        if candidates:
            await asyncio.gather(*[_upload_one(att) for att in candidates])

        for att in list(pending_attachments):
            if att.state != AttachmentState.UPLOADED:
                continue
            marker = _attachment_marker(att.name)
            summary = f"Attachment {marker} uploaded as file_id={att.remote_file_id} (mime={att.mime_type}, bytes={att.size_bytes})"
            if _is_likely_text_mime(att.mime_type):
                preview = _read_attachment_preview(att.path, max_chars=attachment_preview_max_chars)
                if not preview.strip():
                    preview = "[No readable text content]"
                summary = f"{summary}\n{preview}"
            else:
                summary = f"{summary}\n[Binary attachment; content not inlined in prompt.]"
            context_parts.append(summary)
            uploaded.append(att)
            if att.remote_file_id:
                file_ids.append(att.remote_file_id)

        attachment_context = ""
        if context_parts:
            attachment_context = "\n\nAttached file context:\n\n" + "\n\n---\n\n".join(context_parts)
        return uploaded, markers, attachment_context, file_ids

    async def _transcribe_audio_attachments_for_turn(
        uploaded: list[PendingAttachment],
    ) -> tuple[str, int, bool]:
        audio_items = [
            att
            for att in list(uploaded or [])
            if _attachment_is_audio(att) and str(att.remote_file_id or "").strip()
        ]
        if not audio_items:
            return "", 0, False

        stt_effective = bool(
            voice_transcribe_requested
            and resolve_capability(current_provider, current_model, "speech_to_text")
        )
        if not stt_effective:
            _render_capability_status("speech_to_text", "audio attached but speech-to-text unavailable")
            return "", 0, False

        transcript_blocks: list[str] = []
        used_count = 0
        for att in audio_items:
            marker = _attachment_marker(att.name)
            _render_capability_status("speech_to_text", f"transcribing {marker}...")
            try:
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_started"})
                payload = await asyncio.wait_for(
                    client.speech_transcribe(
                        file_id=str(att.remote_file_id or ""),
                        provider=current_provider,
                        model=voice_transcribe_model,
                        language=voice_transcribe_language,
                    ),
                    timeout=_voice_timeout("OPENVEGAS_CHAT_VOICE_TRANSCRIBE_TIMEOUT_SEC", 90.0),
                )
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_succeeded"})
            except APIError as exc:
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
                detail = str(exc.detail or "speech transcription failed").strip()
                console.print(f"[yellow]Speech-to-text failed {marker}: {detail}[/yellow]")
                continue
            except asyncio.TimeoutError:
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
                console.print(f"[yellow]Speech-to-text failed {marker}: timeout[/yellow]")
                continue
            except Exception as exc:
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
                console.print(f"[yellow]Speech-to-text failed {marker}: {exc}[/yellow]")
                continue

            text = str(payload.get("text") or "").strip()
            if not text:
                console.print(f"[yellow]Speech-to-text returned empty transcript for {marker}[/yellow]")
                continue
            used_count += 1
            console.print(f"[dim]Transcribed {marker} ({len(text)} chars)[/dim]")
            transcript_blocks.append(f"[Speech transcript {marker}]\n{text}")

        if not transcript_blocks:
            return "", 0, stt_effective
        return "\n\n".join(transcript_blocks), used_count, stt_effective

    def _capability_label(name: str) -> str:
        if unicode_ok:
            return {
                "web_search": "🌐",
                "file_search": "📚",
                "image_analyze": "🖼",
                "speech_to_text": mic_icon(),
                "mcp": "🔌",
                "file_read": "📄",
            }.get(name, "⚙")
        return {
            "web_search": "[WEB]",
            "file_search": "[FILES]",
            "image_analyze": "[IMG]",
            "speech_to_text": "[AUDIO]",
            "mcp": "[MCP]",
            "file_read": "[FILE]",
        }.get(name, "[TOOL]")

    def _render_capability_status(name: str, detail: str) -> None:
        console.print(f"[dim]{_capability_label(name)} {detail}[/dim]")

    def _insert_voice_transcript_text(transcript: str) -> tuple[str, int]:
        nonlocal pending_voice_prefill, pending_voice_meta, prompt_input_active
        pending_voice_prefill, mode, chars = _insert_or_queue_voice_transcript(
            transcript=transcript,
            chat_prompt_session=chat_prompt_session,
            prompt_active=bool(prompt_input_active),
            pending_prefill=pending_voice_prefill,
        )
        if chars <= 0:
            return "none", 0
        pending_voice_meta = {"chars": chars, "mode": mode, "at": time.time()}
        if mode == "live":
            emit_metric("voice_capture_phase_total", {"phase": "transcript_inserted_live"})
        elif mode == "prefill":
            emit_metric("voice_capture_phase_total", {"phase": "transcript_queued_prefill"})
        return mode, chars

    def _voice_timeout(name: str, default_sec: float) -> float:
        raw = str(os.getenv(name, str(default_sec))).strip()
        try:
            return max(3.0, min(180.0, float(raw)))
        except Exception:
            return float(default_sec)

    async def _transcribe_voice_wav(wav_path: str, duration_sec: float) -> str:
        file_path = Path(str(wav_path or "").strip())
        if not file_path.exists() or not file_path.is_file():
            raise RuntimeError("voice capture file missing")
        marker = _attachment_marker(file_path.name)
        try:
            size_bytes = int(file_path.stat().st_size)
        except Exception as exc:
            raise RuntimeError(f"voice metadata unavailable: {exc}")
        if size_bytes <= 0:
            raise RuntimeError("voice capture is empty")
        try:
            digest = _file_sha256(file_path)
        except Exception as exc:
            raise RuntimeError(f"voice hash failed: {exc}")

        mime_type = _sniff_mime_type(str(file_path)).strip().lower()
        if mime_type in {"audio/x-wav", "audio/wave", "audio/vnd.wave"}:
            mime_type = "audio/wav"
        if not mime_type:
            mime_type = "audio/wav"

        _render_capability_status("speech_to_text", f"transcribing {marker} ({duration_sec:.1f}s)...")

        phase = "upload_init"
        try:
            emit_metric("voice_capture_phase_total", {"phase": "upload_init_started"})
            init_resp = await asyncio.wait_for(
                client.upload_init(
                    filename=file_path.name,
                    size_bytes=size_bytes,
                    mime_type=mime_type,
                    sha256_hex=digest,
                ),
                timeout=_voice_timeout("OPENVEGAS_CHAT_VOICE_UPLOAD_TIMEOUT_SEC", 30.0),
            )
            upload_id = str(init_resp.get("upload_id") or "").strip()
            if not upload_id:
                raise RuntimeError("upload init did not return upload_id")
            file_bytes = file_path.read_bytes()
            phase = "upload_complete"
            emit_metric("voice_capture_phase_total", {"phase": "upload_complete_started"})
            complete_resp = await asyncio.wait_for(
                client.upload_complete(
                    upload_id=upload_id,
                    content_base64=base64.b64encode(file_bytes).decode("ascii"),
                ),
                timeout=_voice_timeout("OPENVEGAS_CHAT_VOICE_UPLOAD_TIMEOUT_SEC", 30.0),
            )
            remote_file_id = str(
                complete_resp.get("file_id")
                or complete_resp.get("upload_id")
                or upload_id
            ).strip()
            if not remote_file_id:
                raise RuntimeError("upload complete did not return file_id")

            uploaded_attachment_cache[_attachment_key(str(file_path), size_bytes, digest)] = remote_file_id
            emit_metric("voice_capture_phase_total", {"phase": "upload_succeeded"})

            phase = "transcribe"
            emit_metric("voice_capture_phase_total", {"phase": "transcribe_started"})
            stt = await asyncio.wait_for(
                client.speech_transcribe(
                    file_id=remote_file_id,
                    provider=current_provider,
                    model=voice_transcribe_model,
                    language=voice_transcribe_language,
                    prompt="Transcribe exactly; keep punctuation concise.",
                ),
                timeout=_voice_timeout("OPENVEGAS_CHAT_VOICE_TRANSCRIBE_TIMEOUT_SEC", 90.0),
            )
            emit_metric("voice_capture_phase_total", {"phase": "transcribe_succeeded"})
            transcript = str((stt or {}).get("text") or "").strip()
            if not transcript:
                raise RuntimeError("speech-to-text returned empty transcript")
            return transcript
        except asyncio.TimeoutError:
            if phase in {"upload_init", "upload_complete"}:
                emit_metric("voice_capture_phase_total", {"phase": "upload_timeout"})
                raise RuntimeError("voice upload timed out")
            emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
            raise RuntimeError("speech transcription timed out")
        except APIError as exc:
            if phase in {"upload_init", "upload_complete"}:
                emit_metric("voice_capture_phase_total", {"phase": "upload_failed"})
            else:
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
            detail = str(exc.detail or "speech transcription failed").strip()
            raise RuntimeError(detail or "speech transcription failed")
        except Exception:
            if phase in {"upload_init", "upload_complete"}:
                emit_metric("voice_capture_phase_total", {"phase": "upload_failed"})
            else:
                emit_metric("voice_capture_phase_total", {"phase": "transcribe_failed"})
            raise

    def _erase_prompt_line_if_possible() -> None:
        if not use_prompt_toolkit_chat:
            return
        try:
            if not bool(getattr(sys.stdout, "isatty", lambda: False)()):
                return
            stream = getattr(console, "file", None) or sys.stdout
            stream.write("\x1b[1A\x1b[2K\r")
            stream.flush()
        except Exception:
            return

    def _render_usage_summary(payload: dict[str, Any]) -> None:
        if not show_token_usage:
            return
        in_tok = int(payload.get("input_tokens") or 0)
        out_tok = int(payload.get("output_tokens") or 0)
        total = in_tok + out_tok
        if total <= 0:
            return
        console.print(f"[dim]tokens: in={in_tok} out={out_tok} total={total}[/dim]")

    def _render_web_source_table(payload: dict[str, Any]) -> None:
        sources = payload.get("web_search_sources")
        if not isinstance(sources, list) or not sources:
            return
        ranking_raw = payload.get("web_search_source_ranking")
        ranking_map: dict[str, dict[str, Any]] = {}
        if isinstance(ranking_raw, list):
            for item in ranking_raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if url:
                    ranking_map[url] = item

        table = Table(title="Web Sources")
        table.add_column("Host")
        table.add_column("Quality", justify="right")
        table.add_column("URL")
        for url in sources[:8]:
            token = str(url or "").strip()
            if not token:
                continue
            parts = urlparse(token)
            host = str(parts.netloc or token)
            item = ranking_map.get(token, {})
            score = item.get("score")
            score_text = _fmt_num(score) if score is not None else "-"
            table.add_row(host, score_text, token)
        console.print(table)

    def _update_fence(payload: dict | None) -> None:
        nonlocal current_run_version, current_signature
        if not isinstance(payload, dict):
            return
        if payload.get("run_version") is not None:
            try:
                current_run_version = int(payload["run_version"])
            except Exception:
                pass
        if payload.get("valid_actions_signature"):
            current_signature = str(payload["valid_actions_signature"])

    def _tool_protocol_prompt(
        user_message: str,
        tool_observations: list[dict],
        ide_context_json: str | None,
        *,
        web_search_effective: bool,
        attachment_context: str,
    ) -> str:
        obs_json = json.dumps(tool_observations, ensure_ascii=False)
        ide_line = f"IDE context (JSON, capped): {ide_context_json}\n\n" if ide_context_json else ""
        web_search_lines = (
            "Built-in tool available:\n"
            "  - web_search_preview (live web lookup)\n"
            "Web search rules:\n"
            "7) For current external information (news, listings, prices, schedules), use web_search_preview.\n"
            "8) If user asks to scrape or bypass site restrictions, do not bypass controls; "
            "convert to lawful web search across publicly accessible pages and provide best-effort results with source URLs.\n\n"
            if web_search_effective
            else ""
        )
        return (
            "You are OpenVegas coding runtime.\n"
            f"Workspace root: {workspace_root}\n"
            f"Plan mode: {'on' if plan_mode else 'off'}\n"
            f"Approval mode: {approval_mode}\n"
            "Available tools:\n"
            "  - Read({ filepath })\n"
            "  - Search({ pattern, path? })\n"
            "  - FindAndReplace({ filepath, old_string, new_string, replace_all? })\n"
            "  - InsertAtEnd({ filepath, content })\n"
            "  - Write({ filepath, content, write_mode? })\n"
            "  - Bash({ command })\n"
            "  - List({ path? })\n"
            "Rules:\n"
            "1) If a tool is needed, emit a tool call via tool-calling (preferred).\n"
            "   Fallback only if tool-calling is unavailable: output ONE JSON tool_call object.\n"
            "2) If no tool is needed, return the final user-facing answer as normal text.\n"
            "3) Do not claim you cannot access files; tools are available through this runtime.\n"
            "4) Use mutating tools only when required.\n"
            "5) Never repeat the exact same tool call (same tool + same args) after it succeeded; use prior observations to answer.\n\n"
            "5b) For file mutations, prefer FindAndReplace and InsertAtEnd; only use Write(replace) when explicit full-file replacement is intended.\n\n"
            "6) For requests like 'apply a tiny patch to a temp file', do not ask for clarification.\n"
            "   Choose a safe workspace-local temp file path and produce a minimal valid unified diff.\n\n"
            f"{web_search_lines}"
            f"{attachment_context}\n\n"
            f"Prior tool observations (JSON): {obs_json}\n\n"
            f"{ide_line}"
            f"User request: {user_message}"
        )

    async def _run_tool_loop(client, user_message: str) -> bool:
        """Return True when assistant produced a final non-tool answer."""
        nonlocal current_thread_id
        nonlocal current_run_version, current_signature
        nonlocal last_successful_tool
        nonlocal context_warning_emitted
        nonlocal web_search_requested
        nonlocal last_web_search_effective
        nonlocal last_web_search_used
        nonlocal last_web_search_retry_without_tool
        nonlocal last_assistant_text_for_turn

        from openvegas.client import APIError

        def _maybe_warn_context_disabled(result_payload: dict[str, Any]) -> None:
            nonlocal context_warning_emitted
            if context_warning_emitted:
                return
            if conversation_mode != "persistent":
                return
            status = str(result_payload.get("thread_status") or "")
            context_enabled = result_payload.get("context_enabled")
            if status == "disabled" or context_enabled is False:
                console.print(
                    "[yellow]Persistent conversation mode requested, but server context is disabled. "
                    "Enable OPENVEGAS_CONTEXT_ENABLED=1 to retain chat history.[/yellow]"
                )
                context_warning_emitted = True

        async def _request_final_response(observations: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal current_thread_id
            compression_hint = ""
            if streamed_tools_seen.get("shell_run"):
                compression_hint = (
                    "If shell output was already streamed live, do not replay full output; summarize key results only.\n\n"
                )
            final_prompt = (
                "Use the prior tool observations and answer the user now. "
                "Do not call tools. Return a concise final answer.\n\n"
                f"{compression_hint}"
                f"User request: {user_message}\n"
                f"Observations: {json.dumps(observations, ensure_ascii=False)}"
            )
            final_res = await client.ask(
                final_prompt,
                current_provider,
                current_model,
                idempotency_key=f"chat-finalize-{uuid.uuid4()}",
                thread_id=current_thread_id,
                conversation_mode=conversation_mode,
                persist_context=(conversation_mode == "persistent"),
                enable_tools=False,
                enable_web_search=False,
            )
            next_thread = final_res.get("thread_id")
            if next_thread:
                current_thread_id = str(next_thread)
            _maybe_warn_context_disabled(final_res if isinstance(final_res, dict) else {})
            return final_res

        async def _ask_direct_one_shot(
            prompt: str,
            *,
            idempotency_key: str,
            enable_web_search: bool,
            attachments: list[str],
        ) -> dict[str, Any]:
            stream_enabled = bool(
                _env_flag("OPENVEGAS_CHAT_STREAM_EVENTS", "1")
                and resolve_capability(current_provider, current_model, "stream_events")
            )
            ask_stream_fn = getattr(client, "ask_stream", None)
            if not stream_enabled or not callable(ask_stream_fn):
                return await client.ask(
                    prompt,
                    current_provider,
                    current_model,
                    idempotency_key=idempotency_key,
                    thread_id=current_thread_id,
                    conversation_mode=conversation_mode,
                    persist_context=(conversation_mode == "persistent"),
                    enable_tools=False,
                    enable_web_search=enable_web_search,
                    attachments=attachments,
                )

            seen_event_keys: set[tuple[str, str, str, int]] = set()
            chunks: list[str] = []
            completed_payload: dict[str, Any] = {}
            try:
                async for raw_event in ask_stream_fn(
                    prompt,
                    current_provider,
                    current_model,
                    idempotency_key=idempotency_key,
                    thread_id=current_thread_id,
                    conversation_mode=conversation_mode,
                    persist_context=(conversation_mode == "persistent"),
                    enable_tools=False,
                    enable_web_search=enable_web_search,
                    attachments=attachments,
                ):
                    if not isinstance(raw_event, dict):
                        continue
                    event_name = str(raw_event.get("event") or "").strip()
                    event_data = raw_event.get("data")
                    if not event_name or not isinstance(event_data, dict):
                        continue
                    run_id = str(event_data.get("run_id") or "")
                    turn_id = str(event_data.get("turn_id") or "")
                    seq_raw = event_data.get("sequence_no")
                    try:
                        sequence_no = int(seq_raw)
                    except Exception:
                        sequence_no = 0
                    if sequence_no > 0:
                        dedupe_key = (run_id, turn_id, event_name, sequence_no)
                        if dedupe_key in seen_event_keys:
                            continue
                        seen_event_keys.add(dedupe_key)

                    payload = event_data.get("payload")
                    payload_dict = payload if isinstance(payload, dict) else {}

                    if event_name in {"response.started", "stream_start"}:
                        if show_stream_status:
                            _render_capability_status("stream_events", "streaming response...")
                        continue
                    if event_name in {"tool.call", "tool_start"}:
                        tool = payload_dict.get("tool")
                        tool_name = ""
                        if isinstance(tool, dict):
                            tool_name = str(tool.get("tool_name") or tool.get("name") or "").strip()
                        elif isinstance(tool, str):
                            tool_name = tool.strip()
                        status_text = f"tool_start {tool_name}".strip()
                        if show_stream_status:
                            _render_capability_status("stream_events", status_text)
                        continue
                    if event_name in {"tool_progress"}:
                        tool = payload_dict.get("tool")
                        tool_name = ""
                        if isinstance(tool, dict):
                            tool_name = str(tool.get("tool_name") or tool.get("name") or "").strip()
                        elif isinstance(tool, str):
                            tool_name = tool.strip()
                        phase = str(payload_dict.get("phase") or "progress").strip()
                        status_text = f"tool_progress {tool_name} {phase}".strip()
                        if show_stream_status:
                            _render_capability_status("stream_events", status_text)
                        continue
                    if event_name in {"tool.result", "tool_result"}:
                        tool = payload_dict.get("tool")
                        tool_name = ""
                        if isinstance(tool, dict):
                            tool_name = str(tool.get("tool_name") or tool.get("name") or "").strip()
                        elif isinstance(tool, str):
                            tool_name = tool.strip()
                        status_text = f"tool_result {tool_name}".strip()
                        if show_stream_status:
                            _render_capability_status("stream_events", status_text)
                        continue
                    if event_name in {"response.delta", "stream_delta"}:
                        delta = str(payload_dict.get("text") or "")
                        if delta:
                            chunks.append(delta)
                        continue
                    if event_name in {"response.error", "error"}:
                        code = str(payload_dict.get("error") or "stream_error")
                        detail = str(payload_dict.get("detail") or code)
                        raise APIError(400, f"{code}: {detail}", data=payload_dict)
                    if event_name in {"response.completed", "stream_end"}:
                        completed_payload = dict(payload_dict)
                        continue
            except APIError as e:
                # Backward compatibility: if stream endpoint is unavailable, retry once with non-stream ask.
                if e.status in {404, 405, 501}:
                    return await client.ask(
                        prompt,
                        current_provider,
                        current_model,
                        idempotency_key=idempotency_key,
                        thread_id=current_thread_id,
                        conversation_mode=conversation_mode,
                        persist_context=(conversation_mode == "persistent"),
                        enable_tools=False,
                        enable_web_search=enable_web_search,
                        attachments=attachments,
                    )
                raise

            merged_text = "".join(chunks).strip()
            if not merged_text:
                merged_text = str(completed_payload.get("text") or "").strip()

            warning_value = str(completed_payload.get("warning") or "").strip()
            warnings_list = list(completed_payload.get("warnings") or [])
            if warning_value and warning_value not in warnings_list:
                warnings_list.append(warning_value)

            return {
                "text": merged_text,
                "v_cost": str(completed_payload.get("v_cost") or "0"),
                "thread_id": completed_payload.get("thread_id"),
                "thread_status": completed_payload.get("thread_status"),
                "context_enabled": completed_payload.get("context_enabled"),
                "warning": warning_value,
                "warnings": warnings_list,
                "input_tokens": int(completed_payload.get("input_tokens", 0) or 0),
                "output_tokens": int(completed_payload.get("output_tokens", 0) or 0),
                "total_tokens": int(completed_payload.get("total_tokens", 0) or 0),
                "web_search_requested": bool(completed_payload.get("web_search_requested", enable_web_search)),
                "web_search_effective": bool(completed_payload.get("web_search_effective", enable_web_search)),
                "web_search_used": bool(completed_payload.get("web_search_used", False)),
                "web_search_retry_without_tool": bool(
                    completed_payload.get("web_search_retry_without_tool", False)
                ),
                "web_search_sources": list(completed_payload.get("web_search_sources") or []),
                "web_search_source_ranking": list(completed_payload.get("web_search_source_ranking") or []),
            }

        async def _force_finalize(final_res: dict[str, Any], *, reason: str = "completed") -> LoopAction:
            nonlocal last_assistant_text_for_turn
            emit_metric("tool_loop_finalize_reason", {"reason": str(reason or "completed")})
            dealer_panel.render(map_lifecycle_event_to_state("finalize"), "finalized")
            final_text = _sanitize_user_visible_response_text(str(final_res.get("text", "")).strip())
            if final_text:
                render_assistant(console, final_text)
                last_assistant_text_for_turn = final_text
                render_status_bar(
                    console,
                    _status_actor(),
                    f"cost {final_res.get('v_cost', '?')} $V",
                    workspace_root,
                )
                _render_usage_summary(final_res if isinstance(final_res, dict) else {})
            return LoopAction.FINALIZED

        async def _execute_with_heartbeat(
            *,
            tool_request: dict[str, Any],
            tool_call_id: str,
            execution_token: str,
        ) -> tuple[Any | None, str | None]:
            tool_name_local = str(tool_request.get("tool_name", ""))
            args_local = tool_request.get("arguments", {})
            shell_mode_local = str(tool_request.get("shell_mode") or "read_only")
            timeout_local = int(tool_request.get("timeout_sec") or 30)

            if tool_name_local == "shell_run":
                task = asyncio.create_task(
                    execute_shell_run_streaming(
                        workspace_root=workspace_root,
                        arguments=args_local,
                        timeout_sec=timeout_local,
                        on_stdout=lambda s: console.print(s.rstrip("\n")) if s.strip() else None,
                        on_stderr=lambda s: console.print(f"[red]{s.rstrip()}[/red]") if s.strip() else None,
                    )
                )
            elif tool_name_local == "mcp_call":
                async def _run_mcp_call() -> ToolExecutionResult:
                    server_id = str(args_local.get("server_id") or "").strip()
                    tool_name = str(args_local.get("tool") or "").strip()
                    tool_args = args_local.get("arguments")
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                    if not server_id or not tool_name:
                        return ToolExecutionResult(
                            "blocked",
                            {
                                "ok": False,
                                "reason_code": "invalid_tool_arguments",
                                "detail": "mcp_call requires server_id and tool",
                            },
                            "",
                            "",
                        )
                    try:
                        res = await client.mcp_call_tool(
                            server_id=server_id,
                            tool=tool_name,
                            arguments=tool_args,
                            timeout_sec=timeout_local,
                        )
                        return ToolExecutionResult(
                            "succeeded",
                            {
                                "ok": True,
                                "server_id": server_id,
                                "tool": tool_name,
                                "mcp_result": res.get("result"),
                                "transport": res.get("transport"),
                            },
                            json.dumps(res, ensure_ascii=False)[:8000],
                            "",
                        )
                    except APIError as exc:
                        body = exc.data if isinstance(exc.data, dict) else {}
                        detail = str(body.get("detail") or exc.detail)
                        return ToolExecutionResult(
                            "failed",
                            {
                                "ok": False,
                                "reason_code": str(body.get("error") or "mcp_call_failed"),
                                "detail": detail,
                            },
                            "",
                            detail[:4000],
                        )

                task = asyncio.create_task(_run_mcp_call())
            else:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        execute_tool_request,
                        workspace_root=workspace_root,
                        tool_name=tool_name_local,
                        arguments=args_local,
                        shell_mode=shell_mode_local,
                        timeout_sec=timeout_local,
                    )
                )

            heartbeat_interval = 2.0
            heartbeat_failures = 0
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_interval), None
                except asyncio.TimeoutError:
                    try:
                        hb = await client.agent_tool_heartbeat(
                            run_id=current_run_id,
                            runtime_session_id=runtime_session_id,
                            tool_call_id=tool_call_id,
                            execution_token=execution_token,
                        )
                        if not bool(hb.get("active", False)):
                            remote_status = str(hb.get("status") or "unknown")
                            if not task.done():
                                task.cancel()
                                try:
                                    await task
                                except Exception:
                                    pass
                            return None, remote_status
                    except APIError as e:
                        heartbeat_failures += 1
                        if heartbeat_failures <= 1:
                            body = e.data if isinstance(e.data, dict) else {}
                            code = body.get("error", "tool_heartbeat_failed")
                            detail = body.get("detail", e.detail)
                            console.print(f"[yellow]{code}: {detail}[/yellow]")
                    except Exception:
                        heartbeat_failures += 1

        completion_force_patch_intent = bool(
            last_successful_tool == "fs_apply_patch" and _is_patch_repeat_followup_intent(user_message)
        )
        state = ToolLoopState(
            completion_criteria=_build_completion_criteria(
                user_message,
                planner_edit_intent=completion_force_patch_intent,
            ),
        )
        completion_criteria = state.completion_criteria or _build_completion_criteria(
            user_message,
            planner_edit_intent=completion_force_patch_intent,
        )
        tool_observations = state.tool_observations
        executed_tool_calls = state.executed_tool_calls
        streamed_tools_seen = state.streamed_tools_seen
        successful_append_payload_fingerprints: set[str] = set()
        stall_limit_iters = max(2, int(os.getenv("OPENVEGAS_WORKFLOW_STALL_LIMIT_ITERS", "4")))
        mutation_not_observed_limit = max(
            1,
            int(os.getenv("OPENVEGAS_MUTATION_NOT_OBSERVED_LIMIT_ITERS", "2")),
        )
        max_active_mutation_wait_sec = max(1.0, float(os.getenv("OPENVEGAS_ACTIVE_MUTATION_TIMEOUT_SEC", "10")))
        patch_failure_repeat_limit = max(2, int(os.getenv("OPENVEGAS_PATCH_FAILURE_REPEAT_LIMIT", "2")))

        def _run_has_started_tool(snapshot: dict[str, Any]) -> bool:
            if str(snapshot.get("current_state") or "") != "running":
                return False
            valid_actions = snapshot.get("valid_actions")
            if not isinstance(valid_actions, list):
                return False
            names = {str(a.get("action", "")).strip().lower() for a in valid_actions if isinstance(a, dict)}
            return "handoff" not in names

        async def _wait_for_unlock_and_refresh() -> bool:
            if not current_run_id:
                return False
            started = time.monotonic()
            delays = (0.25, 0.5, 1.0)
            attempt = 0
            prev_sig: str | None = None
            while (time.monotonic() - started) < max_active_mutation_wait_sec:
                try:
                    snap = await client.agent_run_get(current_run_id)
                except Exception:
                    await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
                    attempt += 1
                    continue
                _update_fence(snap if isinstance(snap, dict) else None)
                if isinstance(snap, dict):
                    sig = str(snap.get("valid_actions_signature") or "")
                    if prev_sig is not None and sig and sig != prev_sig:
                        state.active_mutation_observation_changed = True
                    if sig:
                        prev_sig = sig
                    if not _run_has_started_tool(snap):
                        return True
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
                attempt += 1
            return False

        def _completion_eval() -> CompletionEvaluation:
            return _evaluate_completion_criteria(completion_criteria, workspace_root)

        def _mutation_observed_for_completion() -> bool:
            if not completion_criteria.requires_mutation:
                return True
            required = {
                str(token).strip().replace("\\", "/")
                for token in completion_criteria.required_files
                if str(token).strip()
            }
            required_names = {Path(token).name for token in required}
            for obs in reversed(tool_observations):
                if str(obs.get("tool_name")) != "fs_apply_patch":
                    continue
                result_status = str(obs.get("result_status") or "").strip().lower()
                obs_status = str(obs.get("status") or "").strip().lower()
                if result_status != "succeeded" and obs_status != "noop":
                    continue
                payload = obs.get("result_payload")
                if not isinstance(payload, dict):
                    return True
                targets = payload.get("files_targeted")
                if not isinstance(targets, list) or not targets:
                    return True
                touched = {str(t).strip().replace("\\", "/") for t in targets if str(t).strip()}
                if not required:
                    return True
                if touched & required:
                    return True
                touched_names = {Path(t).name for t in touched}
                if touched_names & required_names:
                    return True
            return False

        def _latest_blocked_edit_reason() -> str | None:
            for obs in reversed(tool_observations):
                if str(obs.get("status")) != "blocked":
                    continue
                detail = str(obs.get("detail") or "").strip()
                if not detail:
                    continue
                if detail.startswith("edit blocked:"):
                    return detail
                if str(obs.get("error")) in {
                    "user_declined_edit",
                    "mutation_required_but_unavailable",
                    "post_finalize_intercept_skip",
                }:
                    return detail
            return None

        def _record_post_finalize_skip(reason: str) -> tuple[bool, str]:
            _tool_debug(f"post-finalize intercept skipped; reason={reason}")
            tool_observations.append(
                {
                    "status": "blocked",
                    "error": "post_finalize_intercept_skip",
                    "detail": reason,
                }
            )
            return False, reason

        async def _maybe_intercept_final_text_for_mutation(
            *,
            final_text: str,
            edit_intent: bool,
        ) -> tuple[bool, str | None]:
            if state.post_finalize_intercept_attempted:
                prior_reason = _latest_blocked_edit_reason()
                if prior_reason:
                    return _record_post_finalize_skip(prior_reason)
                return _record_post_finalize_skip("post_finalize_intercept_already_attempted")
            state.post_finalize_intercept_attempted = True

            if not completion_criteria.requires_mutation:
                return _record_post_finalize_skip("mutation_not_required")
            if _mutation_observed_for_completion():
                return _record_post_finalize_skip("mutation_already_observed")

            write_fallback = _synth_write_tool_req_from_model_edit(
                user_message=user_message,
                model_text=final_text,
                tool_observations=tool_observations,
                planner_edit_intent=edit_intent,
            )
            if write_fallback is None:
                reason = _diagnose_synth_write_skip_reason(
                    user_message=user_message,
                    model_text=final_text,
                    tool_observations=tool_observations,
                    planner_edit_intent=edit_intent,
                ) or "final_text_code_block_intercept_failed"
                emit_metric("tool_mutation_blocked_total", {"reason": reason})
                return _record_post_finalize_skip(reason)

            state.pending_retry_tool_req = write_fallback
            emit_metric("tool_synth_write_from_code_block_total", {"reason": "post_finalize_interception"})
            _tool_debug("post-finalize interception queued synthesized Write")
            return True, None

        async def _finalize_or_continue_with_intercept(
            *,
            reason: str,
            edit_intent: bool,
        ) -> LoopAction:
            final_res = await _request_final_response(tool_observations)
            final_text = _sanitize_user_visible_response_text(str(final_res.get("text", "")).strip())
            intercepted, blocked_reason = await _maybe_intercept_final_text_for_mutation(
                final_text=final_text,
                edit_intent=edit_intent,
            )
            if intercepted:
                return LoopAction.INTERCEPT
            if blocked_reason and completion_criteria.requires_mutation and not _mutation_observed_for_completion():
                if not _has_workspace_tooling_intent(user_message):
                    return await _force_finalize(
                        final_res,
                        reason="spurious_mutation_block_ignored",
                    )
                emit_metric(
                    "tool_loop_finalize_reason",
                    {"reason": "mutation_required_but_unavailable"},
                )
                tool_observations.append(
                    {
                        "status": "blocked",
                        "error": "mutation_required_but_unavailable",
                        "detail": blocked_reason,
                    }
                )
                render_assistant(console, f"edit blocked: {blocked_reason}")
                last_assistant_text_for_turn = f"edit blocked: {blocked_reason}"
                render_status_bar(
                    console,
                    _status_actor(),
                    f"cost {final_res.get('v_cost', '?')} $V",
                    workspace_root,
                )
                return LoopAction.FINALIZED
            return await _force_finalize(final_res, reason=reason)

        def _progress_fingerprint(eval_result: CompletionEvaluation) -> str:
            latest = tool_observations[-1] if tool_observations else {}
            raw_result_payload = latest.get("result_payload")
            result_payload_fingerprint = ""
            if isinstance(raw_result_payload, (dict, list)):
                try:
                    result_payload_fingerprint = _sha256_hex(
                        json.dumps(raw_result_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    )
                except Exception:
                    result_payload_fingerprint = ""
            elif raw_result_payload is not None:
                result_payload_fingerprint = _sha256_hex(str(raw_result_payload).encode("utf-8"))
            payload = {
                "tool_call_id": str(latest.get("tool_call_id", "")),
                "tool_name": str(latest.get("tool_name", "")),
                "status": str(latest.get("status", latest.get("result_status", ""))),
                "error": str(latest.get("error", "")),
                "result_status": str(latest.get("result_status", "")),
                "detail": str(latest.get("detail", ""))[:256],
                "result_payload_fp": result_payload_fingerprint,
                "artifact_fingerprint": eval_result.fingerprint,
                "mutation_observed": _mutation_observed_for_completion(),
            }
            return _sha256_hex(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))

        async def _continue_or_finalize_for_completion(
            *,
            reason_if_finalize: str,
            step: int,
            edit_intent: bool,
            after_execution: bool = False,
        ) -> LoopAction:
            if after_execution and not completion_criteria.active:
                if step >= (max_tool_steps - 1):
                    return await _finalize_or_continue_with_intercept(
                        reason="completion_criteria_unmet_after_retries",
                        edit_intent=edit_intent,
                    )
                return LoopAction.CONTINUE
            if not completion_criteria.active:
                return await _finalize_or_continue_with_intercept(
                    reason=reason_if_finalize,
                    edit_intent=edit_intent,
                )
            eval_result = _completion_eval()
            mutation_observed = _mutation_observed_for_completion()
            if eval_result.satisfied and mutation_observed:
                state.mutation_not_observed_iters = 0
                if after_execution:
                    if step >= (max_tool_steps - 1):
                        return await _finalize_or_continue_with_intercept(
                            reason="completion_criteria_unmet_after_retries",
                            edit_intent=edit_intent,
                        )
                    return LoopAction.CONTINUE
                return await _finalize_or_continue_with_intercept(
                    reason=reason_if_finalize,
                    edit_intent=edit_intent,
                )
            missing = list(eval_result.missing)
            if eval_result.satisfied and completion_criteria.requires_mutation and not mutation_observed:
                missing.append("mutation_not_observed")
                emit_metric("mutation_required_stall_total", {"reason": "mutation_not_observed"})
                state.mutation_not_observed_iters += 1
                _tool_debug("completion satisfied by artifacts but mutation not observed; continuing tool loop")
                if state.mutation_not_observed_iters >= mutation_not_observed_limit:
                    emit_metric(
                        "mutation_required_stall_total",
                        {"reason": "mutation_not_observed_retry_limit"},
                    )
                    tool_observations.append(
                        {
                            "status": "blocked",
                            "error": "mutation_not_observed_retry_limit",
                            "detail": (
                                "Completion artifacts satisfied but no mutation was observed after repeated attempts."
                            ),
                        }
                    )
                    return await _finalize_or_continue_with_intercept(
                        reason="mutation_not_observed_retry_limit",
                        edit_intent=edit_intent,
                    )
            else:
                state.mutation_not_observed_iters = 0
            fp = _progress_fingerprint(eval_result)
            if state.progress_fingerprint_prev is not None and fp == state.progress_fingerprint_prev:
                state.unchanged_progress_iters += 1
            else:
                state.unchanged_progress_iters = 0
            state.progress_fingerprint_prev = fp
            tool_observations.append(
                {
                    "status": "blocked",
                    "error": "completion_criteria_unmet",
                    "detail": ", ".join(missing[:6]),
                }
            )
            if state.unchanged_progress_iters >= stall_limit_iters:
                return await _finalize_or_continue_with_intercept(
                    reason="workflow_stalled_no_new_observations",
                    edit_intent=edit_intent,
                )
            if step >= (max_tool_steps - 1):
                return await _finalize_or_continue_with_intercept(
                    reason="completion_criteria_unmet_after_retries",
                    edit_intent=edit_intent,
                )
            return LoopAction.CONTINUE

        async def _call_with_stale_retry(factory, *, endpoint: str):
            last_exc: APIError | None = None
            max_attempts = 4
            for attempt in range(max_attempts):
                try:
                    return await factory()
                except APIError as e:
                    last_exc = e
                    body = e.data if isinstance(e.data, dict) else {}
                    _update_fence(body)
                    code = str(body.get("error") or "")
                    if code in RETRYABLE_MUTATION_ERRORS and attempt < (max_attempts - 1):
                        emit_metric("tool_cas_conflict_total", {"endpoint": endpoint, "error": code})
                        if code == "active_mutation_in_progress":
                            unlocked = await _wait_for_unlock_and_refresh()
                            if not unlocked:
                                state.active_mutation_timeout_hit = True
                                raise
                        backoff = _mutation_retry_backoff_sec(code, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        _tool_debug(f"retrying mutation after {code} (attempt {attempt + 1}/{max_attempts})")
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("Unexpected mutation retry state.")

        max_tool_steps = max(4, min(40, int(os.getenv("OPENVEGAS_CHAT_MAX_TOOL_STEPS", "24"))))
        for step in range(max_tool_steps):
            cleaned_text = ""
            model_text = ""
            candidate_tool_calls: list[dict[str, Any]] = []
            if state.pending_retry_tool_req is not None:
                candidate_tool_calls = [state.pending_retry_tool_req]
                state.pending_retry_tool_req = None
            force_patch_intent = bool(
                last_successful_tool == "fs_apply_patch" and _is_patch_repeat_followup_intent(user_message)
            )
            planner_edit_intent = force_patch_intent
            edit_intent = _has_patch_intent(user_message) or planner_edit_intent
            if planner_edit_intent:
                _tool_debug(f"forcing patch follow-up intent from prior tool={last_successful_tool!r}")

            if (
                step == 0
                and not candidate_tool_calls
                and not tool_observations
                and (planner_edit_intent or _is_patch_smoke_intent(user_message))
            ):
                synthetic = _synth_patch_tool_req_for_intent(
                    user_message=user_message,
                    tool_observations=tool_observations,
                    force_patch_intent=planner_edit_intent,
                )
                if synthetic is not None:
                    candidate_tool_calls.append(synthetic)
                    _tool_debug("injected synthetic fs_apply_patch on step 0")

            if not candidate_tool_calls:
                ide_context_json: str | None = None
                if current_run_id:
                    try:
                        ide_context = await asyncio.wait_for(
                            client.ide_get_context(
                                run_id=current_run_id,
                                runtime_session_id=runtime_session_id,
                            ),
                            timeout=3.0,
                        )
                        if isinstance(ide_context, dict):
                            ide_context_json = json.dumps(ide_context, ensure_ascii=False)[:8192]
                            state.bridge_caps["connected"] = True
                            state.bridge_caps["show_diff"] = True
                    except Exception:
                        ide_context_json = None

                web_search_requested_turn = bool(
                    web_search_requested
                    and _should_enable_web_search_for_turn(
                        user_message,
                        has_uploaded_attachments=bool(attachment_file_ids_for_turn),
                    )
                )
                web_search_effective_turn = bool(
                    web_search_requested_turn
                    and resolve_capability(
                        current_provider,
                        current_model,
                        "web_search",
                    )
                )
                web_search_activity_turn = bool(web_search_effective_turn)
                attachments_effective_turn = bool(
                    attachment_file_ids_for_turn
                    and resolve_capability(
                        current_provider,
                        current_model,
                        "file_upload",
                    )
                )
                if web_search_requested and not web_search_effective_turn:
                    last_web_search_effective = False
                    last_web_search_used = False
                    last_web_search_retry_without_tool = False
                prompt_user_message = user_message
                if web_search_effective_turn and _is_scrape_request(prompt_user_message):
                    prompt_user_message = _rewrite_lookup_request_for_safe_web_search(prompt_user_message)
                if web_search_effective_turn:
                    prompt_user_message = _augment_web_search_prompt(prompt_user_message)
                combined_attachment_context = "\n\n".join(
                    [part for part in [attachment_context_for_turn, voice_transcript_context_for_turn] if str(part or "").strip()]
                )
                prompt_attachment_context = (
                    ""
                    if attachments_effective_turn and current_provider == "openai" and not voice_transcript_context_for_turn
                    else combined_attachment_context
                )

                enable_local_tools_turn = bool(_has_workspace_tooling_intent(user_message))
                if not enable_local_tools_turn:
                    if web_search_activity_turn:
                        _render_capability_status("web_search", "searching...")
                    direct_prompt = (
                        f"{prompt_user_message}\n\n{prompt_attachment_context}".strip()
                        if prompt_attachment_context
                        else prompt_user_message
                    )
                    one_shot = await _ask_direct_one_shot(
                        direct_prompt,
                        idempotency_key=f"chat-direct-{uuid.uuid4()}",
                        enable_web_search=web_search_effective_turn,
                        attachments=attachment_file_ids_for_turn,
                    )
                    next_thread = one_shot.get("thread_id")
                    if next_thread:
                        current_thread_id = str(next_thread)
                    _maybe_warn_context_disabled(one_shot if isinstance(one_shot, dict) else {})
                    last_web_search_effective = bool(one_shot.get("web_search_effective", web_search_effective_turn))
                    last_web_search_used = bool(one_shot.get("web_search_used", False))
                    last_web_search_retry_without_tool = bool(one_shot.get("web_search_retry_without_tool", False))
                    final_text = _sanitize_user_visible_response_text(str(one_shot.get("text", "")).strip())
                    if web_search_effective_turn and _is_scrape_request(user_message) and _is_scrape_refusal_text(final_text):
                        one_shot_retry = await _ask_direct_one_shot(
                            _rewrite_lookup_request_for_safe_web_search(direct_prompt),
                            idempotency_key=f"chat-direct-retry-{uuid.uuid4()}",
                            enable_web_search=True,
                            attachments=attachment_file_ids_for_turn,
                        )
                        retry_thread = one_shot_retry.get("thread_id")
                        if retry_thread:
                            current_thread_id = str(retry_thread)
                        _maybe_warn_context_disabled(one_shot_retry if isinstance(one_shot_retry, dict) else {})
                        final_text = _sanitize_user_visible_response_text(str(one_shot_retry.get("text", "")).strip())
                        last_web_search_effective = bool(one_shot_retry.get("web_search_effective", True))
                        last_web_search_used = bool(one_shot_retry.get("web_search_used", False))
                        last_web_search_retry_without_tool = bool(
                            one_shot_retry.get("web_search_retry_without_tool", False)
                        )
                        one_shot = one_shot_retry

                    ws_req_default = bool(web_search_requested_turn)
                    warning_text = str(one_shot.get("warning") or "").strip()
                    ws_req = bool(one_shot.get("web_search_requested", ws_req_default))
                    ws_eff = bool(one_shot.get("web_search_effective", web_search_effective_turn))
                    ws_used = bool(one_shot.get("web_search_used", False))
                    ws_sources = one_shot.get("web_search_sources") or []
                    if warning_text:
                        console.print(f"[yellow]{warning_text}[/yellow]")
                    if ws_req:
                        source_count = len(ws_sources) if isinstance(ws_sources, list) else 0
                        if show_web_diagnostics:
                            _render_capability_status(
                                "web_search",
                                f"requested={ws_req} effective={ws_eff} used={ws_used} sources={source_count}",
                            )
                            console.print(
                                f"[dim]web: requested={ws_req} effective={ws_eff} used={ws_used} sources={source_count}[/dim]"
                            )
                            _render_web_source_table(one_shot if isinstance(one_shot, dict) else {})
                    if final_text:
                        render_assistant(console, final_text)
                        last_assistant_text_for_turn = final_text
                    render_status_bar(
                        console,
                        _status_actor(),
                        f"cost {one_shot.get('v_cost', '?')} $V",
                        workspace_root,
                    )
                    _render_usage_summary(one_shot if isinstance(one_shot, dict) else {})
                    return True

                prompt = _tool_protocol_prompt(
                    prompt_user_message,
                    tool_observations,
                    ide_context_json,
                    web_search_effective=web_search_effective_turn,
                    attachment_context=prompt_attachment_context,
                )
                if web_search_activity_turn:
                    _render_capability_status("web_search", "searching...")
                ask_idem = f"chat-ask-{uuid.uuid4()}"
                result = await client.ask(
                    prompt,
                    current_provider,
                    current_model,
                    idempotency_key=ask_idem,
                    thread_id=current_thread_id,
                    conversation_mode=conversation_mode,
                    persist_context=(conversation_mode == "persistent"),
                    enable_tools=enable_local_tools_turn,
                    enable_web_search=web_search_effective_turn,
                    attachments=attachment_file_ids_for_turn,
                )
                next_thread = result.get("thread_id")
                if next_thread:
                    current_thread_id = str(next_thread)
                _maybe_warn_context_disabled(result if isinstance(result, dict) else {})
                last_web_search_effective = bool(result.get("web_search_effective", web_search_effective_turn))
                last_web_search_used = bool(result.get("web_search_used", False))
                last_web_search_retry_without_tool = bool(result.get("web_search_retry_without_tool", False))
                model_text = _sanitize_user_visible_response_text(str(result.get("text", "")).strip())
                cleaned_text = model_text
                candidate_tool_calls = _collect_tool_call_candidates(result.get("tool_calls"), model_text)
                if (
                    web_search_effective_turn
                    and not candidate_tool_calls
                    and _is_scrape_request(user_message)
                    and _is_scrape_refusal_text(model_text)
                ):
                    retry_prompt = _tool_protocol_prompt(
                        _rewrite_lookup_request_for_safe_web_search(user_message),
                        tool_observations,
                        ide_context_json,
                        web_search_effective=True,
                        attachment_context=prompt_attachment_context,
                    )
                    retry_result = await client.ask(
                        retry_prompt,
                        current_provider,
                        current_model,
                        idempotency_key=f"chat-ask-retry-{uuid.uuid4()}",
                        thread_id=current_thread_id,
                        conversation_mode=conversation_mode,
                        persist_context=(conversation_mode == "persistent"),
                        enable_tools=enable_local_tools_turn,
                        enable_web_search=True,
                        attachments=attachment_file_ids_for_turn,
                    )
                    retry_thread = retry_result.get("thread_id")
                    if retry_thread:
                        current_thread_id = str(retry_thread)
                    _maybe_warn_context_disabled(retry_result if isinstance(retry_result, dict) else {})
                    model_text = _sanitize_user_visible_response_text(str(retry_result.get("text", "")).strip())
                    cleaned_text = model_text
                    candidate_tool_calls = _collect_tool_call_candidates(
                        retry_result.get("tool_calls"),
                        model_text,
                    )
                    last_web_search_effective = bool(retry_result.get("web_search_effective", True))
                    last_web_search_used = bool(retry_result.get("web_search_used", False))
                    last_web_search_retry_without_tool = bool(
                        retry_result.get("web_search_retry_without_tool", False)
                    )
                # Avoid duplicate long answers: during tool-mode iterations, model text can
                # include draft/final prose on each step while tool calls are still pending.
                # Only render this immediate text when the turn is clearly text-only and we
                # are about to finish without a follow-up tool step.
                should_render_immediate_text = bool(
                    cleaned_text
                    and not candidate_tool_calls
                    and step == 0
                    and not tool_observations
                    and not completion_criteria.active
                    and not edit_intent
                )
                if should_render_immediate_text:
                    render_assistant(console, cleaned_text)
                    last_assistant_text_for_turn = cleaned_text
                if web_search_effective_turn and show_web_diagnostics:
                    ws_sources = result.get("web_search_sources") or []
                    source_count = len(ws_sources) if isinstance(ws_sources, list) else 0
                    _render_capability_status(
                        "web_search",
                        f"requested={web_search_requested_turn} used={last_web_search_used} sources={source_count}",
                    )
                    _render_web_source_table(result if isinstance(result, dict) else {})
                render_status_bar(
                    console,
                    _status_actor(),
                    f"cost {result.get('v_cost', '?')} $V",
                    workspace_root,
                )
                _render_usage_summary(result if isinstance(result, dict) else {})

            candidate_tool_calls, synth_pre_errors, synth_pre_fired = _maybe_prepend_synth_write(
                tool_reqs=candidate_tool_calls,
                user_message=user_message,
                model_text=model_text,
                planner_edit_intent=edit_intent,
                tool_observations=tool_observations,
                reason_if_empty="no_tool_calls_with_code_block",
                reason_if_non_mutating="non_mutating_candidates_only",
                debug_label="prepended synthesized Write tool call before truncation",
                preprocess=None,
            )
            for err in synth_pre_errors:
                tool_observations.append(err)

            if not candidate_tool_calls:
                code_blocks_count = len(_extract_fenced_code_blocks(model_text))
                path_targets_count = len(_path_hints_from_message(user_message))
                _tool_debug(
                    "text-only gate pre-fallback: "
                    f"candidate_count=0 "
                    f"synth_pre_fired={synth_pre_fired} "
                    f"code_blocks_count={code_blocks_count} "
                    f"path_targets_count={path_targets_count} "
                    f"patch_intent={_has_patch_intent(user_message)} "
                    f"planner_edit_intent={edit_intent}"
                )
                fallback_req = _synth_patch_tool_req_for_intent(
                    user_message=user_message,
                    tool_observations=tool_observations,
                    force_patch_intent=planner_edit_intent,
                )
                if fallback_req is not None:
                    candidate_tool_calls.append(fallback_req)
                    _tool_debug("fallback synthesized fs_apply_patch after model produced no tool request")
                else:
                    if step == 0 and not completion_criteria.active and not edit_intent and not tool_observations:
                        return True
                    if completion_criteria.requires_mutation:
                        synth_skip_reason = _diagnose_synth_write_skip_reason(
                            user_message=user_message,
                            model_text=model_text,
                            tool_observations=tool_observations,
                            planner_edit_intent=edit_intent,
                        )
                        if synth_skip_reason:
                            emit_metric("tool_mutation_blocked_total", {"reason": synth_skip_reason})
                            tool_observations.append(
                                {
                                    "status": "blocked",
                                    "error": "synth_write_skipped",
                                    "detail": synth_skip_reason,
                                }
                            )
                    _tool_debug("finalizing/continuing with text-only answer after synth/fallback checks")
                    loop_action = await _continue_or_finalize_for_completion(
                        reason_if_finalize="completed",
                        step=step,
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue

            if not current_run_id:
                console.print("[red]Tool request ignored: no active run.[/red]")
                return bool(cleaned_text)

            preprocessed_calls: list[dict[str, Any]] = []
            for raw_call in candidate_tool_calls:
                prepared, prep_error = _preprocess_tool_request_for_runtime(
                    tool_req=raw_call,
                    user_message=user_message,
                    model_text=model_text,
                    workspace_root=workspace_root,
                    tool_observations=tool_observations,
                    force_patch_intent=planner_edit_intent,
                )
                if prep_error is not None:
                    tool_observations.append(prep_error)
                    continue
                if prepared is not None:
                    preprocessed_calls.append(prepared)

            # Recovery: after preprocessing, if executable calls are still non-mutating
            # (or empty), synthesize and preprocess Write from model code block.
            preprocessed_calls, synth_post_errors, _ = _maybe_prepend_synth_write(
                tool_reqs=preprocessed_calls,
                user_message=user_message,
                model_text=model_text,
                planner_edit_intent=edit_intent,
                tool_observations=tool_observations,
                reason_if_empty="post_preprocess_no_tool_calls_with_code_block",
                reason_if_non_mutating="post_preprocess_non_mutating_only",
                debug_label="prepended synthesized Write tool call after preprocess",
                preprocess=lambda req: _preprocess_tool_request_for_runtime(
                    tool_req=req,
                    user_message=user_message,
                    model_text=model_text,
                    workspace_root=workspace_root,
                    tool_observations=tool_observations,
                    force_patch_intent=planner_edit_intent,
                ),
            )
            for err in synth_post_errors:
                tool_observations.append(err)
            synth_post_blocked = bool(synth_post_errors)

            if not preprocessed_calls:
                if synth_post_blocked and completion_criteria.requires_mutation:
                    loop_action = await _finalize_or_continue_with_intercept(
                        reason="synth_prepare_blocked",
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue
                if any(str(obs.get("status")) == "noop" for obs in tool_observations):
                    loop_action = await _continue_or_finalize_for_completion(
                        reason_if_finalize="completed",
                        step=step,
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue
                reason = "blocked_invalid_args"
                if any(str(obs.get("error")) == "unknown_tool_name" for obs in tool_observations):
                    reason = "unknown_tool"
                loop_action = await _continue_or_finalize_for_completion(
                    reason_if_finalize=reason,
                    step=step,
                    edit_intent=edit_intent,
                )
                if loop_action == LoopAction.FINALIZED:
                    return True
                continue

            # Continue-aligned execution discipline: process one tool call at a time,
            # then re-ask model with fresh observations.
            if len(preprocessed_calls) > 1:
                emit_metric(
                    "tool_batch_truncated_total",
                    {"count": str(len(preprocessed_calls))},
                )
                preprocessed_calls = preprocessed_calls[:1]

            did_any_execution = False
            duplicate_suppressed = False
            policy_denied = False
            mutation_conflict = False
            terminal_reason: str | None = None
            for tool_req in preprocessed_calls:
                tool_name = str(tool_req.get("tool_name", "")).strip()
                arguments = tool_req.get("arguments", {})
                shell_mode = tool_req.get("shell_mode")
                timeout_sec = int(tool_req.get("timeout_sec", 30))
                write_meta = tool_req.get("_write_meta") if isinstance(tool_req.get("_write_meta"), dict) else None
                append_payload_fp: str | None = None
                if tool_name == "fs_apply_patch" and isinstance(write_meta, dict):
                    if str(write_meta.get("operation_kind") or "") == "append":
                        append_path = str(write_meta.get("path") or "").strip().replace("\\", "/")
                        append_content = write_meta.get("append_content")
                        if append_path and isinstance(append_content, str) and append_content.strip():
                            append_payload_fp = (
                                f"{append_path}|{_sha256_hex(append_content.encode('utf-8'))}"
                            )
                if (
                    append_payload_fp is not None
                    and append_payload_fp in successful_append_payload_fingerprints
                    and not _is_patch_repeat_followup_intent(user_message)
                ):
                    duplicate_suppressed = True
                    emit_metric(
                        "duplicate_mutation_block_total",
                        {"intent": "append", "reason": "duplicate_append_same_payload_blocked"},
                    )
                    tool_observations.append(
                        {
                            "tool_name": tool_name,
                            "status": "blocked",
                            "error": "duplicate_append_same_payload_blocked",
                            "detail": "Append payload already applied in this run; blocked repeated mutation.",
                        }
                    )
                    continue

                if tool_name == "fs_apply_patch" and isinstance(write_meta, dict):
                    old_contents = write_meta.get("old_contents")
                    new_contents = write_meta.get("new_contents")
                    operation_kind = str(write_meta.get("operation_kind") or "full_replace").strip().lower()
                    if isinstance(old_contents, str) and isinstance(new_contents, str):
                        safety_ok, safety_reason = _validate_patch_safety(
                            old_text=old_contents,
                            new_text=new_contents,
                            intent=operation_kind,
                        )
                        if not safety_ok:
                            _emit_intent_validator_result(
                                intent=operation_kind or "unknown",
                                reason=str(safety_reason or "unknown"),
                            )
                            tool_observations.append(
                                {
                                    "tool_name": tool_name,
                                    "status": "blocked",
                                    "error": str(safety_reason or "patch_safety_blocked"),
                                    "detail": "Patch safety validator blocked mutation before execution.",
                                }
                            )
                            continue
                        _emit_intent_validator_result(intent=operation_kind or "unknown")

                policy = evaluate_tool_policy(
                    tool_name=tool_name,
                    shell_mode=str(shell_mode or "read_only"),
                    approval_mode=approval_mode,
                )
                if policy == ToolPolicyDecision.EXCLUDE:
                    policy_denied = True
                    tool_observations.append(
                        {
                            "tool_name": tool_name,
                            "status": "blocked",
                            "error": "tool_excluded_by_policy",
                        }
                    )
                    continue
                # Existing-file Write path: shape edits via IDE diff or terminal fallback before approval/propose.
                if (
                    tool_name == "fs_apply_patch"
                    and isinstance(write_meta, dict)
                    and bool(write_meta.get("existing_file"))
                    and current_run_id
                ):
                    raw_diff_result: dict[str, Any] | None = None
                    diff_surface = ""
                    ide_fallback_reason: str | None = None
                    write_path = str(write_meta.get("path") or "")
                    patch_text = str(arguments.get("patch") or "")
                    if (
                        bool(state.bridge_caps.get("connected"))
                        and bool(state.bridge_caps.get("show_diff"))
                    ):
                        diff_timeout_sec = max(
                            1.0,
                            float(os.getenv("OPENVEGAS_IDE_INTERACTIVE_DIFF_TIMEOUT_SEC", "12")),
                        )
                        try:
                            _maybe_prompt_vscode_extension_for_interactive_diff()
                            envelope = await asyncio.wait_for(
                                client.ide_message(
                                    request_id=f"show-diff-{uuid.uuid4()}",
                                    method="show_diff_interactive",
                                    params={
                                        "run_id": current_run_id,
                                        "runtime_session_id": runtime_session_id,
                                        "path": write_path,
                                        "new_contents": str(write_meta.get("new_contents") or ""),
                                        "allow_partial_accept": True,
                                    },
                                ),
                                timeout=diff_timeout_sec,
                            )
                            if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
                                err = envelope.get("error") or {}
                                code = str(err.get("code") or "")
                                detail = str(err.get("detail") or "interactive show_diff failed")
                                raise APIError(409, {"error": code or "show_diff_interactive_failed", "detail": detail})
                            payload = envelope.get("result") if isinstance(envelope, dict) else None
                            if not isinstance(payload, dict):
                                ide_fallback_reason = "ide_bridge_unavailable"
                                emit_metric(
                                    "tool_diff_fallback_total",
                                    {"from": "ide_interactive", "to": "terminal", "reason": "bridge_error"},
                                )
                                payload = None
                            if isinstance(payload, dict):
                                if is_valid_show_diff_payload(payload):
                                    raw_diff_result = payload
                                    diff_surface = "ide"
                                    emit_metric("tool_show_diff_invoked_total", {"tool": "write"})
                                    emit_metric("tool_diff_surface_total", {"surface": "ide_interactive"})
                                else:
                                    if _ide_bridge_trace_enabled():
                                        _ide_bridge_debug(
                                            "malformed interactive payload shape="
                                            + json.dumps(
                                                redact_show_diff_payload_shape(payload),
                                                sort_keys=True,
                                                ensure_ascii=False,
                                            )
                                        )
                                    emit_metric(
                                        "tool_diff_fallback_total",
                                        {"from": "ide_interactive", "to": "terminal", "reason": "malformed_payload"},
                                    )
                                    ide_fallback_reason = "malformed_diff_payload"
                                    raw_diff_result = None
                        except asyncio.TimeoutError:
                            emit_metric(
                                "tool_diff_fallback_total",
                                {"from": "ide_interactive", "to": "terminal", "reason": "timeout"},
                            )
                            ide_fallback_reason = "timeout"
                            raw_diff_result = None
                        except APIError as e:
                            body = e.data if isinstance(e.data, dict) else {}
                            code = str(body.get("error") or "")
                            emit_metric(
                                "tool_diff_fallback_total",
                                {"from": "ide_interactive", "to": "terminal", "reason": "bridge_error"},
                            )
                            ide_fallback_reason = "ide_bridge_unavailable"
                            if code in {"invalid_transition"}:
                                state.bridge_caps["connected"] = False
                                state.bridge_caps["show_diff"] = False
                                emit_metric("tool_show_diff_skipped_total", {"reason": "bridge_unavailable"})
                            else:
                                detail = body.get("detail", e.detail)
                                console.print(f"[yellow]show_diff skipped: {detail}[/yellow]")
                        except Exception as e:
                            emit_metric(
                                "tool_diff_fallback_total",
                                {"from": "ide_interactive", "to": "terminal", "reason": "bridge_error"},
                            )
                            ide_fallback_reason = "ide_bridge_unavailable"
                            if _ide_bridge_trace_enabled():
                                _ide_bridge_debug(f"interactive diff bridge error={type(e).__name__}: {e}")

                    if raw_diff_result is None and patch_text.strip() and _terminal_diff_fallback_enabled():
                        parsed_original = parse_unified_patch_terminal(patch_text)
                        if parsed_original.parse_error:
                            tool_observations.append(
                                {
                                    "tool_name": tool_name,
                                    "status": "blocked",
                                    "error": "user_declined_edit",
                                    "detail": "Patch could not be parsed for diff review.",
                                }
                            )
                            continue

                        max_hunks = max(1, int(os.getenv("OPENVEGAS_TERMINAL_DIFF_MAX_HUNKS", "80")))
                        max_patch_bytes = max(
                            1024,
                            int(os.getenv("OPENVEGAS_TERMINAL_DIFF_MAX_PATCH_BYTES", "262144")),
                        )
                        patch_bytes = len(patch_text.encode("utf-8"))
                        if parsed_original.hunks_total > max_hunks or patch_bytes > max_patch_bytes:
                            emit_metric("tool_terminal_diff_invoked_total", {"tool": "write"})
                            emit_metric("tool_terminal_diff_error_total", {"tool": "write", "reason": "large_diff"})
                            emit_metric(
                                "tool_diff_decision_total",
                                {"diff_surface": "terminal", "diff_outcome": "error"},
                            )
                            tool_observations.append(
                                {
                                    "tool_name": tool_name,
                                    "status": "blocked",
                                    "error": "user_declined_edit_large_diff",
                                    "detail": "Patch exceeds terminal diff review bounds.",
                                }
                            )
                            continue

                        emit_metric("tool_terminal_diff_invoked_total", {"tool": "write"})
                        _drain_stdin_buffer(window_ms=0)
                        raw_diff_result = review_patch_terminal(
                            path=write_path,
                            patch_text=patch_text,
                            allow_partial_accept=True,
                            console=console,
                        )
                        _drain_stdin_buffer(window_ms=0)
                        diff_surface = "terminal"
                    elif raw_diff_result is None and patch_text.strip():
                        emit_metric("tool_show_diff_skipped_total", {"reason": "terminal_fallback_disabled"})

                    if raw_diff_result is not None:
                        raw_diff_error = ""
                        if isinstance(raw_diff_result, dict):
                            raw_diff_error = str(raw_diff_result.get("error") or "").strip().lower()
                        diff_result = normalize_show_diff_result(raw_diff_result, default_path=write_path)
                        decisions = diff_result.get("decisions", [])
                        hunks_total = int(diff_result.get("hunks_total", len(decisions)))
                        accepted_hunks: set[int] = set()
                        for d in decisions:
                            if not isinstance(d, dict):
                                continue
                            if str(d.get("decision")) != "accepted":
                                continue
                            try:
                                accepted_hunks.add(int(d.get("hunk_index")))
                            except Exception:
                                continue

                        reject_reason = "reject_all"
                        if bool(diff_result.get("timed_out")):
                            reject_reason = "timeout"

                        if hunks_total > 0 and not accepted_hunks:
                            if diff_surface == "ide":
                                emit_metric("tool_show_diff_rejected_total", {"tool": "write"})
                            elif diff_surface == "terminal":
                                emit_metric("tool_terminal_diff_rejected_total", {"tool": "write"})
                            emit_metric(
                                "tool_diff_decision_total",
                                {"diff_surface": diff_surface or "unknown", "diff_outcome": reject_reason},
                            )
                            blocked_reason = "All diff hunks were rejected."
                            if raw_diff_error == "non_tty":
                                blocked_reason = "edit blocked: non_interactive_terminal"
                            elif raw_diff_error == "timeout" or reject_reason == "timeout":
                                blocked_reason = "edit blocked: timeout"
                            elif ide_fallback_reason == "malformed_diff_payload":
                                blocked_reason = "edit blocked: malformed_diff_payload"
                            elif ide_fallback_reason == "ide_bridge_unavailable":
                                blocked_reason = "edit blocked: ide_bridge_unavailable"
                            tool_observations.append(
                                {
                                    "tool_name": tool_name,
                                    "status": "blocked",
                                    "error": "user_declined_edit",
                                    "detail": blocked_reason,
                                }
                            )
                            continue

                        if hunks_total > 0 and len(accepted_hunks) < hunks_total:
                            filtered_patch, filtered_parsed = _filter_patch_by_accepted_hunks_with_parsed(
                                patch_text,
                                accepted_hunks,
                            )
                            if (
                                not filtered_patch
                                or filtered_parsed is None
                                or not _is_valid_filtered_patch(filtered_patch, parsed_patch=filtered_parsed)
                            ):
                                if diff_surface == "ide":
                                    emit_metric("tool_show_diff_rejected_total", {"tool": "write"})
                                elif diff_surface == "terminal":
                                    emit_metric("tool_terminal_diff_error_total", {"tool": "write", "reason": "invalid_filtered_patch"})
                                emit_metric(
                                    "tool_diff_decision_total",
                                    {"diff_surface": diff_surface or "unknown", "diff_outcome": "error"},
                                )
                                tool_observations.append(
                                    {
                                        "tool_name": tool_name,
                                        "status": "blocked",
                                        "error": "user_declined_edit",
                                        "detail": "Filtered patch was invalid after hunk decisions.",
                                    }
                                )
                                continue
                            arguments["patch"] = filtered_patch
                            patch_text = filtered_patch
                            footprint = filtered_patch_footprint(filtered_parsed)
                            write_meta = dict(write_meta)
                            write_meta["filtered_target_files"] = list(footprint.get("target_files", []))
                            write_meta["filtered_hunks_total"] = int(footprint.get("hunks_total", 0))
                            write_meta["filtered_touched_per_file"] = dict(footprint.get("touched_per_file", {}))
                            tool_req["_write_meta"] = write_meta
                            if diff_surface == "ide":
                                emit_metric(
                                    "tool_show_diff_partial_accept_total",
                                    {"tool": "write", "accepted": str(len(accepted_hunks)), "total": str(hunks_total)},
                                )
                            elif diff_surface == "terminal":
                                emit_metric(
                                    "tool_terminal_diff_partial_accept_total",
                                    {"tool": "write", "accepted": str(len(accepted_hunks)), "total": str(hunks_total)},
                                )
                            emit_metric(
                                "tool_diff_decision_total",
                                {"diff_surface": diff_surface or "unknown", "diff_outcome": "partial"},
                            )
                        elif hunks_total > 0:
                            if diff_surface == "ide":
                                emit_metric("tool_show_diff_accept_all_total", {"tool": "write"})
                            elif diff_surface == "terminal":
                                emit_metric("tool_terminal_diff_accept_all_total", {"tool": "write"})
                            emit_metric(
                                "tool_diff_decision_total",
                                {"diff_surface": diff_surface or "unknown", "diff_outcome": "accept_all"},
                            )

                call_key = _semantic_tool_signature(tool_name, arguments, str(shell_mode or "read_only"))
                if executed_tool_calls.get(call_key, 0) >= 1:
                    duplicate_suppressed = True
                    if tool_name == "fs_read":
                        emit_metric("tool_duplicate_read_suppressed_total", {"tool": "fs_read"})
                    tool_observations.append(
                        {
                            "tool_name": tool_name,
                            "status": "blocked",
                            "error": "duplicate_tool_call",
                            "detail": "Repeated identical tool call suppressed.",
                        }
                    )
                    continue

                if policy == ToolPolicyDecision.ASK:
                    action_scope = action_scope_for(tool_name, arguments if isinstance(arguments, dict) else {})
                    if not should_auto_allow(session_approval, action_scope):
                        dealer_panel.render(map_lifecycle_event_to_state("approval_wait"), "approval required")
                        action_label = describe_tool_action(tool_name, arguments)
                        _drain_stdin_buffer(window_ms=0)
                        decision = choose_approval(
                            tool_name=tool_name,
                            arguments=arguments if isinstance(arguments, dict) else {},
                            action_label=action_label,
                            console=console,
                        )
                        _drain_stdin_buffer(window_ms=0)
                        apply_approval_decision(session_approval, action_scope, decision)
                        if decision == ApprovalDecision.DENY_AND_REPLAN:
                            policy_denied = True
                            tool_observations.append(
                                {
                                    "tool_name": tool_name,
                                    "status": "blocked",
                                    "error": "approval_denied_replan",
                                }
                            )
                            continue

                try:
                    proposed = await _call_with_stale_retry(
                        lambda: client.agent_tool_propose(
                            run_id=current_run_id,
                            runtime_session_id=runtime_session_id,
                            expected_run_version=current_run_version,
                            expected_valid_actions_signature=current_signature,
                            idempotency_key=f"tool-propose-{uuid.uuid4()}",
                            tool_name=tool_name,
                            arguments=arguments,
                            shell_mode=str(shell_mode) if shell_mode is not None else None,
                            timeout_sec=timeout_sec,
                            plan_mode=plan_mode,
                        ),
                        endpoint="propose",
                    )
                except APIError as e:
                    body = e.data if isinstance(e.data, dict) else {}
                    code = body.get("error", "tool_propose_failed")
                    if code in {"stale_projection", "idempotency_conflict", "active_mutation_in_progress"}:
                        emit_metric("tool_cas_conflict_total", {"endpoint": "propose", "error": code})
                    if code == "active_mutation_in_progress":
                        mutation_conflict = True
                        if not state.active_mutation_timeout_hit:
                            state.pending_retry_tool_req = {
                                "type": "tool_call",
                                "tool_name": tool_name,
                                "arguments": dict(arguments) if isinstance(arguments, dict) else {},
                                "shell_mode": str(shell_mode or "read_only"),
                                "timeout_sec": timeout_sec,
                            }
                    detail = body.get("detail", e.detail)
                    console.print(f"[red]{code}: {detail}[/red]")
                    tool_observations.append({"tool_name": tool_name, "status": "proposal_error", "error": code})
                    if mutation_conflict:
                        break
                    continue

                _update_fence(proposed)
                tool_request = proposed.get("tool_request")
                if not isinstance(tool_request, dict):
                    err = proposed.get("error")
                    if err:
                        console.print(f"[yellow]{err}: {proposed.get('detail', '')}[/yellow]")
                        tool_observations.append({"tool_name": tool_name, "status": "blocked", "error": err})
                        continue
                    console.print("[red]Tool proposal did not return a tool_request payload.[/red]")
                    continue

                tool_call_id = str(tool_request.get("tool_call_id", ""))
                execution_token = str(tool_request.get("execution_token", ""))
                if not tool_call_id or not execution_token:
                    console.print("[red]Invalid tool request payload from server.[/red]")
                    tool_observations.append({"tool_name": tool_name, "status": "start_error", "error": "invalid_tool_request_payload"})
                    continue

                try:
                    started = await _call_with_stale_retry(
                        lambda: client.agent_tool_start(
                            run_id=current_run_id,
                            runtime_session_id=runtime_session_id,
                            tool_call_id=tool_call_id,
                            execution_token=execution_token,
                            expected_run_version=current_run_version,
                            expected_valid_actions_signature=current_signature,
                            idempotency_key=f"tool-start-{uuid.uuid4()}",
                        ),
                        endpoint="start",
                    )
                    _update_fence(started)
                except APIError as e:
                    body = e.data if isinstance(e.data, dict) else {}
                    code = body.get("error", "tool_start_failed")
                    if code in {"stale_projection", "idempotency_conflict", "active_mutation_in_progress"}:
                        emit_metric("tool_cas_conflict_total", {"endpoint": "start", "error": code})
                    if code == "active_mutation_in_progress":
                        mutation_conflict = True
                        if not state.active_mutation_timeout_hit:
                            state.pending_retry_tool_req = {
                                "type": "tool_call",
                                "tool_name": tool_name,
                                "arguments": dict(arguments) if isinstance(arguments, dict) else {},
                                "shell_mode": str(shell_mode or "read_only"),
                                "timeout_sec": timeout_sec,
                            }
                    detail = body.get("detail", e.detail)
                    console.print(f"[red]{code}: {detail}[/red]")
                    tool_observations.append(
                        {"tool_name": tool_name, "tool_call_id": tool_call_id, "status": "start_error", "error": code}
                    )
                    if mutation_conflict:
                        break
                    continue

                event_label = describe_tool_action(tool_name, arguments)
                event_detail = f"id={tool_call_id}" if verbose_tool_events else ""
                render_tool_event(console, event_label, event_detail)
                dealer_panel.render(
                    map_lifecycle_event_to_state("tool_start", tool_name=tool_name, status="running"),
                    event_label,
                )
                outcome, inactive_status = await _execute_with_heartbeat(
                    tool_request=tool_request,
                    tool_call_id=tool_call_id,
                    execution_token=execution_token,
                )
                if tool_name == "shell_run":
                    streamed_tools_seen["shell_run"] = True
                if outcome is None:
                    emit_metric(
                        "tool_heartbeat_miss_total",
                        {"remote_status": str(inactive_status or "inactive")},
                    )
                    dealer_panel.render(
                        map_lifecycle_event_to_state("tool_result", tool_name=tool_name, status="failed"),
                        str(inactive_status or "inactive"),
                    )
                    tool_observations.append(
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "status": "inactive",
                            "remote_status": inactive_status,
                        }
                    )
                    continue

                # Continue-style recovery: for Write-derived edits, if patch apply fails due
                # hunk drift, regenerate a fresh patch from current file state and retry once.
                if (
                    str(outcome.result_status) == "failed"
                    and tool_name == "fs_apply_patch"
                    and isinstance(write_meta, dict)
                    and isinstance(write_meta.get("path"), str)
                    and isinstance(write_meta.get("new_contents"), str)
                ):
                    original_outcome = outcome
                    original_patch = str(arguments.get("patch", ""))
                    regen_patch = original_patch
                    regen, regen_err = _prepare_write_patch(
                        workspace_root=workspace_root,
                        arguments={
                            "filepath": str(write_meta.get("path")),
                            "content": str(write_meta.get("new_contents")),
                        },
                    )
                    if regen_err is not None:
                        if str(regen_err.get("status")) == "noop" and str(regen_err.get("error")) == "no_change":
                            emit_metric("tool_apply_patch_retry_total", {"status": "already_applied"})
                            outcome = ToolExecutionResult(
                                result_status="succeeded",
                                result_payload={
                                    "ok": True,
                                    "detail": "Write target already matches requested content.",
                                    "recovery_mode": "already_applied",
                                },
                                stdout=original_outcome.stdout,
                                stderr=original_outcome.stderr,
                            )
                        else:
                            fallback = _attempt_bootstrap_write_fallback(
                                workspace_root=workspace_root,
                                rel_path=str(write_meta.get("path") or ""),
                                new_contents=str(write_meta.get("new_contents") or ""),
                                existing_file=bool(write_meta.get("existing_file")),
                            )
                            if fallback is not None and str(fallback.result_status) == "succeeded":
                                emit_metric("tool_apply_patch_retry_total", {"status": "bootstrap_fallback_succeeded"})
                                outcome = fallback
                            else:
                                emit_metric("tool_apply_patch_retry_total", {"status": "prepare_failed"})
                                reason = (
                                    "patch_recovery_failed_bootstrap_fallback_exhausted"
                                    if fallback is not None
                                    else "patch_recovery_failed"
                                )
                                detail = str(regen_err.get("detail") or "Patch recovery preparation failed.")
                                outcome = ToolExecutionResult(
                                    result_status="failed",
                                    result_payload=_patch_recovery_payload(
                                        reason_code=reason,
                                        detail=detail,
                                        original_outcome=original_outcome,
                                        retry_outcome=fallback,
                                        original_patch=original_patch,
                                        regenerated_patch=regen_patch,
                                    ),
                                    stdout=(fallback.stdout if fallback is not None else original_outcome.stdout),
                                    stderr=(fallback.stderr if fallback is not None else original_outcome.stderr),
                                )
                                terminal_reason = reason
                    elif isinstance(regen, dict):
                        regen_args = dict(regen.get("arguments", {}))
                        regenerated_patch = str(regen_args.get("patch", ""))
                        regen_patch = regenerated_patch
                        scope_failure = _validate_patch_recovery_scope(
                            original_patch=original_patch,
                            regenerated_patch=regenerated_patch,
                        )
                        if scope_failure is not None:
                            emit_metric("tool_apply_patch_retry_total", {"status": "scope_rejected"})
                            outcome = ToolExecutionResult(
                                result_status="failed",
                                result_payload=_patch_recovery_payload(
                                    reason_code=scope_failure,
                                    detail="Regenerated patch exceeded bounded recovery scope.",
                                    original_outcome=original_outcome,
                                    retry_outcome=None,
                                    original_patch=original_patch,
                                    regenerated_patch=regenerated_patch,
                                    scope_guard_rejected=True,
                                    scope_guard_subreason=scope_failure,
                                ),
                                stdout=original_outcome.stdout,
                                stderr=original_outcome.stderr,
                            )
                            terminal_reason = "patch_recovery_scope_expansion"
                        else:
                            retry_outcome = await asyncio.to_thread(
                                execute_tool_request,
                                workspace_root=workspace_root,
                                tool_name="fs_apply_patch",
                                arguments=regen_args,
                                shell_mode=str(shell_mode or "read_only"),
                                timeout_sec=timeout_sec,
                            )
                            if str(retry_outcome.result_status) == "succeeded":
                                emit_metric("tool_apply_patch_retry_total", {"status": "succeeded"})
                                outcome = retry_outcome
                                arguments = regen_args
                            else:
                                fallback = _attempt_bootstrap_write_fallback(
                                    workspace_root=workspace_root,
                                    rel_path=str(write_meta.get("path") or ""),
                                    new_contents=str(write_meta.get("new_contents") or ""),
                                    existing_file=bool(write_meta.get("existing_file")),
                                )
                                if fallback is not None and str(fallback.result_status) == "succeeded":
                                    emit_metric("tool_apply_patch_retry_total", {"status": "bootstrap_fallback_succeeded"})
                                    outcome = fallback
                                else:
                                    emit_metric("tool_apply_patch_retry_total", {"status": "failed"})
                                    reason = (
                                        "patch_recovery_failed_bootstrap_fallback_exhausted"
                                        if fallback is not None
                                        else "patch_recovery_failed"
                                    )
                                    outcome = ToolExecutionResult(
                                        result_status="failed",
                                        result_payload=_patch_recovery_payload(
                                            reason_code=reason,
                                            detail="Patch recovery retry failed.",
                                            original_outcome=original_outcome,
                                            retry_outcome=(fallback if fallback is not None else retry_outcome),
                                            original_patch=original_patch,
                                            regenerated_patch=regenerated_patch,
                                        ),
                                        stdout=(fallback.stdout if fallback is not None else retry_outcome.stdout),
                                        stderr=(fallback.stderr if fallback is not None else retry_outcome.stderr),
                                    )
                                    terminal_reason = reason

                try:
                    stdout_cap = max(1024, int(os.getenv("OPENVEGAS_TOOL_STDOUT_MAX_BYTES", "131072")))
                    stderr_cap = max(1024, int(os.getenv("OPENVEGAS_TOOL_STDERR_MAX_BYTES", "131072")))
                    stdout_meta = redact_hash_truncate(outcome.stdout or "", stdout_cap)
                    stderr_meta = redact_hash_truncate(outcome.stderr or "", stderr_cap)
                    submission_hash = compute_result_submission_hash(
                        result_status=outcome.result_status,
                        result_payload=outcome.result_payload,
                        stdout_sha256=stdout_meta.sha256,
                        stderr_sha256=stderr_meta.sha256,
                    )
                    finished = await _call_with_stale_retry(
                        lambda: client.agent_tool_result(
                            run_id=current_run_id,
                            runtime_session_id=runtime_session_id,
                            tool_call_id=tool_call_id,
                            execution_token=execution_token,
                            result_status=outcome.result_status,
                            result_payload=outcome.result_payload,
                            stdout=outcome.stdout,
                            stderr=outcome.stderr,
                            stdout_truncated=stdout_meta.truncated,
                            stderr_truncated=stderr_meta.truncated,
                            stdout_sha256=stdout_meta.sha256,
                            stderr_sha256=stderr_meta.sha256,
                            result_submission_hash=submission_hash,
                        ),
                        endpoint="result",
                    )
                    _update_fence(finished)
                except APIError as e:
                    body = e.data if isinstance(e.data, dict) else {}
                    _update_fence(body)
                    code = body.get("error", "tool_result_failed")
                    if code in {"stale_projection", "idempotency_conflict", "active_mutation_in_progress"}:
                        emit_metric("tool_cas_conflict_total", {"endpoint": "result", "error": code})
                    if code == "active_mutation_in_progress":
                        mutation_conflict = True
                        if not state.active_mutation_timeout_hit:
                            state.pending_retry_tool_req = {
                                "type": "tool_call",
                                "tool_name": tool_name,
                                "arguments": dict(arguments) if isinstance(arguments, dict) else {},
                                "shell_mode": str(shell_mode or "read_only"),
                                "timeout_sec": timeout_sec,
                            }
                    detail = body.get("detail", e.detail)
                    console.print(f"[red]{code}: {detail}[/red]")
                    tool_observations.append(
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "status": "result_error",
                            "error": code,
                        }
                    )
                    # Best-effort cleanup: if result terminalization failed, attempt cancel
                    # to avoid leaving run blocked by a started tool row.
                    try:
                        await client.agent_tool_cancel(
                            run_id=current_run_id,
                            runtime_session_id=runtime_session_id,
                            tool_call_id=tool_call_id,
                            execution_token=execution_token,
                        )
                    except Exception:
                        pass
                    if mutation_conflict:
                        break
                    continue

                tool_observations.append(
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "result_status": outcome.result_status,
                        "result_payload": outcome.result_payload,
                        "stdout": "" if streamed_tools_seen.get(tool_name) else outcome.stdout[-8000:],
                        "stderr": outcome.stderr[-4000:],
                        "output_streamed": bool(streamed_tools_seen.get(tool_name)),
                    }
                )
                render_tool_result(
                    console,
                    describe_tool_action(tool_name, arguments),
                    str(outcome.result_status),
                )
                dealer_panel.render(
                    map_tool_event_to_avatar_state(tool_name, str(outcome.result_status)),
                    describe_tool_action(tool_name, arguments),
                )
                render_status_bar(
                    console,
                    _status_actor(),
                    f"tool {str(outcome.result_status)}",
                    workspace_root,
                )
                if str(outcome.result_status) == "succeeded":
                    last_successful_tool = tool_name
                    _tool_debug(f"last_successful_tool={last_successful_tool}")
                    if tool_name == "fs_apply_patch":
                        state.repeated_patch_failures.clear()
                        if append_payload_fp is not None:
                            successful_append_payload_fingerprints.add(append_payload_fp)
                elif tool_name == "fs_apply_patch":
                    failure_sig = _patch_failure_signature(
                        arguments=arguments if isinstance(arguments, dict) else {},
                        write_meta=write_meta if isinstance(write_meta, dict) else None,
                        outcome=outcome,
                    )
                    repeat_count = state.repeated_patch_failures.get(failure_sig, 0) + 1
                    state.repeated_patch_failures[failure_sig] = repeat_count
                    emit_metric("tool_apply_patch_same_intent_fail_total", {"count": str(repeat_count)})
                    if repeat_count >= patch_failure_repeat_limit:
                        tool_observations.append(
                            {
                                "tool_name": tool_name,
                                "status": "blocked",
                                "error": "patch_recovery_failed_same_intent_circuit_break",
                                "detail": "Repeated identical patch failure; stopped retry loop.",
                            }
                        )
                        loop_action = await _finalize_or_continue_with_intercept(
                            reason="patch_recovery_failed_same_intent_circuit_break",
                            edit_intent=edit_intent,
                        )
                        if loop_action == LoopAction.FINALIZED:
                            return True
                        continue
                executed_tool_calls[call_key] = executed_tool_calls.get(call_key, 0) + 1
                did_any_execution = True
                if terminal_reason is not None:
                    loop_action = await _finalize_or_continue_with_intercept(
                        reason=terminal_reason,
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue

            if did_any_execution:
                loop_action = await _continue_or_finalize_for_completion(
                    reason_if_finalize="completed",
                    step=step,
                    edit_intent=edit_intent,
                    after_execution=True,
                )
                if loop_action == LoopAction.FINALIZED:
                    return True
                continue
            if not did_any_execution and preprocessed_calls:
                if mutation_conflict:
                    if state.active_mutation_timeout_hit:
                        timeout_reason = (
                            "workflow_stalled_no_new_observations"
                            if not state.active_mutation_observation_changed
                            else "active_mutation_timeout"
                        )
                        loop_action = await _finalize_or_continue_with_intercept(
                            reason=timeout_reason,
                            edit_intent=edit_intent,
                        )
                        if loop_action == LoopAction.FINALIZED:
                            return True
                        continue
                    if state.pending_retry_tool_req is not None:
                        continue
                    loop_action = await _finalize_or_continue_with_intercept(
                        reason="active_mutation_in_progress",
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue
                if duplicate_suppressed:
                    loop_action = await _continue_or_finalize_for_completion(
                        reason_if_finalize="duplicate_suppressed",
                        step=step,
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue
                if policy_denied:
                    loop_action = await _continue_or_finalize_for_completion(
                        reason_if_finalize="policy_denied",
                        step=step,
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue
                if any(str(obs.get("error")) == "unknown_tool_name" for obs in tool_observations):
                    loop_action = await _continue_or_finalize_for_completion(
                        reason_if_finalize="unknown_tool",
                        step=step,
                        edit_intent=edit_intent,
                    )
                    if loop_action == LoopAction.FINALIZED:
                        return True
                    continue
                loop_action = await _continue_or_finalize_for_completion(
                    reason_if_finalize="blocked_invalid_args",
                    step=step,
                    edit_intent=edit_intent,
                )
                if loop_action == LoopAction.FINALIZED:
                    return True
                continue
        console.print(f"[yellow]Stopped after max tool iterations ({max_tool_steps}).[/yellow]")
        if completion_criteria.active and not _completion_eval().satisfied:
            final_res = await _request_final_response(tool_observations)
            action = await _force_finalize(final_res, reason="completion_criteria_unmet_after_retries")
            return action == LoopAction.FINALIZED
        final_res = await _request_final_response(tool_observations)
        action = await _force_finalize(final_res, reason="max_iterations")
        return action == LoopAction.FINALIZED

    fullscreen_handoff_message: str | None = None

    async def _run_chat() -> str:
        nonlocal current_provider, current_model, current_thread_id
        nonlocal current_run_id, current_run_version, current_signature
        nonlocal plan_mode, conversation_mode, workspace_root, workspace_fp, approval_mode
        nonlocal verbose_tool_events
        nonlocal fullscreen_handoff_message
        nonlocal web_search_requested
        nonlocal last_web_search_effective
        nonlocal last_web_search_used
        nonlocal last_web_search_retry_without_tool
        nonlocal voice_transcribe_requested
        nonlocal last_voice_transcribe_effective
        nonlocal last_voice_transcribe_used
        nonlocal attachment_context_for_turn
        nonlocal voice_transcript_context_for_turn
        nonlocal attachment_markers_for_turn
        nonlocal attachment_file_ids_for_turn
        low_floor_usd = Decimal(os.getenv("TOPUP_LOW_BALANCE_FLOOR_USD", "5.00"))
        v_per_usd = Decimal(os.getenv("V_PER_USD", "100"))
        suggest_cooldown_sec = max(30, int(os.getenv("TOPUP_SUGGEST_COOLDOWN_SEC", "300")))
        last_seen_balance_usd: Decimal | None = None
        shown_topup_id: str | None = None
        shown_topup_status: str | None = None
        shown_at_monotonic: float = 0.0

        def _balance_usd_equiv(balance_v_raw: str | Decimal | float | int | None) -> Decimal:
            raw = Decimal(str(balance_v_raw or "0"))
            if v_per_usd <= 0:
                return Decimal("0.00")
            return (raw / v_per_usd).quantize(Decimal("0.01"))

        def _material_balance_change(prev_usd: Decimal | None, next_usd: Decimal) -> bool:
            if prev_usd is None:
                return next_usd <= low_floor_usd
            crossed_floor = (prev_usd > low_floor_usd and next_usd <= low_floor_usd) or (
                prev_usd <= low_floor_usd and next_usd > low_floor_usd
            )
            delta = abs(next_usd - prev_usd)
            return crossed_floor or delta >= Decimal("0.50")

        async def _maybe_render_low_balance_hint(*, force: bool = False) -> None:
            nonlocal last_seen_balance_usd, shown_topup_id, shown_topup_status, shown_at_monotonic
            try:
                bal = await client.get_balance()
                balance_v = bal.get("balance", "0")
                usd_now = _balance_usd_equiv(balance_v)

                wakeup_by_status = False
                if shown_topup_id:
                    try:
                        topup_state = await client.get_topup_status(shown_topup_id)
                        current_status = str(topup_state.get("status", ""))
                        if current_status and current_status != (shown_topup_status or ""):
                            if current_status in {"paid", "expired", "failed", "manual_reconciliation_required"}:
                                wakeup_by_status = True
                                shown_topup_id = None
                                shown_topup_status = None
                    except Exception:
                        pass

                should_check = force or wakeup_by_status or _material_balance_change(last_seen_balance_usd, usd_now)
                last_seen_balance_usd = usd_now
                if not should_check:
                    return

                if shown_topup_id and (time.monotonic() - shown_at_monotonic) < suggest_cooldown_sec:
                    emit_metric("topup_suggest_suppressed_total", {"reason": "cooldown"})
                    return

                hint = await client.suggest_topup()
                if not bool(hint.get("low_balance", False)):
                    return
                render_topup_hint(console, hint)
                emit_metric("topup_qr_generated_total", {"surface": "cli"})
                shown_topup_id = str(hint.get("topup_id") or "") or None
                shown_topup_status = str(hint.get("status") or "") or None
                shown_at_monotonic = time.monotonic()
            except Exception:
                return

        async def _read_chat_message() -> str:
            nonlocal prompt_toolkit_unavailable_warned, chat_prompt_session, chat_prompt_bindings, pending_voice_prefill, pending_voice_meta, prompt_input_active
            if use_prompt_toolkit_chat:
                if PromptSession is None:
                    if not prompt_toolkit_unavailable_warned:
                        console.print(
                            "[yellow]prompt-toolkit not installed; falling back to basic chat input.[/yellow]"
                        )
                        prompt_toolkit_unavailable_warned = True
                else:
                    if chat_prompt_bindings is None and KeyBindings is not None:
                        chat_prompt_bindings = KeyBindings()

                        @chat_prompt_bindings.add("c-v")
                        def _chat_paste_from_clipboard(event) -> None:
                            pasted = _read_clipboard_text()
                            if pasted:
                                event.current_buffer.insert_text(pasted)

                        @chat_prompt_bindings.add("escape", "v")
                        def _chat_toggle_voice(event) -> None:
                            buf = event.current_buffer
                            saved_text = str(buf.text or "")
                            saved_cursor = int(getattr(buf, "cursor_position", 0) or 0)
                            loop = asyncio.get_event_loop()

                            def _insert_from_voice(transcript: str) -> None:
                                token = str(transcript or "").strip()
                                if not token:
                                    emit_metric("voice_capture_phase_total", {"phase": "transcript_empty"})
                                    return
                                inserted = False
                                try:
                                    current = str(buf.text or "")
                                    if current == saved_text:
                                        cursor = max(0, min(len(saved_text), saved_cursor))
                                        prefix = saved_text[:cursor]
                                        suffix = saved_text[cursor:]
                                        needs_space = bool(prefix) and not prefix.endswith((" ", "\n", "\t"))
                                        injected = f" {token}" if needs_space else token
                                        buf.text = f"{prefix}{injected}{suffix}"
                                        buf.cursor_position = len(prefix) + len(injected)
                                        inserted = True
                                    else:
                                        if current and not current.endswith((" ", "\n", "\t")):
                                            buf.insert_text(" ")
                                        buf.insert_text(token)
                                        inserted = True
                                    app = getattr(chat_prompt_session, "app", None)
                                    if app is not None and hasattr(app, "invalidate"):
                                        app.invalidate()
                                except Exception:
                                    inserted = False
                                if inserted:
                                    emit_metric("voice_capture_phase_total", {"phase": "transcript_inserted_live"})


                            async def _do_toggle() -> None:
                                voice_effective = bool(
                                    resolve_capability(
                                        current_provider,
                                        current_model,
                                        "speech_to_text",
                                    )
                                )
                                if not voice_effective:
                                    console.print("[yellow]Voice capture unavailable for current provider/model.[/yellow]")
                                    return
                                was_listening = voice_button.is_recording
                                await voice_button.toggle(
                                    insert_text=_insert_from_voice,
                                    transcribe_wav=_transcribe_voice_wav,
                                )
                                if not was_listening and voice_button.is_recording:
                                    console.print("[dim]Listening... click Voice again to stop.[/dim]")

                            loop.create_task(_do_toggle())

                        @chat_prompt_bindings.add("escape", "m")
                        def _chat_mcp_list(event) -> None:
                            event.current_buffer.text = "/mcp list"
                            event.current_buffer.validate_and_handle()

                        @chat_prompt_bindings.add("escape", "a")
                        def _chat_actions_help(event) -> None:
                            event.current_buffer.text = "/help"
                            event.current_buffer.validate_and_handle()

                    if chat_prompt_session is None:
                        kwargs: dict[str, Any] = {}
                        if InMemoryHistory is not None:
                            kwargs["history"] = InMemoryHistory()
                        chat_prompt_session = PromptSession(**kwargs)

                        state = {"editing": False}

                        def _on_text_changed(_) -> None:
                            if state["editing"]:
                                return
                            try:
                                buffer = chat_prompt_session.default_buffer
                                before = str(buffer.text or "")
                                after = _normalize_live_chat_input_text(before)
                                if after != before:
                                    cursor = int(buffer.cursor_position or 0)
                                    delta = len(after) - len(before)
                                    state["editing"] = True
                                    buffer.text = after
                                    buffer.cursor_position = max(0, min(len(after), cursor + delta))
                            finally:
                                state["editing"] = False

                        chat_prompt_session.default_buffer.on_text_changed += _on_text_changed

                    try:
                        composer_rprompt = lambda: str(
                            _format_live_composer_status_row(
                                draft_text=str(chat_prompt_session.default_buffer.text or ""),
                                attachments=pending_attachments,
                                provider=current_provider,
                                model=current_model,
                            )
                            or ""
                        )

                        _mouse_default = "1"
                        mouse_actions_enabled = str(os.getenv("OPENVEGAS_CHAT_MOUSE_ACTIONS", _mouse_default)).strip().lower() in {
                            "1",
                            "true",
                            "yes",
                            "on",
                        }
                        actions_menu_expanded = {"value": False}

                        def _toolbar_click(command: str | None = None, *, submit: bool = False, toggle_actions: bool = False):
                            def _handler(mouse_event) -> None:
                                try:
                                    event_type = getattr(mouse_event, "event_type", None)
                                    if MouseEventType is not None and event_type != MouseEventType.MOUSE_UP:
                                        return
                                except Exception:
                                    pass
                                if toggle_actions:
                                    actions_menu_expanded["value"] = not actions_menu_expanded["value"]
                                    try:
                                        app = getattr(chat_prompt_session, "app", None)
                                        if app is not None:
                                            app.invalidate()
                                    except Exception:
                                        pass
                                    return
                                buf = chat_prompt_session.default_buffer
                                if command is None:
                                    if submit:
                                        buf.validate_and_handle()
                                    return
                                if submit:
                                    buf.text = command
                                    buf.validate_and_handle()
                                else:
                                    if str(buf.text or "").strip():
                                        buf.insert_text(" ")
                                    buf.insert_text(command)
                            return _handler

                        prompt_style = None
                        if PromptStyle is not None:
                            prompt_style = PromptStyle.from_dict({
                                "bottom-toolbar": "bg:#202020 #d0d0d0",
                                "bottom-toolbar.voice-chip": "bg:#2b2f36 #f5f7fa bold",
                                "bottom-toolbar.voice-chip-active": "bg:#123a24 #caffdb bold",
                            })

                        def _composer_bottom_toolbar():
                            if not mouse_actions_enabled:
                                return ""
                            voice_chip = f"◖{_icon('voice')}◗"
                            if voice_button.is_recording:
                                status = voice_button.label(include_hint=False)
                                icon_token = _icon('voice')
                                status_tail = str(status).replace(icon_token, "", 1).strip()
                                if status_tail:
                                    voice_chip = f"{voice_chip} {status_tail}"
                            voice_style = "class:bottom-toolbar.voice-chip-active" if voice_button.is_recording else "class:bottom-toolbar.voice-chip"
                            row = [
                                ("class:bottom-toolbar", " "),
                                (voice_style, f" {voice_chip} ", _toolbar_click("/voice", submit=True)),
                                ("class:bottom-toolbar", "  "),
                                ("class:bottom-toolbar", f"{_icon('actions')} Actions ", _toolbar_click(toggle_actions=True)),
                            ]
                            if actions_menu_expanded["value"]:
                                row.extend([
                                    ("class:bottom-toolbar", "  "),
                                    ("class:bottom-toolbar", f"{_icon('attach')} Attach ", _toolbar_click("/attach")),
                                    ("class:bottom-toolbar", "  "),
                                    ("class:bottom-toolbar", f"{_icon('paste')} Paste ", _toolbar_click("/paste", submit=True)),
                                    ("class:bottom-toolbar", "  "),
                                    ("class:bottom-toolbar", f"{_icon('mcp')} MCP ", _toolbar_click("/mcp list", submit=True)),
                                    ("class:bottom-toolbar", "  "),
                                    ("class:bottom-toolbar", f"{_icon('quit')} Quit ", _toolbar_click("/exit", submit=True)),
                                ])
                            row.append(("class:bottom-toolbar", " "))
                            return row

                        toolbar_fn = _composer_bottom_toolbar if mouse_actions_enabled else None
                        prefill_default = str(pending_voice_prefill or "")
                        if prefill_default:
                            pending_voice_prefill = None
                            pending_voice_meta = None
                        prompt_input_active = True
                        try:
                            raw = await chat_prompt_session.prompt_async(
                                "chat: ",
                                key_bindings=chat_prompt_bindings,
                                multiline=False,
                                wrap_lines=True,
                                rprompt=composer_rprompt,
                                mouse_support=mouse_actions_enabled,
                                bottom_toolbar=toolbar_fn,
                                style=prompt_style,
                                refresh_interval=0.15,
                                default=prefill_default,
                            )
                        finally:
                            prompt_input_active = False
                    except EOFError:
                        return "/exit"
                    merged = _normalize_live_chat_input_text(raw)
                    if merged:
                        return merged
                    auto_clip = str(os.getenv("OPENVEGAS_CHAT_AUTO_CLIPBOARD_PASTE", "1")).strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    if auto_clip and _clipboard_has_image():
                        return "/paste"
                    return merged

            first = Prompt.ask("chat")
            extras = _drain_stdin_buffer(window_ms=40)
            merged = _normalize_live_chat_input_text(_merge_chat_prompt_and_buffered_lines(first, extras))
            if merged:
                return merged
            # Best-effort clipboard-image paste parity (Cmd/Ctrl+V workflows).
            auto_clip = str(os.getenv("OPENVEGAS_CHAT_AUTO_CLIPBOARD_PASTE", "1")).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if auto_clip and _clipboard_has_image():
                return "/paste"
            return merged

        try:
            mode = await client.get_mode()
            conversation_mode = str(mode.get("conversation_mode", "persistent"))
        except Exception:
            conversation_mode = "persistent"
        if current_provider == "openai":
            try:
                models_resp = await client.list_models("openai")
                enabled_models = [
                    str(m.get("model_id", "")).strip()
                    for m in models_resp.get("models", [])
                    if m.get("enabled")
                ]
                selected = _pick_preferred_model(enabled_models, current_model)
                if selected and selected != current_model:
                    current_model = selected
            except Exception:
                pass

        try:
            run_info = await client.agent_run_create(state="running", is_resumable=True)
            current_run_id = str(run_info.get("run_id", "") or "")
            current_run_version = int(run_info.get("run_version", 0))
            current_signature = str(run_info.get("valid_actions_signature", "sha256:"))
            if current_run_id:
                await client.agent_register_workspace(
                    run_id=current_run_id,
                    runtime_session_id=runtime_session_id,
                    workspace_root=workspace_root,
                    workspace_fingerprint=workspace_fp,
                    git_root=workspace_git_root,
                )
        except Exception:
            current_run_id = None
            current_run_version = 0
            current_signature = "sha256:"

        if show_model_meta:
            console.print(f"OpenVegas Chat · {current_provider}/{current_model} · {conversation_mode}")
        else:
            console.print(f"OpenVegas Chat · {conversation_mode}")
        console.print("Type /help for commands")
        if attach_search_home_enabled:
            console.print(
                "[yellow]OPENVEGAS_CHAT_ATTACH_SEARCH_HOME=1 may slow auto-attach. "
                "Recommended default is 0.[/yellow]"
            )
        render_status_bar(console, _status_actor(), "ready", workspace_root)
        _render_action_hint()
        dealer_panel.render("idle", "ready")

        while True:
            if str(fullscreen_handoff_message or "").strip():
                message = str(fullscreen_handoff_message or "").strip()
                fullscreen_handoff_message = None
                console.print(f"[dim]chat: {message}[/dim]")
            else:
                message = await _read_chat_message()
            if not message:
                continue

            if message.startswith("/"):
                parts = message.split()
                cmd = parts[0].lower()

                if cmd == "/exit":
                    voice_button.stop_if_recording()
                    console.print("[dim]Exiting chat.[/dim]")
                    return "exit"
                if cmd == "/help":
                    _show_help()
                    continue
                if cmd == "/legend":
                    _show_legend()
                    continue
                if cmd == "/status":
                    web_search_effective = bool(
                        web_search_requested
                        and resolve_capability(
                            current_provider,
                            current_model,
                            "web_search",
                        )
                    )
                    provider_line = (
                        f"[bold]Provider:[/bold] {current_provider}\n[bold]Model:[/bold] {current_model}\n"
                        if show_model_meta
                        else "[bold]Provider/Model:[/bold] hidden by policy\n"
                    )
                    console.print(
                        Panel(
                            f"{provider_line}"
                            f"[bold]Thread:[/bold] {current_thread_id or '(none)'}\n"
                            f"[bold]Run:[/bold] {current_run_id or '(none)'}\n"
                            f"[bold]Run Version:[/bold] {current_run_version}\n"
                            f"[bold]Workspace:[/bold] {workspace_root}\n"
                            f"[bold]Plan Mode:[/bold] {'on' if plan_mode else 'off'}\n"
                            f"[bold]Approval Mode:[/bold] {approval_mode}\n"
                            f"[bold]Tool Events:[/bold] {'verbose' if verbose_tool_events else 'compact'}\n"
                            f"[bold]Web Search Requested:[/bold] {web_search_requested}\n"
                            f"[bold]Web Search Effective:[/bold] {web_search_effective}\n"
                            f"[bold]Last Web Search Used:[/bold] {last_web_search_used}\n"
                            f"[bold]Last Web Search Retry:[/bold] {last_web_search_retry_without_tool}\n"
                            f"[bold]Voice STT Requested:[/bold] {voice_transcribe_requested}\n"
                            f"[bold]Voice STT Effective:[/bold] {last_voice_transcribe_effective}\n"
                            f"[bold]Last Voice STT Used:[/bold] {last_voice_transcribe_used}\n"
                            f"[bold]MCP Feature Enabled:[/bold] {_mcp_feature_enabled()}\n"
                            f"[bold]Pending Attachments:[/bold] {len(pending_attachments)}\n"
                            "[bold]Style:[/bold] minimal (fixed)",
                            title="Chat Status",
                            border_style="cyan",
                        )
                    )
                    continue
                if cmd == "/attachments":
                    if not pending_attachments:
                        console.print("[dim]No pending attachments.[/dim]")
                        continue
                    rows = []
                    for att in pending_attachments:
                        rows.append(
                            f"{_attachment_label(att)} id={att.local_id} state={att.state.value}"
                        )
                    console.print(Panel("\n".join(rows), title="Pending Attachments", border_style="blue"))
                    continue
                if cmd == "/export-transcript":
                    path_arg = message.split(" ", 1)[1] if " " in message else ""
                    _export_transcript(path_arg)
                    continue
                if cmd == "/attach":
                    if len(parts) < 2:
                        console.print("[red]Usage: /attach <path>[/red]")
                        continue
                    raw_path = message.split(" ", 1)[1] if " " in message else ""
                    if _attach_file(raw_path):
                        _render_attachment_status_row(force=True)
                    continue
                if cmd == "/paste":
                    attached = _paste_from_clipboard()
                    if attached <= 0:
                        console.print(
                            "[yellow]Clipboard did not contain attachable files/images. "
                            "Use /attach <path> or paste full file paths.[/yellow]"
                        )
                    else:
                        console.print(f"[green]Attached {attached} item(s) from clipboard.[/green]")
                        _render_attachment_status_row(force=True)
                    continue
                if cmd == "/detach":
                    if len(parts) < 2:
                        console.print("[red]Usage: /detach <name|id>[/red]")
                        continue
                    token = message.split(" ", 1)[1] if " " in message else ""
                    att = _find_attachment(token)
                    if not att:
                        console.print(f"[yellow]No attachment found for '{token.strip()}'.[/yellow]")
                        continue
                    pending_attachments.remove(att)
                    _emit_attachment_event(
                        "attachment_removed",
                        {"id": att.local_id, "marker": _attachment_marker(att.name)},
                    )
                    _render_attachment_status_row(force=True)
                    continue
                if cmd == "/clear-attachments":
                    if not pending_attachments:
                        console.print("[dim]No pending attachments.[/dim]")
                        continue
                    for att in list(pending_attachments):
                        _emit_attachment_event(
                            "attachment_removed",
                            {"id": att.local_id, "marker": _attachment_marker(att.name)},
                        )
                    pending_attachments.clear()
                    console.print("[green]Cleared all pending attachments.[/green]")
                    continue
                if cmd == "/cancel-uploads":
                    if not pending_attachments:
                        console.print("[dim]No pending attachments.[/dim]")
                        continue
                    pending_attachments.clear()
                    console.print("[green]Cancelled and cleared pending upload queue.[/green]")
                    continue
                if cmd == "/retry-failed":
                    failed_count = 0
                    for att in pending_attachments:
                        if att.state == AttachmentState.FAILED:
                            att.state = AttachmentState.ATTACHED
                            att.error = None
                            failed_count += 1
                    if failed_count <= 0:
                        console.print("[dim]No failed uploads to retry.[/dim]")
                    else:
                        console.print(f"[green]Reset {failed_count} failed attachment(s) for retry.[/green]")
                    continue
                if cmd == "/web":
                    web_search_effective = bool(
                        resolve_capability(
                            current_provider,
                            current_model,
                            "web_search",
                        )
                    )
                    console.print(
                        "[dim]Web search is always on in chat. "
                        f"effective={web_search_effective}[/dim]"
                    )
                    continue
                if cmd == "/voice":
                    voice_effective = bool(
                        resolve_capability(
                            current_provider,
                            current_model,
                            "speech_to_text",
                        )
                    )
                    if not voice_effective:
                        console.print("[yellow]Voice capture unavailable for current provider/model.[/yellow]")
                        continue
                    if chat_prompt_session is None:
                        console.print("[yellow]Voice capture requires prompt-toolkit chat mode.[/yellow]")
                        continue
                    was_listening = str(getattr(voice_button.state, "value", "")) == "listening"
                    insert_observed = {"mode": "none", "chars": 0}

                    def _insert_for_slash(transcript: str) -> None:
                        mode, chars = _insert_voice_transcript_text(transcript)
                        insert_observed["mode"] = mode
                        insert_observed["chars"] = chars

                    await voice_button.toggle(
                        insert_text=_insert_for_slash,
                        transcribe_wav=_transcribe_voice_wav,
                    )
                    if not was_listening and str(getattr(voice_button.state, "value", "")) == "listening":
                        console.print("[dim]Listening... click Voice again to stop.[/dim]")
                    elif was_listening:
                        if int(insert_observed.get("chars") or 0) > 0:
                            console.print(
                                f"[green]Voice captured: transcript ready in input ({int(insert_observed.get('chars') or 0)} chars).[/green]"
                            )
                        elif not voice_button.last_error:
                            console.print("[yellow]Voice error: empty transcript returned.[/yellow]")
                    continue
                if cmd == "/mcp":
                    if len(parts) < 2:
                        console.print("[red]Usage: /mcp <list|health|call> ...[/red]")
                        console.print("[dim]Examples: /mcp list | /mcp health <server_id> | /mcp call <server_id> <tool> {\"k\":\"v\"}[/dim]")
                        continue
                    sub = parts[1].strip().lower()
                    if sub == "list":
                        _render_capability_status("mcp", "listing servers...")
                        try:
                            payload = await client.mcp_list_servers()
                        except APIError as exc:
                            console.print(f"[red]mcp list failed: {exc.detail}[/red]")
                            continue
                        servers = payload.get("servers") if isinstance(payload, dict) else []
                        if not isinstance(servers, list) or not servers:
                            console.print("[dim]No MCP servers registered.[/dim]")
                            continue
                        catalog_timeout = max(2, min(30, int(os.getenv("OPENVEGAS_MCP_CATALOG_TIMEOUT_SEC", "6"))))
                        table = Table(title="MCP Servers")
                        table.add_column("ID")
                        table.add_column("Name")
                        table.add_column("Transport")
                        table.add_column("Health")
                        table.add_column("Tools", justify="right")
                        table.add_column("Target")
                        for row in servers[:20]:
                            if not isinstance(row, dict):
                                continue
                            server_id = str(row.get("id") or "").strip()
                            health_label = "unknown"
                            tool_count = "-"
                            if server_id:
                                try:
                                    health = await client.mcp_server_health(server_id=server_id)
                                    health_label = str((health or {}).get("status") or "unknown")
                                except Exception:
                                    health_label = "error"
                                try:
                                    tools_payload = await client.mcp_list_tools(server_id=server_id, timeout_sec=catalog_timeout)
                                    tools = tools_payload.get("tools") if isinstance(tools_payload, dict) else []
                                    if isinstance(tools, list):
                                        tool_count = str(len(tools))
                                except Exception:
                                    tool_count = "?"
                            table.add_row(
                                server_id[:12],
                                str(row.get("name") or ""),
                                str(row.get("transport") or ""),
                                health_label,
                                tool_count,
                                str(row.get("target") or "")[:64],
                            )
                        console.print(table)
                        continue
                    if sub == "health":
                        if len(parts) < 3:
                            console.print("[red]Usage: /mcp health <server_id>[/red]")
                            continue
                        server_id = str(parts[2] or "").strip()
                        _render_capability_status("mcp", f"health {server_id}...")
                        try:
                            health = await client.mcp_server_health(server_id=server_id)
                        except APIError as exc:
                            console.print(f"[red]mcp health failed: {exc.detail}[/red]")
                            continue
                        status = str((health or {}).get("status") or "unknown")
                        detail = str((health or {}).get("detail") or "")
                        style = "green" if status == "ok" else "yellow"
                        console.print(f"[{style}]MCP {server_id}: {status}[/{style}] [dim]{detail}[/dim]")
                        continue
                    if sub == "call":
                        server_id, tool_name, tool_args, parse_err = _parse_mcp_call_command(message)
                        if parse_err:
                            console.print(f"[red]{parse_err}[/red]")
                            continue
                        event_label = f"mcp {server_id}::{tool_name}"
                        render_tool_event(console, event_label, "start")
                        _render_capability_status("mcp", f"tool_start {tool_name}")
                        dealer_panel.render(
                            map_lifecycle_event_to_state("tool_start", tool_name="mcp_call", status="running"),
                            event_label,
                        )
                        try:
                            result_payload = await client.mcp_call_tool(
                                server_id=server_id,
                                tool=tool_name,
                                arguments=tool_args,
                                timeout_sec=max(1, min(120, int(os.getenv("OPENVEGAS_MCP_CALL_TIMEOUT_SEC", "20")))),
                            )
                        except APIError as exc:
                            dealer_panel.render(
                                map_lifecycle_event_to_state("tool_result", tool_name="mcp_call", status="failed"),
                                "mcp call failed",
                            )
                            _render_capability_status("mcp", f"tool_result {tool_name} failed")
                            render_tool_result(console, event_label, "failed")
                            console.print(f"[red]mcp call failed: {exc.detail}[/red]")
                            continue
                        dealer_panel.render(
                            map_lifecycle_event_to_state("tool_result", tool_name="mcp_call", status="succeeded"),
                            "mcp call succeeded",
                        )
                        _render_capability_status("mcp", f"tool_result {tool_name} succeeded")
                        render_tool_result(console, event_label, "succeeded")
                        if isinstance(result_payload, dict):
                            lifecycle = result_payload.get("events")
                            if isinstance(lifecycle, list):
                                for evt in lifecycle[:20]:
                                    if not isinstance(evt, dict):
                                        continue
                                    etype = str(evt.get("type") or "event")
                                    detail = str(evt.get("detail") or "").strip()
                                    _render_capability_status("mcp", f"{etype} {detail}".strip())
                        result_value = result_payload.get("result") if isinstance(result_payload, dict) else result_payload
                        pretty = json.dumps(result_value, indent=2, ensure_ascii=False, sort_keys=True)
                        console.print(
                            Panel(
                                pretty[:8000] if pretty else "{}",
                                title=f"MCP Result · {server_id} · {tool_name}",
                                border_style="cyan",
                            )
                        )
                        continue
                    console.print("[red]Usage: /mcp <list|health|call> ...[/red]")
                    continue
                if cmd == "/tooling":
                    console.print(
                        Panel(
                            f"[bold]Runtime Session:[/bold] {runtime_session_id}\n"
                            f"[bold]Workspace Root:[/bold] {workspace_root}\n"
                            f"[bold]Workspace Fingerprint:[/bold] {workspace_fp}\n"
                            f"[bold]Plan Mode:[/bold] {'on' if plan_mode else 'off'}\n"
                            f"[bold]Approval Mode:[/bold] {approval_mode}\n"
                            f"[bold]Tool Events:[/bold] {'verbose' if verbose_tool_events else 'compact'}",
                            title="Tool Runtime",
                            border_style="magenta",
                        )
                    )
                    continue
                if cmd == "/style":
                    console.print("[yellow]/style is deprecated. Minimal UI style is always on.[/yellow]")
                    continue
                if cmd == "/verbose-tools":
                    if len(parts) < 2:
                        console.print("[red]Usage: /verbose-tools <on|off>[/red]")
                        continue
                    val = parts[1].strip().lower()
                    if val not in {"on", "off"}:
                        console.print("[red]Value must be on or off.[/red]")
                        continue
                    verbose_tool_events = val == "on"
                    console.print(
                        f"[green]Tool event verbosity set to {'verbose' if verbose_tool_events else 'compact'}.[/green]"
                    )
                    continue
                if cmd == "/approvals":
                    console.print(
                        Panel(
                            f"[bold]Approval Mode:[/bold] {approval_mode}\n"
                            f"{approval_rules_summary(session_approval)}",
                            title="Session Approvals",
                            border_style="yellow",
                        )
                    )
                    continue
                if cmd == "/plan":
                    if len(parts) >= 2:
                        plan_mode = parts[1].lower() in {"on", "1", "true", "yes"}
                    else:
                        plan_mode = not plan_mode
                    console.print(
                        f"[yellow]Plan mode {'enabled' if plan_mode else 'disabled'}.[/yellow] "
                        "[dim](Mutating local tools are blocked while plan mode is enabled.)[/dim]"
                    )
                    continue
                if cmd == "/approve":
                    if len(parts) < 2:
                        console.print("[red]Usage: /approve <ask|allow|exclude>[/red]")
                        continue
                    mode = parts[1].strip().lower()
                    if mode not in {"ask", "allow", "exclude"}:
                        console.print("[red]Approval mode must be ask, allow, or exclude.[/red]")
                        continue
                    approval_mode = mode
                    console.print(f"[green]Approval mode set to {approval_mode}.[/green]")
                    continue
                if cmd == "/provider":
                    if not allow_model_switch:
                        console.print("[yellow]Provider switching is disabled in this environment.[/yellow]")
                        continue
                    if len(parts) < 2:
                        console.print("[red]Usage: /provider <openai|anthropic|gemini> [model][/red]")
                        continue
                    next_provider = parts[1].strip().lower()
                    if next_provider not in {"openai", "anthropic", "gemini"}:
                        console.print("[red]Provider must be openai, anthropic, or gemini.[/red]")
                        continue
                    next_model = parts[2].strip() if len(parts) >= 3 else get_default_model(next_provider)
                    if next_provider != current_provider and current_thread_id:
                        if not Confirm.ask(
                            "Switching provider resets context thread. Continue?",
                            default=False,
                        ):
                            continue
                        current_thread_id = None
                    current_provider = next_provider
                    current_model = next_model
                    web_search_requested = (
                        current_provider == "openai"
                        and str(os.getenv("OPENVEGAS_CHAT_WEB_SEARCH_DEFAULT", "1")).strip().lower()
                        in {"1", "true", "yes", "on"}
                    )
                    console.print(f"[green]Provider/model set to {current_provider}/{current_model}.[/green]")
                    continue
                if cmd == "/model":
                    if not allow_model_switch:
                        console.print("[yellow]Model switching is disabled in this environment.[/yellow]")
                        continue
                    if len(parts) < 2:
                        console.print("[red]Usage: /model <model_id>[/red]")
                        continue
                    current_model = parts[1].strip()
                    console.print(f"[green]Model set to {current_model}.[/green]")
                    continue
                if cmd == "/ui":
                    if current_run_id:
                        try:
                            handoff_transition = await client.agent_run_transition(
                                run_id=current_run_id,
                                action="handoff",
                                expected_run_version=current_run_version,
                                expected_valid_actions_signature=current_signature,
                                idempotency_key=f"chat-handoff-{uuid.uuid4()}",
                                payload={},
                            )
                            current_run_version = int(handoff_transition.get("run_version", current_run_version))
                            current_signature = str(
                                handoff_transition.get("valid_actions_signature", current_signature)
                            )
                            handoff = await client.agent_run_handoff_check(run_id=current_run_id)
                            if handoff.get("error") == "handoff_blocked":
                                console.print(
                                    f"[red]UI handoff blocked:[/red] {handoff.get('handoff_block_reason', 'unknown')}"
                                )
                                continue
                        except APIError as e:
                            body = e.data if isinstance(e.data, dict) else {}
                            if body.get("error") == "stale_projection":
                                current_run_version = int(body.get("run_version", current_run_version))
                                current_signature = str(
                                    body.get("valid_actions_signature", current_signature)
                                )
                                console.print("[yellow]Run state refreshed. Retry /ui.[/yellow]")
                                continue
                            if body.get("error") == "handoff_blocked":
                                console.print(
                                    f"[red]UI handoff blocked:[/red] {body.get('handoff_block_reason', 'unknown')}"
                                )
                                continue
                    voice_button.stop_if_recording()
                    return "ui"

                console.print("[red]Unknown slash command. Use /help.[/red]")
                continue

            turn_is_workspace_intent = _has_workspace_tooling_intent(message)
            auto_paths: list[str] = []
            unresolved_inline: list[str] = []
            auto_resolve_timed_out = False
            if not turn_is_workspace_intent:
                if not pending_attachments:
                    if _extract_filename_like_tokens(message):
                        _render_capability_status("file_read", "parsing files...")
                    elif _message_requests_attachment_analysis(message):
                        _render_capability_status("file_read", "parsing attachments...")
                auto_paths, unresolved_inline, auto_resolve_timed_out = await _detect_auto_attach_paths_with_deadline(
                    message,
                    workspace_root=workspace_root,
                    max_candidates=max_attachments_per_turn,
                    deadline_ms=auto_attach_deadline_ms,
                )
                for candidate in auto_paths:
                    if len(pending_attachments) >= max_attachments_per_turn:
                        break
                    _attach_file(candidate)
                # Continue-style UX parity: use clipboard contents automatically when message implies file/image analysis.
                if (
                    not pending_attachments
                    and _message_requests_attachment_analysis(message)
                    and len(pending_attachments) < max_attachments_per_turn
                ):
                    clip_text = _read_clipboard_text()
                    if clip_text:
                        for token in _extract_pasted_path_candidates(clip_text):
                            if len(pending_attachments) >= max_attachments_per_turn:
                                break
                            resolved = _resolve_attachment_token_path(token, workspace_root=workspace_root)
                            if resolved:
                                _attach_file(resolved)
                    if (
                        not pending_attachments
                        and auto_clipboard_image_attach
                        and _clipboard_has_image()
                    ):
                        img_path = _save_clipboard_image_to_file()
                        if img_path:
                            _attach_file(img_path)
            if not pending_attachments and not turn_is_workspace_intent:
                if auto_resolve_timed_out:
                    markers = " ".join(f"{{{_normalize_space_chars(name)}}}" for name in unresolved_inline[:5])
                    console.print(
                        "[yellow]Auto-attach search timed out; skipping this turn"
                        f" (>{auto_attach_deadline_ms}ms).[/yellow] "
                        + (
                            f"{markers} [yellow]Use /attach <path> or paste the full file path.[/yellow]"
                            if markers
                            else "[yellow]Use /attach <path> or paste the full file path.[/yellow]"
                        )
                    )
                elif unresolved_inline:
                    markers = " ".join(f"{{{_normalize_space_chars(name)}}}" for name in unresolved_inline)
                    console.print(
                        "[yellow]Detected file names but could not auto-attach:[/yellow] "
                        f"{markers} [yellow]Use /attach <path> or paste the full file path.[/yellow]"
                    )
                else:
                    inline_mentions = _extract_inline_file_mentions(message, workspace_root=workspace_root)
                    if inline_mentions:
                        markers = " ".join(f"{{{name}}}" for name in inline_mentions)
                        console.print(
                            "[yellow]Detected file names in your prompt but they are not attached:[/yellow] "
                            f"{markers} [yellow]Use /attach <path> before sending.[/yellow]"
                        )

            (
                capability_filtered_pending,
                dropped_image_count,
                blocked_all_images,
            ) = _preflight_filter_attachments_for_capabilities(
                pending_attachments,
                provider=current_provider,
                model=current_model,
            )
            if dropped_image_count > 0:
                if blocked_all_images:
                    console.print(
                        "[yellow]"
                        f"{current_provider}/{current_model} does not support image input. "
                        "Remove image attachments or switch to a vision-capable model."
                        "[/yellow]"
                    )
                    emit_metric(
                        "chat_attachment_blocked_capability_total",
                        {
                            "feature": "image_input",
                            "provider": current_provider,
                            "model": current_model,
                            "had_uploaded": False,
                        },
                    )
                    render_status_bar(console, _status_actor(), "image input unavailable", workspace_root)
                    pending_attachments.clear()
                    continue
                pending_attachments[:] = capability_filtered_pending
                console.print(
                    "[yellow]"
                    f"Dropped {dropped_image_count} image attachment(s) — "
                    f"{current_provider}/{current_model} does not support image input. "
                    f"Continuing with {len(capability_filtered_pending)} non-image file(s)."
                    "[/yellow]"
                )
                emit_metric(
                    "chat_attachment_dropped_capability_total",
                    {
                        "feature": "image_input",
                        "provider": current_provider,
                        "model": current_model,
                        "dropped": dropped_image_count,
                        "kept": len(capability_filtered_pending),
                    },
                )

            if pending_attachments:
                _render_attachment_status_row(force=True)
                _render_upload_queue_preview()

            (
                _uploaded_for_turn,
                attachment_markers_for_turn,
                attachment_context_for_turn,
                attachment_file_ids_for_turn,
            ) = await _prepare_attachments_for_turn()
            voice_transcript_context_for_turn = ""
            last_voice_transcribe_used = False
            last_voice_transcribe_effective = bool(
                voice_transcribe_requested
                and resolve_capability(
                    current_provider,
                    current_model,
                    "speech_to_text",
                )
            )
            if _uploaded_for_turn:
                (
                    voice_transcript_context_for_turn,
                    transcript_count_for_turn,
                    stt_effective_for_turn,
                ) = await _transcribe_audio_attachments_for_turn(_uploaded_for_turn)
                last_voice_transcribe_effective = bool(stt_effective_for_turn)
                last_voice_transcribe_used = transcript_count_for_turn > 0
            display_message = _inject_attachment_markers_into_message(message, _uploaded_for_turn)
            if attachment_markers_for_turn and display_message == message:
                display_message = f"{message} {' '.join(attachment_markers_for_turn)}".strip()
            if show_user_echo:
                _erase_prompt_line_if_possible()
                render_user_input(console, display_message)
            chat_transcript.append(
                {
                    "role": "user",
                    "text": display_message,
                    "attachments": list(attachment_markers_for_turn),
                    "ts": time.time(),
                }
            )
            last_assistant_text_for_turn = ""
            failed_for_turn = [
                att for att in pending_attachments if att.state in {AttachmentState.FAILED, AttachmentState.UNSUPPORTED}
            ]
            mentioned_files = _extract_filename_like_tokens(message)
            if (
                _message_requests_attachment_analysis(message)
                and mentioned_files
                and not attachment_file_ids_for_turn
            ):
                if failed_for_turn:
                    for att in failed_for_turn:
                        reason = str(att.error or "upload failed").strip()
                        console.print(
                            f"[yellow]Attachment upload failed for {_attachment_marker(att.name)}: {reason}[/yellow]"
                        )
                    console.print(
                        "[yellow]Skipped model request for this turn to avoid extra cost. "
                        "Resolve upload issue and retry (use /retry-failed if needed).[/yellow]"
                    )
                else:
                    markers = " ".join(_attachment_marker(name) for name in mentioned_files[:5])
                    console.print(
                        "[yellow]This looks like local file/image analysis but no attachment uploaded:[/yellow] "
                        f"{markers}"
                    )
                render_status_bar(console, _status_actor(), "attachment upload required", workspace_root)
                attachment_markers_for_turn = []
                attachment_context_for_turn = ""
                voice_transcript_context_for_turn = ""
                attachment_file_ids_for_turn = []
                continue
            has_image_attachment = any(
                str(att.mime_type or "").lower().startswith("image/")
                for att in _uploaded_for_turn
            )
            if has_image_attachment and not resolve_capability(current_provider, current_model, "image_input"):
                emit_metric(
                    "chat_attachment_blocked_capability_total",
                    {
                        "feature": "image_input",
                        "provider": current_provider,
                        "model": current_model,
                        "had_uploaded": True,
                        "bypass": "preflight_missed",
                    },
                )
                emit_metric(
                    "chat_attachment_preflight_bypass_total",
                    {
                        "feature": "image_input",
                        "provider": current_provider,
                        "model": current_model,
                    },
                )
                _tool_debug(
                    "unexpected capability preflight bypass: uploaded image attachment reached post-upload guard"
                )
                non_image_uploaded = [att for att in _uploaded_for_turn if not _attachment_is_image(att)]
                if non_image_uploaded:
                    dropped_after_upload = len(_uploaded_for_turn) - len(non_image_uploaded)
                    _uploaded_for_turn = non_image_uploaded
                    attachment_markers_for_turn = [_attachment_marker(att.name) for att in _uploaded_for_turn]
                    attachment_file_ids_for_turn = [
                        str(att.remote_file_id).strip()
                        for att in _uploaded_for_turn
                        if str(att.remote_file_id or "").strip()
                    ]
                    console.print(
                        "[yellow]"
                        f"Dropped {dropped_after_upload} uploaded image attachment(s) — "
                        "model does not support image input."
                        "[/yellow]"
                    )
                else:
                    console.print(
                        "[yellow]Image input is not supported for this provider/model. "
                        "Switch model/provider or remove image attachments.[/yellow]"
                    )
                    render_status_bar(console, _status_actor(), "image input unavailable", workspace_root)
                    continue
            try:
                rendered = await _run_tool_loop(client, message)
                if not rendered:
                    console.print("[dim](no final assistant response)[/dim]")
                elif str(last_assistant_text_for_turn or "").strip():
                    chat_transcript.append(
                        {
                            "role": "assistant",
                            "text": str(last_assistant_text_for_turn),
                            "ts": time.time(),
                        }
                    )
                await _maybe_render_low_balance_hint(force=False)
                pending_attachments.clear()
                attachment_markers_for_turn = []
                attachment_context_for_turn = ""
                voice_transcript_context_for_turn = ""
                attachment_file_ids_for_turn = []

            except APIError as e:
                attachment_markers_for_turn = []
                attachment_context_for_turn = ""
                voice_transcript_context_for_turn = ""
                attachment_file_ids_for_turn = []
                body = e.data if isinstance(e.data, dict) else {}
                code = str(body.get("error", ""))
                if code in {"insufficient_balance", "balance_insufficient"}:
                    await _maybe_render_low_balance_hint(force=True)
                if code == "model_disabled":
                    enabled_models: list[str] = []
                    try:
                        models_resp = await client.list_models(current_provider)
                        for m in models_resp.get("models", []):
                            if m.get("enabled"):
                                enabled_models.append(str(m.get("model_id", "")))
                    except Exception:
                        enabled_models = []
                    suggestions = [s for s in enabled_models if s][:3]
                    if not allow_model_switch and suggestions:
                        previous_model = current_model
                        current_model = _pick_preferred_model(enabled_models, current_model)
                        console.print(
                            "[yellow]Model unavailable; auto-switched "
                            f"{previous_model} -> {current_model}.[/yellow]"
                        )
                        continue
                    console.print(f"[red]{e.detail}[/red]")
                    if suggestions and allow_model_switch:
                        console.print(
                            "[yellow]Try:[/yellow] "
                            + " ".join(f"`/model {m}`" for m in suggestions)
                        )
                    continue

                if code:
                    console.print(f"[red]{code}: {body.get('detail', e.detail)}[/red]")
                else:
                    try:
                        parsed = json.loads(str(e.detail))
                        if isinstance(parsed, dict) and parsed.get("error"):
                            console.print(f"[red]{parsed.get('error')}: {parsed.get('detail', '')}[/red]")
                        else:
                            console.print(f"[red]{e.detail}[/red]")
                    except Exception:
                        console.print(f"[red]{e.detail}[/red]")

    use_fullscreen_chat = str(os.getenv("OPENVEGAS_CHAT_FULLSCREEN", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    } and bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(getattr(sys.stdout, "isatty", lambda: False)())
    if use_fullscreen_chat:
        try:
            from openvegas.tui.chat_fullscreen import run_chat_fullscreen

            async def _fullscreen_auto_attach(message: str) -> tuple[list[str], list[str], bool]:
                return await _detect_auto_attach_paths_with_deadline(
                    message,
                    workspace_root=workspace_root,
                    max_candidates=max_attachments_per_turn,
                    deadline_ms=auto_attach_deadline_ms,
                )

            def _fullscreen_path_resolver(token: str) -> str | None:
                return _resolve_attachment_token_path(token, workspace_root=workspace_root)

            def _fullscreen_web_gate(message: str, has_uploaded_attachments: bool) -> bool:
                return _should_enable_web_search_for_turn(
                    message,
                    has_uploaded_attachments=has_uploaded_attachments,
                )

            fullscreen_outcome = run_chat_fullscreen(
                provider=current_provider,
                model=current_model,
                workspace_root=workspace_root,
                web_search_requested=web_search_requested,
                voice_transcribe_requested=voice_transcribe_requested,
                mcp_enabled=_mcp_feature_enabled(),
                max_attachments_per_turn=max_attachments_per_turn,
                max_attachment_bytes=max_attachment_bytes,
                auto_attach_resolver=_fullscreen_auto_attach,
                path_resolver=_fullscreen_path_resolver,
                clipboard_text_reader=_read_clipboard_text,
                clipboard_has_image=_clipboard_has_image,
                clipboard_save_image=_save_clipboard_image_to_file,
                pasted_path_extractor=_extract_pasted_path_candidates,
                web_search_gate=_fullscreen_web_gate,
                parse_mcp_call=_parse_mcp_call_command,
                workspace_intent_detector=_has_workspace_tooling_intent,
            )
            raw = str(fullscreen_outcome or "").strip()
            handoff_message = ""
            if raw:
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                if isinstance(payload, dict) and str(payload.get("action") or "") == "handoff_legacy":
                    handoff_message = str(payload.get("message") or "").strip()
            if handoff_message:
                fullscreen_handoff_message = handoff_message
            else:
                return
        except Exception as exc:
            console.print(
                "[yellow]Fullscreen chat unavailable; falling back to legacy mode."
                f" ({type(exc).__name__})[/yellow]"
            )

    while True:
        outcome = run_async(_run_chat())
        if outcome == "ui":
            from openvegas.tui.prompt_ui import run_prompt_ui

            run_prompt_ui(no_render=False, render_timeout_sec=15.0)
            continue
        break


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--provider", default=None, help="Filter by provider")
def models(provider: str | None):
    """List available models and $V prices."""
    async def _models():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.list_models(provider)
            models_list = data.get("models", [])

            table = Table(title="Available Models")
            table.add_column("Provider")
            table.add_column("Model")
            table.add_column("Name")
            table.add_column("Input $/1M", justify="right")
            table.add_column("Output $/1M", justify="right")
            table.add_column("$V In/1M", justify="right")
            table.add_column("$V Out/1M", justify="right")
            table.add_column("Status")

            for m in models_list:
                status = "[green]enabled[/green]" if m.get("enabled") else "[red]disabled[/red]"
                table.add_row(
                    m.get("provider", ""),
                    m.get("model_id", ""),
                    m.get("display_name", ""),
                    str(m.get("cost_input_per_1m", "")),
                    str(m.get("cost_output_per_1m", "")),
                    str(m.get("v_price_input_per_1m", "")),
                    str(m.get("v_price_output_per_1m", "")),
                    status,
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_models())


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


@cli.group()
def ops():
    """Inspect runtime diagnostics, alerts, and rollback checklist."""
    pass


def _fmt_num(value: Any, *, pct: bool = False) -> str:
    try:
        number = float(value)
    except Exception:
        return str(value)
    if pct:
        return f"{number * 100.0:.2f}%"
    if abs(number) >= 1000.0:
        return f"{number:,.2f}"
    return f"{number:.3f}"


def _render_ops_recent_runs_table(runs: list[dict[str, Any]]) -> None:
    table = Table(title="Recent Runs")
    table.add_column("Run ID")
    table.add_column("Provider/Model")
    table.add_column("Latency ms", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost USD", justify="right")
    table.add_column("Tool Fail", justify="right")
    table.add_column("Fallbacks", justify="right")
    for row in runs:
        run_id = str(row.get("run_id", ""))
        provider = str(row.get("provider", ""))
        model = str(row.get("model", ""))
        latency = _fmt_num(row.get("turn_latency_ms", 0.0))
        tokens = int(row.get("input_tokens", 0) or 0) + int(row.get("output_tokens", 0) or 0)
        cost = _fmt_num(row.get("cost_usd", 0.0))
        table.add_row(
            run_id[:12],
            f"{provider}/{model}".strip("/"),
            latency,
            str(tokens),
            f"${cost}",
            str(int(row.get("tool_failures", 0) or 0)),
            str(int(row.get("fallbacks", 0) or 0)),
        )
    console.print(table)


@ops.command("diagnostics")
@click.option("--json-output", is_flag=True, help="Print raw JSON payload.")
def ops_diagnostics(json_output: bool):
    """Show diagnostics summary, thresholds, alerts, and rollback owner."""

    async def _diagnostics():
        from openvegas.client import APIError, OpenVegasClient

        try:
            client = OpenVegasClient()
            payload = await client.get_ops_diagnostics()
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")
            return

        if json_output:
            console.print_json(json.dumps(payload))
            return

        run_summary = payload.get("run_summary", {}) if isinstance(payload, dict) else {}
        thresholds = payload.get("thresholds", {}) if isinstance(payload, dict) else {}
        alerts = payload.get("alerts", []) if isinstance(payload, dict) else []
        rollback = payload.get("rollback", {}) if isinstance(payload, dict) else {}
        recent_runs = payload.get("recent_runs", []) if isinstance(payload, dict) else []

        summary_table = Table(title="Ops Run Summary")
        summary_table.add_column("Metric")
        summary_table.add_column("Value", justify="right")
        summary_table.add_row("run_count", str(run_summary.get("run_count", 0)))
        summary_table.add_row("turn_latency_ms_p50", _fmt_num(run_summary.get("turn_latency_ms_p50", 0.0)))
        summary_table.add_row("turn_latency_ms_p95", _fmt_num(run_summary.get("turn_latency_ms_p95", 0.0)))
        summary_table.add_row("turn_latency_ms_avg", _fmt_num(run_summary.get("turn_latency_ms_avg", 0.0)))
        summary_table.add_row("tool_fail_rate", _fmt_num(run_summary.get("tool_fail_rate", 0.0), pct=True))
        summary_table.add_row("fallback_rate", _fmt_num(run_summary.get("fallback_rate", 0.0), pct=True))
        summary_table.add_row("avg_cost_usd", f"${_fmt_num(run_summary.get('avg_cost_usd', 0.0))}")
        console.print(summary_table)

        threshold_table = Table(title="Alert Thresholds")
        threshold_table.add_column("Metric")
        threshold_table.add_column("Threshold", justify="right")
        for metric, threshold in thresholds.items():
            threshold_table.add_row(str(metric), _fmt_num(threshold))
        console.print(threshold_table)

        if alerts:
            alert_table = Table(title="Active Alerts")
            alert_table.add_column("Metric")
            alert_table.add_column("Severity")
            alert_table.add_column("Observed", justify="right")
            alert_table.add_column("Threshold", justify="right")
            alert_table.add_column("Status")
            for item in alerts:
                observed = _fmt_num(item.get("observed", 0.0))
                threshold = _fmt_num(item.get("threshold", 0.0))
                alert_table.add_row(
                    str(item.get("metric", "")),
                    str(item.get("severity", "")),
                    observed,
                    threshold,
                    str(item.get("status", "")),
                )
            console.print(alert_table)
        else:
            console.print("[green]No active alerts.[/green]")

        if isinstance(recent_runs, list) and recent_runs:
            _render_ops_recent_runs_table([r for r in recent_runs if isinstance(r, dict)])

        owner = str(rollback.get("owner", "")).strip() or "unassigned"
        checklist = rollback.get("checklist", [])
        checklist_lines = "\n".join(f"{idx}. {item}" for idx, item in enumerate(checklist, start=1))
        if not checklist_lines:
            checklist_lines = "1. No rollback checklist configured."
        console.print(Panel(checklist_lines, title=f"Rollback Owner: {owner}", border_style="yellow"))

    run_async(_diagnostics())


@ops.command("alerts")
@click.option("--json-output", is_flag=True, help="Print raw JSON payload.")
def ops_alerts(json_output: bool):
    """Show current alerts and threshold comparisons."""

    async def _alerts():
        from openvegas.client import APIError, OpenVegasClient

        try:
            client = OpenVegasClient()
            payload = await client.get_ops_alerts()
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")
            return

        if json_output:
            console.print_json(json.dumps(payload))
            return

        alerts = payload.get("alerts", []) if isinstance(payload, dict) else []
        if not alerts:
            console.print("[green]No active alerts.[/green]")
            return
        table = Table(title="Ops Alerts")
        table.add_column("Metric")
        table.add_column("Severity")
        table.add_column("Observed", justify="right")
        table.add_column("Threshold", justify="right")
        table.add_column("Status")
        for item in alerts:
            table.add_row(
                str(item.get("metric", "")),
                str(item.get("severity", "")),
                _fmt_num(item.get("observed", 0.0)),
                _fmt_num(item.get("threshold", 0.0)),
                str(item.get("status", "")),
            )
        console.print(table)

    run_async(_alerts())


@ops.command("runs")
@click.option("--limit", default=25, show_default=True, help="Number of recent runs to fetch (max 200).")
@click.option("--json-output", is_flag=True, help="Print raw JSON payload.")
def ops_runs(limit: int, json_output: bool):
    """Show recent inference run metrics."""

    async def _runs():
        from openvegas.client import APIError, OpenVegasClient

        try:
            client = OpenVegasClient()
            payload = await client.get_ops_runs(limit=limit)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")
            return

        if json_output:
            console.print_json(json.dumps(payload))
            return
        rows = payload.get("runs", []) if isinstance(payload, dict) else []
        if not rows:
            console.print("[dim]No run metrics available.[/dim]")
            return
        _render_ops_recent_runs_table([r for r in rows if isinstance(r, dict)])

    run_async(_runs())


@ops.command("watch")
@click.option("--interval-sec", default=5.0, show_default=True, help="Refresh interval in seconds.")
@click.option("--cycles", default=0, show_default=True, help="Number of refresh cycles (0 = until Ctrl+C).")
def ops_watch(interval_sec: float, cycles: int):
    """Watch alerts + summary in a polling loop."""

    async def _watch():
        from openvegas.client import APIError, OpenVegasClient

        client = OpenVegasClient()
        tick = 0
        max_cycles = max(0, int(cycles))
        interval = max(1.0, float(interval_sec))
        while True:
            tick += 1
            try:
                diag = await client.get_ops_diagnostics()
            except APIError as e:
                console.print(f"[red]{e.detail}[/red]")
                return

            summary = diag.get("run_summary", {}) if isinstance(diag, dict) else {}
            alerts = diag.get("alerts", []) if isinstance(diag, dict) else []
            latency = _fmt_num(summary.get("turn_latency_ms_p95", 0.0))
            fallback = _fmt_num(summary.get("fallback_rate", 0.0), pct=True)
            tool_fail = _fmt_num(summary.get("tool_fail_rate", 0.0), pct=True)
            console.print(
                f"[dim]tick={tick} p95_ms={latency} tool_fail={tool_fail} fallback={fallback} "
                f"alerts={len(alerts) if isinstance(alerts, list) else 0}[/dim]"
            )
            if isinstance(alerts, list) and alerts:
                for item in alerts:
                    metric = str(item.get("metric", ""))
                    observed = _fmt_num(item.get("observed", 0.0))
                    threshold = _fmt_num(item.get("threshold", 0.0))
                    severity = str(item.get("severity", "warning"))
                    console.print(
                        f"[yellow]{severity}[/yellow] {metric}: observed={observed} threshold={threshold}"
                    )

            if max_cycles > 0 and tick >= max_cycles:
                return
            await asyncio.sleep(interval)

    try:
        run_async(_watch())
    except KeyboardInterrupt:
        console.print("[dim]Stopped ops watch.[/dim]")


@ops.command("rollback")
@click.option("--json-output", is_flag=True, help="Print raw JSON payload.")
def ops_rollback(json_output: bool):
    """Show rollback owner and checklist."""

    async def _rollback():
        from openvegas.client import APIError, OpenVegasClient

        try:
            client = OpenVegasClient()
            payload = await client.get_ops_alerts()
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")
            return

        rollback = payload.get("rollback", {}) if isinstance(payload, dict) else {}
        if json_output:
            console.print_json(json.dumps(rollback))
            return
        owner = str(rollback.get("owner", "")).strip() or "unassigned"
        checklist = rollback.get("checklist", [])
        if checklist:
            body = "\n".join(f"{idx}. {item}" for idx, item in enumerate(checklist, start=1))
        else:
            body = "1. No rollback checklist configured."
        console.print(Panel(body, title=f"Rollback Owner: {owner}", border_style="yellow"))

    run_async(_rollback())


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@cli.group()
def store():
    """Browse and buy from the redemption store."""
    pass


@store.command("list")
def store_list():
    """Browse the redemption catalog."""
    async def _list():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.store_list()
            items = data.get("items", {})

            table = Table(title="OpenVegas Store")
            table.add_column("ID")
            table.add_column("Name")
            table.add_column("Description")
            table.add_column("Cost ($V)", justify="right")
            table.add_column("Type")

            for item_id, item in items.items():
                table.add_row(
                    item_id,
                    item.get("name", ""),
                    item.get("description", ""),
                    str(item.get("cost_v", "")),
                    item.get("type", ""),
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_list())


@store.command("buy")
@click.argument("item_id")
@click.option("--idempotency-key", default=None, help="Optional idempotency key for safe retries")
def store_buy(item_id: str, idempotency_key: str | None):
    """Buy an item from the store."""
    async def _buy():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.store_buy(item_id=item_id, idempotency_key=idempotency_key)
            console.print(
                f"[green]Order {data.get('order_id', '')}[/green] "
                f"status={data.get('status', '')} state={data.get('state', '')}"
            )
            console.print(f"[bold]Cost:[/bold] {data.get('cost_v', '0')} $V")
            grants = data.get("grants", [])
            if grants:
                table = Table(title="Granted Inference Credits")
                table.add_column("Provider")
                table.add_column("Model")
                table.add_column("Tokens", justify="right")
                for g in grants:
                    table.add_row(g.get("provider", ""), g.get("model_id", ""), str(g.get("tokens_total", 0)))
                console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_buy())


@store.command("grants")
def store_grants():
    """List remaining inference grants."""
    async def _grants():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.store_grants()
            grants = data.get("grants", [])
            if not grants:
                console.print("[dim]No inference grants found.[/dim]")
                return
            table = Table(title="Inference Grants")
            table.add_column("Provider")
            table.add_column("Model")
            table.add_column("Remaining", justify="right")
            table.add_column("Total", justify="right")
            table.add_column("Order")
            for g in grants:
                table.add_row(
                    g.get("provider", ""),
                    g.get("model_id", ""),
                    str(g.get("tokens_remaining", 0)),
                    str(g.get("tokens_total", 0)),
                    str(g.get("source_order_id", ""))[:8],
                )
            console.print(table)
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_grants())


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("game_id")
def verify(game_id: str):
    """Verify a provably fair game outcome."""
    async def _verify():
        from openvegas.client import OpenVegasClient, APIError
        try:
            client = OpenVegasClient()
            data = await client.verify_game(game_id)
            from openvegas.rng.provably_fair import ProvablyFairRNG

            valid = ProvablyFairRNG.verify(
                data.get("server_seed", ""),
                data.get("server_seed_hash", ""),
            )
            if valid:
                console.print("[bold green]Outcome verified! Seed matches commitment.[/bold green]")
            else:
                console.print("[bold red]Verification failed! Seed does not match.[/bold red]")

            console.print(f"  Server seed: {data.get('server_seed', '')[:16]}...")
            console.print(f"  Commitment:  {data.get('server_seed_hash', '')[:16]}...")
            console.print(f"  Client seed: {data.get('client_seed', '')}")
            console.print(f"  Nonce:       {data.get('nonce', '')}")
        except APIError as e:
            console.print(f"[red]{e.detail}[/red]")

    run_async(_verify())


@cli.command("ui")
@click.option("--no-render", is_flag=True, help="Skip game animation rendering in inline UI.")
@click.option(
    "--render-timeout-sec",
    type=float,
    default=15.0,
    show_default=True,
    help="Inline UI render timeout in seconds.",
)
def interactive_ui(no_render: bool, render_timeout_sec: float):
    """Open guided terminal UI."""
    _load_openvegas_env_defaults_from_dotenv()
    try:
        from openvegas.tui.prompt_ui import run_prompt_ui
    except Exception as e:  # pragma: no cover - runtime-only import fallback
        console.print(f"[red]Unable to load inline UI mode: {e}[/red]")
        return
    run_prompt_ui(no_render=no_render, render_timeout_sec=render_timeout_sec)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group():
    """Manage OpenVegas configuration."""
    pass


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value."""
    from openvegas.config import load_config, save_config

    config = load_config()

    if key == "default_provider":
        if value not in ("openai", "anthropic", "gemini"):
            console.print("[red]Provider must be openai, anthropic, or gemini[/red]")
            return
        config["default_provider"] = value
    elif key.startswith("default_model_"):
        provider = key.removeprefix("default_model_")
        models = config.get("default_model_by_provider", {})
        models[provider] = value
        config["default_model_by_provider"] = models
    elif key in (
        "theme",
        "animation",
        "backend_url",
        "supabase_url",
        "supabase_anon_key",
        "chat_style",
        "tool_event_density",
        "approval_ui",
    ):
        if key == "animation":
            value = value.lower() in ("true", "1", "yes")
        elif key == "chat_style":
            value = normalize_chat_style(value)
        elif key == "tool_event_density":
            value = normalize_tool_event_density(value)
        elif key == "approval_ui":
            value = normalize_approval_ui(value)
        config[key] = value
    else:
        console.print(f"[red]Unknown config key: {key}[/red]")
        return

    save_config(config)
    console.print(f"[green]Set {key} = {value}[/green]")


@config_group.command("show")
def config_show():
    """Show current configuration."""
    from openvegas.config import load_config
    import json

    config = load_config()
    # Redact sensitive fields
    display = dict(config)
    if "session" in display:
        redacted_session: dict[str, Any] = {}
        for k, v in display["session"].items():
            if isinstance(v, str):
                redacted_session[k] = v[:8] + "..." if v else ""
            else:
                redacted_session[k] = v
        display["session"] = redacted_session
    for p in display.get("providers", {}):
        if "api_key" in display["providers"][p]:
            key = display["providers"][p]["api_key"]
            display["providers"][p]["api_key"] = key[:8] + "..." if key else ""

    console.print(json.dumps(display, indent=2))


if __name__ == "__main__":
    cli()
