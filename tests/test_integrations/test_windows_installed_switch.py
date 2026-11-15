"""Real foreground clients and customer controls; only remote financial services are synthetic."""
import asyncio
import json
import os
import re
import sys
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from scripts import verify_windows_conpty as conpty
from scripts import verify_windows_customer as qa
from tests.test_integrations import test_windows_claude_stream_native as claude
from tests.test_integrations import test_windows_codex_payer_roundtrip as codex


@asynccontextmanager
async def supplier(host):
    if host == 'codex':
        async with codex.supplier() as (sink, port):
            sink.token = qa.GRANT
            yield sink, port
        return
    seen, faults = [], []

    async def http(reader, writer):
        status, media, data = 400, 'application/json', b'{}'
        try:
            async with asyncio.timeout(10):
                head = (await reader.readuntil(b'\r\n\r\n')).decode('latin1').split('\r\n')
                method, route, _ = head[0].split()
                headers = {k.lower(): v.strip() for k, v in (s.split(':', 1) for s in head[1:] if s)}
                path = route.partition('?')[0]
                n = int(headers.get('content-length', '0'))
                if method != 'POST' or not 0 < n <= 2**20:
                    raise ValueError
                body = await reader.readexactly(n)
                if headers.get('content-encoding') == 'gzip':
                    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    body = dec.decompress(body, 4 * 2**20 + 1)
                    if not dec.eof or dec.unused_data or len(body) > 4 * 2**20:
                        raise ValueError
                if path.endswith('/v1/messages/count_tokens'):
                    data = b'{"input_tokens":12}'
                else:
                    mode = {'/_host/v1/messages': 'host', '/integrations/native/v1/messages': 'v'}[path]
                    payload = json.loads(body)
                    if any('/ov-billing' in json.dumps(m['content']).lower()
                           for m in payload['messages'] if m['role'] == 'user'):
                        raise ValueError
                    claude._validate_turn(len(seen), mode, payload, headers)
                    data, media = claude._sse(len(seen)), 'text/event-stream'
                    seen.append(mode)
                status = 200
        except (ValueError, KeyError, TypeError, OSError, TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, zlib.error):
            faults.append('unexpected-request')
        try:
            writer.write(f'HTTP/1.1 {status} Result\r\nContent-Type: {media}\r\nContent-Length: {len(data)}\r\n'
                         'Connection: close\r\n\r\n'.encode() + data)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(http, '127.0.0.1', 0)
    async with server:
        yield (seen, faults), server.sockets[0].getsockname()[1]


def configure(host, exe, env, cwd):
    from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY
    env.update(PATH=str(Path(exe).parent) + os.pathsep + env['PATH'],
               TERM='xterm-256color', PYTHON_DOTENV_DISABLED='1', OPENVEGAS_LOAD_PROJECT_ENV='0')
    for key in ('PROGRAMFILES', 'PROGRAMDATA', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
                'SYSTEMDRIVE', 'ALLUSERSPROFILE', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)'):
        if key in os.environ:
            env[key] = os.environ[key]
    root = Path(env['USERPROFILE'])
    env.update(HOMEDRIVE=root.drive, HOMEPATH=str(root)[len(root.drive):])
    # Shell known-folder resolution expands %USERPROFILE% and checks existence.
    for key, subdir in (('LOCALAPPDATA', 'Local'), ('APPDATA', 'Roaming')):
        path = root / 'AppData' / subdir
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    if host == 'codex':
        home = Path(env['CODEX_HOME'])
        (home / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': SYNTHETIC_KEY}))
        (home / 'config.toml').write_text(
            'model_provider="openai"\nmodel="gpt-6-astra"\nmodel_reasoning_effort="low"\n'
            'cli_auth_credentials_store="file"\nsandbox_mode="read-only"\napproval_policy="on-request"\n'
            'web_search="disabled"\ncheck_for_update_on_startup=false\n'
            '[projects.' + json.dumps(str(cwd)) + ']\ntrust_level="trusted"\n'
            '[tui]\nscreen_reader_detection_done=true\n', encoding='utf-8')
    else:
        home = Path(env['CLAUDE_CONFIG_DIR'])
        env.update(ANTHROPIC_MODEL=qa.MODELS['claude'], DISABLE_ERROR_REPORTING='1',
                   CLAUDE_CODE_GIT_BASH_PATH=os.environ['OV_WINDOWS_BASH_EXE'])
        git_cmd = Path(os.environ['OV_WINDOWS_BASH_EXE']).parents[1] / 'cmd'
        env['PATH'] += os.pathsep + str(git_cmd)
        (home / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True, 'theme': 'dark',
            'customApiKeyResponses': {'approved': [SYNTHETIC_KEY[-20:]]},
            'projects': {Path(cwd).resolve().as_posix(): {'hasTrustDialogAccepted': True}}}), encoding='utf-8')


def drive(host, sink, port, checkpoint):
    from openvegas.integrations import windows_billing_host as launcher
    from openvegas.integrations.inference_router_adapter import VERSIONS
    from openvegas.integrations.windows_native_image import pinned_windows_image

    entry = os.environ['OV_WINDOWS_' + host.upper() + '_EXE']
    checkpoint('isolation')
    conpty.require_hosted_lane()
    with (pinned_windows_image(host, entry, VERSIONS[host]) as exe,
          launcher.fresh_environment(host) as (env, cwd)):
        configure(host, exe, env, cwd)
        checkpoint('conpty')
        with conpty.isolated_console(Path(qa.__file__), [host, exe, str(port)], env, cwd) as term:
            checkpoint('native-start')
            term.expect(r'Windows managed ' + host, 'managed launch')
            checkpoint('composer')
            term.expect(r'OpenAI\s+Codex|Claude\s+Code|shortcuts|for help|Sonnet\s+4\.6', 'native composer')
            time.sleep(.75)
            for i, mode in enumerate(('host', 'v', 'host')):
                prefix = 'openvegas ' if host == 'codex' else '/ov-billing '
                if i:
                    # The native TUI draws text deltas before the completed-turn event.
                    time.sleep(1)
                    checkpoint('switch-' + mode)
                    term.type_line(prefix + ('use ' if host == 'codex' else '') + mode)
                    term.expect(r'Payer:\s*' + mode + r'\.\s*No prompt sent\.', 'confirmed switch')
                checkpoint('status-' + str(i))
                term.type_line(prefix + 'status')
                term.expect(r'Payer:\s*' + mode + r';\s*\$V\s*73\.25\.', 'confirmed balance and payer')
                checkpoint('answer-' + str(i))
                if host == 'codex':
                    sink.prompt = 'ov-synthetic-turn-' + str(i)
                    term.type_line(sink.prompt)
                    answer = term.expect(r'fixture-reply-[0-9a-f]{32}', 'native answer')
                    assert answer == sink.last_answer
                else:
                    term.type_line(claude.PROMPTS[i])
                    term.expect(re.escape(claude.ANSWERS[i]), 'native answer')
            checkpoint('shutdown')
            term.type_line('/exit')
            term.require_success()


@pytest.mark.skipif(sys.platform != 'win32', reason='requires disposable Windows ConPTY runner')
@pytest.mark.parametrize('host', ['codex', 'claude'])
@pytest.mark.asyncio
async def test_installed_customer_switch(host, request):
    def checkpoint(name):
        request.node.user_properties.append(('qa_stage', name))

    conpty.require_hosted_lane()
    async with supplier(host) as (sink, port):
        await asyncio.to_thread(drive, host, sink, port, checkpoint)
    if host == 'codex':
        assert not sink.rejections and [r['payer'] for r in sink.rows] == ['host', 'v', 'host']
        assert all(r['wire_model'] == qa.MODELS['codex'] and r['wire_effort'] == 'low' for r in sink.rows)
        assert all(r['prior_reply_linked'] for r in sink.rows[1:])
    else:
        assert sink == (['host', 'v', 'host'], [])


def test_bad_bank_request_cannot_issue_or_revoke():
    from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL
    bank = qa.Bank('claude')
    with httpx.Client(transport=httpx.MockTransport(bank), base_url=FALLBACK_DEFAULT_BACKEND_URL) as client:
        assert client.get('/integrations/account').status_code == 403
        assert client.post('/integrations/native/sessions', json={},
                           headers={'authorization': 'Bearer ' + qa.DEVICE}).status_code == 403
        assert bank.state == 'initial'


def test_console_keeps_coalesced_composer_and_answers_split_query():
    class Stream:
        chunks = iter(['Windows managed Claude\r\nClaude Code', '\x1b[', '6nPayer: host'])

        def read(self, *_, **kwargs):
            return next(self.chunks)

        def write(self, value):
            assert value == '\x1b[1;1R'

    terminal = conpty.Terminal(Stream())
    for text in ('Windows managed Claude', 'Claude Code', 'Payer: host'):
        assert terminal.expect(text, 'test') == text
