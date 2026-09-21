"""Structured metadata only. No native finality inferred from hook names."""

import pytest
from openvegas.emotes.adapters import adapt_codex_app_server, adapt_hook
from openvegas.emotes.controller import EmoteController, State
from openvegas.emotes.events import Phase
from openvegas.emotes.resources import PackRepository

IDENTITY = {"session_id": "session", "turn_id": "turn", "generation": 1, "sequence": 1, "event_id": "event"}


@pytest.mark.parametrize("provider,name", [("claude", "Stop"), ("gemini", "AfterAgent")])
@pytest.mark.parametrize("retry", [False, True, None, "false"])
def test_native_final_hooks_never_prove_success(provider, name, retry):
    payload = {"hook_event_name": name, "stop_hook_active": retry,
               "last_assistant_message": "done!", "prompt_response": "success!",
               "outcome": "success", "status": "completed"}
    assert adapt_hook(provider, payload, authoritative_success=True, **IDENTITY) is None


@pytest.mark.parametrize("payload", [{"prompt_id": "other"}, {"agent_id": "sub"},
                                    {"session_id": "other"}])
def test_identity_and_subagent_checks(payload):
    assert adapt_hook("claude", {"hook_event_name": "UserPromptSubmit", **payload}, **IDENTITY) is None


@pytest.mark.parametrize("payload", [{"prompt_id": "turn", "turn_id": "other"},
                                    {"session_id": "session", "thread-id": "other"},
                                    {"hook_event_name": []}, {"hook_event_name": {}}])
def test_conflicting_aliases_and_malformed_event_names_fail_closed(payload):
    assert adapt_hook("claude", {"hook_event_name": "UserPromptSubmit", **payload}, **IDENTITY) is None


@pytest.mark.parametrize("payload", [{"status": "failed"}, {"status": "interrupted"},
                                    {"error": {}}, {"cancelled": True}])
def test_codex_legacy_bridge_cannot_ignore_negative_metadata(payload):
    assert adapt_hook("codex", {"type": "agent-turn-complete", **payload},
                      authoritative_success=True, **IDENTITY) is None


def adapt(method, params, **identity):
    return adapt_codex_app_server({"method": method, "params": {"threadId": "session", **params}},
                                 host_version=(0, 153, 4), **{**IDENTITY, **identity})


@pytest.mark.parametrize("status,error,phase", [("completed", None, Phase.COMPLETE),
                                               ("failed", {"message": "secret"}, Phase.ERROR),
                                               ("interrupted", None, Phase.CANCEL),
                                               ("completed", {}, None), ("inProgress", None, None),
                                               ("unknown", None, None), (None, None, None)])
def test_codex_final_status_not_notification_name(status, error, phase):
    event = adapt("turn/completed", {"turn": {"id": "turn", "status": status, "error": error,
                                             "items": [{"text": "secret"}]}})
    assert (event.phase if event else None) == phase
    if event:
        assert b"secret" not in event.to_bytes()


@pytest.mark.parametrize("version", [None, [0, 153, 4], (0, 153, True), (0, 153, 3), (0, 154, 0)])
def test_codex_schema_gate(version):
    assert adapt_codex_app_server({"method": "turn/completed", "params": {
        "threadId": "session", "turn": {"id": "turn", "status": "completed"}}},
        host_version=version, **IDENTITY) is None


@pytest.mark.parametrize("method", ["item/completed", "hook/completed", "serverRequest/resolved",
                                   "thread/tokenUsage/updated", "turn/plan/updated"])
def test_non_turn_completion_not_success(method):
    assert adapt(method, {"turnId": "turn", "status": "completed"}) is None


def test_rapid_turn_approval_cancel_then_delayed_success_is_not_celebrated():
    controller = EmoteController(PackRepository().load("pixel-courier"), source="codex",
                                 session_id="session", clock=lambda: 0.0)
    def deliver(method, params, **identity):
        event = adapt(method, params, **identity)
        assert event is not None
        return controller.handle(event)
    assert deliver("turn/started", {"turn": {"id": "turn", "status": "inProgress"}})
    assert deliver("item/commandExecution/requestApproval", {"turnId": "turn"}, sequence=2, event_id="e2")
    assert controller.current_state == State.PAUSED
    assert deliver("turn/completed", {"turn": {"id": "turn", "status": "interrupted"}}, sequence=3, event_id="e3")
    deliver("turn/completed", {"turn": {"id": "turn", "status": "completed"}}, sequence=4, event_id="e4")
    assert controller.current_state == State.CANCELLED
    assert deliver("turn/started", {"turn": {"id": "second", "status": "inProgress"}},
                   turn_id="second", generation=2, sequence=1, event_id="e5")
    assert not deliver("turn/completed", {"turn": {"id": "turn", "status": "completed"}},
                       sequence=5, event_id="e6")
    assert controller.current_state == State.ACTIVE


def test_recoverable_error_does_not_finish_turn_or_resume_approval():
    assert adapt("error", {"turnId": "turn", "willRetry": True, "error": {"message": "private"}}) is None
    assert adapt("error", {"turnId": "turn", "willRetry": False}).phase == Phase.ERROR
    assert adapt("turn/completed", {"threadId": "other", "turn": {"id": "turn", "status": "completed"}}) is None
    assert adapt("turn/completed", {"turn": {"id": "other", "status": "completed"}}) is None
