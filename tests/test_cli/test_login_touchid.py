"""Regression: the removed optional biometric path cannot be re-enabled."""
import pytest
from click.testing import CliRunner
from openvegas.cli import cli


@pytest.mark.parametrize('legacy_opt_in', ['0', '1'])
def test_login_uses_normal_authentication(monkeypatch, legacy_opt_in):
    monkeypatch.setenv('OPENVEGAS_ENABLE_TOUCHID', legacy_opt_in)
    calls = []
    class Auth:
        def login_with_email(self, email, password):
            calls.append((email, password))
            return {'email': email, 'user_id': 'fixture'}
    monkeypatch.setattr('openvegas.auth.SupabaseAuth', Auth)
    answers = iter(['user@example.com', 'fixture-password'])
    monkeypatch.setattr('openvegas.cli.Prompt.ask', lambda *a, **kw: next(answers))
    result = CliRunner().invoke(cli, ['login'])
    assert result.exit_code == 0, result.output
    assert calls == [('user@example.com', 'fixture-password')]
    assert 'Touch ID' not in result.output
    assert 'fixture-password' not in result.output


def test_login_failure_stays_an_error(monkeypatch):
    class Auth:
        def login_with_email(self, *args):
            raise RuntimeError('private upstream detail')
    monkeypatch.setattr('openvegas.auth.SupabaseAuth', Auth)
    monkeypatch.setattr('openvegas.cli.Prompt.ask', lambda *a, **kw: 'fixture')
    result = CliRunner().invoke(cli, ['login'])
    assert result.exit_code == 1
    assert 'Login failed' in result.output
    assert 'private upstream detail' not in result.output
