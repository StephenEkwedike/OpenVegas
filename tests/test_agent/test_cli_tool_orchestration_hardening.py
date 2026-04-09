from __future__ import annotations

from pathlib import Path

from openvegas.cli import _canonical_tool_name
from openvegas.cli import _preprocess_tool_request_for_runtime
from openvegas.cli import _promote_tool_call_for_patch_intent


def test_loop_finalize_never_crashes_on_thread_update():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "async def _force_finalize(" in src
    assert "nonlocal current_thread_id" in src


def test_patch_intent_promotes_second_fs_list_to_apply_patch():
    tool_name, args = _promote_tool_call_for_patch_intent(
        user_message="apply a tiny patch to a temp file in this repo",
        tool_name="fs_list",
        arguments={"path": "."},
        tool_observations=[{"tool_name": "fs_list"}],
    )
    assert tool_name == "fs_apply_patch"
    assert args["path"] == ".openvegas_tmp_patch.txt"


def test_unknown_tool_alias_fs_create_maps_to_apply_patch():
    assert _canonical_tool_name("fs_create") == "fs_apply_patch"


def test_unknown_tool_after_canonicalization_forces_finalize():
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "totally_unknown_tool", "arguments": {}},
        user_message="do a thing",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert prepared is None
    assert err is not None
    assert err["error"] == "unknown_tool_name"


def test_max_iteration_forces_final_answer_no_empty_response():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "OPENVEGAS_CHAT_MAX_TOOL_STEPS" in src
    assert "Stopped after max tool iterations ({max_tool_steps})." in src
    assert 'reason="max_iterations"' in src


def test_tool_loop_processes_one_tool_call_per_step():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "tool_batch_truncated_total" in src
    assert "preprocessed_calls = preprocessed_calls[:1]" in src


def test_result_failure_attempts_tool_cancel_cleanup():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "await client.agent_tool_cancel(" in src


def test_write_patch_failure_has_regen_retry_path():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "tool_apply_patch_retry_total" in src
    assert "_prepare_write_patch(" in src
    assert "regen_args = dict(regen.get(\"arguments\", {}))" in src
    assert "_validate_patch_recovery_scope(" in src
    assert "patch_recovery_scope_expansion" in src
    assert "loop_action = await _finalize_or_continue_with_intercept(" in src
    assert "LoopAction.FINALIZED" in src


def test_fs_apply_patch_patch_recovery_from_alias_payload():
    prepared, err = _preprocess_tool_request_for_runtime(
        tool_req={
            "tool_name": "fs_apply_patch",
            "arguments": {
                "file": ".openvegas_tmp_patch.txt",
                "content": "openvegas orchestration hardening\n",
            },
        },
        user_message="apply a tiny patch to a temp file in this repo",
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err is None
    assert prepared is not None
    assert isinstance(prepared["arguments"].get("patch"), str)
    assert prepared["arguments"]["patch"].strip()


def test_fs_search_pattern_recovery_from_alias_and_user_quoted_text():
    prepared_alias, err_alias = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_search", "arguments": {"query": "result_submission_hash"}},
        user_message='search for "ignored"',
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err_alias is None
    assert prepared_alias is not None
    assert prepared_alias["arguments"]["pattern"] == "result_submission_hash"

    prepared_quote, err_quote = _preprocess_tool_request_for_runtime(
        tool_req={"tool_name": "fs_search", "arguments": {}},
        user_message='search for "result_submission_hash" across this repo',
        model_text="",
        workspace_root=str(Path.cwd()),
        tool_observations=[],
    )
    assert err_quote is None
    assert prepared_quote is not None
    assert prepared_quote["arguments"]["pattern"] == "result_submission_hash"


def test_write_existing_path_invokes_show_diff_before_propose():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "client.ide_message(" in src
    assert "method=\"show_diff_interactive\"" in src
    assert "write_meta.get(\"existing_file\")" in src
    assert "bridge_caps.get(\"connected\")" in src
    assert "tool_show_diff_skipped_total" in src
    assert "is_valid_show_diff_payload(" in src
    assert "tool_diff_fallback_total" in src


def test_noop_preprocess_path_force_finalizes_completed():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "if any(str(obs.get(\"status\")) == \"noop\" for obs in tool_observations)" in src
    assert "reason_if_finalize=\"completed\"" in src
    assert "_continue_or_finalize_for_completion(" in src


def test_completion_gate_and_stall_reasoning_are_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "CompletionCriteria" in src
    assert "_continue_or_finalize_for_completion(" in src
    assert "completion_criteria_unmet_after_retries" in src
    assert "workflow_stalled_no_new_observations" in src
    assert "if did_any_execution:" in src
    assert "reason_if_finalize=\"completed\"" in src


def test_duplicate_append_same_payload_block_guard_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "duplicate_append_same_payload_blocked" in src
    assert "duplicate_mutation_block_total" in src
    assert "successful_append_payload_fingerprints" in src


def test_active_mutation_wait_refresh_and_timeout_reason_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "await _wait_for_unlock_and_refresh()" in src
    assert "await client.agent_run_get(current_run_id)" in src
    assert "active_mutation_timeout" in src


def test_streamed_shell_finalization_prompt_is_compressed():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "streamed_tools_seen" in src
    assert "If shell output was already streamed live" in src
    assert "\"output_streamed\": bool(streamed_tools_seen.get(tool_name))" in src


def test_patch_failure_regen_retry_generalized_path():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "_validate_patch_recovery_scope(" in src
    assert "patch_recovery_scope_expansion" in src
    assert "patch_recovery_failed" in src


def test_completion_gate_does_not_finalize_on_partial_artifact_creation():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "completion_criteria_unmet" in src
    assert "_continue_or_finalize_for_completion(" in src
    assert "completion_criteria_unmet_after_retries" in src


def test_show_diff_skipped_when_bridge_unavailable():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "bridge_caps" in src
    assert "tool_show_diff_skipped_total" in src
    assert "bridge_unavailable" in src
    assert "OPENVEGAS_TERMINAL_DIFF_FALLBACK" in src
    assert "review_patch_terminal(" in src
    assert "normalize_show_diff_result(" in src
    assert "tool_terminal_diff_invoked_total" in src


def test_streamed_shell_final_answer_is_compressed():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "If shell output was already streamed live" in src
    assert "\"output_streamed\": bool(streamed_tools_seen.get(tool_name))" in src


def test_active_mutation_wait_resume_succeeds_after_unlock():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "await _wait_for_unlock_and_refresh()" in src
    assert "pending_retry_tool_req" in src
    assert "continue" in src


def test_active_mutation_wait_resume_times_out_with_typed_error():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "active_mutation_timeout" in src
    assert "OPENVEGAS_ACTIVE_MUTATION_TIMEOUT_SEC" in src


def test_active_mutation_refreshes_fence_before_retry():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "await client.agent_run_get(current_run_id)" in src
    assert "_update_fence(snap if isinstance(snap, dict) else None)" in src


def test_same_intent_patch_failure_circuit_breaker_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "OPENVEGAS_PATCH_FAILURE_REPEAT_LIMIT" in src
    assert "patch_recovery_failed_same_intent_circuit_break" in src
    assert "tool_apply_patch_same_intent_fail_total" in src


def test_filtered_patch_becomes_canonical_signature_input():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "_filter_patch_by_accepted_hunks_with_parsed(" in src
    assert "arguments[\"patch\"] = filtered_patch" in src
    assert "call_key = _semantic_tool_signature(tool_name, arguments" in src


def test_invalid_filtered_patch_blocks_before_execution():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "_is_valid_filtered_patch(" in src
    assert "Filtered patch was invalid after hunk decisions." in src
    assert "\"error\": \"user_declined_edit\"" in src


def test_bootstrap_write_fallback_is_narrow_and_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "_attempt_bootstrap_write_fallback(" in src
    assert "_is_artifact_whole_file_fallback_target(" in src
    assert "bootstrap_whole_file" in src


def test_no_tool_branch_exits_without_second_finalize_when_completion_inactive():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "if step == 0 and not completion_criteria.active and not edit_intent and not tool_observations:" in src
    assert "return True" in src
    assert "finalizing/continuing with text-only answer after synth/fallback checks" in src


def test_inline_attachment_warning_only_for_non_workspace_turns():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "turn_is_workspace_intent = _has_workspace_tooling_intent(message)" in src
    assert "_detect_auto_attach_paths(" in src
    assert "if not pending_attachments and not turn_is_workspace_intent:" in src


def test_direct_one_shot_renders_warning_and_web_diagnostics():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "warning_text = str(one_shot.get(\"warning\") or \"\").strip()" in src
    assert "ws_req = bool(one_shot.get(\"web_search_requested\", ws_req_default))" in src


def test_chat_attachment_commands_and_transcript_export_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "if cmd == \"/attach\":" in src
    assert "if cmd == \"/detach\":" in src
    assert "if cmd == \"/clear-attachments\":" in src
    assert "if cmd == \"/cancel-uploads\":" in src
    assert "if cmd == \"/retry-failed\":" in src
    assert "if cmd == \"/export-transcript\":" in src
    assert "chat_transcript.append(" in src


def test_chat_legend_command_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "/legend - show icon/status legend" in src
    assert "if cmd == \"/legend\":" in src
    assert "web: requested=" in src
    assert "_should_enable_web_search_for_turn(" in src


def test_pre_dispatch_image_input_capability_block_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "has_image_attachment = any(" in src
    assert "resolve_capability(current_provider, current_model, \"image_input\")" in src
    assert "image input unavailable" in src


def test_context_disabled_warning_is_one_time_and_explicit():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "context_warning_emitted" in src
    assert "thread_status" in src
    assert "context_enabled" in src
    assert "server context is disabled" in src


def test_chat_web_command_and_status_diagnostics_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "/web - show effective web search status (always on)" in src
    assert "if cmd == \"/web\":" in src
    assert "Web Search Requested" in src
    assert "Web Search Effective" in src


def test_chat_web_capability_unavailable_pre_dispatch_message_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "Web search is always on in chat." in src
    assert "resolve_capability(" in src
    assert "enable_web_search=web_search_effective_turn" in src


def test_chat_scrape_request_rewrite_and_refusal_retry_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "def _is_scrape_request(" in src
    assert "def _is_scrape_refusal_text(" in src
    assert "def _rewrite_lookup_request_for_safe_web_search(" in src
    assert "web_search_preview (live web lookup)" in src
    assert "chat-ask-retry-" in src


def test_chat_attachment_commands_and_lifecycle_present():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "class AttachmentState" in src
    assert "class PendingAttachment" in src
    assert "if cmd == \"/attach\":" in src
    assert "if cmd == \"/detach\":" in src
    assert "if cmd == \"/clear-attachments\":" in src
    assert "if cmd == \"/cancel-uploads\":" in src
    assert "if cmd == \"/retry-failed\":" in src
    assert "if cmd == \"/attachments\":" in src
    assert "upload_started" in src
    assert "upload_succeeded" in src
    assert "client.upload_init(" in src
    assert "client.upload_complete(" in src
    assert "uploaded_attachment_cache[file_key]" in src
    assert "attachments=attachment_file_ids_for_turn" in src
    assert "Detected file names in your prompt but they are not attached" in src


def test_chat_non_workspace_queries_use_direct_no_tool_path():
    src = Path("openvegas/cli.py").read_text(encoding="utf-8")
    assert "def _has_workspace_tooling_intent(" in src
    assert "idempotency_key=f\"chat-direct-" in src
    assert "enable_tools=False" in src
