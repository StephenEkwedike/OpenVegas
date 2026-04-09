from __future__ import annotations

import difflib
import json
import os
import time
from pathlib import Path

import pytest

import openvegas.cli as cli_mod
from openvegas.cli import _collect_tool_call_candidates
from openvegas.cli import _canonical_tool_name
from openvegas.cli import _build_completion_criteria
from openvegas.cli import CompletionCriteria
from openvegas.cli import _evaluate_completion_criteria
from openvegas.cli import _attempt_bootstrap_write_fallback
from openvegas.cli import _has_patch_intent
from openvegas.cli import _is_file_create_intent
from openvegas.cli import _is_patch_repeat_followup_intent
from openvegas.cli import _is_patch_smoke_intent
from openvegas.cli import _is_temp_patch_smoke_intent
from openvegas.cli import _mutation_retry_backoff_sec
from openvegas.cli import _pair_hunks_by_file_order_nearest
from openvegas.cli import _pairing_quality
from openvegas.cli import _parse_patch_scope
from openvegas.cli import _filter_patch_by_accepted_hunks
from openvegas.cli import _filter_patch_by_accepted_hunks_with_parsed
from openvegas.cli import _is_valid_filtered_patch
from openvegas.cli import _preprocess_tool_request_for_runtime
from openvegas.cli import _prepare_write_patch
from openvegas.cli import _prepare_find_replace_patch
from openvegas.cli import _prepare_insert_at_end_patch
from openvegas.cli import _promote_tool_call_for_patch_intent
from openvegas.cli import _patch_recovery_payload
from openvegas.cli import _diagnose_synth_write_skip_reason
from openvegas.cli import _deep_find_keyed_string
from openvegas.cli import RETRYABLE_MUTATION_ERRORS
from openvegas.cli import _semantic_tool_signature
from openvegas.cli import _search_pattern_hint_from_message
from openvegas.cli import _shell_command_hint_from_message
from openvegas.cli import _synth_patch_tool_req_for_intent
from openvegas.cli import _synth_write_tool_req_from_model_edit
from openvegas.cli import _is_scrape_request
from openvegas.cli import _is_scrape_refusal_text
from openvegas.cli import _rewrite_lookup_request_for_safe_web_search
from openvegas.cli import _has_workspace_tooling_intent
from openvegas.cli import _should_enable_web_search_for_turn
from openvegas.cli import _augment_web_search_prompt
from openvegas.cli import _attachment_search_roots
from openvegas.cli import _attachment_path_allowed
from openvegas.cli import _detect_auto_attach_paths
from openvegas.cli import _extract_filename_like_tokens
from openvegas.cli import _split_compound_attachment_token
from openvegas.cli import _merge_chat_prompt_and_buffered_lines
from openvegas.cli import _coalesce_prompt_text
from openvegas.cli import _normalize_live_chat_input_text
from openvegas.cli import _coalesce_live_prompt_text
from openvegas.cli import _maybe_prepend_synth_write
from openvegas.cli import _should_synth_write_from_model_text
from openvegas.cli import _extract_fenced_code_blocks
from openvegas.cli import _extract_first_fenced_code_block
from openvegas.cli import _synthesize_patch_from_arguments
from openvegas.cli import _tool_result_reason_code
from openvegas.cli import _validate_patch_recovery_scope
from openvegas.cli import _rewrite_shell_command_for_env
from openvegas.cli import _resolve_attachment_token_path
from openvegas.cli import _resolve_screenshot_stem_to_path
from openvegas.cli import _message_requests_attachment_analysis
from openvegas.cli import _format_composer_attachment_status_row
from openvegas.cli import _format_live_composer_status_row
from openvegas.cli import _parse_mcp_call_command
from openvegas.cli import _preflight_filter_attachments_for_capabilities
from openvegas.cli import _inject_attachment_markers_into_message
from openvegas.cli import _is_chat_attachment_mime_allowed
from openvegas.cli import AttachmentState
from openvegas.cli import PendingAttachment
from openvegas.agent.local_tools import ToolExecutionResult
from openvegas.telemetry import get_metrics_snapshot
from openvegas.telemetry import reset_metrics


def test_scrape_request_detection():
    assert _is_scrape_request("can you scrape zillow for houses") is True
    assert _is_scrape_request("can you help find listings") is False


def test_scrape_refusal_detection():
    assert _is_scrape_refusal_text("Sorry, I can’t help scrape Zillow or bypass site restrictions.") is True
    assert _is_scrape_refusal_text("Here are listings from public sources.") is False


def test_rewrite_lookup_request_for_safe_web_search():
    out = _rewrite_lookup_request_for_safe_web_search(
        "can you scrape zillow for houses in austin tx under $500k"
    )
    assert "scrape" not in out.lower()
    assert "find zillow" in out.lower()
    assert "lawful web search" in out.lower()


def test_workspace_tooling_intent_requires_action_or_path_for_code_filenames():
    assert _has_workspace_tooling_intent("summarize notes.txt from our meeting") is False
    assert _has_workspace_tooling_intent("open notes.txt and edit the intro") is True
    assert _has_workspace_tooling_intent("read ./config.json") is True
    assert _has_workspace_tooling_intent("search code for config.json") is True
    assert _has_workspace_tooling_intent("what is in Color pallette.pdf") is False


def test_web_search_turn_gate_skips_attachment_only_turns():
    msg = "tell me what you see in this screenshot and pdf"
    assert _should_enable_web_search_for_turn(msg, has_uploaded_attachments=True) is False
    web_msg = "find latest Zillow listings in Austin under 500k"
    assert _should_enable_web_search_for_turn(web_msg, has_uploaded_attachments=True) is True


def test_augment_web_search_prompt_is_generic_not_domain_specific():
    base = "find Python package updates this week"
    out = _augment_web_search_prompt(base)
    assert "prefer original source pages" in out.lower()
    assert "source url" in out.lower()
    assert "listing pages" not in out.lower()


def test_web_request_signal_is_generic_not_domain_hardcoded():
    assert cli_mod._has_web_request_signal("look up latest changes to React") is True
    assert cli_mod._has_web_request_signal("zillow") is False


def test_detect_auto_attach_paths_from_filename_and_screenshot_stem(tmp_path: Path):
    pdf = tmp_path / "Color pallette.pdf"
    pdf.write_text("palette", encoding="utf-8")
    shot = tmp_path / "Screenshot 2026-02-02 at 12.39.11 PM.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    text = "review Color pallette.pdf and Screenshot 2026-02-02 at 12.39.11\u202fPM"
    paths, unresolved = _detect_auto_attach_paths(text, workspace_root=str(tmp_path))
    names = {Path(p).name for p in paths}
    assert "Color pallette.pdf" in names
    assert "Screenshot 2026-02-02 at 12.39.11 PM.png" in names
    assert unresolved == []


def test_attachment_search_roots_include_common_dirs_without_home_scan(monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "home"
    downloads = fake_home / "Downloads"
    desktop = fake_home / "Desktop"
    documents = fake_home / "Documents"
    for path in (downloads, desktop, documents):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("OPENVEGAS_CHAT_ATTACH_SEARCH_HOME", "0")
    roots = _attachment_search_roots(str(tmp_path))
    roots_str = {str(p) for p in roots}
    assert str(downloads.resolve()) in roots_str
    assert str(desktop.resolve()) in roots_str
    assert str(documents.resolve()) in roots_str


def test_detect_auto_attach_paths_finds_file_in_downloads_without_home_scan(monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "home"
    downloads = fake_home / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / "Color pallette.pdf"
    target.write_text("palette", encoding="utf-8")

    monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("OPENVEGAS_CHAT_ATTACH_SEARCH_HOME", "0")
    paths, unresolved = _detect_auto_attach_paths(
        "Can you see what's in {Color pallette.pdf}",
        workspace_root=str(tmp_path / "workspace"),
    )
    assert str(target.resolve()) in {str(Path(p).resolve()) for p in paths}
    assert unresolved == []


def test_attachment_path_allowed_blocks_sensitive_prefixes_by_default(monkeypatch, tmp_path: Path):
    target = tmp_path / "secret.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setenv("OPENVEGAS_CHAT_ATTACH_BLOCK_SENSITIVE", "1")
    monkeypatch.setenv("OPENVEGAS_CHAT_ATTACH_BLOCK_PATH_PREFIXES", str(tmp_path))
    assert _attachment_path_allowed(target) is False


def test_attachment_path_allowed_can_be_disabled(monkeypatch, tmp_path: Path):
    target = tmp_path / "safe.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setenv("OPENVEGAS_CHAT_ATTACH_BLOCK_SENSITIVE", "0")
    monkeypatch.setenv("OPENVEGAS_CHAT_ATTACH_BLOCK_PATH_PREFIXES", str(tmp_path))
    assert _attachment_path_allowed(target) is True


@pytest.mark.asyncio
async def test_detect_auto_attach_paths_with_deadline_times_out(monkeypatch, tmp_path: Path):
    def _slow_detect(*args, **kwargs):
        time.sleep(0.2)
        return [], []

    monkeypatch.setattr(cli_mod, "_detect_auto_attach_paths", _slow_detect)
    paths, unresolved, timed_out = await cli_mod._detect_auto_attach_paths_with_deadline(
        "Can you see what's in {Color pallette.pdf}",
        workspace_root=str(tmp_path),
        max_candidates=5,
        deadline_ms=50,
    )
    assert paths == []
    assert timed_out is True
    assert "Color pallette.pdf" in unresolved


def test_extract_filename_tokens_avoids_sentence_swallowing():
    msg = "tell me what you see in these: Screenshot 2026-02-02 at 12.39.11 PM Color pallette.pdf"
    tokens = _extract_filename_like_tokens(msg)
    assert "Color pallette.pdf" in tokens
    assert not any(token.startswith("tell me what you see") for token in tokens)


def test_coalesce_prompt_text_keeps_multiline_input_as_one_turn():
    msg = "Can you review\nColor pallette.pdf\nand screenshot"
    out = _coalesce_prompt_text(msg)
    assert out == "Can you review Color pallette.pdf and screenshot"


def test_live_chat_input_text_wraps_inline_file_mentions_with_markers():
    out = _normalize_live_chat_input_text(
        "Can you see what's in Color pallette.pdf and Screenshot 2026-02-02 at 12.39.11 PM"
    )
    assert "{Color pallette.pdf}" in out
    assert "{Screenshot 2026-02-02 at 12.39.11 PM}" in out


def test_live_chat_input_text_skips_workspace_edit_prompts():
    out = _normalize_live_chat_input_text("open ./notes.txt and add one line")
    assert out == "open ./notes.txt and add one line"


def test_live_chat_input_text_preserves_trailing_space_while_typing():
    assert _normalize_live_chat_input_text("hello ") == "hello "


def test_coalesce_live_prompt_text_merges_multiline_paste():
    out = _coalesce_live_prompt_text("Can you review\nColor pallette.pdf\nand screenshot")
    assert out == "Can you review Color pallette.pdf and screenshot"


def test_split_compound_attachment_token_extracts_suffix_filename():
    token = "Screenshot 2026-02-02 at 12.39.11 PM Color pallette.pdf"
    pieces = _split_compound_attachment_token(token)
    assert "Color pallette.pdf" in pieces


def test_resolve_attachment_token_path_matches_filename_inside_phrase(tmp_path: Path):
    target = tmp_path / "Color pallette.pdf"
    target.write_text("palette", encoding="utf-8")
    resolved = _resolve_attachment_token_path(
        "Screenshot 2026-02-02 at 12.39.11 PM Color pallette.pdf",
        workspace_root=str(tmp_path),
    )
    assert resolved is not None
    assert Path(resolved).name == "Color pallette.pdf"


def test_resolve_screenshot_stem_selects_latest_match(tmp_path: Path):
    older = tmp_path / "Screenshot 2026-02-02 at 12.39.11 PM.png"
    newer = tmp_path / "Screenshot 2026-02-02 at 12.39.11 PM (1).png"
    older.write_bytes(b"\x89PNG\r\n\x1a\nold")
    newer.write_bytes(b"\x89PNG\r\n\x1a\nnew")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))
    resolved = _resolve_screenshot_stem_to_path(
        "Screenshot 2026-02-02 at 12.39.11 PM",
        workspace_root=str(tmp_path),
    )
    assert resolved is not None
    assert Path(resolved).name == newer.name


def test_message_requests_attachment_analysis_detection():
    assert _message_requests_attachment_analysis("tell me what you see in this screenshot") is True
    assert _message_requests_attachment_analysis("can you analyze this pdf") is True
    assert _message_requests_attachment_analysis("please transcribe this audio clip") is True
    assert _message_requests_attachment_analysis("find latest homes in austin") is False


def test_live_composer_status_shows_candidates_without_attached_files():
    status = _format_live_composer_status_row(
        draft_text="Can you review Color pallette.pdf",
        attachments=[],
        provider="openai",
        model="gpt-5",
    )
    assert status is not None
    assert "candidates" in status
    assert "{Color pallette.pdf}" in status


def test_inject_attachment_markers_into_message_replaces_inline_names():
    att = PendingAttachment(
        local_id="a1",
        path="/tmp/Color pallette.pdf",
        name="Color pallette.pdf",
        mime_type="application/pdf",
        size_bytes=123,
        sha256="abc",
        state=AttachmentState.UPLOADED,
        remote_file_id="f1",
    )
    out = _inject_attachment_markers_into_message(
        "Can you see what's in Color pallette.pdf?",
        [att],
    )
    assert "{Color pallette.pdf}" in out
    assert "Color pallette.pdf?" not in out


def test_format_composer_attachment_status_row_for_empty_list():
    assert _format_composer_attachment_status_row([]) is None


def test_chat_attachment_mime_allowlist(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CHAT_ALLOWED_MIME", "image/*,application/pdf")
    assert _is_chat_attachment_mime_allowed("image/png") is True
    assert _is_chat_attachment_mime_allowed("application/pdf") is True
    assert _is_chat_attachment_mime_allowed("text/plain") is False


def test_preprocess_mcp_call_aliases_and_object_arguments():
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "mcp_call", "arguments": {"server": "srv-1", "name": "ping", "args": {"x": 1}}},
        user_message="call MCP tool",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "mcp_call"
    assert prepared["arguments"]["server_id"] == "srv-1"
    assert prepared["arguments"]["tool"] == "ping"
    assert prepared["arguments"]["arguments"] == {"x": 1}


def test_format_composer_attachment_status_row_compacts_long_lists():
    attachments = [
        PendingAttachment(
            local_id=f"a{i}",
            path=f"/tmp/file-{i}.txt",
            name=f"file-{i}.txt",
            mime_type="text/plain",
            size_bytes=10,
            sha256=f"hash-{i}",
            state=AttachmentState.ATTACHED,
        )
        for i in range(6)
    ]
    out = _format_composer_attachment_status_row(attachments, max_markers=4)
    assert "📄 6 file(s)" in str(out)


def test_format_composer_attachment_status_row_shows_unsupported_images(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "0")
    attachments = [
        PendingAttachment(
            local_id="a1",
            path="/tmp/image.png",
            name="image.png",
            mime_type="image/png",
            size_bytes=10,
            sha256="hash-image",
            state=AttachmentState.ATTACHED,
        )
    ]
    out = _format_composer_attachment_status_row(
        attachments,
        provider="openai",
        model="gpt-5",
    )
    assert "unsupported" in str(out)


def test_format_composer_attachment_status_row_shows_audio_files(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_ENABLE_SPEECH_TO_TEXT", "1")
    attachments = [
        PendingAttachment(
            local_id="a1",
            path="/tmp/voice-note.m4a",
            name="voice-note.m4a",
            mime_type="audio/m4a",
            size_bytes=10,
            sha256="hash-audio",
            state=AttachmentState.ATTACHED,
        )
    ]
    out = _format_composer_attachment_status_row(
        attachments,
        provider="openai",
        model="gpt-5",
    )
    assert "audio file" in str(out).lower()


def test_extract_filename_like_tokens_includes_audio_extensions():
    tokens = _extract_filename_like_tokens("Please review voice-note.m4a and standup.mp3")
    lowered = {t.lower() for t in tokens}
    assert any(tok.endswith("voice-note.m4a") for tok in lowered)
    assert any(tok.endswith("standup.mp3") for tok in lowered)


def test_parse_mcp_call_command_accepts_json_args():
    server, tool, args, err = _parse_mcp_call_command('/mcp call srv-1 ping {"x":1,"ok":true}')
    assert err is None
    assert server == "srv-1"
    assert tool == "ping"
    assert args == {"x": 1, "ok": True}


def test_parse_mcp_call_command_accepts_key_value_args():
    server, tool, args, err = _parse_mcp_call_command("/mcp call srv-2 search query=hello limit=3")
    assert err is None
    assert server == "srv-2"
    assert tool == "search"
    assert args == {"query": "hello", "limit": 3}


def test_parse_mcp_call_command_rejects_invalid_shape():
    server, tool, args, err = _parse_mcp_call_command("/mcp call srv-2")
    assert server == ""
    assert tool == ""
    assert args == {}
    assert "usage:" in str(err)


def test_preflight_drops_images_when_vision_unsupported(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "0")
    pending = [
        PendingAttachment(
            local_id="i1",
            path="/tmp/shot.png",
            name="shot.png",
            mime_type="image/png",
            size_bytes=10,
            sha256="hash-i1",
            state=AttachmentState.ATTACHED,
        ),
        PendingAttachment(
            local_id="f1",
            path="/tmp/report.pdf",
            name="report.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="hash-f1",
            state=AttachmentState.ATTACHED,
        ),
    ]
    kept, dropped, blocked = _preflight_filter_attachments_for_capabilities(
        pending,
        provider="openai",
        model="gpt-5",
    )
    assert dropped == 1
    assert blocked is False
    assert len(kept) == 1
    assert kept[0].name == "report.pdf"


def test_preflight_blocks_when_all_attachments_are_images(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "0")
    pending = [
        PendingAttachment(
            local_id="i1",
            path="/tmp/shot.png",
            name="shot.png",
            mime_type="image/png",
            size_bytes=10,
            sha256="hash-i1",
            state=AttachmentState.ATTACHED,
        ),
        PendingAttachment(
            local_id="i2",
            path="/tmp/photo.jpg",
            name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=10,
            sha256="hash-i2",
            state=AttachmentState.ATTACHED,
        ),
    ]
    kept, dropped, blocked = _preflight_filter_attachments_for_capabilities(
        pending,
        provider="openai",
        model="gpt-5",
    )
    assert dropped == 2
    assert blocked is True
    assert kept == []


def test_preflight_passes_all_when_vision_supported(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_ENABLE_VISION", raising=False)
    pending = [
        PendingAttachment(
            local_id="i1",
            path="/tmp/shot.png",
            name="shot.png",
            mime_type="image/png",
            size_bytes=10,
            sha256="hash-i1",
            state=AttachmentState.ATTACHED,
        ),
        PendingAttachment(
            local_id="f1",
            path="/tmp/report.pdf",
            name="report.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="hash-f1",
            state=AttachmentState.ATTACHED,
        ),
    ]
    kept, dropped, blocked = _preflight_filter_attachments_for_capabilities(
        pending,
        provider="openai",
        model="gpt-5",
    )
    assert dropped == 0
    assert blocked is False
    assert len(kept) == 2


def test_search_pattern_hint_prefers_quoted_token():
    msg = 'search for "result_submission_hash" across this repo and summarize where it is used'
    assert _search_pattern_hint_from_message(msg) == "result_submission_hash"


def test_search_pattern_hint_uses_search_clause_when_unquoted():
    msg = "search for result_submission_hash across this repo"
    assert _search_pattern_hint_from_message(msg) == "result_submission_hash"


def test_synthesize_patch_from_arguments_from_new_content(tmp_path: Path):
    target = tmp_path / "tmp.txt"
    target.write_text("a\n", encoding="utf-8")
    patch = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "tmp.txt", "new_content": "b\n"},
    )
    assert patch is not None
    assert "--- tmp.txt" in patch
    assert "+++ tmp.txt" in patch
    assert "-a" in patch
    assert "+b" in patch


def test_canonical_tool_name_maps_fs_create_to_fs_apply_patch():
    assert _canonical_tool_name("fs_create") == "fs_apply_patch"


def test_canonical_tool_name_maps_external_abi_to_internal_tools():
    assert _canonical_tool_name("Read") == "fs_read"
    assert _canonical_tool_name("Search") == "fs_search"
    assert _canonical_tool_name("Write") == "fs_apply_patch"
    assert _canonical_tool_name("FindAndReplace") == "fs_apply_patch"
    assert _canonical_tool_name("InsertAtEnd") == "fs_apply_patch"
    assert _canonical_tool_name("Bash") == "shell_run"
    assert _canonical_tool_name("List") == "fs_list"


def test_promote_tool_call_for_patch_intent_upgrades_second_fs_list():
    name, args = _promote_tool_call_for_patch_intent(
        user_message="apply a tiny patch to a temp file in this repo",
        tool_name="fs_list",
        arguments={"path": "."},
        tool_observations=[{"tool_name": "fs_list"}],
    )
    assert _has_patch_intent("apply a tiny patch to a temp file")
    assert name == "fs_apply_patch"
    assert args["path"] == ".openvegas_tmp_patch.txt"
    assert args["new_content"].startswith("openvegas temp patch applied ")


def test_promote_tool_call_for_patch_intent_upgrades_unknown_tool():
    name, args = _promote_tool_call_for_patch_intent(
        user_message="apply a tiny patch to a temp file in this repo",
        tool_name="fs_create",
        arguments={},
        tool_observations=[],
    )
    assert name == "fs_apply_patch"
    assert args["path"] == ".openvegas_tmp_patch.txt"
    assert args["new_content"].startswith("openvegas temp patch applied ")


def test_synth_patch_tool_req_for_intent_when_no_patch_attempt_yet():
    req = _synth_patch_tool_req_for_intent(
        user_message="apply a tiny patch to a temp file in this repo",
        tool_observations=[],
    )
    assert req is not None
    assert req["tool_name"] == "fs_apply_patch"
    assert req["arguments"]["path"] == ".openvegas_tmp_patch.txt"
    assert req["arguments"]["new_content"].startswith("openvegas temp patch applied ")


def test_synth_patch_tool_req_for_create_file_intent_uses_requested_filename_and_sections():
    req = _synth_patch_tool_req_for_intent(
        user_message="Create a temp file named .openvegas_audit.md in repo root with sections: Summary, Findings, Risks, Next Steps.",
        tool_observations=[],
    )
    assert req is not None
    assert req["tool_name"] == "fs_apply_patch"
    assert req["arguments"]["path"] == ".openvegas_audit.md"
    content = req["arguments"]["new_content"]
    assert "## Summary" in content
    assert "## Findings" in content
    assert "## Risks" in content
    assert "## Next Steps" in content


def test_synth_patch_tool_req_for_intent_none_after_patch_attempt():
    req = _synth_patch_tool_req_for_intent(
        user_message="apply a tiny patch to a temp file in this repo",
        tool_observations=[{"tool_name": "fs_apply_patch"}],
    )
    assert req is None


def test_extract_first_fenced_code_block_parses_python_block():
    text = "Here is the file:\n```python\nprint('hi')\n```\nDone."
    out = _extract_first_fenced_code_block(text)
    assert out == "print('hi')"


def test_extract_fenced_code_blocks_returns_all_blocks():
    text = (
        "```python\nprint('a')\n```\n"
        "and\n"
        "```js\nconsole.log('b')\n```"
    )
    assert _extract_fenced_code_blocks(text) == ["print('a')", "console.log('b')"]


def test_extract_fenced_code_blocks_supports_tilde_fences():
    text = "~~~python\nprint('tilde')\n~~~\n"
    assert _extract_fenced_code_blocks(text) == ["print('tilde')"]


def test_synth_write_tool_req_from_model_edit_builds_find_replace_call_when_uniquely_anchorable():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text=(
            "```python\n"
            "def add(a, b):\n"
            "    return a + b + 1\n"
            "```"
        ),
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {
                    "path": "tests/fixtures/diff_accept_demo/calc.py",
                    "content": "def add(a, b):\n    return a + b\n",
                },
            }
        ],
    )
    assert req is not None
    assert req["tool_name"] == "FindAndReplace"
    assert req["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"
    assert "return a + b" in req["arguments"]["old_string"]
    assert "return a + b + 1" in req["arguments"]["new_string"]


def test_synth_write_tool_req_from_model_edit_uses_append_mode_for_bottom_intent():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Append this line at the bottom of this file.",
        model_text=(
            "```python\n"
            "1 + 1 = 2\n"
            "```"
        ),
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {"path": "tests/fixtures/diff_accept_demo/calc.py"},
            }
        ],
    )
    assert req is not None
    assert req["tool_name"] == "InsertAtEnd"
    assert req["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"
    assert req["arguments"]["operation_kind"] == "append"


def test_synth_write_tool_req_from_model_edit_append_comment_fallback_without_code_block():
    req = _synth_write_tool_req_from_model_edit(
        user_message="add a comment at end of calc.py. Make it french",
        model_text="I will add the requested French comment.",
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {"path": "tests/fixtures/diff_accept_demo/calc.py"},
            }
        ],
    )
    assert req is not None
    assert req["tool_name"] == "InsertAtEnd"
    assert req["arguments"]["filepath"].endswith("calc.py")
    assert "Commentaire" in req["arguments"]["content"]
    assert req["arguments"]["content"].startswith("# ")


def test_synth_write_tool_req_from_model_edit_append_comment_fallback_with_absolute_path():
    abs_target = "/Users/stephenekwedike/Desktop/OpenVegas/tests/fixtures/diff_accept_demo/calc.py"
    req = _synth_write_tool_req_from_model_edit(
        user_message=f"add a comment at end of {abs_target}. Make it french",
        model_text="Understood. I will do it.",
        tool_observations=[],
    )
    assert req is not None
    assert req["tool_name"] == "InsertAtEnd"
    assert req["arguments"]["filepath"] == abs_target
    assert req["arguments"]["content"].startswith("# ")


def test_diagnose_synth_write_skip_reason_none_for_append_comment_fallback():
    reason = _diagnose_synth_write_skip_reason(
        user_message="add a comment at end of tests/fixtures/diff_accept_demo/calc.py. Make it french",
        model_text="No code block needed.",
        tool_observations=[],
        planner_edit_intent=True,
    )
    assert reason is None


def test_prepare_write_patch_append_mode_appends_instead_of_replacing(tmp_path: Path):
    target = tmp_path / "demo.md"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    prepared, err = _prepare_write_patch(
        workspace_root=str(tmp_path),
        arguments={"filepath": "demo.md", "content": "1 + 1 = 2\n", "write_mode": "append"},
    )
    assert err is None
    assert prepared is not None
    patch = str(prepared["arguments"]["patch"])
    assert "--- demo.md" in patch
    assert "+++ demo.md" in patch
    assert "+1 + 1 = 2" in patch
    assert "-alpha" not in patch
    assert "-beta" not in patch


def test_prepare_write_patch_does_not_infer_append_mode_from_user_message(tmp_path: Path):
    target = tmp_path / "demo.md"
    target.write_text("line-a\n", encoding="utf-8")
    prepared, err = _prepare_write_patch(
        workspace_root=str(tmp_path),
        arguments={"filepath": "demo.md", "content": "line-b\n"},
        user_message="Add line-b at the bottom of demo.md",
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "existing_file_replace_requires_explicit_intent"


def test_synth_write_tool_req_from_model_edit_can_use_recent_fs_read_path():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit this file and add divide with guard",
        model_text=(
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```"
        ),
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {"path": "tests/fixtures/diff_accept_demo/calc.py"},
            }
        ],
    )
    assert req is not None
    assert req["tool_name"] in {"FindAndReplace", "InsertAtEnd", "Write"}
    assert req["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"


def test_synth_write_tool_req_from_model_edit_uses_write_only_for_explicit_replace_intent():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Rewrite entire tests/fixtures/diff_accept_demo/calc.py with this content",
        model_text=(
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```"
        ),
        tool_observations=[],
    )
    assert req is not None
    assert req["tool_name"] == "Write"
    assert req["arguments"]["write_mode"] == "replace"
    assert req["arguments"]["explicit_replace_intent"] is True


def test_prepare_find_replace_patch_exact_match_only(tmp_path: Path):
    target = tmp_path / "demo.py"
    target.write_text("alpha\nbeta\nbeta\n", encoding="utf-8")
    prepared, err = _prepare_find_replace_patch(
        workspace_root=str(tmp_path),
        arguments={
            "filepath": "demo.py",
            "old_string": "beta\n",
            "new_string": "gamma\n",
            "replace_all": False,
        },
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "old_string_not_unique"


def test_prepare_insert_at_end_patch_appends_only(tmp_path: Path):
    target = tmp_path / "demo.md"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    prepared, err = _prepare_insert_at_end_patch(
        workspace_root=str(tmp_path),
        arguments={"filepath": "demo.md", "content": "1 + 1 = 2\n"},
    )
    assert err is None
    assert prepared is not None
    patch = str(prepared["arguments"]["patch"])
    assert "+1 + 1 = 2" in patch
    assert "-alpha" not in patch
    assert "-beta" not in patch
    assert prepared["meta"]["append_content"] == "1 + 1 = 2\n"


def test_prepare_insert_at_end_patch_noop_when_content_already_at_eof(tmp_path: Path):
    target = tmp_path / "demo.md"
    target.write_text("alpha\nbeta\n1 + 1 = 2\n", encoding="utf-8")
    prepared, err = _prepare_insert_at_end_patch(
        workspace_root=str(tmp_path),
        arguments={"filepath": "demo.md", "content": "1 + 1 = 2\n"},
    )
    assert prepared is None
    assert err is not None
    assert err["status"] == "noop"
    assert err["error"] == "no_change"
    assert "already present at end of file" in err["detail"]


def test_prepare_insert_at_end_patch_noop_when_content_already_at_eof_without_newline(tmp_path: Path):
    target = tmp_path / "demo.md"
    target.write_text("alpha\nbeta\n1 + 1 = 2", encoding="utf-8")
    prepared, err = _prepare_insert_at_end_patch(
        workspace_root=str(tmp_path),
        arguments={"filepath": "demo.md", "content": "1 + 1 = 2"},
    )
    assert prepared is None
    assert err is not None
    assert err["status"] == "noop"
    assert err["error"] == "no_change"


def test_force_finalize_remains_terminal_contract():
    import openvegas.cli as cli_module

    src = Path(cli_module.__file__).read_text(encoding="utf-8")
    start = src.find("async def _force_finalize(")
    end = src.find("async def _execute_with_heartbeat(", start)
    assert start != -1 and end != -1
    body = src[start:end]
    assert "async def _force_finalize(final_res" in body
    assert "_maybe_intercept_final_text_for_mutation" not in body
    assert "return False" not in body


def test_interception_runs_before_force_finalize():
    import openvegas.cli as cli_module

    src = Path(cli_module.__file__).read_text(encoding="utf-8")
    start = src.find("async def _finalize_or_continue_with_intercept(")
    end = src.find("def _progress_fingerprint(", start)
    assert start != -1 and end != -1
    body = src[start:end]
    intercept_idx = body.find("await _maybe_intercept_final_text_for_mutation(")
    finalize_idx = body.find("return await _force_finalize(")
    assert intercept_idx != -1 and finalize_idx != -1
    assert intercept_idx < finalize_idx


def test_synth_write_selects_target_language_block_when_multiple_blocks_present():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text=(
            "```text\n"
            "usage: call divide(4,2)\n"
            "```\n"
            "```python\n"
            "def divide(a, b):\n"
            "    if b == 0:\n"
            "        raise ValueError('Cannot divide by zero.')\n"
            "    return a / b\n"
            "```"
        ),
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {
                    "path": "tests/fixtures/diff_accept_demo/calc.py",
                    "content": "def divide(a, b):\n    return a / (b if b else 1)\n",
                },
            }
        ],
    )
    assert req is not None
    assert req["tool_name"] in {"FindAndReplace", "Write"}
    assert req["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"
    if req["tool_name"] == "Write":
        assert "def divide" in req["arguments"]["content"]


def test_synth_write_is_not_suppressed_by_blocked_patch_observation():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text="```python\ndef divide(a,b):\n    return a / b\n```",
        tool_observations=[
            {
                "tool_name": "fs_apply_patch",
                "result_status": "blocked",
                "result_payload": {"reason": "approval_denied"},
            },
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {"path": "tests/fixtures/diff_accept_demo/calc.py"},
            },
        ],
    )
    assert req is not None
    assert req["tool_name"] in {"FindAndReplace", "InsertAtEnd", "Write"}


def test_unfenced_extraction_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OPENVEGAS_UNFENCED_CODE_EXTRACTION", raising=False)
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text=(
            "def divide(a: float, b: float) -> float:\n"
            "    if b == 0:\n"
            "        raise ValueError('Cannot divide by zero.')\n"
            "    return a / b\n"
        ),
        tool_observations=[],
    )
    assert req is None


def test_unfenced_extraction_python_guardrails_when_enabled(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_UNFENCED_CODE_EXTRACTION", "1")
    short_req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text="def divide(a, b):\n    return a / b\n",
        tool_observations=[],
    )
    assert short_req is None

    valid_req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text=(
            "from __future__ import annotations\n"
            "import math\n"
            "def divide(a: float, b: float) -> float:\n"
            "    if b == 0:\n"
            "        raise ValueError('Cannot divide by zero.')\n"
            "    return a / b\n"
        ),
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {"path": "tests/fixtures/diff_accept_demo/calc.py", "content": "x = 1\n"},
            }
        ],
    )
    assert valid_req is not None
    assert valid_req["tool_name"] in {"FindAndReplace", "Write"}
    assert valid_req["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"
    if valid_req["tool_name"] == "Write":
        assert "def divide" in valid_req["arguments"]["content"]


def test_should_synth_write_requires_explicit_edit_intent():
    assert _should_synth_write_from_model_text(
        user_message="show an example replacement for tests/fixtures/diff_accept_demo/calc.py",
        model_text="```python\nprint('demo')\n```",
        planner_edit_intent=False,
    ) is False


def test_should_synth_write_rejects_multifile_or_multiblock():
    assert _should_synth_write_from_model_text(
        user_message="edit a.py and b.py",
        model_text="```python\nx=1\n```",
        planner_edit_intent=True,
    ) is False


def test_has_patch_intent_detects_add_fix_and_implement_verbs():
    assert _has_patch_intent("add divide() to tests/fixtures/diff_accept_demo/calc.py")
    assert _has_patch_intent("fix bug in tests/fixtures/diff_accept_demo/calc.py")
    assert _has_patch_intent("implement divide in tests/fixtures/diff_accept_demo/calc.py")


def test_has_patch_intent_defaults_true_for_file_targets_unless_read_only():
    assert _has_patch_intent("tests/fixtures/diff_accept_demo/calc.py")
    assert _has_patch_intent("please update tests/fixtures/diff_accept_demo/calc.py")
    assert _has_patch_intent("show tests/fixtures/diff_accept_demo/calc.py") is False


def test_deep_find_keyed_string_emits_metric_for_nested_fallback():
    reset_metrics()
    payload = {
        "arguments": {
            "path": {
                "nested": {
                    "filepath": "demo.py",
                }
            }
        }
    }
    out = _deep_find_keyed_string(payload, ("filepath", "path"))
    assert out == "demo.py"
    snapshot = get_metrics_snapshot()
    assert any(key.startswith("tool_argument_deep_fallback_total|") for key in snapshot.keys())


def test_synth_write_does_not_trigger_without_explicit_file_target_or_read_observation():
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit this and add divide with guard",
        model_text=(
            "```python\n"
            "def divide(a, b):\n"
            "    return a / b\n"
            "```"
        ),
        tool_observations=[],
    )
    assert req is None


def test_merge_chat_prompt_and_buffered_lines_dedupes_chat_echo():
    merged = _merge_chat_prompt_and_buffered_lines(
        "Edit tests/fixtures/diff_accept_demo/calc.py",
        [
            "chat:",
            "Edit tests/fixtures/diff_accept_demo/calc.py",
            "chat",
            "",
        ],
    )
    assert merged == "Edit tests/fixtures/diff_accept_demo/calc.py"


def test_merge_chat_prompt_and_buffered_lines_keeps_real_multiline():
    merged = _merge_chat_prompt_and_buffered_lines(
        "Line 1",
        ["Line 2", "Line 3"],
    )
    assert merged == "Line 1 Line 2 Line 3"


def test_maybe_prepend_synth_write_triggers_on_zero_tool_calls_with_code_block():
    reqs, errs, inserted = _maybe_prepend_synth_write(
        tool_reqs=[],
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py: add divide",
        model_text=(
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```"
        ),
        planner_edit_intent=False,
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {
                    "path": "tests/fixtures/diff_accept_demo/calc.py",
                    "content": "def add(a, b):\n    return a + b\n",
                },
            }
        ],
        reason_if_empty="no_tool_calls_with_code_block",
        reason_if_non_mutating="non_mutating_candidates_only",
        debug_label="unit synth check",
    )
    assert inserted is True
    assert errs == []
    assert reqs
    assert reqs[0]["tool_name"] in {"FindAndReplace", "InsertAtEnd", "Write"}
    assert reqs[0]["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"
    assert _should_synth_write_from_model_text(
        user_message="edit a.py",
        model_text="```python\nx=1\n```\n```python\ny=2\n```",
        planner_edit_intent=True,
    ) is False


def test_maybe_prepend_synth_write_does_not_prepend_when_mutating_candidate_exists():
    reqs, errs, inserted = _maybe_prepend_synth_write(
        tool_reqs=[
            {
                "tool_name": "fs_apply_patch",
                "arguments": {"path": "x.py", "patch": "--- x.py\n+++ x.py\n@@ -1 +1 @@\n-x\n+y\n"},
            }
        ],
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py: add divide",
        model_text=(
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```"
        ),
        planner_edit_intent=False,
        tool_observations=[],
        reason_if_empty="no_tool_calls_with_code_block",
        reason_if_non_mutating="non_mutating_candidates_only",
        debug_label="unit synth mutating short-circuit",
    )
    assert inserted is False
    assert errs == []
    assert len(reqs) == 1
    assert reqs[0]["tool_name"] == "fs_apply_patch"


def test_maybe_prepend_synth_write_post_preprocess_path_uses_preprocess_result():
    def _reject_preprocess(_req: dict):
        return None, {"status": "noop", "error": "blocked_invalid_args"}

    reqs, errs, inserted = _maybe_prepend_synth_write(
        tool_reqs=[],
        user_message="Rewrite entire tests/fixtures/diff_accept_demo/calc.py with this code",
        model_text=(
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n"
            "```"
        ),
        planner_edit_intent=False,
        tool_observations=[],
        reason_if_empty="post_preprocess_no_tool_calls_with_code_block",
        reason_if_non_mutating="post_preprocess_non_mutating_only",
        debug_label="unit synth post-preprocess",
        preprocess=_reject_preprocess,
    )
    assert inserted is False
    assert reqs == []
    assert errs == [{"status": "noop", "error": "blocked_invalid_args"}]


def test_synth_write_metric_reasons_are_kept_distinct():
    reset_metrics()
    common_kwargs = dict(
        user_message="Rewrite entire tests/fixtures/diff_accept_demo/calc.py with this code",
        model_text="```python\nprint('x')\n```",
        planner_edit_intent=False,
        tool_observations=[],
        preprocess=None,
    )
    _maybe_prepend_synth_write(
        tool_reqs=[],
        reason_if_empty="no_tool_calls_with_code_block",
        reason_if_non_mutating="non_mutating_candidates_only",
        debug_label="reason-empty",
        **common_kwargs,
    )
    _maybe_prepend_synth_write(
        tool_reqs=[{"tool_name": "fs_read", "arguments": {"path": "tests/fixtures/diff_accept_demo/calc.py"}}],
        reason_if_empty="no_tool_calls_with_code_block",
        reason_if_non_mutating="non_mutating_candidates_only",
        debug_label="reason-non-mutating",
        **common_kwargs,
    )
    _maybe_prepend_synth_write(
        tool_reqs=[],
        reason_if_empty="post_preprocess_no_tool_calls_with_code_block",
        reason_if_non_mutating="post_preprocess_non_mutating_only",
        debug_label="reason-post-empty",
        **common_kwargs,
    )
    _maybe_prepend_synth_write(
        tool_reqs=[{"tool_name": "fs_read", "arguments": {"path": "tests/fixtures/diff_accept_demo/calc.py"}}],
        reason_if_empty="post_preprocess_no_tool_calls_with_code_block",
        reason_if_non_mutating="post_preprocess_non_mutating_only",
        debug_label="reason-post-non-mutating",
        **common_kwargs,
    )

    snapshot = get_metrics_snapshot()
    assert snapshot.get(
        "tool_synth_write_from_code_block_total|reason=no_tool_calls_with_code_block",
        0,
    ) >= 1
    assert snapshot.get(
        "tool_synth_write_from_code_block_total|reason=non_mutating_candidates_only",
        0,
    ) >= 1
    assert snapshot.get(
        "tool_synth_write_from_code_block_total|reason=post_preprocess_no_tool_calls_with_code_block",
        0,
    ) >= 1
    assert snapshot.get(
        "tool_synth_write_from_code_block_total|reason=post_preprocess_non_mutating_only",
        0,
    ) >= 1


def test_synth_write_skip_metrics_capture_false_paths():
    reset_metrics()
    # no explicit path + no read observation
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit this file and add divide",
        model_text="```python\ndef divide(a,b):\n    return a / b\n```",
        tool_observations=[],
    )
    assert req is None

    # multiple targets
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit a.py and b.py",
        model_text="```python\nprint('x')\n```",
        tool_observations=[],
        planner_edit_intent=True,
    )
    assert req is None

    # no code block
    req = _synth_write_tool_req_from_model_edit(
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py and add divide",
        model_text="def divide(a,b): return a/b",
        tool_observations=[],
    )
    assert req is None

    snapshot = get_metrics_snapshot()
    assert snapshot.get("tool_synth_write_skipped_total|reason=zero_targets", 0) >= 1
    assert snapshot.get("tool_synth_write_skipped_total|reason=multiple_targets", 0) >= 1
    assert snapshot.get("tool_synth_write_skipped_total|reason=zero_code_blocks", 0) >= 1


def test_write_replace_requires_explicit_intent_emits_typed_reason_metric(tmp_path: Path):
    reset_metrics()
    target = tmp_path / "demo.md"
    target.write_text("old\n", encoding="utf-8")
    prepared, err = _prepare_write_patch(
        workspace_root=str(tmp_path),
        arguments={"filepath": "demo.md", "content": "new\n"},
        user_message="update demo.md",
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "existing_file_replace_requires_explicit_intent"
    snapshot = get_metrics_snapshot()
    assert snapshot.get(
        "intent_validator_block_total|intent=full_replace,reason=existing_file_replace_requires_explicit_intent",
        0,
    ) >= 1


def test_replay_captured_payload_read_then_code_block_still_synthesizes_write():
    fixture = Path("tests/fixtures/diff_accept_demo/replay_payload_read_then_code.json")
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    turns = payload.get("turns", [])
    assert len(turns) == 2

    first = turns[0]
    second = turns[1]
    turn1_calls = _collect_tool_call_candidates(first.get("tool_calls"), str(first.get("text") or ""))
    assert turn1_calls
    assert _canonical_tool_name(str(turn1_calls[0].get("tool_name"))) == "fs_read"

    tool_observations = [
        {
            "tool_name": "fs_read",
            "result_status": "succeeded",
            "result_payload": {
                "path": "tests/fixtures/diff_accept_demo/calc.py",
                "content": "def add(a, b):\n    return a + b\n",
            },
        }
    ]
    turn2_calls = _collect_tool_call_candidates(second.get("tool_calls"), str(second.get("text") or ""))
    reqs, errs, inserted = _maybe_prepend_synth_write(
        tool_reqs=turn2_calls,
        user_message="Edit tests/fixtures/diff_accept_demo/calc.py: add divide(), add zero-division guard, and add docstrings to all functions.",
        model_text=str(second.get("text") or ""),
        planner_edit_intent=False,
        tool_observations=tool_observations,
        reason_if_empty="no_tool_calls_with_code_block",
        reason_if_non_mutating="non_mutating_candidates_only",
        debug_label="replay payload synth",
    )
    assert inserted is True
    assert errs == []
    assert reqs
    assert reqs[0]["tool_name"] in {"FindAndReplace", "InsertAtEnd", "Write"}
    assert reqs[0]["arguments"]["filepath"] == "tests/fixtures/diff_accept_demo/calc.py"


def test_replay_captured_payload_append_bottom_prefers_insert_at_end_and_append_only_patch(tmp_path: Path):
    fixture = Path("tests/fixtures/diff_accept_demo/replay_payload_append_bottom.json")
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    turns = payload.get("turns", [])
    assert len(turns) == 2

    root = tmp_path / "repo"
    root.mkdir()
    target = root / "ux-flow.md"
    target.write_text("line-a\nline-b\n", encoding="utf-8")

    second = turns[1]
    turn2_calls = _collect_tool_call_candidates(second.get("tool_calls"), str(second.get("text") or ""))
    reqs, errs, inserted = _maybe_prepend_synth_write(
        tool_reqs=turn2_calls,
        user_message="Append `1 + 1 = 2` at the bottom of ux-flow.md",
        model_text=str(second.get("text") or ""),
        planner_edit_intent=False,
        tool_observations=[
            {
                "tool_name": "fs_read",
                "result_status": "succeeded",
                "result_payload": {"path": "ux-flow.md", "content": "line-a\nline-b\n"},
            }
        ],
        reason_if_empty="no_tool_calls_with_code_block",
        reason_if_non_mutating="non_mutating_candidates_only",
        debug_label="replay append synth",
    )
    assert inserted is True
    assert errs == []
    assert reqs
    assert reqs[0]["tool_name"] == "InsertAtEnd"
    assert reqs[0]["arguments"]["filepath"] == "ux-flow.md"

    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req=reqs[0],
        user_message="Append `1 + 1 = 2` at the bottom of ux-flow.md",
        model_text=str(second.get("text") or ""),
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    patch = str(prepared["arguments"]["patch"])
    assert "+1 + 1 = 2" in patch
    assert "-line-a" not in patch
    assert "-line-b" not in patch


def test_is_temp_patch_smoke_intent_true_for_temp_file_patch_prompt():
    assert _is_temp_patch_smoke_intent("apply a tiny patch to a temp file in this repo")
    assert not _is_temp_patch_smoke_intent("apply a patch to src/main.py")


def test_is_patch_smoke_intent_allows_another_patch_without_target():
    assert _is_patch_smoke_intent("apply another patch")
    assert _is_patch_smoke_intent("apply a tiny patch")
    assert not _is_patch_smoke_intent("apply a patch to openvegas/cli.py")


def test_is_patch_repeat_followup_intent():
    assert _is_patch_repeat_followup_intent("apply another one")
    assert _is_patch_repeat_followup_intent("again")
    assert _is_patch_repeat_followup_intent("one more")
    assert not _is_patch_repeat_followup_intent("run shell command again")


def test_shell_command_hint_prefers_quoted_command():
    msg = 'run shell command "ls -la | head -20" and show output as it streams'
    assert _shell_command_hint_from_message(msg) == "ls -la | head -20"


def test_collect_tool_call_candidates_parses_function_payload():
    tool_calls = [
        {
            "id": "tc1",
            "function": {
                "name": "call_local_tool",
                "arguments": '{"tool_name":"fs_search","arguments":{"pattern":"x","path":"."}}',
            },
        }
    ]
    out = _collect_tool_call_candidates(tool_calls, "")
    assert len(out) == 1
    assert out[0]["tool_name"] == "fs_search"
    assert out[0]["arguments"]["pattern"] == "x"


def test_collect_tool_call_candidates_uses_function_name_and_top_level_args():
    tool_calls = [
        {
            "id": "tc2",
            "function": {
                "name": "Write",
                "arguments": '{"filepath":"README.md","content":"x\\n"}',
            },
        }
    ]
    out = _collect_tool_call_candidates(tool_calls, "")
    assert len(out) == 1
    assert out[0]["tool_name"] == "Write"
    assert out[0]["arguments"]["filepath"] == "README.md"
    assert out[0]["arguments"]["content"] == "x\n"


def test_preprocess_tool_request_recovers_fs_search_pattern_from_user_message():
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_search", "arguments": {}},
        user_message='search for "result_submission_hash" across this repo',
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["arguments"]["pattern"] == "result_submission_hash"
    assert prepared["arguments"]["path"] == "."
    assert prepared["arguments"]["max_files"] <= 500
    assert prepared["arguments"]["max_matches"] <= 200


def test_preprocess_tool_request_clamps_large_search_caps():
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "fs_search",
            "arguments": {"pattern": "x", "max_files": 999999, "max_matches": 999999},
        },
        user_message="search for x",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["arguments"]["max_files"] == 500
    assert prepared["arguments"]["max_matches"] == 200


def test_preprocess_tool_request_recovers_shell_command_from_user_message():
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "shell_run", "arguments": {}},
        user_message='run shell command "ls -la | head -20" and show output as it streams',
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["arguments"]["command"] == "ls -la | head -20"


def test_rewrite_shell_command_for_env_rewrites_rg_when_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("openvegas.cli.shutil.which", lambda cmd: None if cmd == "rg" else "/usr/bin/true")
    rewritten, reason = _rewrite_shell_command_for_env("rg 'pytest' .")
    assert rewritten.startswith("grep -R -n ")
    assert reason == "rg_unavailable_rewritten_to_grep"


def test_has_patch_intent_detects_create_file_intent():
    msg = "Create a temp file named .openvegas_audit.md in repo root"
    assert _has_patch_intent(msg)
    assert _is_file_create_intent(msg)


def test_file_scan_cache_is_bounded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_mod, "_FILE_SCAN_CACHE_MAX_ENTRIES", 2)
    cli_mod._FILE_SCAN_CACHE.clear()
    cli_mod._set_file_scan_cache("a", 1.0, [Path("/tmp/a")])
    cli_mod._set_file_scan_cache("b", 2.0, [Path("/tmp/b")])
    cli_mod._set_file_scan_cache("c", 3.0, [Path("/tmp/c")])
    assert len(cli_mod._FILE_SCAN_CACHE) == 2
    assert "a" not in cli_mod._FILE_SCAN_CACHE


def test_promote_tool_call_for_create_file_intent_upgrades_fs_read():
    name, args = _promote_tool_call_for_patch_intent(
        user_message="Create a temp file named .openvegas_audit.md in repo root with sections: Summary, Findings",
        tool_name="fs_read",
        arguments={},
        tool_observations=[],
    )
    assert name == "fs_apply_patch"
    assert args["path"] == ".openvegas_audit.md"
    assert "## Summary" in args["new_content"]


def test_semantic_tool_signature_ignores_timeout_noise_for_fs_read():
    sig_a = _semantic_tool_signature("fs_read", {"path": "README.md", "max_bytes": 1024}, "read_only")
    sig_b = _semantic_tool_signature("fs_read", {"path": "README.md", "max_bytes": 4096}, "read_only")
    assert sig_a == sig_b


def test_preprocess_fs_read_recovers_relative_filename_from_user_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "openvegas-infra-play.md"
    target.write_text("hello\n", encoding="utf-8")
    monkeypatch.chdir(root)
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_read", "arguments": {}},
        user_message="what do you think of your plan in here: openvegas-infra-play.md",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_read"
    assert prepared["arguments"]["path"] == "openvegas-infra-play.md"


def test_preprocess_fs_read_uses_hidden_filename_token_even_without_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_read", "arguments": {}},
        user_message="open .openvegas_tmp_patch.txt and show current contents",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_read"
    assert prepared["arguments"]["path"] == ".openvegas_tmp_patch.txt"


def test_preprocess_write_existing_file_generates_patch_and_meta(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "a.md"
    target.write_text("old\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "Write",
            "arguments": {"filepath": "a.md", "content": "new\n", "write_mode": "replace", "explicit_replace_intent": True},
        },
        user_message="replace the file a.md",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"
    assert "--- a.md" in prepared["arguments"]["patch"]
    assert prepared["_write_meta"]["existing_file"] is True
    assert prepared["_write_meta"]["path"] == "a.md"


def test_preprocess_write_new_file_create_path_no_existing_meta(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "Write", "arguments": {"filepath": "new.md", "content": "# hi\n"}},
        user_message="create new.md",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"
    assert "+++ new.md" in prepared["arguments"]["patch"]
    assert prepared["_write_meta"]["existing_file"] is False


def test_preprocess_write_existing_file_requires_explicit_replace_intent(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "a.md"
    target.write_text("old\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "Write", "arguments": {"filepath": "a.md", "content": "new\n"}},
        user_message="update a.md",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "existing_file_replace_requires_explicit_intent"


def test_preprocess_write_existing_file_allows_patch_file_with_wording(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "a.md"
    target.write_text("old\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "Write", "arguments": {"filepath": "a.md", "content": "new\n"}},
        user_message="Patch `a.md` with final findings",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"
    assert "--- a.md" in prepared["arguments"]["patch"]
    assert "+new" in prepared["arguments"]["patch"]
    assert prepared["_write_meta"]["existing_file"] is True
    assert prepared["_write_meta"]["explicit_replace_intent"] is True


def test_preprocess_find_and_replace_generates_patch(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "a.py"
    target.write_text("value = 1\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "FindAndReplace",
            "arguments": {"filepath": "a.py", "old_string": "value = 1\n", "new_string": "value = 2\n"},
        },
        user_message="edit a.py",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"
    assert "--- a.py" in prepared["arguments"]["patch"]
    assert "+value = 2" in prepared["arguments"]["patch"]
    assert prepared["_write_meta"]["operation_kind"] == "find_replace"


def test_preprocess_insert_at_end_generates_append_patch(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "a.md"
    target.write_text("alpha\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "InsertAtEnd", "arguments": {"filepath": "a.md", "content": "omega\n"}},
        user_message="append omega",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"
    assert "+omega" in prepared["arguments"]["patch"]
    assert "-alpha" not in prepared["arguments"]["patch"]
    assert prepared["_write_meta"]["operation_kind"] == "append"


def test_preprocess_write_out_of_bounds_is_blocked(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "Write",
            "arguments": {"filepath": "../outside.md", "content": "x\n", "write_mode": "replace", "explicit_replace_intent": True},
        },
        user_message="write file",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "workspace_path_out_of_bounds"


def test_preprocess_write_no_change_returns_noop(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "a.md"
    target.write_text("same\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "Write",
            "arguments": {"filepath": "a.md", "content": "same\n", "write_mode": "replace", "explicit_replace_intent": True},
        },
        user_message="rewrite file",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert prepared is None
    assert err is not None
    assert err["status"] == "noop"
    assert err["error"] == "no_change"


def test_preprocess_fs_apply_patch_preserves_patch_trailing_newline(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    patch_text = (
        "--- /dev/null\n"
        "+++ dummy.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+print('x')\n"
    )
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_apply_patch", "arguments": {"patch": patch_text}},
        user_message="apply patch",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"
    assert prepared["arguments"]["patch"] == patch_text
    assert prepared["arguments"]["patch"].endswith("\n")


def test_filter_patch_by_accepted_hunks_keeps_only_selected(tmp_path: Path):
    original = "a\nb\nc\n"
    updated = "a\nx\nc\ny\n"
    patch = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "x.txt", "old_content": original, "new_content": updated},
    )
    assert patch is not None
    filtered = _filter_patch_by_accepted_hunks(patch, {0})
    assert filtered is not None
    assert filtered.startswith("--- x.txt")


def test_filter_and_validation_share_same_surviving_hunk_definition():
    patch = "\n".join(
        [
            "--- a/a.txt",
            "+++ b/a.txt",
            "@@ -1,1 +1,1 @@",
            "-a",
            "+b",
            "--- a/b.txt",
            "+++ b/b.txt",
            "@@ -1,1 +1,1 @@",
            "-x",
            "+y",
            "",
        ]
    )
    filtered, parsed = _filter_patch_by_accepted_hunks_with_parsed(patch, {1})
    assert filtered is not None
    assert parsed is not None
    assert _is_valid_filtered_patch(filtered, parsed_patch=parsed) is True
    assert "a/a.txt" not in filtered
    assert "b/b.txt" in filtered


def test_retryable_mutation_error_contract_and_backoff():
    assert "stale_projection" in RETRYABLE_MUTATION_ERRORS
    assert "active_mutation_in_progress" in RETRYABLE_MUTATION_ERRORS
    assert "idempotency_conflict" in RETRYABLE_MUTATION_ERRORS
    assert _mutation_retry_backoff_sec("stale_projection", 0) == 0.0
    assert _mutation_retry_backoff_sec("active_mutation_in_progress", 0) > 0.0
    assert _mutation_retry_backoff_sec("idempotency_conflict", 1) > 0.0


def test_strict_abi_mode_rejects_noncanonical_tool_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENVEGAS_TOOL_ABI_MODE", "strict")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_read", "arguments": {"path": "README.md"}},
        user_message="read file",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "invalid_tool_arguments"


def test_strict_abi_mode_accepts_canonical_external_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENVEGAS_TOOL_ABI_MODE", "strict")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "Read", "arguments": {"filepath": "README.md"}},
        user_message="read file",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_read"


def test_strict_abi_mode_accepts_find_and_replace_external_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OPENVEGAS_TOOL_ABI_MODE", "strict")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "FindAndReplace",
            "arguments": {"filepath": "a.py", "old_string": "x = 1\n", "new_string": "x = 2\n"},
        },
        user_message="edit a.py",
        model_text="",
        workspace_root=str(root),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert prepared["tool_name"] == "fs_apply_patch"


def test_patch_recovery_retry_does_not_expand_to_unrelated_files(tmp_path: Path):
    original = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "a.txt", "old_content": "a\n", "new_content": "b\n"},
    )
    extra = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "b.txt", "old_content": "x\n", "new_content": "y\n"},
    )
    regenerated = f"{(original or '').rstrip()}\n{(extra or '').lstrip()}"
    reason = _validate_patch_recovery_scope(
        original_patch=original or "",
        regenerated_patch=regenerated,
    )
    assert reason == "patch_recovery_scope_expansion"


def test_patch_recovery_retry_rejected_when_scope_exceeds_drift_tolerance(tmp_path: Path):
    original = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "a.txt", "old_content": ("x\n" * 20), "new_content": ("x\n" * 19) + "y\n"},
    )
    regenerated = (
        "".join(
            difflib.unified_diff(
                ("x\n" * 20).splitlines(keepends=True),
                ("x\n" * 5) + "y\n" + ("x\n" * 14),
                fromfile="a.txt",
                tofile="a.txt",
                lineterm="",
            )
        )
        + "\n"
    )
    reason = _validate_patch_recovery_scope(
        original_patch=original or "",
        regenerated_patch=regenerated,
        drift_tolerance_lines=2,
        pair_max_anchor_distance_lines=2,
    )
    assert reason == "patch_recovery_scope_expansion"


def test_patch_recovery_retry_rejected_when_touched_lines_exceed_multiplier(tmp_path: Path):
    original = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "a.txt", "old_content": "a\nb\n", "new_content": "a\nc\n"},
    )
    regenerated = _synthesize_patch_from_arguments(
        str(tmp_path),
        {"path": "a.txt", "old_content": "a\nb\n", "new_content": "x\n" * 30},
    )
    reason = _validate_patch_recovery_scope(
        original_patch=original or "",
        regenerated_patch=regenerated or "",
        scope_multiplier=1.0,
        absolute_slack_lines=1,
    )
    assert reason == "patch_recovery_scope_expansion"


def test_patch_recovery_retry_rejected_when_hunks_cannot_be_paired_deterministically():
    original_patch = "\n".join(
        [
            "--- a.txt",
            "+++ a.txt",
            "@@ -10,1 +10,1 @@",
            "-a",
            "+b",
            "@@ -20,1 +20,1 @@",
            "-c",
            "+d",
            "",
        ]
    )
    regenerated_patch = "\n".join(
        [
            "--- a.txt",
            "+++ a.txt",
            "@@ -15,1 +15,1 @@",
            "-a",
            "+b",
            "@@ -15,1 +15,1 @@",
            "-c",
            "+d",
            "",
        ]
    )
    original_scope = _parse_patch_scope(original_patch)
    regen_scope = _parse_patch_scope(regenerated_patch)
    assert original_scope is not None and regen_scope is not None
    pairing = _pair_hunks_by_file_order_nearest(original_scope, regen_scope)
    quality = _pairing_quality(pairing)
    assert quality.is_partial is True
    reason = _validate_patch_recovery_scope(
        original_patch=original_patch,
        regenerated_patch=regenerated_patch,
    )
    assert reason == "patch_recovery_scope_expansion"


def test_patch_recovery_retry_rejected_when_pair_quality_exceeds_threshold():
    original_patch = "\n".join(
        [
            "--- a.txt",
            "+++ a.txt",
            "@@ -1,1 +1,1 @@",
            "-a",
            "+b",
            "",
        ]
    )
    regenerated_patch = "\n".join(
        [
            "--- a.txt",
            "+++ a.txt",
            "@@ -100,1 +100,1 @@",
            "-a",
            "+b",
            "",
        ]
    )
    reason = _validate_patch_recovery_scope(
        original_patch=original_patch,
        regenerated_patch=regenerated_patch,
        pair_max_anchor_distance_lines=8,
    )
    assert reason == "patch_recovery_scope_expansion"


def test_workflow_completion_gate_requires_artifact_sections(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / ".openvegas_ml_runbook.md"
    target.write_text("# Runbook\n\n## Summary\n\nok\n", encoding="utf-8")
    msg = (
        "Create `.openvegas_ml_runbook.md` in repo root with sections: "
        "Summary, Data Contracts, Pipeline DAG"
    )
    criteria = _build_completion_criteria(msg)
    assert criteria.requires_mutation is True
    eval_result = _evaluate_completion_criteria(criteria, str(root))
    assert not eval_result.satisfied
    assert any("heading:Data Contracts" in m for m in eval_result.missing)


def test_completion_gate_uses_nonempty_section_requirement(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / ".openvegas_ml_runbook.md"
    target.write_text(
        "\n".join(
            [
                "# Runbook",
                "",
                "## Summary",
                "done",
                "",
                "## Data Contracts",
                "",
                "## Pipeline DAG",
                "graph",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    msg = (
        "Create `.openvegas_ml_runbook.md` in repo root with sections: "
        "Summary, Data Contracts, Pipeline DAG"
    )
    criteria = _build_completion_criteria(msg)
    assert criteria.requires_mutation is True
    eval_result = _evaluate_completion_criteria(criteria, str(root))
    assert not eval_result.satisfied
    assert any(":empty:" in m or ":heading:Data Contracts" in m for m in eval_result.missing)


def test_completion_criteria_requires_mutation_for_edit_intent_file_prompt():
    msg = "Edit tests/fixtures/diff_accept_demo/calc.py and add divide"
    criteria = _build_completion_criteria(msg)
    assert "tests/fixtures/diff_accept_demo/calc.py" in criteria.required_files
    assert criteria.requires_mutation is True


def test_completion_criteria_requires_mutation_for_add_verb_file_prompt():
    msg = "Add divide() to tests/fixtures/diff_accept_demo/calc.py"
    criteria = _build_completion_criteria(msg)
    assert "tests/fixtures/diff_accept_demo/calc.py" in criteria.required_files
    assert criteria.requires_mutation is True


def test_completion_eval_marks_empty_section_when_heading_present(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "runbook.md"
    target.write_text(
        "\n".join(
            [
                "# Runbook",
                "",
                "## Data Contracts",
                "",
                "## Next",
                "ok",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    criteria = CompletionCriteria(
        required_files=("runbook.md",),
        required_headings={"runbook.md": ("Data Contracts",)},
        required_nonempty_sections={"runbook.md": ("Data Contracts",)},
    )
    eval_result = _evaluate_completion_criteria(criteria, str(root))
    assert not eval_result.satisfied
    assert any(m == "runbook.md:empty:Data Contracts" for m in eval_result.missing)


def test_patch_recovery_payload_preserves_original_and_retry_diagnostics():
    original = ToolExecutionResult(
        result_status="failed",
        result_payload={
            "ok": False,
            "reason_code": "tool_execution_failed",
            "patch_failure_code": "patch_context_mismatch",
            "patch_diagnostics": {"dry_run_rc": 1, "attempts": [{"p_level": 1}]},
        },
        stdout="orig out",
        stderr="orig err",
    )
    retry = ToolExecutionResult(
        result_status="failed",
        result_payload={
            "ok": False,
            "reason_code": "tool_execution_failed",
            "patch_failure_code": "patch_context_mismatch",
            "patch_diagnostics": {"dry_run_rc": 1, "attempts": [{"p_level": 0}]},
        },
        stdout="retry out",
        stderr="retry err",
    )
    patch = "\n".join(["--- a.txt", "+++ a.txt", "@@ -1,1 +1,1 @@", "-a", "+b", ""])
    payload = _patch_recovery_payload(
        reason_code="patch_recovery_failed",
        detail="Patch recovery retry failed.",
        original_outcome=original,
        retry_outcome=retry,
        original_patch=patch,
        regenerated_patch=patch,
    )
    assert payload["reason_code"] == "patch_recovery_failed"
    assert payload["original_reason_code"] == "patch_context_mismatch"
    assert payload["retry_reason_code"] == "patch_context_mismatch"
    assert payload["target_files"] == ["a.txt"]
    assert payload["hunk_count"] == 1
    assert isinstance(payload.get("original_patch_diagnostics"), dict)
    assert isinstance(payload.get("retry_patch_diagnostics"), dict)


def test_attempt_bootstrap_write_fallback_writes_new_markdown_file(tmp_path: Path):
    result = _attempt_bootstrap_write_fallback(
        workspace_root=str(tmp_path),
        rel_path=".openvegas_ml_runbook.md",
        new_contents="# Runbook\n",
        existing_file=False,
    )
    assert result is not None
    assert result.result_status == "succeeded"
    assert (tmp_path / ".openvegas_ml_runbook.md").read_text(encoding="utf-8") == "# Runbook\n"


def test_attempt_bootstrap_write_fallback_rejects_code_files(tmp_path: Path):
    result = _attempt_bootstrap_write_fallback(
        workspace_root=str(tmp_path),
        rel_path="openvegas/cli.py",
        new_contents="print('x')\n",
        existing_file=False,
    )
    assert result is None


def test_attempt_bootstrap_write_fallback_rejects_nested_paths(tmp_path: Path):
    result = _attempt_bootstrap_write_fallback(
        workspace_root=str(tmp_path),
        rel_path="docs/runbook.md",
        new_contents="# Runbook\n",
        existing_file=False,
    )
    assert result is None


def test_attempt_bootstrap_write_fallback_allows_existing_artifact_file(tmp_path: Path):
    target = tmp_path / ".openvegas_ml_runbook.md"
    target.write_text("existing\n", encoding="utf-8")
    result = _attempt_bootstrap_write_fallback(
        workspace_root=str(tmp_path),
        rel_path=".openvegas_ml_runbook.md",
        new_contents="# Replacement\n",
        existing_file=True,
    )
    assert result is not None
    assert result.result_status == "succeeded"
    assert target.read_text(encoding="utf-8") == "# Replacement\n"


def test_tool_result_reason_code_prefers_patch_failure_code():
    result = ToolExecutionResult(
        result_status="failed",
        result_payload={"reason_code": "tool_execution_failed", "patch_failure_code": "patch_parse_invalid"},
        stdout="",
        stderr="",
    )
    assert _tool_result_reason_code(result) == "patch_parse_invalid"
