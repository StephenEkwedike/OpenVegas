"""Real pinned Claude protocol test; only the upstream and wallet are synthetic."""
import asyncio
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path
from uuid import UUID

import pytest

from openvegas.integrations import windows_billing_host as host
from openvegas.integrations import windows_terminal as terminal
from openvegas.integrations.windows_native_image import pinned_windows_image

MODEL = 'claude-sonnet-4-6'
PROMPTS = ('OV_WINDOWS_FIRST', 'OV_WINDOWS_SECOND', 'OV_WINDOWS_THIRD')
ANSWERS = ('OV_HOST_ANSWER_ONE', 'OV_WALLET_ANSWER_TWO', 'OV_HOST_ANSWER_THREE')
MODES = ('host', 'v', 'host')


def _firewall_check(exe):
    # The CI lane installs this application-specific block before running
    # tests. Don't start a real assistant if that step or runner isolation is missing.
    assert os.environ.get('CI') == 'true' and os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    assert Path(exe) == Path(os.environ['OV_WINDOWS_CLAUDE_EXE'])
    ps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    check = """
$ErrorActionPreference = 'Stop'
$r = Get-NetFirewallRule -PolicyStore ActiveStore -Name 'OpenVegas-QA-claude.exe'
$app = $r | Get-NetFirewallApplicationFilter
$addr = $r | Get-NetFirewallAddressFilter
$profiles = @(Get-NetFirewallProfile)
if (!$profiles.Count -or @($profiles | Where-Object Enabled -ne True).Count -or
    $r.Enabled -ne 'True' -or $r.Direction -ne 'Outbound' -or $r.Action -ne 'Block' -or
    $r.Profile -ne 'Any' -or $app.Program -ine $env:OV_WINDOWS_CLAUDE_EXE -or
    (@($addr.RemoteAddress) -join ',') -ne 'Any') { exit 2 }
"""
    subprocess.run([str(ps), '-NoProfile', '-NonInteractive', '-Command', check], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def _sse(i):
    usage = {'input_tokens': 12, 'output_tokens': 1}
    msg = {'id': f'msg_ov_windows_{i}', 'type': 'message', 'role': 'assistant', 'model': MODEL,
           'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage}
    events = [
        ('message_start', {'message': msg}),
        ('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
        ('content_block_delta', {'index': 0, 'delta': {'type': 'text_delta', 'text': ANSWERS[i]}}),
        ('content_block_stop', {'index': 0}),
        ('message_delta', {'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': usage}),
        ('message_stop', {}),
    ]
    return ''.join('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **body}) + '\n\n'
                   for kind, body in events).encode()


def _validate_turn(i, mode, body, headers):
    # Inspect only content generated in this empty fixture profile. Never log
    # raw headers/native requests. Require exact role-separated prior turns.
    if (i >= 3 or mode != MODES[i] or body.get('model') != MODEL or body.get('stream') is not True
            or 'x-openvegas-local-auth' in headers):
        raise ValueError
    if mode == 'host':
        if headers.get('x-api-key') != host.SYNTHETIC_KEY or 'authorization' in headers:
            raise ValueError
    elif (not headers.get('authorization', '').startswith('Bearer ov_native_fixture_')
          or 'x-api-key' in headers):
        raise ValueError
    history = body['messages']
    turns = []
    for msg in history:
        text = json.dumps(msg['content'])
        markers = [p for p in (*PROMPTS, *ANSWERS) for _ in range(text.count(p))]
        if markers:
            turns.append((msg['role'], markers))
    expected = []
    for n in range(i + 1):
        expected.append(('user', [PROMPTS[n]]))
        if n < i:
            expected.append(('assistant', [ANSWERS[n]]))
    if turns != expected:
        raise ValueError


def test_fixture_requires_prior_answers_and_correct_payer():
    history = []
    for i, mode in enumerate(MODES):
        history.append({'role': 'user', 'content': PROMPTS[i]})
        headers = ({'x-api-key': host.SYNTHETIC_KEY} if mode == 'host' else
                   {'authorization': 'Bearer ov_native_fixture_' + 'a' * 32})
        body = {'model': MODEL, 'stream': True, 'messages': history}
        _validate_turn(i, mode, body, headers)
        if i:
            with pytest.raises(ValueError):
                _validate_turn(i, mode, {**body, 'messages': history[2:]}, headers)
        assert ANSWERS[i].encode() in _sse(i)
        history.append({'role': 'assistant', 'content': ANSWERS[i]})


@pytest.mark.skipif(sys.platform != 'win32' or not os.getenv('CI'), reason='disposable native Windows CI only')
@pytest.mark.asyncio
async def test_pinned_claude_stream_json_round_trip(monkeypatch):
    seen, faults, sessions = [], [], set()
    async def upstream(reader, writer):
        code, media, data = '400 Bad Request', 'application/json', b'{}'
        try:
            async with asyncio.timeout(8):
                head = await reader.readuntil(b'\r\n\r\n')
                first, *lines = head.decode('latin1').rstrip('\r\n').split('\r\n')
                method, route, _ = first.split(' ')
                route = route.split('?')[0]
                headers = {k.lower(): v.strip() for k, v in (line.split(':', 1) for line in lines)}
                size = int(headers['content-length'])
                if method != 'POST' or not 0 < size <= 2**20:
                    raise ValueError
                raw = await reader.readexactly(size)
                if headers.get('content-encoding') == 'gzip':
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    raw = decoder.decompress(raw, 4 * 2**20 + 1)
                    if not decoder.eof or decoder.unused_data:
                        raise ValueError
                if len(raw) > 4 * 2**20:
                    raise ValueError
                if route.endswith('/v1/messages/count_tokens'):
                    data = b'{"input_tokens":12}'
                else:
                    mode = {'/_host/v1/messages': 'host', '/integrations/native/v1/messages': 'v'}[route]
                    _validate_turn(len(seen), mode, json.loads(raw), headers)
                    data, media = _sse(len(seen)), 'text/event-stream'
                    seen.append(mode)
                code = '200 OK'
        except (OSError, ValueError, KeyError, TypeError, zlib.error, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as exc:
            faults.append(type(exc).__name__)
        finally:
            writer.write(f'HTTP/1.1 {code}\r\nContent-Type: {media}\r\nContent-Length: {len(data)}\r\n'
                         'Connection: close\r\n\r\n'.encode() + data)
            await writer.drain()
            writer.close()

    children = []
    def spawn(argv, env, cwd):
        # Pipe only this evidence run. Product Windows launches inherit their console.
        child = subprocess.Popen([*argv, '-p', '--bare', '--tools', '', '--model', MODEL,
                                  '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 cwd=cwd, env=env, bufsize=0)
        children.append(child)
        return child
    monkeypatch.setattr(host, '_spawn', spawn)
    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    public = asyncio.get_running_loop().create_future()
    async with server:
        with (pinned_windows_image('claude', os.environ['OV_WINDOWS_CLAUDE_EXE'], '2.1.322') as exe,
              host.fresh_environment('claude') as (env, cwd), terminal._directory() as directory):
            _firewall_check(exe)
            task = asyncio.create_task(host.run_routed('claude', exe, server.sockets[0].getsockname()[1],
                                                       env, cwd, directory, public.set_result))
            try:
                async with asyncio.timeout(150):
                    await asyncio.wait((public, task), return_when=asyncio.FIRST_COMPLETED)
                    if task.done():
                        await task
                        pytest.fail('Claude exited before local controls became available')
                    sid, child = public.result(), children[0]
                    for i, mode in enumerate(MODES):
                        assert await terminal.request(sid, mode if i else 'status') == mode
                        envelope = {'type': 'user', 'message': {'role': 'user', 'content': PROMPTS[i]}}
                        child.stdin.write(json.dumps(envelope).encode() + b'\n')
                        child.stdin.flush()
                        while True:
                            line = await asyncio.to_thread(child.stdout.readline, 2**20)
                            if not line or not line.endswith(b'\n'):
                                pytest.fail('Native Claude exited or produced an oversized frame')
                            event = json.loads(line)
                            if event.get('session_id'):
                                sessions.add(str(UUID(event['session_id'])))
                            if event.get('type') == 'result':
                                if (event.get('is_error') is not False or event.get('result') != ANSWERS[i]
                                        or not event.get('session_id')):
                                    pytest.fail('Native Claude did not return the selected fixture answer')
                                break
                        assert seen == list(MODES[:i + 1]) and not faults and len(sessions) == 1
                    child.stdin.close()
                    assert await task == 0 and len(children) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                for child in children:
                    child.stdin.close()
                    child.stdout.close()
