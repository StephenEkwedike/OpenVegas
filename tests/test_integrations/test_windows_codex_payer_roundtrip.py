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


@asynccontextmanager
async def supplier():
    from websockets.asyncio.server import serve

    sink = Supplier()
    async with serve(sink.connection, '127.0.0.1', 0, max_size=2_000_000) as server:
        yield sink, server.sockets[0].getsockname()[1]


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
