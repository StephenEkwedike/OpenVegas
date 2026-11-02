"""Opt-in native Codex resume fixture; synthetic history, local server, no paid calls.

Requires macOS sandbox-exec. Never controls VS Code or reads personal auth/history.
"""

import asyncio
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.asyncio.client import unix_connect

from openvegas.integrations.codex_billing_route import route, rpc
from openvegas.integrations.credentials import IntegrationError
from openvegas.integrations.native import codex_resume_argv, launch

requests = []
auth_headers = []
ALLOWED_MODELS = [{'provider': 'openai', 'host': 'codex', 'model': 'gpt-5.6-luna'}]


async def read_pty(master, process, seconds, until=None):
    output = b''
    end = time.monotonic() + seconds
    while time.monotonic() < end and process.poll() is None:
        readable, _, _ = select.select([master], [], [], 0.1)
        if readable:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            output += chunk
            if b'\x1b[6n' in chunk:
                os.write(master, b'\x1b[1;1R')
            if until is not None and until in output:
                break
        await asyncio.sleep(0.02)
    return output


def visible_text(output):
    return re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', output).decode('utf-8', errors='replace')


def event(kind, **kw):
    return "event: " + kind + "\ndata: " + json.dumps({"type": kind, **kw}) + "\n\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        requests.append(body)
        auth_headers.append(self.headers.get("Authorization"))
        item = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "ORIGINAL_CONTEXT_MARKER", "annotations": []}
            ],
        }
        response = {
            "id": "resp_test",
            "object": "response",
            "model": "gpt-5.6-luna",
            "status": "completed",
            "output": [item],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        payload = (
            event("response.created", response={**response, "status": "in_progress", "output": []})
            + event("response.output_item.added", output_index=0, item=item)
            + event(
                "response.output_text.delta",
                output_index=0,
                content_index=0,
                item_id=item["id"],
                delta="ORIGINAL_CONTEXT_MARKER",
            )
            + event("response.output_item.done", output_index=0, item=item)
            + event("response.completed", response=response)
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload.encode())))
        self.end_headers()
        self.wfile.write(payload.encode())


async def main():
    if os.environ.get("OV_TEST_NATIVE_RESUME") != "1":
        raise SystemExit(
            "Set OV_TEST_NATIVE_RESUME=1 for isolated, synthetic native-host verification."
        )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="ov-resume-", dir="/tmp") as root:
            h = Path(root)
            home = h / "codex"
            home.mkdir()
            workspace = h / "project"
            workspace.mkdir()
            original = '{"OPENAI_API_KEY":"fixture_original_personal_not_real"}\n'
            (home / "auth.json").write_text(original)
            (home / "config.toml").write_text(
                'model_provider="openai"\nmodel="gpt-5.6-luna"\n[projects.'
                + json.dumps(str(workspace.resolve()))
                + ']\ntrust_level="trusted"\n[tui]\nscreen_reader_detection_done=true\n'
            )
            original_config = (home / "config.toml").read_bytes()
            exe = shutil.which("codex")
            wrapper = h / "host"
            policy = (
                '(version 1)(allow default)(deny network*)(allow network-inbound (local unix-socket))(allow network-outbound (remote unix-socket))(allow network-outbound (remote ip "localhost:'
                + str(server.server_port)
                + '"))'
            )
            import shlex

            wrapper.write_text(
                "#!/bin/sh\nexec /usr/bin/sandbox-exec -p "
                + shlex.quote(policy)
                + " "
                + shlex.quote(exe)
                + ' "$@"\n'
            )
            wrapper.chmod(0o700)
            env = {
                "PATH": os.environ["PATH"],
                "HOME": root,
                "CODEX_HOME": str(home),
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
                "OPENVEGAS_NATIVE_TOKEN": "ov_native_fake_private_test",
            }
            backend = "http://127.0.0.1:" + str(server.server_port)
            os.chdir(workspace)
            async with route(str(wrapper), backend, "gpt-5.6-luna", env,  # noqa: SIM117
                             allowed_models=ALLOWED_MODELS) as endpoint:
                async with unix_connect(endpoint.removeprefix("unix://")) as ws:
                    await rpc(
                        ws,
                        1,
                        "initialize",
                        {"clientInfo": {"name": "ov-native-test", "version": "1"}},
                    )
                    await ws.send(json.dumps({"method": "initialized", "params": {}}))
                    result = await rpc(
                        ws,
                        2,
                        "thread/start",
                        {
                            "model": "gpt-5.6-luna",
                            "cwd": str(workspace),
                            "approvalPolicy": "on-request",
                            "sandbox": "read-only",
                        },
                    )
                    sid = result["thread"]["id"]
                    print("created_provider", result["thread"]["modelProvider"], flush=True)
                    await rpc(
                        ws,
                        3,
                        "turn/start",
                        {
                            "threadId": sid,
                            "input": [
                                {
                                    "type": "text",
                                    "text": "ORIGINAL_USER_MARKER",
                                    "text_elements": [],
                                }
                            ],
                        },
                    )
                    async with asyncio.timeout(25):
                        while True:
                            v = json.loads(await ws.recv())
                            if v.get("method") == "turn/completed":
                                print("first_turn", v["params"]["turn"]["status"], flush=True)
                                break
            history = list(home.glob("sessions/**/*.jsonl"))
            before = {str(p): p.read_bytes() for p in history}
            print("historyfiles", len(history), flush=True)
            assert requests
            request_count = len(requests)
            for provider, arguments in (
                ('openai', ['--no-daemon', '--sandbox', 'read-only', 'resume', sid]),
                ('custom-provider', ['--no-daemon', 'resume', sid]),
            ):
                original_arguments = list(arguments)
                try:
                    await launch('codex', 'gpt-5.6-luna', '10', provider='openai',
                                 resume_provider=provider, wallet_mode=True, host_args=arguments,
                                 local_tools_only=True, yes=True)
                except IntegrationError as exc:
                    assert ('permission overrides' in str(exc) if provider == 'openai'
                            else 'original built-in openai' in str(exc))
                else:
                    raise AssertionError('Unsupported handoff reached native launch')
                assert arguments == original_arguments
            assert len(requests) == request_count
            print('permission_and_custom_provider_preflight_rejected', True,
                  'original_args_preserved', True, 'new_requests', 0, flush=True)
            async with route(str(wrapper), backend, "gpt-5.6-luna", env,
                             allowed_models=ALLOWED_MODELS) as endpoint:
                async with unix_connect(endpoint.removeprefix("unix://")) as ws:
                    await rpc(
                        ws,
                        1,
                        "initialize",
                        {"clientInfo": {"name": "ov-native-test", "version": "1"}},
                    )
                    await ws.send(json.dumps({"method": "initialized", "params": {}}))
                    result = await rpc(ws, 2, "thread/resume", {"threadId": sid})
                    print("resumed_provider", result["thread"]["modelProvider"], flush=True)
                    assert result['approvalPolicy'] == 'on-request', 'Saved approval policy changed'
                    assert result['sandbox']['type'] == 'readOnly', 'Saved read-only policy changed'
                    print('saved_remote_policy', result['approvalPolicy'], result['sandbox']['type'], flush=True)
                    assert "ORIGINAL_CONTEXT_MARKER" in json.dumps(result)
                    listing = await rpc(ws, 3, 'model/list', {'includeHidden': True})
                    assert [row['model'] for row in listing['data']] == ['gpt-5.6-luna']
                    native_model = listing['data'][0]
                    assert native_model['upgrade'] is None
                    print('reviewed_native_catalog', [row['model'] for row in listing['data']],
                          'efforts', [level['reasoningEffort']
                                      for level in native_model['supportedReasoningEfforts']], flush=True)
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
                host_args = ['--no-daemon', 'resume', sid]
                original_host_args = list(host_args)
                argv = codex_resume_argv(str(wrapper), endpoint, 'gpt-5.6-luna', host_args)
                assert '--no-daemon' not in argv and host_args == original_host_args
                p = subprocess.Popen(  # noqa: ASYNC220 - Real disposable PTY, not the user's terminal.
                    argv,
                    env=env,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=workspace,
                    start_new_session=True,
                )
                os.close(slave)
                try:
                    output = await read_pty(master, p, 18, until=b'ORIGINAL_CONTEXT_MARKER')
                    assert b'ORIGINAL_CONTEXT_MARKER' in output, visible_text(output)[-2500:]
                    await read_pty(master, p, 1)
                    request_count = len(requests)
                    # Separate typing from Enter so native paste-burst detection
                    # does not treat the command as a pasted multiline prompt.
                    os.write(master, b'/model')
                    await read_pty(master, p, .5)
                    os.write(master, b'\r')
                    menu = visible_text(await read_pty(master, p, 3))
                    if not ('GPT-5.6-Luna' in menu or 'gpt-5.6-luna' in menu):
                        print('model_menu_failure', repr(menu[-2500:]), flush=True)
                    assert 'GPT-5.6-Luna' in menu or 'gpt-5.6-luna' in menu, menu[-2500:]
                    assert 'gpt-6' not in menu.lower() and 'GPT-6' not in menu
                    print('native_model_menu_reviewed_only', True, flush=True)
                    os.write(master, b'\r')
                    effort_menu = visible_text(await read_pty(master, p, 2))
                    if not ('reasoning' in effort_menu.lower() or 'effort' in effort_menu.lower()):
                        print('effort_menu_failure', repr(effort_menu[-2500:]), flush=True)
                    assert 'reasoning' in effort_menu.lower() or 'effort' in effort_menu.lower(), effort_menu[-2500:]
                    for option in native_model['supportedReasoningEfforts']:
                        if option['reasoningEffort'] == 'max':
                            continue  # The native picker puts Max in its own submenu.
                        assert option['description'].lower() in effort_menu.lower(), effort_menu[-2500:]
                    if any(option['reasoningEffort'] == 'max'
                           for option in native_model['supportedReasoningEfforts']):
                        assert 'More reasoning' in effort_menu
                        # Initial selection is Medium; High, Extra high, More.
                        os.write(master, b'\x1b[B\x1b[B\x1b[B')
                        await read_pty(master, p, .3)
                        os.write(master, b'\r')
                        more_menu = visible_text(await read_pty(master, p, 1))
                        # Native rendering emits cell diffs. A resize forces a
                        # complete repaint instead of guessing missing letters.
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 141, 0, 0))
                        os.killpg(p.pid, signal.SIGWINCH)
                        more_menu += visible_text(await read_pty(master, p, .5))
                        assert 'max' in more_menu.lower(), more_menu[-2500:]
                        os.write(master, b'\x1b')
                        await read_pty(master, p, .3)
                    os.write(master, b'\x1b')
                    await read_pty(master, p, .5)
                    os.write(master, b'\x1b')
                    await read_pty(master, p, .5)
                    assert len(requests) == request_count, 'Native menus submitted a model request'
                    print('native_effort_menu_verified', True, 'menu_provider_calls', 0, flush=True)
                    print('resume_no_daemon_sanitized', True, 'restore_args_preserved',
                          host_args == original_host_args, flush=True)
                    native_alive = p.poll() is None
                except AssertionError as exc:
                    print('native_rehearsal_assertion', str(exc), flush=True)
                    raise
                finally:
                    if p.poll() is None:
                        os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=5)
                    os.close(master)
                print(
                    "native_tui_alive",
                    native_alive,
                    "context_displayed",
                    b"ORIGINAL_CONTEXT_MARKER" in output,
                    "mismatch",
                    b"modelProviderMismatch" in output,
                    flush=True,
                )
                assert b"modelProviderMismatch" not in output
                assert b"Meet GPT" not in output
                assert b"ORIGINAL_CONTEXT_MARKER" in output, output[-2500:]
            assert (home / "auth.json").read_text() == original
            assert (home / "config.toml").read_bytes() == original_config
            assert 'model_provider="openai"' in (home / "config.toml").read_text()
            assert all(Path(p).read_bytes().startswith(data) for p, data in before.items())
            restored = await asyncio.create_subprocess_exec(
                str(wrapper),
                "-c",
                'model_provider="openai"',
                "-c",
                "openai_base_url=" + json.dumps(backend + "/host/v1"),
                "exec",
                "resume",
                sid,
                "--skip-git-repo-check",
                "--model",
                "gpt-5.6-luna",
                "HOST_RETURN_MARKER",
                env={k: v for k, v in env.items() if k != "OPENVEGAS_NATIVE_TOKEN"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(restored.communicate(), 30)
            assert restored.returncode == 0, stderr[-1500:]
            assert "HOST_RETURN_MARKER" in json.dumps(requests[-1])
            assert "ORIGINAL_CONTEXT_MARKER" in json.dumps(requests[-1])
            assert auth_headers[-1] == "Bearer fixture_original_personal_not_real"
            assert auth_headers[0] == "Bearer ov_native_fake_private_test"
            assert all(body.get('model') == 'gpt-5.6-luna' for body in requests)
            assert (home / "auth.json").read_text() == original
            print(
                "PASS: native resume/TUI context, host return with original auth, preserved history, no external requests",
                flush=True,
            )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
