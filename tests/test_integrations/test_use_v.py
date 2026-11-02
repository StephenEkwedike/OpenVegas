import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.integrations import use, billing_host, installer, native
from openvegas.integrations.credentials import IntegrationError

SID = '01a17668-079e-7e90-b434-58f63208e39f'


@pytest.mark.asyncio
@pytest.mark.parametrize('host,prefix', [('codex', '$'), ('claude', '/')])
@pytest.mark.parametrize('mode', ['v', 'host'])
async def test_exact_pre_model_command_blocks_inference(monkeypatch, host, prefix, mode):
    request = AsyncMock(return_value='Billing controls')
    monkeypatch.setattr(use, 'request', request)
    for prompt in ['openvegas use ' + mode, prefix + 'openvegas use ' + mode,
                   prefix + 'openvegas:openvegas use ' + mode]:
        reply = await use.hook(host, dict(hook_event_name='UserPromptSubmit', prompt=prompt, session_id=SID))
        assert reply == {'continue': False, 'stopReason': 'Billing controls', 'suppressOutput': True}
        assert request.call_args.args == (host, mode)


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', ['please openvegas use v', 'openvegas use v\nthen work',
                                   'openvegas play --stake 50', 'echo openvegas use v'])
async def test_other_prompts_are_not_intercepted(monkeypatch, prompt):
    request = AsyncMock()
    monkeypatch.setattr(use, 'request', request)
    assert await use.hook('codex', dict(hook_event_name='UserPromptSubmit', prompt=prompt)) == {}
    request.assert_not_called()


@pytest.mark.asyncio
async def test_disconnected_hook_blocks_switch_without_model_or_spend(monkeypatch):
    monkeypatch.delenv(use.SOCKET, raising=False)
    reply = await use.hook('codex', dict(hook_event_name='UserPromptSubmit', prompt='$openvegas use v'))
    assert reply['decision'] == 'block' and 'No billing changed' in reply['reason']


@pytest.mark.parametrize('host,args,expected', [
    ('codex', ['--no-daemon', 'resume', SID, '--sandbox', 'read-only'], ['--no-daemon', '--sandbox', 'read-only', 'resume', SID]),
    ('claude', ['--resume', SID, '--permission-mode', 'default'], ['--permission-mode', 'default', '--resume', SID]),
    ('claude', ['--model', 'old', '--continue'], ['--resume', SID]),
])
def test_resume_same_conversation_preserves_permissions(host, args, expected):
    assert billing_host.resume_args(host, args, SID) == expected


@pytest.mark.parametrize('args', [['do this task'], ['--', 'task'], ['--profile', 'custom'], ['-c', 'model_provider="other"']])
def test_unsafe_or_ambiguous_resume_fails_before_switch(args):
    with pytest.raises(IntegrationError):
        billing_host.resume_args('codex', args, SID)


@pytest.mark.asyncio
async def test_socket_auth_identity_and_no_double_switch(tmp_path):
    supervisor = billing_host.Supervisor('codex', '/fixture', [], {'HOME': str(tmp_path)})
    supervisor.child = SimpleNamespace(returncode=None)
    class Writer:
        def write(self, value): self.reply = json.loads(value)
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass
    async def send(**patch):
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps(dict(token=supervisor.token, host='codex', action='register', session_id=SID, model='model', **patch)).encode() + b'\n')
        writer = Writer()
        await supervisor.handle(reader, writer)
        return writer.reply
    # Use a separate reader for overrides to avoid merging duplicate kwargs.
    async def invoke(action, token=None, identifier=SID):
        reader = asyncio.StreamReader()
        writer = Writer()
        reader.feed_data(json.dumps({'token': token or supervisor.token, 'host': 'codex', 'action': action,
                                     'session_id': identifier, 'model': 'model'}).encode() + b'\n')
        await supervisor.handle(reader, writer)
        return writer.reply
    assert not (await invoke('register', token='wrong'))['ok']
    assert (await invoke('register'))['ok']
    assert supervisor.identifier == SID and supervisor.original_model == 'model'
    assert not (await invoke('v', identifier='01a17668-079e-7e90-b434-58f63208e39e'))['ok']
    assert (await invoke('v'))['ok'] and supervisor.pending == 'v'
    assert not (await invoke('v'))['ok']


@pytest.mark.parametrize('host', ['claude', 'codex'])
def test_plugin_hooks_and_scoped_helpers(monkeypatch, host):
    monkeypatch.setattr(installer, 'helper', lambda *_: ['/safe/runtime', '--host', host])
    files = installer.bundle_files({'host': host, 'mod': host == 'claude'}, 'test')
    hooks = json.loads(files['plugins/openvegas/hooks/hooks.json'])
    assert set(hooks['hooks']) == {'SessionStart', 'UserPromptSubmit'}
    assert '--billing-hook' in hooks['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    assert 'API_KEY' not in str(files) and 'ov_native_' not in str(files)


def test_child_preserves_only_scoped_local_connections(monkeypatch):
    monkeypatch.setenv('OPENVEGAS_SWITCH_SOCKET', '/tmp/local-fixture')
    monkeypatch.setenv('OPENVEGAS_SWITCH_TOKEN', 'local-capability')
    monkeypatch.setenv('OPENAI_API_KEY', 'server-key-must-not-forward')
    env = native.child_environment('codex', 'https://example.invalid', 'exact', 100, 'ov_native_fixture')
    assert env['OPENVEGAS_SWITCH_TOKEN'] == 'local-capability'
    assert env['OPENVEGAS_NATIVE_TOKEN'] == 'ov_native_fixture'
    assert 'OPENAI_API_KEY' not in env
