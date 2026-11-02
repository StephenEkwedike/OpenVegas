"""Actual installed host hook entry, isolated homes, synthetic endpoint only."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openvegas.integrations.native import child_environment, host_argv


@pytest.mark.skipif(os.getenv('OV_TEST_NATIVE_LAUNCHER_HOSTS') != '1', reason='Explicit installed-host loopback opt-in')
@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize('handled', [False, True])
def test_installed_prompt_hook_blocks_before_inference(host, tmp_path, monkeypatch, handled):
    binary = shutil.which(host)
    if sys.platform != 'darwin' or not binary:
        pytest.skip('Installed macOS native hosts required')
    home = tmp_path / 'home'; home.mkdir()
    config = home / 'config'; config.mkdir()
    work = tmp_path / 'work'; work.mkdir()
    root = Path(__file__).resolve().parents[2]
    capture = tmp_path / 'hook.json'
    script = tmp_path / 'hook.py'
    success = ('async def accepted(*args, **kwargs):\n return "Opening OpenVegas. No AI credits used."\n'
               'hook.__globals__["request"] = accepted\n') if handled else ''
    script.write_text('import sys,json,asyncio\nfrom pathlib import Path\n' +
        f'sys.path.insert(0,{str(root)!r})\nfrom openvegas.integrations.use import hook\n' +
        success +
        'data=json.load(sys.stdin)\n' +
        f'result=asyncio.run(hook({host!r},data))\nPath({str(capture)!r}).write_text(json.dumps(result))\nprint(json.dumps(result))\n')
    hooks = {'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': shlex.join([sys.executable, '-I', str(script)]), 'timeout': 5}]}]}}
    (config / 'hooks.json').write_text(json.dumps(hooks))
    (config / 'settings.json').write_text(json.dumps(hooks))
    calls = []
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.path)
            self.send_response(429); self.end_headers()
            self.wfile.write(b'{"error":{"message":"Offline fixture: no model permitted"}}')
        def do_GET(self):
            self.send_response(404); self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        monkeypatch.setenv('OPENVEGAS_HOST_HOME', str(home))
        monkeypatch.setenv('OPENVEGAS_HOST_CODEX_HOME', str(config))
        monkeypatch.setenv('OPENVEGAS_HOST_CLAUDE_CONFIG_DIR', str(config))
        for key in ('OPENVEGAS_SWITCH_SOCKET', 'OPENVEGAS_SWITCH_TOKEN'):
            monkeypatch.delenv(key, raising=False)
        url = f'http://127.0.0.1:{server.server_port}'
        model = 'anthropic/claude-haiku-4.5' if host == 'claude' else 'openai/gpt-4.1-nano'
        env = child_environment(host, url, model, 64, 'ov_native_offline_fixture_only')
        env.update(DISABLE_AUTOUPDATER='1', DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1')
        args = ['-p', '--output-format', 'json', '--', 'openvegas use v'] if host == 'claude' else [
            # Isolated, generated test hook only. Never ship this trust bypass
            # in the customer launcher; customers review OpenVegas in /hooks.
            '--no-daemon', '--dangerously-bypass-hook-trust', 'exec', '--skip-git-repo-check', '--sandbox', 'read-only', '--json', 'openvegas use v']
        argv = host_argv(binary, host, url, model, args, local_tools_only=host == 'codex')
        sandbox = '(version 1)(allow default)(deny network*)(allow network-outbound (remote ip "localhost:*"))(allow network-inbound (local ip "localhost:*"))'
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', sandbox, *argv], env=env, cwd=work,
                                capture_output=True, text=True, timeout=40)
        assert capture.exists(), (result.returncode, result.stdout[-1800:], result.stderr[-1800:])
        value = json.loads(capture.read_text())
        if handled:
            assert value.get('continue') is False and value.get('suppressOutput') is True
            assert 'decision' not in value
        else:
            assert value.get('decision') == 'block'
        assert not calls, (calls, result.stdout[-1000:], result.stderr[-1000:])
        print(host + ': installed pre-model hook stopped exact billing command; 0 provider POSTs')
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
