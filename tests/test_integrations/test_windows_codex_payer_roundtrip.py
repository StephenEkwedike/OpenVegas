"""Installed Windows Codex app-server (stdio), NOT a TUI or a supplier test.

Offline disposable Windows x64 account only. Before opting in, deny the native
process non-loopback egress at OS level; pytest's socket guard is Python-only.
Set OV_WINDOWS_CODEX_PAYER_PROBE=1 and OV_WINDOWS_CODEX_ENTRY to the installed
0.166.0 codex.exe or npm shim. Run python -B -m pytest -q -p no:cacheprovider
tests/test_integrations/test_windows_codex_payer_roundtrip.py. Never upload the
disposable home; only run-*.json is intended for evidence.
"""
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import pytest

from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY


OUT = Path(__file__).resolve().parents[2] / '.local/native-windows-models-20261114/model-roundtrip'
MODELS = ('gpt-6-astra', 'gpt-6.1-sol', 'gpt-6.1-terra')
PATHS = {'/_host/v1/responses': 'host', '/integrations/native/v1/responses': 'v'}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def frames(model, response_id, output):
    response = {'object': 'response', 'id': response_id, 'model': model,
                'status': 'completed', 'output': output,
                'usage': {'input_tokens': 10, 'output_tokens': 5 if output else 0,
                          'total_tokens': 15 if output else 10}}
    yield {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}
    for item in output:
        yield {'type': 'response.output_item.added', 'output_index': 0,
               'item': {**item, 'content': [], 'status': 'in_progress'}}
        yield {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0,
               'content_index': 0, 'delta': item['content'][0]['text']}
        yield {'type': 'response.output_item.done', 'output_index': 0, 'item': item}
    yield {'type': 'response.completed', 'response': response}


class Supplier:
    """Observe authenticated WS creates; a generate:false warmup is not a turn."""
    def __init__(self):
        self.token = self.prompt = self.last_answer = None
        self.rows, self.rejections, self.warmups = [], [], []

    async def connection(self, ws):
        # Do not share supplier-side continuation IDs across payer connections.
        history = {}
        path = ws.request.path
        mode = PATHS.get(path)
        key = SYNTHETIC_KEY if mode == 'host' else self.token
        try:
            if not mode or not key or ws.request.headers.get_all('Authorization') != ['Bearer ' + key]:
                raise ValueError('route_or_auth')
            async for raw in ws:
                body = json.loads(raw)
                if body.get('type') != 'response.create' or (mode == 'v' and self.token != key):
                    raise ValueError('unexpected_create')
                previous = body.get('previous_response_id')
                inputs = (history[previous] if previous is not None else []) + body.get('input', [])
                ident = secrets.token_hex(16)
                rid, output = 'resp_' + ident, []
                if body.get('generate') is False:
                    self.warmups.append(mode)
                else:
                    users = [i for i in inputs if i.get('role') == 'user']
                    if not self.prompt or not users or [p.get('text') for p in users[-1]['content']] != [self.prompt]:
                        raise ValueError('not_current_deliberate_prompt')
                    if self.last_answer and not any(p.get('text') == self.last_answer for i in inputs
                            if i.get('role') == 'assistant' for p in i.get('content', [])):
                        raise ValueError('previous_native_reply_missing')
                    model, effort = body['model'], body['reasoning']['effort']
                    if model not in MODELS or effort not in ('low', 'high'):
                        raise ValueError('wire_model_or_effort')
                    answer = 'fixture-reply-' + ident
                    output = [{'type': 'message', 'id': 'msg_' + ident, 'role': 'assistant',
                               'status': 'completed', 'content': [{'type': 'output_text',
                               'text': answer, 'annotations': []}]}]
                    self.rows.append({'payer': mode, 'path': path, 'auth_matched': True,
                                      'wire_model': model, 'wire_effort': effort,
                                      'prior_reply_linked': self.last_answer is not None,
                                      'reply_sha256': digest(answer)})
                    self.prompt, self.last_answer = None, answer
                history[rid] = inputs + output
                for event in frames(body['model'], rid, output):
                    await ws.send(json.dumps(event))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            known = ('route_or_auth', 'unexpected_create', 'not_current_deliberate_prompt',
                     'previous_native_reply_missing', 'wire_model_or_effort')
            self.rejections.append(str(exc) if str(exc) in known else 'invalid_request_shape')
            await ws.close(code=1008)


def _check(value):
    if not value:
        raise ValueError('foreground_identity')


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _object(raw):
    def unique(pairs):
        out = dict(pairs)
        _check(len(out) == len(pairs))
        return out
    value = json.loads(raw, object_pairs_hook=unique)
    _check(isinstance(value, dict))
    return value


class ForegroundSupplier(Supplier):
    """Synthetic TUI oracle only. Native analytics labels are not payer authority."""

    def __init__(self, lookup):
        super().__init__()
        self.lookup = lookup
        self.root, self.helpers, self.turns = None, (), set()
        self.helper_legs, self.warmed, self.answered = {}, set(), set()
        self.title_warmups = self.title_turns = 0
        self.wallet_seen = False

    async def _bind(self, source, sid):
        # Native prewarm can race publication of the matched app-server reply.
        # Never learn an authorized UUID from the provider request itself.
        async with asyncio.timeout(3):
            while True:
                ids = self.lookup()
                _check(isinstance(ids, dict) and ids.keys() <= {'user', 'thread_title'})
                root, helpers = ids.get('user'), ids.get('thread_title', [])
                _check(root is None or _uuid(root))
                _check(isinstance(helpers, list) and len(helpers) <= 2 and all(_uuid(i) for i in helpers))
                _check(len(set(helpers)) == len(helpers) and root not in helpers and (root or not helpers))
                _check(self.root in (None, root) and tuple(helpers[:len(self.helpers)]) == self.helpers)
                self.root, self.helpers = root, tuple(helpers)
                if sid == root or sid in helpers or (source == 'user' and root) or len(helpers) == 2:
                    _check((source == 'user' and sid == root) or (source == 'thread_title' and sid in helpers))
                    return
                await asyncio.sleep(.02)

    async def connection(self, ws):
        try:
            mode = PATHS.get(ws.request.path)
            key = SYNTHETIC_KEY if mode == 'host' else self.token
            headers = ws.request.headers
            _check(mode and key and headers.get_all('authorization') == ['Bearer ' + key])
            claims = headers.get_all('x-codex-turn-metadata')
            _check(len(claims) == 1)
            header = _object(claims[0])
            sid, source = header['thread_id'], header['thread_source']
            _check(_uuid(sid) and header['session_id'] == sid and source in ('user', 'thread_title'))
            _check(headers.get_all('thread-id') == headers.get_all('session-id') == [sid])
            _check(header['request_kind'] in ('prewarm', 'turn'))
            _check(source == 'user' or mode == 'host')
            await self._bind(source, sid)
            if mode == 'v':
                self.wallet_seen = True
            leg = int(self.wallet_seen)
            if source == 'thread_title':
                _check(not leg or (len(self.rows) >= 2 and self.rows[1]['payer'] == 'v'))
                _check(leg or sid == self.helpers[0])
                _check(self.helper_legs.get(leg, sid) == sid and self.helper_legs.get(1 - leg) != sid)
                self.helper_legs[leg] = sid
            first = True

            def inspect(raw):
                nonlocal first
                body = _object(raw)
                meta = body['client_metadata']
                detail = _object(meta['x-codex-turn-metadata'])
                _check(body['type'] == 'response.create' and isinstance(meta, dict))
                _check(detail['thread_source'] == source and
                       detail['thread_id'] == meta['thread_id'] == detail['session_id'] == meta['session_id'] == sid)
                _check(body['model'] == detail['model'] == MODELS[0]
                       and body['reasoning']['effort'] == detail['reasoning_effort'] == 'low')
                _check('generate' not in body or type(body['generate']) is bool)
                warmup = body.get('generate') is False
                _check(detail['request_kind'] == ('prewarm' if warmup else 'turn'))
                _check(not warmup or first)
                first = False
                _check(source == 'user' or leg == int(self.wallet_seen))
                if not warmup:
                    tid = detail['turn_id']
                    _check(_uuid(tid) and meta['turn_id'] == meta['root_turn_id'] == detail['root_turn_id'] == tid)
                    _check(tid not in self.turns and len(self.turns) < 5)
                    if source == 'user':
                        _check(len(self.rows) < 3 and mode == ('host', 'v', 'host')[len(self.rows)])
                    self.turns.add(tid)
                return body

            if source == 'user':
                class RootSocket:
                    request = ws.request
                    send = ws.send
                    close = ws.close

                    async def __aiter__(self):
                        async for raw in ws:
                            inspect(raw)
                            yield raw

                # All original prompt arming, history, token, wire model/effort,
                # and prior-reply assertions still apply to every root turn.
                return await super().connection(RootSocket())

            own_warmup = None
            async for raw in ws:
                body = inspect(raw)
                previous = body.get('previous_response_id')
                _check(previous is None or (own_warmup is not None and previous == own_warmup))
                rid = 'resp_' + secrets.token_hex(16)
                if body.get('generate') is False:
                    _check(sid not in self.answered and sid not in self.warmed and previous is None)
                    self.warmed.add(sid)
                    self.title_warmups += 1
                    own_warmup, output = rid, []
                else:
                    _check(sid not in self.answered and mode == 'host')
                    fmt = body['text']['format']
                    schema = fmt['schema']
                    _check(fmt['type'] == 'json_schema' and fmt['name'] == 'codex_output_schema'
                           and fmt['strict'] is True and schema['type'] == 'object'
                           and schema['required'] == ['title'] and schema['additionalProperties'] is False
                           and schema['properties'] == {'title': {'type': 'string', 'minLength': 1, 'maxLength': 36}})
                    self.answered.add(sid)
                    self.title_turns += 1
                    output = [{'type': 'message', 'id': 'msg_' + secrets.token_hex(16), 'role': 'assistant',
                               'status': 'completed', 'content': [{'type': 'output_text',
                               'text': '{"title":"Synthetic workspace"}', 'annotations': []}]}]
                # Title replies never consume an armed prompt, enter root history,
                # count as customer answers or set the customer's last answer.
                for event in frames(body['model'], rid, output):
                    await ws.send(json.dumps(event))
        except (ValueError, KeyError, IndexError, AttributeError, TypeError, TimeoutError, OSError):
            self.rejections.append('foreground_identity')
            await ws.close(code=1008)


@asynccontextmanager
async def supplier(*, foreground_threads=None):
    from websockets.asyncio.server import serve

    sink = Supplier() if foreground_threads is None else ForegroundSupplier(foreground_threads)
    async with serve(sink.connection, '127.0.0.1', 0, max_size=2_000_000) as server:
        yield sink, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "valid", "legacy", "unwitnessed", "aliased", "wrong_role", "wallet_title", "wrong_auth",
    "multiple_headers", "wrong_header_session", "wrong_frame_thread", "wrong_nested_thread",
    "wrong_kind", "wrong_model", "duplicate_key", "duplicate_turn", "duplicate_warmup",
    "foreign_previous", "wrong_schema", "rebound_witness", "two_helpers", "two_in_one_leg",
    "helper_reused_across_legs",
])
async def test_foreground_title_oracle(case):
    from types import SimpleNamespace

    from websockets.datastructures import Headers

    root, title, tid, other = [str(UUID(int=i)) for i in range(1, 5)]
    witness = {'user': root, 'thread_title': [title]}
    sink = Supplier() if case == 'legacy' else ForegroundSupplier(lambda: witness)
    sink.prompt, sink.last_answer, sink.token = 'armed-root-input', 'earlier-root-answer', 'ov_native_fixture_only'
    md = {'thread_source': 'thread_title', 'thread_id': title, 'session_id': title,
          'request_kind': 'turn', 'turn_id': tid, 'root_turn_id': tid,
          'model': MODELS[0], 'reasoning_effort': 'low'}
    header = dict(md, request_kind='prewarm')
    meta = {'thread_id': title, 'session_id': title, 'turn_id': tid, 'root_turn_id': tid}
    schema = {'type': 'object', 'properties': {'title': {'type': 'string', 'minLength': 1, 'maxLength': 36}},
              'required': ['title'], 'additionalProperties': False}
    body = {'type': 'response.create', 'model': MODELS[0], 'reasoning': {'effort': 'low'}, 'input': [],
            'text': {'format': {'type': 'json_schema', 'strict': True,
                                'name': 'codex_output_schema', 'schema': schema}}}
    if case == 'unwitnessed':
        witness['thread_title'] = [other, str(UUID(int=5))]
    elif case == 'aliased':
        witness['user'] = title
    elif case == 'wrong_role':
        header['thread_source'] = 'user'
    elif case == 'wrong_frame_thread':
        meta['thread_id'] = root
    elif case == 'wrong_nested_thread':
        md['thread_id'] = root
    elif case == 'wrong_kind':
        md['request_kind'] = 'prewarm'
    elif case == 'wrong_model':
        md['model'] = MODELS[1]
    elif case == 'foreign_previous':
        body['previous_response_id'] = 'resp_from_another_connection'
    elif case == 'wrong_schema':
        schema['properties']['title']['type'] = 'integer'
    elif case == 'rebound_witness':
        sink.root = other
    mode = 'v' if case == 'wallet_title' else 'host'
    headers = Headers({'authorization': 'Bearer ' + (sink.token if mode == 'v' else SYNTHETIC_KEY),
                       'thread-id': title, 'session-id': title, 'x-codex-turn-metadata': json.dumps(header)})
    if case in ('wrong_header_session', 'wrong_auth'):
        key = 'session-id' if case == 'wrong_header_session' else 'authorization'
        del headers[key]
        headers[key] = other
    if case == 'multiple_headers':
        headers['thread-id'] = title
    raw = json.dumps({**body, 'client_metadata': {**meta, 'x-codex-turn-metadata': json.dumps(md)}})
    if case == 'duplicate_key':
        raw = raw.replace('"client_metadata":', '"client_metadata": {}, "client_metadata":')
    warmup = json.dumps({**body, 'generate': False, 'client_metadata': {
        **meta, 'x-codex-turn-metadata': json.dumps({**md, 'request_kind': 'prewarm'})}})
    wire = ([warmup, raw] if case == 'valid' else [raw, raw] if case == 'duplicate_turn'
            else [warmup, warmup] if case == 'duplicate_warmup' else [raw])
    answers, closes = [], []

    class WS:
        request = SimpleNamespace(headers=headers, path=next(p for p, v in PATHS.items() if v == mode))

        async def __aiter__(self):
            for frame in wire:
                yield frame

        async def send(self, packet):
            event = json.loads(packet)
            if event['type'] == 'response.completed':
                answers.extend(json.loads(x['content'][0]['text']) for x in event['response']['output'])

        async def close(self, code):
            closes.append(code)

    await sink.connection(WS())
    assert (sink.prompt, sink.last_answer, sink.rows) == ('armed-root-input', 'earlier-root-answer', [])
    if case in ('two_helpers', 'two_in_one_leg', 'helper_reused_across_legs'):
        assert not sink.rejections and sink.title_turns == 1
        if case != 'two_in_one_leg':
            sink.wallet_seen = True
            sink.rows.extend([{'payer': 'host'}, {'payer': 'v'}])
        if case != 'helper_reused_across_legs':
            witness['thread_title'].append(other)
            for key in ('session-id', 'thread-id', 'x-codex-turn-metadata'):
                value = headers[key].replace(title, other)
                del headers[key]
                headers[key] = value
            wire[:] = [raw.replace(title, other).replace(tid, str(UUID(int=6)))]
        await sink.connection(WS())
        assert sink.title_turns == (2 if case == 'two_helpers' else 1)
        assert bool(sink.rejections) == (case != 'two_helpers')
        return
    if case == 'valid':
        assert not closes and not sink.rejections and sink.title_turns == sink.title_warmups == 1
        assert answers == [{'title': 'Synthetic workspace'}]
    else:
        assert closes == [1008] and len(sink.rejections) == 1
        assert answers == ([{'title': 'Synthetic workspace'}] if case == 'duplicate_turn' else [])
        if case == 'legacy':
            assert sink.rejections == ['not_current_deliberate_prompt']


class Stdio:
    def __init__(self, child):
        self.child = child
        self.seq = 0
        self.pending = []

    def send(self, message):
        self.child.stdin.write(json.dumps(message).encode() + b'\n')
        self.child.stdin.flush()

    async def read(self):
        line = await asyncio.to_thread(self.child.stdout.readline, 4_000_001)
        if not line or len(line) > 4_000_000:
            raise RuntimeError('native stdio closed or exceeded frame bound')
        msg = json.loads(line)
        if 'method' in msg and 'id' in msg:
            raise RuntimeError('unexpected native server request; no permission granted')
        return msg

    async def call(self, method, params):
        self.seq += 1
        self.send({'method': method, 'id': self.seq, 'params': params})
        async with asyncio.timeout(25):
            while True:
                msg = await self.read()
                if msg.get('id') == self.seq:
                    if 'error' in msg:
                        raise RuntimeError(f'native {method} RPC error {msg["error"].get("code")}')
                    return msg['result']
                if msg.get('method') in ('item/completed', 'turn/completed'):
                    self.pending.append(msg)

    async def answer(self, sid, tid):
        answers = []
        async with asyncio.timeout(35):
            while True:
                msg = self.pending.pop(0) if self.pending else await self.read()
                p = msg.get('params', {})
                if msg.get('method') == 'item/completed' and p['item']['type'] == 'agentMessage':
                    assert (p['threadId'], p['turnId']) == (sid, tid)
                    answers.append(p['item']['text'])
                if msg.get('method') == 'turn/completed':
                    assert (p['threadId'], p['turn']['id']) == (sid, tid)
                    if p['turn']['status'] != 'completed' or p['turn'].get('error'):
                        raise RuntimeError('native turn did not complete successfully')
                    assert len(answers) == 1
                    return answers[0]


async def exercise(executable, env, cwd, report, save, model, effort):
    from openvegas.integrations.inference_router_adapter import VERSIONS
    from openvegas.integrations.inference_router_session import RouterSession, WalletAccess
    from openvegas.integrations.windows_billing_host import RehearsalAdapter

    # config/read need not materialize an implicit built-in default. Declare
    # the built-in provider in the fresh profile and check it before any turn.
    with (Path(env['CODEX_HOME']) / 'config.toml').open('x', encoding='utf-8') as f:
        f.write('model_provider="openai"\nproject_root_markers=[".ov-probe-root"]\n')
    (Path(cwd) / '.ov-probe-root').touch(exist_ok=False)
    async with supplier() as (sink, port):
        report['rejected_fixture_creates'] = sink.rejections
        report['fixture_creates'] = sink.rows
        report['non_generating_warmups'] = sink.warmups

        async def prepare(_):
            assert sink.token is None
            sink.token = 'ov_native_fixture_' + secrets.token_hex(16)
            return WalletAccess(sink.token, time.time() + 300)

        async def revoke(access):
            if not sink.token or access.token != sink.token:
                return False
            sink.token = None
            return True

        session = RouterSession(RehearsalAdapter('codex', port), local_auth='peer',
                                request_boundary=True, prepare_wallet=prepare, revoke_wallet=revoke)
        args = ['--no-daemon', '-c', 'cli_auth_credentials_store="ephemeral"',
                '-c', 'web_search="disabled"', '-c', 'analytics.enabled=false',
                '-c', 'feedback.enabled=false', '-c', 'check_for_update_on_startup=false',
                '--disable', 'hooks', '--disable', 'apps', '--disable', 'shell_tool',
                '--disable', 'multi_agent', 'app-server', '--listen', 'stdio://']
        async with session.launch(version=VERSIONS['codex'], env=env, args=args,
                                  effective_policy_verified=True) as (child_env, argv):
            with subprocess.Popen([executable, *argv], env=child_env, cwd=cwd,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as child:
                try:
                    session.bind_native(child)
                    rpc = Stdio(child)
                    await rpc.call('initialize', {'clientInfo': {'name': 'offline-probe', 'version': '1'},
                                                  'capabilities': {'experimentalApi': True}})
                    rpc.send({'method': 'initialized', 'params': {}})
                    config = (await rpc.call('config/read', {'includeLayers': False}))['config']
                    assert config.get('model_provider') == 'openai'
                    assert config.get('openai_base_url') == 'http://' + session.router.authority
                    assert config.get('cli_auth_credentials_store') == 'ephemeral'
                    assert not config.get('mcp_servers')
                    await rpc.call('account/login/start', {'type': 'apiKey', 'apiKey': SYNTHETIC_KEY})
                    result = await rpc.call('thread/start', {'cwd': cwd, 'ephemeral': True,
                        'model': model, 'config': {'model_reasoning_effort': effort},
                        'modelProvider': 'openai', 'sandbox': 'read-only',
                        'approvalPolicy': 'never'})
                    assert (result['model'], result['reasoningEffort']) == (model, effort)
                    sid = str(UUID(result['thread']['id']))
                    report['thread_id'] = sid
                    seen = set()
                    for mode in ('host', 'v', 'host'):
                        report['attempting'] = [model, effort, mode]
                        save()
                        if mode != session.state.mode:
                            switched = await session.command('openvegas use ' + mode)
                            assert switched.changed and switched.billing == mode
                        assert child.poll() is None
                        sink.prompt = 'local-probe-' + secrets.token_hex(16)
                        turn = await rpc.call('turn/start', {'threadId': sid,
                            'input': [{'type': 'text', 'text': sink.prompt}]})
                        tid = turn['turn']['id']
                        assert tid and tid not in seen
                        answer = await rpc.answer(sid, tid)
                        assert not sink.rejections and len(sink.rows) == len(seen) + 1
                        row = sink.rows[-1]
                        save()
                        assert (row['payer'], row['wire_model'], row['wire_effort']) == (mode, model, effort)
                        assert digest(answer) == row['reply_sha256']
                        seen.add(tid)
                        report['turns'].append(dict(row, thread_id=sid, turn_id=tid,
                            generation=session.state.generation, native_reply_sha256=digest(answer)))
                        save()
                    assert len(seen) == 3 and sink.token is None
                finally:
                    child.stdin.close()
                    try:
                        await asyncio.to_thread(child.wait, 3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        await asyncio.to_thread(child.wait, 5)
            assert not sink.rejections and len(sink.rows) == 3
            assert session.state.status()['ambiguous_outcomes'] == 0


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('effort', ('low', 'high'))
def test_windows_codex_payer_roundtrip(monkeypatch, model, effort):
    if os.environ.get('OV_WINDOWS_CODEX_PAYER_PROBE') != '1':
        pytest.skip('opt-in offline native Windows Codex probe')
    assert sys.platform == 'win32', 'Windows acceptance requires native Windows'
    from openvegas.emotes._windows_state import private_windows_directory
    from openvegas.integrations.inference_router_adapter import VERSIONS
    from openvegas.integrations.windows_billing_host import fresh_environment
    from openvegas.integrations.windows_native_image import pinned_windows_image

    with private_windows_directory(OUT):
        monkeypatch.setattr(tempfile, 'tempdir', str(OUT))
        path = OUT / ('run-' + secrets.token_hex(12) + '.json')
        report = dict(surface='installed Windows Codex app-server/stdio (not TUI)',
                      version=VERSIONS['codex'], supplier='loopback synthetic on both routes',
                      scope='routing only; neither provider fulfillment nor financial settlement',
                      model=model, effort=effort, status='incomplete', turns=[])

        def save():
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            temp.replace(path)

        save()
        try:
            with pinned_windows_image('codex', os.environ['OV_WINDOWS_CODEX_ENTRY'],
                                      VERSIONS['codex']) as exe, fresh_environment('codex') as (env, cwd):
                asyncio.run(exercise(exe, env, cwd, report, save, model, effort))
            report.pop('attempting')
            report['status'] = 'passed'
        finally:
            save()
