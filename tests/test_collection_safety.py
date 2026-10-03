"""Subprocess collection checks using synthetic env values, never network calls."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def invocation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    suite = tmp_path / 'suite'
    suite.mkdir()
    # Exercise the real root conftest, not a stand-in fixture installed too late.
    (suite / 'conftest.py').write_text((root / 'tests/conftest.py').read_text())
    probe = suite / 'test_probe.py'
    probe.write_text('''import os
from pathlib import Path
assert not any(k.startswith(("STRIPE_", "SUPABASE_")) for k in os.environ)
assert Path(os.environ["OPENVEGAS_ENV_FILE"]).read_bytes() == b""
print("COLLECTION_REACHED")
def test_mock_environment(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_nonfunctional_fixture")
    assert os.environ["STRIPE_SECRET_KEY"] == "sk_test_nonfunctional_fixture"
''')
    def run(extra=None, code=None):
        if code is not None:
            probe.write_text(code)
        env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'PYTHONPATH': str(root),
               'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        env.update(extra or {})
        return subprocess.run([sys.executable, '-m', 'pytest', '-q', '-s',
                               '--confcutdir='+str(suite), str(probe)],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False)
    return run


@pytest.mark.parametrize('key,value', [
    ('STRIPE_SECRET_KEY','sk_live_synthetic_secret'),
    ('STRIPE_API_KEY','rk_live_synthetic_secret'),
    ('DATABASE_URL','postgresql://user:synthetic_secret@db.example/production'),
    ('DATABASE_URL','postgresql://127.0.0.1/customer'),
    ('OPENVEGAS_INTEGRATION_DATABASE_URL','postgresql://127.0.0.1/ov_test_safe?host=db.example'),
    ('PGSERVICE','synthetic_secret'),
])
def test_unsafe_environment_rejected_before_collection(invocation,key,value):
    result=invocation({key:value})
    output=result.stdout+result.stderr
    assert result.returncode != 0
    assert 'pytest safety:' in output
    assert 'COLLECTION_REACHED' not in output
    assert value not in output and 'synthetic_secret' not in output


@pytest.mark.parametrize('host',['127.0.0.1','localhost','[::1]'])
def test_disposable_database_and_mock_fixtures_allowed(invocation,host):
    dsn=f'postgresql://local:fixture@{host}:55439/ov_test_safety'
    result=invocation({'DATABASE_URL':dsn,'OPENVEGAS_INTEGRATION_DATABASE_URL':dsn,
                       'STRIPE_SECRET_KEY':'sk_test_ambient_removed',
                       'SUPABASE_SERVICE_ROLE_KEY':'synthetic_removed'})
    assert result.returncode==0, result.stdout+result.stderr
    assert 'COLLECTION_REACHED' in result.stdout


def test_external_socket_blocked_before_collection_without_connecting(invocation):
    result=invocation(code='''import socket
import pytest
with socket.socket() as sock:
    with pytest.raises(RuntimeError, match="external network access blocked"):
        sock.connect(("192.0.2.1", 443))
with pytest.raises(RuntimeError, match="external network access blocked"):
    socket.getaddrinfo("never-contact.invalid", 443)
def test_ok(): pass
''')
    assert result.returncode==0,result.stdout+result.stderr
