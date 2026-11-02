"""Opt-in native Codex resume fixture; synthetic history, local server, no paid calls.

Requires macOS sandbox-exec. Never controls VS Code or reads personal auth/history.
"""

import asyncio
import fcntl
import json
import os
import pty
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
from openvegas.integrations.native import codex_resume_argv

requests = []
auth_headers = []


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
            async with route(str(wrapper), backend, "gpt-5.6-luna", env) as endpoint:  # noqa: SIM117
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
            async with route(str(wrapper), backend, "gpt-5.6-luna", env) as endpoint:
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
                    assert "ORIGINAL_CONTEXT_MARKER" in json.dumps(result)
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
                p = subprocess.Popen(  # noqa: ASYNC220 - Real disposable PTY, not the user's terminal.
                    codex_resume_argv(str(wrapper), endpoint, "gpt-5.6-luna", ["resume", sid]),
                    env=env,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=workspace,
                    start_new_session=True,
                )
                os.close(slave)
                output = b""
                end = time.monotonic() + 18
                while time.monotonic() < end and p.poll() is None:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            b = os.read(master, 65536)
                        except OSError:
                            break
                        output += b
                        if b"\x1b[6n" in b:
                            os.write(master, b"\x1b[1;1R")
                    await asyncio.sleep(0.02)
                print(
                    "native_tui_alive",
                    p.poll() is None,
                    "context_displayed",
                    b"ORIGINAL_CONTEXT_MARKER" in output,
                    "mismatch",
                    b"modelProviderMismatch" in output,
                    flush=True,
                )
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=3)
                os.close(master)
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
