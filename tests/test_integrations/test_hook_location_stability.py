"""Unrelated runtime updates must not churn the host's reviewed hook source."""
import json
from pathlib import Path

import pytest

from openvegas.integrations import installer
from openvegas.integrations.credentials import IntegrationError
from tests.test_integrations.test_plugin_updates import host_install


async def native_hooks(executable, env, cwd):
    import asyncio
    process = await asyncio.create_subprocess_exec(executable, 'app-server', '--stdio',
        env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        for number, (method, params) in enumerate([
            ('initialize', {'clientInfo': {'name': 'ov_hook_test', 'version': '1'},
                            'capabilities': {'experimentalApi': True}}),
            ('hooks/list', {'cwds': [str(cwd)]}),
        ], 1):
            process.stdin.write((json.dumps({'id': number, 'method': method, 'params': params}) + '\n').encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 15)
                assert line
                result = json.loads(line)
                if result.get('id') == number:
                    assert 'error' not in result, result
                    break
        return result['result']['data'][0]['hooks']
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


def runtime_change(monkeypatch):
    original = installer.bundle_files
    def newer(*args):
        files = original(*args)
        files.pop('revision.txt', None)
        name = 'plugins/openvegas/.mcp.json'
        data = json.loads(files[name])
        data['mcpServers']['openvegas']['command'] = '/new/reviewed/openvegas'
        files[name] = json.dumps(data, indent=2) + '\n'
        return files
    monkeypatch.setattr(installer, 'bundle_files', newer)


def test_runtime_update_keeps_codex_hook_location(host_install, monkeypatch):
    host, before, actual, calls, _ = host_install
    if host != 'codex':
        return
    runtime_change(monkeypatch)
    root = Path(before['path'])
    hooks = (root / 'plugins/openvegas/hooks/hooks.json').read_bytes()
    count = len(calls)
    after = installer.install(host, upgrade=True)
    assert after['path'] == before['path'] and after['hooks_unchanged']
    assert len(calls) == count  # No uninstall/add; never writes Codex trust.
    assert (root / 'plugins/openvegas/hooks/hooks.json').read_bytes() == hooks
    assert installer.install(host, upgrade=True)['unchanged']
    assert len(calls) == count
    receipt = json.loads((root.parent / 'installed.json').read_text())
    assert installer._verified_previous(receipt, root.parent, before['marketplace'], actual) == root


def test_runtime_receipt_failure_restores_old_bundle(host_install, monkeypatch):
    host, before, actual, calls, _ = host_install
    if host != 'codex':
        return
    runtime_change(monkeypatch)
    from openvegas.emotes import spool
    original = spool.atomic_write
    def fail(fd, name, data):
        if name == 'installed.json':
            raise OSError('synthetic receipt failure')
        return original(fd, name, data)
    monkeypatch.setattr(spool, 'atomic_write', fail)
    root = Path(before['path'])
    snapshot = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    receipt = (root.parent / 'installed.json').read_bytes()
    with pytest.raises(OSError, match='synthetic'):
        installer.install(host, upgrade=True)
    assert all(p.read_bytes() == data for p, data in snapshot.items())
    assert (root.parent / 'installed.json').read_bytes() == receipt


def test_changed_hooks_never_use_runtime_only_path(host_install, monkeypatch):
    host, before, actual, calls, _ = host_install
    if host != 'codex':
        return
    runtime_change(monkeypatch)
    original = installer.bundle_files
    def changed(*args):
        files = original(*args)
        files['plugins/openvegas/hooks/hooks.json'] += '\n'
        return files
    monkeypatch.setattr(installer, 'bundle_files', changed)
    after = installer.install(host, upgrade=True)
    assert after['path'] != before['path']
    assert not after.get('hooks_unchanged')


def test_runtime_update_requires_consent_and_refuses_tampering(host_install, monkeypatch):
    host, before, actual, calls, _ = host_install
    if host != 'codex':
        return
    runtime_change(monkeypatch)
    with pytest.raises(IntegrationError, match='approve its update'):
        installer.install(host)
    (Path(before['path']) / 'plugins/openvegas/.mcp.json').write_text('{}')
    with pytest.raises(IntegrationError, match='modified'):
        installer.install(host, upgrade=True)


def test_native_codex_runtime_update_isolated_home(tmp_path, monkeypatch):
    import os
    import asyncio
    from tests.test_integrations.test_plugin_updates import codex_plugin_details
    if os.getenv('OV_TEST_NATIVE_INSTALL_HOSTS') != '1':
        pytest.skip('Opt in to native Codex registration using isolated HOME')
    home = tmp_path.resolve()
    (home / 'codex').mkdir()
    env = {k: os.environ[k] for k in ('PATH', 'TMPDIR', 'LANG') if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / 'codex'))
    monkeypatch.setenv('OPENVEGAS_INTEGRATION_PROFILE', str(home / 'profile'))
    monkeypatch.setenv('OPENVEGAS_BACKEND_URL', 'http://127.0.0.1:8000')
    first = installer.install('codex', env=env)
    try:
        hooks_before = asyncio.run(native_hooks(first['executable'], env, home))
        assert len(hooks_before) == 2
        runtime_change(monkeypatch)
        second = installer.install('codex', upgrade=True, env=env)
        assert second['path'] == first['path'] and second['hooks_unchanged']
        assert installer.install('codex', upgrade=True, env=env)['unchanged']
        hooks_after = asyncio.run(native_hooks(first['executable'], env, home))
        assert hooks_after == hooks_before  # Includes host currentHash, sourcePath and key.
        details = asyncio.run(codex_plugin_details(
            first['executable'], Path(first['path']) / '.agents/plugins/marketplace.json', env))
        assert details['mcpServers'] == ['openvegas']
        listing = json.loads(installer._run([first['executable'], 'plugin', 'list', '--json'], env=env).stdout)
        entry = next(r for r in listing['installed'] if r.get('id', r.get('pluginId')) == 'openvegas@' + first['marketplace'])
        loaded = Path(entry.get('readFromFolder') or entry.get('installPath') or entry['source']['path'])
        assert json.loads((loaded / '.mcp.json').read_text())['mcpServers']['openvegas']['command'] == '/new/reviewed/openvegas'
    finally:
        installer.uninstall('codex', env=env)
