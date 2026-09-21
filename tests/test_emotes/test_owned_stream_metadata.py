"""Privacy projection and the actual owner-API/watcher boundary."""

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
import subprocess
import sys

import pytest
from click.testing import CliRunner

from openvegas.emotes import owned_stream as owned
from openvegas.emotes.commands import emote
from openvegas.emotes.events import Event, Phase
from openvegas.emotes.spool import MAX_QUEUE, EventSpool

VERSION = (0, 153, 4)


def test_projection_never_visits_content_and_does_not_mutate_input():
    class Content:
        def __str__(self):
            pytest.fail("content stringified")
        def __iter__(self):
            pytest.fail("content traversed")
        def __bool__(self):
            pytest.fail("content inspected")

    content = Content()
    turn = {"id": "turn", "status": "completed", "error": content, "items": content}
    payload = {"method": "turn/completed", "params": {"threadId": "thread", "turn": turn},
               "prompt": content, "token": content}
    projected = owned.project_codex_lifecycle(payload, host_version=VERSION)
    assert asdict(projected) == {
        "thread_id": "thread", "turn_id": "turn", "method": "turn/completed",
        "status": "completed", "has_error": True, "will_retry": None,
    }
    assert projected.event(VERSION) is None
    assert payload["params"]["turn"] is turn
    assert turn["items"] is content
    assert not hasattr(projected, "__dict__")


@pytest.mark.parametrize("method", owned.PAUSE_METHODS)
def test_approval_projection_contains_no_tool_input_or_response(method):
    projected = owned.project_codex_lifecycle({
        "method": method, "id": "secret-request-id",
        "params": {"threadId": "thread", "turnId": "turn", "command": "secret command",
                   "cwd": "/secret/path", "questions": ["secret question"],
                   "permissions": {"secret": True}, "itemId": "secret item"},
    }, host_version=VERSION)
    assert projected.event(VERSION).phase == Phase.PAUSE
    assert "secret" not in json.dumps(asdict(projected))


@pytest.mark.parametrize("status,phase", [("inProgress", None), ("completed", Phase.COMPLETE),
                                         ("failed", Phase.ERROR), ("interrupted", Phase.CANCEL)])
def test_projection_reuses_pinned_structured_semantics(status, phase):
    projected = owned.project_codex_lifecycle({
        "method": "turn/completed", "params": {"threadId": "thread", "turn": {
            "id": "turn", "status": status, "error": None, "items": [{"text": "secret"}],
        }},
    }, host_version=VERSION)
    event = projected.event(VERSION)
    assert (event.phase if event else None) == phase
    assert "secret" not in json.dumps(asdict(projected))


@pytest.mark.parametrize("status", [None, [], {}, "future-status", "success prose"])
def test_unknown_status_is_neutral_not_copied(status):
    projected = owned.project_codex_lifecycle({
        "method": "turn/completed", "params": {"threadId": "thread", "turn": {
            "id": "turn", "status": status,
        }},
    }, host_version=VERSION)
    assert projected.status == "unknown"
    assert projected.event(VERSION) is None


@pytest.mark.parametrize("payload", [
    None, [], {}, {"method": []}, {"method": "turn/started", "params": []},
    {"method": "turn/started", "params": {"threadId": "thread", "turn": {"id": "bad id"}}},
    {"method": "turn/completed", "params": {"threadId": "thread", "turn": []}},
    {"method": "error", "params": {"threadId": "thread", "turnId": "turn", "willRetry": "false"}},
    {"method": "error", "params": {"threadId": "thread", "turnId": "turn"}},
    {"method": "serverRequest/resolved", "params": {"threadId": "thread", "turnId": "turn"}},
    {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn"}},
    {"method": "item/agentMessage/delta", "params": {"delta": "success"}},
    {"method": "turn/start", "params": {"input": "prompt"}},
])
def test_unknown_or_malformed_payloads_never_create_metadata(payload):
    assert owned.project_codex_lifecycle(payload, host_version=VERSION) is None


def test_projection_unknown_version_and_error_bodies():
    payload = {"method": "error", "params": {"threadId": "thread", "turnId": "turn",
                                            "willRetry": False, "error": {"message": "secret"}}}
    assert owned.project_codex_lifecycle(payload, host_version=(0, 154, 0)) is None
    projected = owned.project_codex_lifecycle(payload, host_version=VERSION)
    assert projected.event(VERSION).phase == Phase.ERROR
    assert "secret" not in json.dumps(asdict(projected))


@pytest.mark.parametrize("changes", [
    {"thread_id": "x" * 129}, {"turn_id": "\x1bsecret"}, {"method": "turn/start"},
    {"method": ["turn/started"]}, {"status": []}, {"has_error": 0},
    {"will_retry": "false"}, {"prompt": "secret"},
])
def test_metadata_cannot_expand_schema(changes):
    args = {"thread_id": "thread", "turn_id": "turn", "method": "turn/started",
            "status": "inProgress", **changes}
    with pytest.raises((ValueError, TypeError)):
        owned.CodexLifecycleMetadata(**args)


def test_spool_capacity_failure_blocks_later_success(tmp_path):
    spool = EventSpool(tmp_path / "events")
    auth = owned.OwnedStreamAuthorization("host", "stream", "thread", "watch", VERSION,
                                          owns_stream=True, ordered=True, metadata_only=True)
    stream = owned.OwnedCodexStream(auth, enabled=True, spool=spool)
    metadata = owned.CodexLifecycleMetadata("thread", "turn", "turn/started", "inProgress")
    assert stream.deliver(owned.OwnedStreamRecord("host", "stream", 0, metadata))
    start = Event.from_bytes(next(spool.directory.glob("*.json")).read_bytes())
    # Saturate the same turn with non-evictable edges, as an external same-user
    # publisher can do. Do not modify spool limits or bypass its permissions.
    for index in range(1, MAX_QUEUE):
        assert spool.publish(replace(start, phase=Phase.PAUSE, sequence=index, event_id=f"e-{index}"))
    pause = owned.CodexLifecycleMetadata("thread", "turn", "item/tool/requestUserInput")
    assert not stream.deliver(owned.OwnedStreamRecord("host", "stream", 1, pause))
    assert stream.reason == "publication_failed"
    assert len(list(spool.directory.glob("*.json"))) == MAX_QUEUE
    spool.drain(source="codex", session_id="watch")
    final = replace(metadata, method="turn/completed", status="completed")
    assert not stream.deliver(owned.OwnedStreamRecord("host", "stream", 2, final))
    assert not spool.drain(source="codex", session_id="watch")


def test_bridge_rejects_raw_payloads_instead_of_parsing_them(tmp_path):
    auth = owned.OwnedStreamAuthorization("host", "stream", "thread", "watch", VERSION,
                                          owns_stream=True, ordered=True, metadata_only=True)
    spool = EventSpool(tmp_path / "events")
    stream = owned.OwnedCodexStream(auth, enabled=True, spool=spool)
    assert not stream.deliver({"prompt": "secret", "method": "turn/completed"})
    assert stream.reason == "invalid_record"
    assert not spool.directory.exists()


def test_actual_command_help_has_watcher_but_no_attachment_or_owned_stream_launcher():
    runner = CliRunner()
    help_result = runner.invoke(emote, ["watch", "--help"])
    assert help_result.exit_code == 0
    assert "--source" in help_result.output and "--session" in help_result.output
    assert "SECOND terminal" in help_result.output
    assert "Startup discards queued events" in help_result.output
    assert "owned-stream" not in emote.commands
    assert "attach" not in emote.commands


def test_isolated_import_and_disabled_observer_do_not_touch_host(tmp_path):
    root = Path(owned.__file__).resolve().parents[2]
    code = r'''
import socket, sys
sys.path.insert(0, sys.argv[1])
def deny(*args, **kwargs):
    raise RuntimeError("forbidden side effect")
socket.create_connection = socket.getaddrinfo = deny
socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = deny
def audit(event, args):
    if event == "subprocess.Popen":
        deny()
    if event == "import" and args[0] in (
        "openvegas.cli", "openvegas.config", "openvegas.telemetry", "dotenv",
    ):
        deny()
    if event == "open" and isinstance(args[0], str) and args[0].rsplit("/", 1)[-1] in (
        ".env", "config.toml", "settings.json",
    ):
        deny()
sys.addaudithook(audit)
from openvegas.emotes.owned_stream import OwnedCodexStream, OwnedStreamAuthorization
auth = OwnedStreamAuthorization("owner", "stream", "thread", "watch", (0, 153, 4))
bridge = OwnedCodexStream(auth)
assert not bridge.enabled
bridge.close()
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root)], cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": os.defpath}, stdin=subprocess.DEVNULL,
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == b""
    assert not list(tmp_path.iterdir())
