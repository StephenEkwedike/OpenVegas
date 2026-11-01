"""Execute the actual small Node hook against an isolated local socket."""
import asyncio
import json
import os
import shlex
import shutil

import pytest

from openvegas.integrations import installer


def command(host='codex'):
    if not shutil.which('node'):
        pytest.skip('Node required')
    files = installer.bundle_files({'host': host, 'mod': False}, 'fixture')
    hooks = json.loads(files['plugins/openvegas/hooks/hooks.json'])
    return shlex.split(hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'])


async def invoke(argv, data, env):
    child = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(child.communicate(json.dumps(data).encode()), 5)
    assert child.returncode == 0 and not err
    return json.loads(out)


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['codex', 'claude'])
async def test_non_control_hook_is_fast_and_ordinary_prompts_untouched(host):
    args = command(host)
    assert 'node' in args[0] and '--billing-hook' in args
    assert await invoke(args, {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hello'}, {}) == {}
    response = await invoke(args, {'hook_event_name': 'UserPromptSubmit', 'prompt': 'openvegas use v'}, {})
    assert response['decision'] == 'block' and 'No billing changed' in response['reason']


@pytest.mark.asyncio
@pytest.mark.parametrize('host,prompt,action,game', [
    ('codex', '$openvegas:openvegas play roulette', 'play', 'roulette'),
    ('claude', '/openvegas use host', 'host', None),
])
async def test_same_local_supervisor_protocol(host, prompt, action, game, tmp_path):
    if not hasattr(os, 'getuid'):
        pytest.skip('POSIX owned terminal socket')
    # Keep the Unix socket path below the macOS sockaddr limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='ov-hook-', dir='/tmp') as directory:
        socket = directory + '/hook.sock'
        seen = []
        async def serve(reader, writer):
            seen.append(json.loads(await reader.readline()))
            writer.write(b'{"ok":true,"message":"Game menu opened."}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_unix_server(serve, socket)
        try:
            result = await invoke(command(host), {'hook_event_name': 'UserPromptSubmit',
                'prompt': prompt, 'session_id': 'synthetic-session'},
                {'OPENVEGAS_SWITCH_SOCKET': socket, 'OPENVEGAS_SWITCH_TOKEN': 'synthetic-token'})
            assert result == {'decision': 'block', 'reason': 'Game menu opened.'}
            assert seen == [{'host': host, 'token': 'synthetic-token', 'action': action,
                'session_id': 'synthetic-session', 'model': None, 'game': game,
                'transcript_path': None, 'rate_limits': None}]
        finally:
            server.close()
            await server.wait_closed()
