import json
import time

import pytest

from openvegas.integrations.host_quota import HostQuota, windows, claude_settings
from openvegas.integrations.terminal_wallet import LOCAL_CONTROL_SHORTCUT, wallet_label

SID = '01a17668-079e-7e90-b434-58f63208e39f'


@pytest.mark.parametrize('used,expected', [(89, 11), (90, 10), (100, 0), (True, None),
                                         ('95', None), (float('nan'), None), (101, None), (-1, None)])
def test_usage_metadata_never_guessed(used, expected):
    data = windows({'primary': {'used_percent': used, 'resets_at': 200}}, field='used_percent', now=100)
    assert (data[0]['remaining'] if data else None) == expected
    assert windows({'primary': {'used_percent': 95, 'resets_at': 99}}, field='used_percent', now=100) == []


@pytest.mark.parametrize('remaining,shown', [(11, False), (10, True), (0, True), (None, False), ('10', False)])
def test_sticky_warning_with_balance_and_billing_reset(remaining, shown):
    payload = {'state': 'ready', 'balance': '500', 'billing': 'host', 'controls': True,
               'quota_remaining': remaining, 'controls_shortcut': LOCAL_CONTROL_SHORTCUT}
    label = wallet_label(payload, 150)
    assert '$V 500.00' in label
    assert ('switch to $V' in label) == shown
    assert 'switch to $V' not in wallet_label({**payload, 'billing': 'v'}, 150)


def test_codex_uses_registered_session_metadata_only(tmp_path):
    quota = HostQuota('codex', {'HOME': str(tmp_path)})
    path = tmp_path / '.codex/sessions/2026/10/31' / ('rollout-' + SID + '.jsonl')
    path.parent.mkdir(parents=True)
    fake = {'type': 'message', 'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 99, 'resets_at': time.time() + 100}}}}
    real = {'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 90, 'resets_at': time.time() + 100}}}}
    path.write_text(json.dumps(real) + '\n' + json.dumps(fake) + '\n')
    quota.transcript(str(path), SID)
    assert quota.remaining() == 10
    assert quota.remaining() == 10
    quota.values[0]['reset'] = time.time() - 1
    assert quota.remaining() is None
    rejected = HostQuota('codex', {'HOME': str(tmp_path)})
    rejected.transcript(str(path), 'different-id')
    assert rejected.remaining() is None


def test_claude_missing_data_reset_and_context_are_not_quota(tmp_path):
    quota = HostQuota('claude', {'HOME': str(tmp_path)})
    quota.observe({'context_window': {'remaining_percentage': 5}})
    assert quota.remaining() is None
    quota.observe({'five_hour': {'used_percentage': 90, 'resets_at': time.time() + 100}})
    assert quota.remaining() == 10
    quota.observe({})
    assert quota.remaining() is None


def test_claude_status_observer_is_ephemeral_and_preserves_custom_settings(tmp_path, monkeypatch):
    from openvegas.integrations import installer
    monkeypatch.setattr(installer, 'helper', lambda _: ['/fixture', 'serve'])
    monkeypatch.chdir(tmp_path)
    env = {'HOME': str(tmp_path)}
    argv = ['/claude', '--model', 'original']
    result = claude_settings(argv, env)
    assert result[:3] == argv
    assert '--quota-statusline' in result[-1]
    assert not (tmp_path / '.claude').exists()
    assert claude_settings([*argv, '--settings', '{}'], env) == [*argv, '--settings', '{}']
    path = tmp_path / '.claude/settings.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'statusLine': {'command': 'existing'}}))
    assert claude_settings(argv, env) == argv
