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
from scripts.qa_loopback_bank import bank_for
from tests.test_integrations import test_windows_claude_stream_native as claude
from tests.test_integrations import test_windows_codex_payer_roundtrip as codex


def claude_input_failure(payload, prompt):
    messages = payload['messages']
    if not messages or messages[-1].get('role') != 'user':
        return conpty.Failure.INPUT
    for msg in messages:
        if msg['role'] != 'user':
            continue
        content = msg['content']
        blocks = [content] if isinstance(content, str) else [b.get('text', '') for b in content]
        for block in blocks:
            if '/ov-billing' in block.lower() and not (
                    block.strip().startswith('<system-reminder>') and block.strip().endswith('</system-reminder>')):
                return conpty.Failure.FORWARDED
    content = messages[-1]['content']
    correct = content == prompt if isinstance(content, str) else any(
        b.get('type') == 'text' and b.get('text') == prompt for b in content)
    return None if correct else conpty.Failure.INPUT


@asynccontextmanager
async def supplier(host):
    if host == 'codex':
        async with codex.supplier(foreground_threads=lambda: {}) as (sink, port):
            sink.token = qa.GRANT
            yield sink, port
        return
    seen, faults, armed = [], [], []

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
                    reason = (conpty.Failure.UNSOLICITED if armed != [len(seen)]
                              else claude_input_failure(payload, claude.PROMPTS[len(seen)]))
                    if reason:
                        faults.append(reason)
                        raise ValueError
                    claude._validate_turn(len(seen), mode, payload, headers)
                    armed.clear()
                    data, media = claude._sse(len(seen)), 'text/event-stream'
                    seen.append(mode)
                status = 200
        except (ValueError, KeyError, IndexError, AttributeError, TypeError, OSError, TimeoutError, asyncio.IncompleteReadError,
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
        yield (seen, faults, armed), server.sockets[0].getsockname()[1]


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
            'projects': {Path(cwd).as_posix(): {'hasTrustDialogAccepted': True}}}), encoding='utf-8')


def canonical_fixture(launcher, host, env, cwd):
    # Windows TEMP can use an 8.3 alias. Give the child and its synthetic
    # profile one path spelling; never add a second trust entry or trust a parent.
    root = Path(env['USERPROFILE']).resolve(strict=True)
    work = root / 'work'
    if (not re.fullmatch('ov-rehearsal-[0-9a-f]{32}', root.name)
            or not work.samefile(cwd) or any(work.iterdir())):
        raise ValueError('Expected the empty disposable fixture workspace.')
    return launcher._fixture_env(host, root, Path(env['SystemRoot'])), str(work)


def drive(host, sink, bank, bank_port, checkpoint):
    from openvegas.integrations import windows_billing_host as launcher
    from openvegas.integrations.inference_router_adapter import VERSIONS
    from openvegas.integrations.windows_native_image import pinned_windows_image

    entry = os.environ['OV_WINDOWS_' + host.upper() + '_EXE']
    checkpoint('isolation')
    conpty.require_hosted_lane()
    with (pinned_windows_image(host, entry, VERSIONS[host]) as exe,
          launcher.fresh_environment(host) as (env, cwd)):
        checkpoint('fixture-canonical')
        env, cwd = canonical_fixture(launcher, host, env, cwd)
        checkpoint('fixture-configure')
        configure(host, exe, env, cwd)
        if host == 'codex':
            bank.catalog = qa.bundled_catalog(exe, env=env, cwd=cwd)
            # Out-of-band observations from the owned app-server, not provider
            # headers. Files exist only inside the disposable job profile.
            from openvegas.emotes._windows_state import private_windows_directory
            def witnessed():
                result = {}
                with private_windows_directory(Path(env['TMP'])) as state:
                    for source, files in qa.THREAD_FILES.items():
                        ids = []
                        for filename in files:
                            try:
                                ids.append(state.read(filename, 36).decode('ascii'))
                            except FileNotFoundError:
                                break
                        if source == 'user' and not ids:
                            return {}
                        result[source] = ids[0] if source == 'user' else ids
                return result
            sink.lookup = witnessed
        checkpoint('conpty')
        script = Path(qa.__file__).with_name('qa_customer_console.py')
        args = [host, os.environ['OV_QA_NODE'], os.environ['OV_QA_APP'], str(bank_port)]
        with conpty.isolated_console(script, args, env, cwd) as term:
            checkpoint('native-start')
            refusals = {
                'Windows native ownership, selection or routing could not be verified.': conpty.Failure.LOCAL_GATE,
                'Managed Windows billing unavailable. No automatic payer fallback.': conpty.Failure.MANAGED_START,
                'Native Windows host not found on an absolute PATH entry.': conpty.Failure.HOST_MISSING,
            }
            banner = term.expect('|'.join(map(re.escape, ['Windows managed ' + host, *refusals])),
                                 'managed launch', diagnose='managed-start')
            for message, reason in refusals.items():
                if message.casefold() == banner.casefold():
                    raise conpty.CheckpointMiss(reason)
            checkpoint('composer')
            # Some ConPTY versions consume Claude's device-attributes probe,
            # delaying first paint for 60s. Do not extend switch deadlines.
            term.expect(r'OpenAI\s+Codex|Claude\s+Code|shortcuts|for help|Sonnet\s+4\.6', 'native composer',
                        timeout=90 if host == 'claude' else 45,
                        diagnose='claude-start' if host == 'claude' else None)
            if host == 'claude':
                # Registration is asynchronous. An early logo does not mean an
                # immediate slash command can already be submitted.
                term.expect(r'OpenVegas billing controls (?:ready|registered;\s*readiness unconfirmed)\.',
                            'registered local billing command', timeout=90, diagnose='claude-start')
            else:
                time.sleep(.75)
            for i, mode in enumerate(('host', 'v', 'host')):
                prefix = 'openvegas ' if host == 'codex' else '/ov-billing '
                if i:
                    # The native TUI draws text deltas before the completed-turn event.
                    time.sleep(1)
                    checkpoint('switch-' + mode)
                    term.type_line(prefix + ('use ' if host == 'codex' else '') + mode)
                    try:
                        term.expect(r'Payer:\s*' + mode + r'\.\s*No prompt sent\.', 'confirmed switch', diagnose='switch')
                    except conpty.CheckpointMiss:
                        if host == 'codex':
                            from openvegas.emotes._windows_state import private_windows_directory
                            with private_windows_directory(Path(env['TMP'])) as state:
                                try:
                                    value = state.read(qa.CONTROL_RECEIPT, 32)
                                except FileNotFoundError:
                                    value = b''
                            for check in qa.CONTROL_CHECKS:
                                if value == check.encode('ascii'):
                                    raise conpty.CheckpointMiss(check) from None
                        raise
                checkpoint('status-' + str(i))
                if host == 'claude':
                    refusal = conpty.screen_failure('claude-start', term.frame())
                    if refusal:
                        raise conpty.CheckpointMiss(refusal)
                term.type_line(prefix + 'status')
                balance = qa.AFTER_WALLET if i == 2 else qa.BALANCE
                term.expect(r'Payer:\s*' + mode + r';\s*\$V\s*' + re.escape(balance) + r'\.', 'confirmed balance and payer',
                            diagnose='claude-control' if host == 'claude' else None)
                checkpoint('answer-' + str(i))
                if host == 'codex':
                    sink.prompt = 'ov-synthetic-turn-' + str(i)
                    term.type_line(sink.prompt)
                    answer = term.expect(r'fixture-reply-[0-9a-f]{32}', 'native answer')
                    assert answer == sink.last_answer
                else:
                    seen, faults, armed = sink
                    if faults or armed or len(seen) != i:
                        raise conpty.CheckpointMiss(next((f for f in faults if isinstance(f, conpty.Failure)),
                                                         conpty.Failure.PROTOCOL))
                    armed.append(i)
                    term.type_line(claude.PROMPTS[i])
                    try:
                        term.expect(re.escape(claude.ANSWERS[i]), 'native answer', diagnose='answer')
                    except conpty.CheckpointMiss:
                        reason = next((f for f in faults if isinstance(f, conpty.Failure)), None) or (
                            conpty.Failure.PROTOCOL if faults else conpty.Failure.DRAW if len(seen) > i else
                            conpty.Failure.NOT_REACHED)
                        raise conpty.CheckpointMiss(reason) from None
            checkpoint('shutdown')
            # Like the preceding switch, exit belongs after the completed turn,
            # not the streamed text delta. Never send a second exit or confirm blindly.
            time.sleep(1)
            term.type_line('/exit')
            term.require_success()


def codex_receipt_failure(sink):
    # Three correctly displayed answers do not excuse a wrong payer or extra request.
    fault = conpty.Failure
    if [r['payer'] for r in sink.rows] != ['host', 'v', 'host']:
        return fault.ROUTE
    if any(r['auth_matched'] is not True or codex.PATHS.get(r['path']) != r['payer'] for r in sink.rows):
        return fault.ROUTE
    if any(r['wire_model'] != qa.MODELS['codex'] or r['wire_effort'] != 'low' for r in sink.rows):
        return fault.MODEL
    if not all(r['prior_reply_linked'] for r in sink.rows[1:]):
        return fault.ROOT
    if sink.rejections:
        return {'route_or_auth': fault.ROUTE, 'not_current_deliberate_prompt': fault.UNSOLICITED,
                'previous_native_reply_missing': fault.ROOT, 'wire_model_or_effort': fault.MODEL
                }.get(sink.rejections[0], fault.PROTOCOL)
    return None


@pytest.mark.skipif(sys.platform != 'win32', reason='requires disposable Windows ConPTY runner')
@pytest.mark.parametrize('host', ['codex', 'claude'])
@pytest.mark.asyncio
async def test_installed_customer_switch(host, request):
    last_stage = None
    def checkpoint(name):
        nonlocal last_stage
        last_stage = name
        request.node.user_properties.append(('qa_stage', name))

    conpty.require_hosted_lane()
    try:
        async with supplier(host) as (sink, port), bank_for(host, supplier_port=port) as (bank, hits, bank_port):
            try:
                await asyncio.to_thread(drive, host, sink, bank, bank_port, checkpoint)
            finally:
                rows = sink.rows if host == 'codex' else sink[0]
                for key, value in [('supplier_answers', len(rows)), ('bank_http', len(hits)),
                                   ('bank_issues', bank.events.count('issue')),
                                   ('bank_revokes', bank.events.count('revoke'))]:
                    request.node.user_properties.append((key, str(value)))
            required = ['options', 'quote', 'issue', 'verify', *(['catalog'] if host == 'codex' else []), 'revoke']
            if (bank.state != 'closed' or bank.events.count('balance') < 2
                    or [op for op in bank.events if op != 'balance'] != required
                    or not hits or any(status != 200 for status in hits)):
                raise conpty.CheckpointMiss(conpty.Failure.BANK)
    except Exception as exc:
        # Do not lose an already observed supplier failure to a later teardown
        # error. The original error is still raised; neither gate is waived.
        reason = codex_receipt_failure(sink) if host == 'codex' and last_stage == 'shutdown' else None
        if reason is None and isinstance(exc, conpty.CheckpointMiss):
            reason = exc.reason
        if reason:
            request.node.user_properties.append(('qa_failure_label', reason.value))
        raise
    checkpoint('supplier-receipts')
    if host == 'codex':
        failure = codex_receipt_failure(sink)
        if failure:
            request.node.user_properties.append(('qa_failure_label', failure.value))
            raise conpty.CheckpointMiss(failure)
    else:
        assert sink == (['host', 'v', 'host'], [], [])


def test_codex_receipts_distinguish_a_bad_turn_from_an_unexpected_extra():
    sink = codex.Supplier()
    path = {v: k for k, v in codex.PATHS.items()}
    sink.rows = [dict(payer=mode, auth_matched=True, path=path[mode], wire_model=qa.MODELS['codex'],
                      wire_effort='low', prior_reply_linked=i > 0)
                 for i, mode in enumerate(('host', 'v', 'host'))]
    assert codex_receipt_failure(sink) is None
    sink.rejections.append('not_current_deliberate_prompt')
    assert codex_receipt_failure(sink) == conpty.Failure.UNSOLICITED
    for field, value, expected in [('payer', 'host', conpty.Failure.ROUTE),
                                   ('path', path['host'], conpty.Failure.ROUTE),
                                   ('auth_matched', False, conpty.Failure.ROUTE),
                                   ('wire_effort', 'high', conpty.Failure.MODEL),
                                   ('prior_reply_linked', False, conpty.Failure.ROOT)]:
        original = sink.rows[1][field]
        sink.rows[1][field] = value
        assert codex_receipt_failure(sink) == expected
        sink.rows[1][field] = original
    sink.rejections[0] = 'unknown value must stay private'
    assert codex_receipt_failure(sink) == conpty.Failure.PROTOCOL


def test_supplier_witness_requires_typed_correlated_native_thread_start():
    from openvegas.integrations.windows_customer_codex import NativeThreads
    tracker = NativeThreads()
    root, title = ('e64139f8-4678-46ac-8cf0-bf765e77016a', '9a7341f6-341e-4d31-865a-37c5e9262edc')
    for key, (sid, source, ephemeral) in enumerate(((root, 'user', False), (title, 'thread_title', True)), 1):
        tracker.sent({'id': key, 'method': 'thread/start', 'params': {}})
        pending = tracker.pending[key]
        thread = {'id': sid, 'parentThreadId': None, 'ephemeral': ephemeral, 'threadSource': source}
        response = {'id': key, 'result': {'thread': thread}}
        tracker.received(response)
        assert qa.thread_witness(pending, response, tracker) == (source, sid)
        assert qa.thread_witness(None, response, tracker) is None
        assert qa.thread_witness(('thread/resume', sid), response, tracker) is None
        thread['threadSource'] = 'user' if ephemeral else 'thread_title'
        assert qa.thread_witness(pending, response, tracker) is None


def test_bad_bank_request_cannot_issue_or_revoke():
    from openvegas.config import FALLBACK_DEFAULT_BACKEND_URL
    bank = qa.Bank('claude')
    with httpx.Client(transport=httpx.MockTransport(bank), base_url=FALLBACK_DEFAULT_BACKEND_URL) as client:
        assert client.get('/integrations/account').status_code == 403
        assert client.post('/integrations/native/sessions', json={},
                           headers={'authorization': 'Bearer ' + qa.DEVICE}).status_code == 403
        assert bank.state == 'initial'


def test_claude_requires_deliberate_current_prompt_not_a_command():
    def message(*blocks):
        return {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': b} for b in blocks]}]}

    prompt = claude.PROMPTS[0]
    assert claude_input_failure(message('<system-reminder>Available: /ov-billing</system-reminder>', prompt), prompt) is None
    assert claude_input_failure(message('/ov-billing status', prompt), prompt) is conpty.Failure.FORWARDED
    assert claude_input_failure(message('not the current prompt'), prompt) is conpty.Failure.INPUT


def test_console_keeps_coalesced_composer_and_answers_split_query():
    class Stream:
        chunks = iter(['Windows managed Claude\r\nClaude Code', '\x1b[', '6nPayer: host'])

        def read(self, *_, **kwargs):
            return next(self.chunks)

        def write(self, value):
            assert value == '\x1b[2;12R'

    terminal = conpty.Terminal(Stream())
    for text in ('Windows managed Claude', 'Claude Code', 'Payer: host'):
        assert terminal.expect(text, 'test') == text


def test_console_requires_new_visible_ack_not_erased_or_stale_text():
    class Stream:
        chunks = iter(['Payer: v. No prompt sent.', '\r\x1b[2Kchecking',
                       '\x1b[2;10HNo prompt sent.\x1b[2;1HPayer: v.'])

        def read(self, *_, **kwargs):
            return next(self.chunks)

        def write(self, _):
            pass

    console = conpty.Terminal(Stream())
    console.read()
    console.type_line('openvegas use v')
    assert console.expect(r'Payer: v\.\s*No prompt sent\.', 'switch') == 'Payer: v.No prompt sent.'
    assert 'checking' in console.frame()


def test_split_primary_device_query_replies_once_without_typing_a_command():
    class Stream:
        chunks = iter(['\x1b[', 'c', '\r\n'])
        replies = []

        def read(self, *_, **kwargs):
            return next(self.chunks)

        def write(self, value):
            self.replies.append(value)

    stream = Stream()
    terminal = conpty.Terminal(stream)
    for _ in range(3):
        terminal.read()
    assert stream.replies == ['\x1b[?6c']


@pytest.mark.parametrize('code,reason', [(0, None), (23, conpty.Failure.BANK),
                                         (26, conpty.Failure.LAUNCH_EXCEPTION), (1, conpty.Failure.EXIT_CODE)])
def test_console_requires_confirmed_launcher_exit(code, reason):
    class Done:
        def isalive(self):
            return False

    term = conpty.Terminal(Done(), exit_code=lambda: code)
    if reason is None:
        term.require_success()
    else:
        with pytest.raises(conpty.CheckpointMiss) as exc:
            term.require_success()
        assert exc.value.reason is reason


def test_closed_console_waits_for_owned_launcher_not_pipe(monkeypatch):
    class Closing:
        def read(self, *_args, **_kwargs):
            raise EOFError

    monkeypatch.setattr(conpty.time, 'sleep', lambda _: None)
    codes = iter((None, 23))
    with pytest.raises(conpty.CheckpointMiss) as exc:
        conpty.Terminal(Closing(), exit_code=lambda: next(codes)).require_success()
    assert exc.value.reason is conpty.Failure.BANK


def test_terminal_failure_labels_are_closed_and_no_generic_banner_accepts_trust():
    assert conpty.screen_failure('switch', 'Control check: native-root.') is conpty.Failure.ROOT
    assert conpty.screen_failure('switch', 'Switch refused. Control check: wallet-prepare.') is conpty.Failure.WALLET_PREPARE
    assert conpty.screen_failure('switch', 'Control check: arbitrary-private-text.') is None
    assert conpty.screen_failure('claude-start', 'Claude Code: Do you trust this folder?') is conpty.Failure.TRUST
    assert conpty.screen_failure('claude-start', 'Claude Code for testing') is None
    assert conpty.screen_failure('claude-control', 'Unknown skill: ov-billing') is conpty.Failure.COMMAND
    assert conpty.screen_failure('claude-control', 'unrelated unknown command') is None
    assert qa.refused_check('Local control unconfirmed. Control check: native-root.') == 'native-root'
    assert qa.refused_check('Control check: some private dynamic text.') is None
    assert qa.refused_check('Payer: v. No prompt sent.') is None
    with pytest.raises(ValueError):
        conpty.CheckpointMiss('private-text')


def test_fixture_reuses_only_the_exact_empty_workspace(tmp_path):
    from openvegas.integrations import windows_billing_host as launcher
    root = tmp_path / ('ov-rehearsal-' + 'a' * 32)
    work = root / 'work'
    work.mkdir(parents=True)
    source = {'USERPROFILE': str(root), 'SystemRoot': str(tmp_path)}
    env, cwd = canonical_fixture(launcher, 'claude', source, work)
    assert cwd == str(work.resolve()) and env['USERPROFILE'] == str(root.resolve())
    with pytest.raises(ValueError):
        canonical_fixture(launcher, 'claude', source, tmp_path)
    (work / 'not-empty').touch()
    with pytest.raises(ValueError):
        canonical_fixture(launcher, 'claude', source, work)
