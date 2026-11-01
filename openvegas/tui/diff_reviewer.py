"""Terminal unified-diff reviewer for CLI fallback flows."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import sys
from typing import Any

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class ParsedDiffHunk:
    hunk_index: int
    file_path: str
    header_line: str
    body_lines: tuple[str, ...]
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def touched_lines(self) -> int:
        touched = 0
        for line in self.body_lines:
            if line.startswith("+") or line.startswith("-"):
                touched += 1
        return max(1, touched)


@dataclass(frozen=True)
class ParsedDiffFile:
    prelude_lines: tuple[str, ...]
    old_header: str
    new_header: str
    metadata_lines: tuple[str, ...]
    file_path: str
    hunks: tuple[ParsedDiffHunk, ...]


@dataclass(frozen=True)
class ParsedUnifiedPatch:
    files: tuple[ParsedDiffFile, ...]
    parse_error: str | None = None

    @property
    def hunks(self) -> tuple[ParsedDiffHunk, ...]:
        out: list[ParsedDiffHunk] = []
        for item in self.files:
            out.extend(item.hunks)
        return tuple(out)

    @property
    def hunks_total(self) -> int:
        return len(self.hunks)

    @property
    def target_files(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in self.files:
            if item.file_path not in seen:
                seen.add(item.file_path)
                ordered.append(item.file_path)
        return tuple(ordered)


def _normalize_diff_path(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    return token


def parse_unified_patch(patch_text: str) -> ParsedUnifiedPatch:
    lines = str(patch_text or "").splitlines(keepends=True)
    if not lines:
        return ParsedUnifiedPatch(files=(), parse_error="empty_patch")

    files: list[ParsedDiffFile] = []
    pending_prelude: list[str] = []
    idx = 0
    hunk_index = 0
    while idx < len(lines):
        line = lines[idx]
        if not line.startswith("--- "):
            pending_prelude.append(line)
            idx += 1
            continue

        old_header = line
        idx += 1
        if idx >= len(lines) or not lines[idx].startswith("+++ "):
            return ParsedUnifiedPatch(files=tuple(files), parse_error="missing_new_file_header")
        new_header = lines[idx]
        idx += 1

        file_path = _normalize_diff_path(new_header[4:].strip())
        metadata_lines: list[str] = []
        hunks: list[ParsedDiffHunk] = []

        while idx < len(lines):
            token = lines[idx]
            if token.startswith("--- "):
                break
            if token.startswith("@@"):
                hunk_header = token
                m = _HUNK_HEADER_RE.match(hunk_header.strip())
                if not m:
                    return ParsedUnifiedPatch(files=tuple(files), parse_error="invalid_hunk_header")
                old_start = int(m.group(1))
                old_count = int(m.group(2) or "1")
                new_start = int(m.group(3))
                new_count = int(m.group(4) or "1")
                idx += 1
                body: list[str] = []
                while idx < len(lines):
                    body_token = lines[idx]
                    if body_token.startswith("--- ") or body_token.startswith("@@"):
                        break
                    body.append(body_token)
                    idx += 1
                hunks.append(
                    ParsedDiffHunk(
                        hunk_index=hunk_index,
                        file_path=file_path,
                        header_line=hunk_header,
                        body_lines=tuple(body),
                        old_start=old_start,
                        old_count=old_count,
                        new_start=new_start,
                        new_count=new_count,
                    )
                )
                hunk_index += 1
                continue
            metadata_lines.append(token)
            idx += 1

        files.append(
            ParsedDiffFile(
                prelude_lines=tuple(pending_prelude),
                old_header=old_header,
                new_header=new_header,
                metadata_lines=tuple(metadata_lines),
                file_path=file_path,
                hunks=tuple(hunks),
            )
        )
        pending_prelude = []

    if not files:
        return ParsedUnifiedPatch(files=(), parse_error="no_file_headers")
    return ParsedUnifiedPatch(files=tuple(files))


def render_unified_patch(
    parsed: ParsedUnifiedPatch,
    *,
    accepted_hunks: set[int] | None = None,
) -> str:
    if parsed.parse_error:
        return ""
    accepted = set(accepted_hunks or [])
    lines: list[str] = []
    for item in parsed.files:
        selected = list(item.hunks)
        if accepted_hunks is not None:
            selected = [h for h in selected if h.hunk_index in accepted]
        if not selected:
            continue
        lines.extend(item.prelude_lines)
        lines.append(item.old_header)
        lines.append(item.new_header)
        lines.extend(item.metadata_lines)
        for hunk in selected:
            lines.append(hunk.header_line)
            lines.extend(hunk.body_lines)
    return "".join(lines)


def filter_patch_by_accepted_hunks(
    patch_text: str,
    accepted_hunks: set[int],
) -> tuple[str | None, ParsedUnifiedPatch | None]:
    parsed = parse_unified_patch(patch_text)
    if parsed.parse_error:
        return None, None
    filtered_text = render_unified_patch(parsed, accepted_hunks=accepted_hunks)
    if not filtered_text.strip():
        return None, None
    filtered_parsed = parse_unified_patch(filtered_text)
    if filtered_parsed.parse_error:
        return None, None
    return filtered_text, filtered_parsed


def _hunk_body_counts_match(hunk: ParsedDiffHunk) -> bool:
    old_seen = 0
    new_seen = 0
    for line in hunk.body_lines:
        if line.startswith("\\"):
            continue
        if line.startswith(" "):
            old_seen += 1
            new_seen += 1
            continue
        if line.startswith("-"):
            old_seen += 1
            continue
        if line.startswith("+"):
            new_seen += 1
            continue
        return False
    return old_seen == max(0, int(hunk.old_count)) and new_seen == max(0, int(hunk.new_count))


def is_valid_filtered_patch(parsed: ParsedUnifiedPatch) -> bool:
    if parsed.parse_error:
        return False
    if not parsed.files:
        return False
    if parsed.hunks_total <= 0:
        return False

    file_headers: set[str] = set()
    for item in parsed.files:
        if not item.old_header.startswith("--- ") or not item.new_header.startswith("+++ "):
            return False
        if not item.hunks:
            return False
        file_headers.add(item.file_path)

    for hunk in parsed.hunks:
        if hunk.file_path not in file_headers:
            return False
        if not hunk.header_line.startswith("@@"):
            return False
        if not _hunk_body_counts_match(hunk):
            return False
    return True


def filtered_patch_footprint(parsed: ParsedUnifiedPatch) -> dict[str, Any]:
    touched_per_file: dict[str, int] = {}
    hunk_count_per_file: dict[str, int] = {}
    for h in parsed.hunks:
        touched_per_file[h.file_path] = touched_per_file.get(h.file_path, 0) + h.touched_lines
        hunk_count_per_file[h.file_path] = hunk_count_per_file.get(h.file_path, 0) + 1
    return {
        "target_files": list(parsed.target_files),
        "hunks_total": int(parsed.hunks_total),
        "touched_per_file": touched_per_file,
        "hunk_count_per_file": hunk_count_per_file,
    }


def _reject_all(path: str, hunks_total: int, *, timed_out: bool, error: str | None = None) -> dict[str, Any]:
    return {
        "file_path": path,
        "hunks_total": int(max(0, hunks_total)),
        "decisions": [
            {"hunk_index": idx, "decision": "rejected"} for idx in range(max(0, int(hunks_total)))
        ],
        "all_accepted": False,
        "timed_out": bool(timed_out),
        "error": error,
    }


def _decision_from_env(*, hunks_total: int) -> tuple[set[int] | None, bool]:
    mode = str(os.getenv("OPENVEGAS_TERMINAL_DIFF_DECISION", "")).strip().lower()
    if mode == "accept_all":
        return set(range(hunks_total)), False
    if mode == "reject_all":
        return set(), False
    if mode == "timeout":
        return set(), True
    if mode == "partial":
        accepted: set[int] = set()
        raw = str(os.getenv("OPENVEGAS_TERMINAL_DIFF_ACCEPT_HUNKS", "")).strip()
        for token in [x.strip() for x in raw.split(",") if x.strip()]:
            try:
                idx = int(token)
            except Exception:
                continue
            if 0 <= idx < hunks_total:
                accepted.add(idx)
        return accepted, False
    return None, False


def _render_hunk(console: Console, hunk: ParsedDiffHunk) -> None:
    console.print(f"[bold cyan]{hunk.file_path}[/bold cyan] [dim]hunk {hunk.hunk_index + 1}[/dim]")
    m = _HUNK_HEADER_RE.match(hunk.header_line.strip())
    old_line = int(m.group(1)) if m else 1
    new_line = int(m.group(3)) if m else 1
    for raw in hunk.body_lines:
        line = raw.rstrip("\n")
        if line.startswith("+"):
            text = Text(f"{'':>5} {new_line:>5} {line}", style="black on green3")
            console.print(text)
            new_line += 1
            continue
        if line.startswith("-"):
            text = Text(f"{old_line:>5} {'':>5} {line}", style="black on red3")
            console.print(text)
            old_line += 1
            continue
        if line.startswith(" "):
            text = Text(f"{old_line:>5} {new_line:>5} {line}", style="dim")
            console.print(text)
            old_line += 1
            new_line += 1
            continue
        console.print(Text(f"{'':>5} {'':>5} {line}", style="dim"))


def _choose_hunks_interactive(console: Console, parsed: ParsedUnifiedPatch) -> tuple[set[int], bool]:
    accepted: set[int] = set()
    accept_rest = False
    reject_rest = False
    for hunk in parsed.hunks:
        if accept_rest:
            accepted.add(hunk.hunk_index)
            continue
        if reject_rest:
            continue
        _render_hunk(console, hunk)
        choice = Prompt.ask(
            "Decision [y=accept/n=reject/a=accept all/r=reject all]",
            choices=["y", "n", "a", "r"],
            default="n",
        )
        if choice == "y":
            accepted.add(hunk.hunk_index)
            continue
        if choice == "a":
            accepted.add(hunk.hunk_index)
            accept_rest = True
            continue
        if choice == "r":
            reject_rest = True
            continue
    return accepted, False


def review_patch_terminal(
    *,
    path: str,
    patch_text: str,
    allow_partial_accept: bool = True,
    console: Console | None = None,
) -> dict[str, Any]:
    parsed = parse_unified_patch(patch_text)
    if parsed.parse_error:
        return _reject_all(path, 0, timed_out=False, error="invalid_patch")
    hunks_total = parsed.hunks_total
    if hunks_total <= 0:
        return {
            "file_path": path,
            "hunks_total": 0,
            "decisions": [],
            "all_accepted": True,
            "timed_out": False,
        }

    max_hunks = max(1, int(os.getenv("OPENVEGAS_TERMINAL_DIFF_MAX_HUNKS", "80")))
    max_patch_bytes = max(1024, int(os.getenv("OPENVEGAS_TERMINAL_DIFF_MAX_PATCH_BYTES", "262144")))
    if hunks_total > max_hunks or len(patch_text.encode("utf-8")) > max_patch_bytes:
        return _reject_all(path, hunks_total, timed_out=False, error="large_diff")

    from_env, timed_out = _decision_from_env(hunks_total=hunks_total)
    if timed_out:
        return _reject_all(path, hunks_total, timed_out=True, error="timeout")
    if from_env is not None:
        decisions = []
        for idx in range(hunks_total):
            decisions.append(
                {"hunk_index": idx, "decision": "accepted" if idx in from_env else "rejected"}
            )
        accepted_count = len(from_env)
        if not allow_partial_accept and 0 < accepted_count < hunks_total:
            return _reject_all(path, hunks_total, timed_out=False, error="partial_not_allowed")
        return {
            "file_path": path,
            "hunks_total": hunks_total,
            "decisions": decisions,
            "all_accepted": accepted_count == hunks_total,
            "timed_out": False,
        }

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _reject_all(path, hunks_total, timed_out=False, error="non_tty")

    target_console = console or Console()
    try:
        accepted_hunks, timed_out = _choose_hunks_interactive(target_console, parsed)
    except KeyboardInterrupt:
        return _reject_all(path, hunks_total, timed_out=False, error="selector_interrupted")
    except Exception:
        return _reject_all(path, hunks_total, timed_out=False, error="selector_error")

    if timed_out:
        return _reject_all(path, hunks_total, timed_out=True, error="timeout")
    if not allow_partial_accept and 0 < len(accepted_hunks) < hunks_total:
        return _reject_all(path, hunks_total, timed_out=False, error="partial_not_allowed")

    decisions = []
    for idx in range(hunks_total):
        decisions.append({"hunk_index": idx, "decision": "accepted" if idx in accepted_hunks else "rejected"})
    return {
        "file_path": path,
        "hunks_total": hunks_total,
        "decisions": decisions,
        "all_accepted": len(accepted_hunks) == hunks_total,
        "timed_out": False,
    }

