"""Python fallback and installer-built Node hook parity; no native account or network."""
import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from openvegas.integrations import installer, use

PREFIXES = ['openvegas', '/openvegas', '$openvegas', 'openvegas:openvegas',
            '/openvegas:openvegas', '$openvegas:openvegas']
ACTIONS = [('', 'play', None), ('play', 'play', None), ('use v', 'v', None),
           ('use host', 'host', None)] + [
    ('play ' + game, 'play', game)
    for game in ('blackjack', 'roulette', 'poker', 'baccarat', 'slots', 'horse')]
STOP = {'continue': False, 'stopReason': 'Local control only.', 'suppressOutput': True}
BLOCK = {'decision': 'block', 'reason': use.MISSING}
SUCCESS = b'{"ok":true,"message":"Local control only."}\n'


@pytest.fixture(params=['codex', 'claude'])
def commands(request):
    if not shutil.which('node'):
        pytest.skip('Real Node required for installer hook equivalence')
    host = request.param
    files = installer.bundle_files({'host': host, 'mod': False}, 'synthetic-fixture')
    hooks = json.loads(files['plugins/openvegas/hooks/hooks.json'])
    node = shlex.split(hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'])
    assert node[1] == '-e' and node[-1] == '--billing-hook'
    python = [sys.executable, '-c',
              'import sys; from openvegas.integrations.use import hook_main; hook_main(sys.argv[1])',
              host]
    return host, [python, node]


def event(prompt='openvegas play', **fields):
    return {'hook_event_name': 'UserPromptSubmit', 'prompt': prompt,
            'session_id': 'synthetic-session', **fields}


async def invoke(argv, data, env=None, *, keep_open=False):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    child = await asyncio.create_subprocess_exec(
        *argv, env=env or {}, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        if keep_open:
            child.stdin.write(raw)
            await child.stdin.drain()
            out = await asyncio.wait_for(child.stdout.read(), 6)
            err = await child.stderr.read()
            await child.wait()
        else:
            out, err = await asyncio.wait_for(child.communicate(raw), 6)
        assert child.returncode == 0 and not err
        return json.loads(out)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@asynccontextmanager
async def supervisor(*, chunks=(SUCCESS,), hang=False):
    if not hasattr(os, 'getuid'):
        pytest.skip('Owned POSIX Unix socket required')
    # Short owned paths fit Darwin's sockaddr_un; no real credentials are read.
    with tempfile.TemporaryDirectory(prefix='ov-zq-', dir='/tmp') as directory:
        socket = directory + '/control.sock'
        seen = []
        tasks = set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                seen.append(json.loads(await reader.readline()))
                if hang:
                    await reader.read()
                else:
                    for chunk in chunks:
                        writer.write(chunk)
                        await writer.drain()
                        await asyncio.sleep(.01)
            finally:
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_unix_server(serve, socket)
        try:
            yield {use.SOCKET: socket, use.TOKEN: 'synthetic-capability'}, seen
        finally:
            server.close()
            await server.wait_closed()
            for task in list(tasks):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', PREFIXES)
async def test_exact_controls_share_supervisor_protocol(commands, prefix):
    host, runtimes = commands
    async with supervisor() as (env, seen):
        for suffix, action, game in ACTIONS:
            prompt = prefix + (' ' + suffix if suffix else '')
            for argv in runtimes:
                assert await invoke(argv, event(prompt), env) == STOP
                assert seen[-1] == {
                    'token': 'synthetic-capability', 'host': host,
                    'action': action, 'game': game, 'session_id': 'synthetic-session',
                    'model': None, 'transcript_path': None, 'rate_limits': None}
        assert len(seen) == len(ACTIONS) * len(runtimes)


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt,action,game', [
    (' \r\n\tOPENVEGAS  \tPLAY\tROULETTE\t \r\n', 'play', 'roulette'),
    ('/OpenVegas:OpenVegas\t\tUse    HOST', 'host', None),
    ('$openvegas:openvegas  use\tv', 'v', None),
    ('\n\tOpenVegas\t\r\n', 'play', None),
])
async def test_whitespace_and_case_are_identical(commands, prompt, action, game):
    async with supervisor() as (env, seen):
        for argv in commands[1]:
            assert await invoke(argv, event(prompt), env) == STOP
            assert (seen[-1]['action'], seen[-1]['game']) == (action, game)


@pytest.mark.asyncio
async def test_ordinary_code_and_malformed_prompts_are_not_swallowed(commands):
    prompts = [None, 1, True, [], {}, 'hello', 'please openvegas play',
               'openvegas play then work', 'openvegas use v\nthen work',
               'openvegas\nplay', 'openvegas play\nroulette', 'openvegas use\rhost',
               'openvegas play craps', 'openvegas play --stake 50',
               'openvegas play; touch /tmp/not-authorized',
               '$(openvegas play)', '`openvegas play`', '```\nopenvegas play\n```',
               'print("openvegas play")', 'echo openvegas use v',
               'openvegas use v && work', 'openvegas\u00a0play',
               'openvegas play\u2028', 'openvegas play \u017flots',
               '\ufeffopenvegas play', 'openvegas play\v', '/openvegas-other play']
    async with supervisor() as (env, seen):
        for prompt in prompts:
            for argv in commands[1]:
                assert await invoke(argv, event(prompt), env) == {}, repr(prompt)
        assert seen == []


@pytest.mark.asyncio
async def test_subagents_invalid_envelopes_and_other_events_do_nothing(commands):
    events = [None, [], 'openvegas play', 1,
              event(hook_event_name='PostToolUse'),
              *[event(agent_id=agent) for agent in ('worker', {}, [], False, 0)],
              event(hook_event_name='SessionStart', agent_id='worker')]
    async with supervisor() as (env, seen):
        for data in events:
            for argv in commands[1]:
                assert await invoke(argv, data, env) == {}
        assert seen == []


@pytest.mark.asyncio
async def test_session_registration_remains_nonblocking(commands):
    async with supervisor() as (env, seen):
        for argv in commands[1]:
            assert await invoke(argv, event(hook_event_name='SessionStart'), env) == {}
            assert seen[-1]['action'] == 'register'
            assert seen[-1]['game'] is None
            assert await invoke(argv, event(hook_event_name='SessionStart')) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', ['OpenVegas', '$openvegas  play', '/openvegas use v',
                                  'openvegas:openvegas use host'])
async def test_disconnected_control_never_falls_through(commands, prompt):
    for argv in commands[1]:
        assert await invoke(argv, event(prompt)) == BLOCK
        assert await invoke(argv, event(prompt), {
            use.SOCKET: '/private/tmp/no-openvegas-supervisor-fixture.sock',
            use.TOKEN: 'synthetic-capability'}) == BLOCK


@pytest.mark.asyncio
@pytest.mark.parametrize('chunks', [
    (), (b'not-json\n',), (b'[]\n',), (b'{"ok":true}\n',),
    (b'{"ok":true,"message":42}\n',), (SUCCESS[:-1],),
    (b'{"ok":true,"message":"\xff"}\n',), (b'x' * (use.MAX_FRAME + 1) + b'\n',),
    ('{"ok":true,"message":"Wrong encoding."}'.encode('utf-16') + b'\n',),
    (b'{"ok":true,"message":"Invalid JSON.","extra":NaN}\n',),
    (b'[' * 1500 + b']' * 1500 + b'\n',),
])
async def test_invalid_oversized_and_truncated_replies_block(commands, chunks):
    async with supervisor(chunks=chunks) as (env, seen):
        for argv in commands[1]:
            assert await invoke(argv, event(), env) == BLOCK
        assert len(seen) == len(commands[1])


@pytest.mark.asyncio
async def test_supervisor_timeout_blocks_without_retry(commands):
    async with supervisor(hang=True) as (env, seen):
        for argv in commands[1]:
            assert await invoke(argv, event(), env) == BLOCK
        assert len(seen) == len(commands[1])


@pytest.mark.asyncio
@pytest.mark.parametrize('ok', [False, None, 'true', 1])
async def test_provider_refusal_or_non_boolean_ok_never_succeeds(commands, ok):
    raw = json.dumps({'ok': ok, 'message': 'Consent required.'}).encode() + b'\n'
    async with supervisor(chunks=(raw,)) as (env, _):
        for argv in commands[1]:
            assert await invoke(argv, event('openvegas use v'), env) == {
                'decision': 'block', 'reason': 'Consent required.'}


@pytest.mark.asyncio
async def test_split_utf8_and_first_response_frame_are_preserved(commands):
    raw = '{"ok":true,"message":"Local caf\u00e9."}\n'.encode()
    at = raw.index(b'\xc3') + 1
    async with supervisor(chunks=(raw[:at], raw[at:] + b'x' * use.MAX_FRAME)) as (env, _):
        for argv in commands[1]:
            assert await invoke(argv, event(), env) == {
                'continue': False, 'stopReason': 'Local caf\u00e9.', 'suppressOutput': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [0, 1])
async def test_response_limit_counts_bytes_including_newline(commands, extra):
    size = use.MAX_FRAME - len(b'{"ok":true,"message":""}\n') + extra
    raw = b'{"ok":true,"message":"' + b'x' * size + b'"}\n'
    async with supervisor(chunks=(raw,)) as (env, _):
        for argv in commands[1]:
            result = await invoke(argv, event(), env)
            assert result == (BLOCK if extra else {
                'continue': False, 'stopReason': 'x' * size, 'suppressOutput': True})


@pytest.mark.asyncio
async def test_oversized_control_metadata_is_not_sent(commands):
    async with supervisor() as (env, seen):
        for argv in commands[1]:
            assert await invoke(argv, event(model='x' * use.MAX_FRAME), env) == BLOCK
            assert await invoke(argv, event('ordinary prose', model='x' * use.MAX_FRAME), env) == {}
        assert seen == []


@pytest.mark.asyncio
async def test_bounded_input_rejects_recognized_overflow_without_swallowing_prose(commands):
    async with supervisor() as (env, seen):
        for prompt, expected in [('openvegas play', BLOCK), ('ordinary prose', {})]:
            raw = json.dumps(event(prompt)).encode()
            raw += b' ' * (use.MAX_INPUT + 1 - len(raw))
            for argv in commands[1]:
                assert await invoke(argv, raw, env) == expected
        assert seen == []


@pytest.mark.asyncio
async def test_complete_control_with_unclosed_stdin_blocks_on_deadline(commands):
    for argv in commands[1]:
        assert await invoke(argv, event(), keep_open=True) == BLOCK


@pytest.mark.asyncio
async def test_slow_input_still_returns_before_installed_node_timeout(commands):
    async with supervisor(hang=True) as (env, seen):
        child = await asyncio.create_subprocess_exec(
            *commands[1][1], env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.sleep(3)
            out, err = await asyncio.wait_for(child.communicate(json.dumps(event()).encode()), 1.8)
            assert child.returncode == 0 and not err
            assert json.loads(out) == BLOCK and len(seen) == 1
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()


@pytest.mark.asyncio
async def test_malformed_or_truncated_input_is_not_guessed_to_be_a_control(commands):
    raw_inputs = [b'{', b'null', b'[]', b'{"prompt":"openvegas play"',
                  b'{"prompt":"openvegas play","hook_event_name":"UserPromptSubmit",',
                  b'{"prompt":"openvegas play\xff","hook_event_name":"UserPromptSubmit"}',
                  b'\xef\xbb\xbf' + json.dumps(event()).encode(),
                  json.dumps(event()).encode()[:-1] + b',"extra":NaN}',
                  json.dumps(event('ordinary ' + 'x' * use.MAX_INPUT)).encode()]
    for raw in raw_inputs:
        for argv in commands[1]:
            assert await invoke(argv, raw) == {}


@pytest.mark.asyncio
async def test_socket_ownership_mode_and_file_type_checks_are_not_bypassed(commands):
    async with supervisor() as (env, seen):
        directory = Path(env[use.SOCKET]).parent
        link = directory / 'link.sock'
        link.symlink_to(env[use.SOCKET])
        regular = directory / 'regular'
        regular.write_text('synthetic fixture')
        for argv in commands[1]:
            for path in (link, regular):
                assert await invoke(argv, event(), {**env, use.SOCKET: str(path)}) == BLOCK
            directory.chmod(0o755)
            try:
                assert await invoke(argv, event(), env) == BLOCK
            finally:
                directory.chmod(0o700)
        assert seen == []
