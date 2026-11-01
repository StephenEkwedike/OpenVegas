"""Owned plugin updates must not become destructive reinstall instructions."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from openvegas.integrations import installer
from openvegas.integrations.credentials import IntegrationError


@pytest.mark.parametrize('patch,expected', [(287, False), (288, False), (289, True), (301, True)])
def test_mod_requires_ui_fault_compatible_host(monkeypatch, patch, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(installer.shutil, 'which', lambda _: '/fake/claude')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=f'Claude Code 2.1.{patch}'))
    assert installer.detect('claude')['mod'] is expected


async def codex_plugin_details(executable, marketplace, env):
    process = await asyncio.create_subprocess_exec(
        executable, 'app-server', '--stdio', env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        requests = [
            ('initialize', {'clientInfo': {'name': 'ov_plugin_test', 'version': '1'},
                            'capabilities': {'experimentalApi': True}}),
            ('plugin/read', {'marketplacePath': str(marketplace), 'pluginName': 'openvegas'}),
        ]
        for number, (method, params) in enumerate(requests, 1):
            process.stdin.write((json.dumps({'id': number, 'method': method, 'params': params}) + '\n').encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 15)
                assert line, 'Codex app server exited before reading the plugin'
                result = json.loads(line)
                if result.get('id') == number:
                    assert 'error' not in result, result
                    break
        return result['result']['plugin']
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


def test_codex_declares_mcp_in_compatible_manifest():
    files = installer.bundle_files({'host': 'codex', 'mod': False}, 'test')
    assert 'plugins/openvegas/plugin.json' not in files
    manifest = json.loads(files['plugins/openvegas/.codex-plugin/plugin.json'])
    assert manifest['mcpServers'] == './.mcp.json'
    assert manifest['skills'] == './skills/'
    server = json.loads(files['plugins/openvegas/.mcp.json'])['mcpServers']['openvegas']
    assert server['command'] and '--profile' in server['args']
    assert server['tool_timeout_sec'] == 900


def test_helper_pins_setup_home_before_loading_account_modules(monkeypatch, tmp_path):
    import subprocess
    setup_home = tmp_path / 'isolated-home'
    monkeypatch.setenv('HOME', str(setup_home))
    command = installer.helper('codex')
    bootstrap = command[command.index('-c') + 1]
    prefix = bootstrap.split('; from openvegas.integrations.mcp_server import main; main()')[0]
    env = dict(os.environ, HOME=str(tmp_path / 'native-host-home'))
    result = subprocess.run([command[0], '-I', '-c', prefix +
        '; from openvegas.config import CONFIG_DIR; print(CONFIG_DIR)'],
        env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(setup_home / '.openvegas')


def test_skill_dispatches_play_before_optional_controls():
    skill = (installer.ASSETS / 'SKILL.md').read_text()
    assert skill.index('## Play Dispatch First') < skill.index('## Explicit Q50 Motion Review')
    assert 'call the OpenVegas `play` tool once as the next action' in skill


def test_game_login_recovery_does_not_loop_through_cosmetic_setup():
    skill = (installer.ASSETS / 'SKILL.md').read_text()
    recovery = skill.split('For `game_login_required`,', 1)[1].split('Codex\'s forms alone', 1)[0]
    assert 'Use plain `openvegas login` only when no login_command is supplied' in recovery
    assert 'Use the exact returned `login_command`' in recovery
    assert '`openvegas go` or `login --browser`' in recovery
    assert 'cannot restore the full game account' in recovery
    assert 'Resume the ordinary host in the same terminal afterward' in recovery


@pytest.fixture(params=["claude", "codex"])
def host_install(request, tmp_path, monkeypatch):
    host = request.param
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENVEGAS_BACKEND_URL", "http://localhost:8000")
    info = {"host": host, "executable": "/fake/" + host, "version": "2.1.301", "mod": host == "claude"}
    monkeypatch.setattr(installer, "detect", lambda _: info)
    actual = {"registered": False, "installed": False, "enabled": False}
    calls = []
    fail = {"next_install": False}

    def run(cmd, **kw):
        calls.append(cmd)
        action = cmd[2]
        if action == "marketplace":
            if cmd[3] == "add":
                actual.update(registered=True, path=cmd[4])
            elif cmd[3] == "remove":
                actual.update(registered=False, path=None)
        elif action in {"install", "add"}:
            if fail["next_install"]:
                fail["next_install"] = False
                raise IntegrationError("synthetic host failure")
            actual.update(installed=True, enabled=True, plugin_path=actual["path"] + "/plugins/openvegas")
        elif action in {"uninstall", "remove"}:
            actual.update(installed=False, enabled=False, plugin_path=None)

    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "registration", lambda *a, **kw: dict(actual))
    original = installer.install(host)
    bundle_files = installer.bundle_files

    def newer(*args):
        return {**bundle_files(*args), "revision.txt": "new reviewed revision"}

    monkeypatch.setattr(installer, "bundle_files", newer)
    return host, original, actual, calls, fail


def test_update_requires_consent_then_is_idempotent_across_workspaces(host_install, tmp_path, monkeypatch):
    host, original, actual, calls, _ = host_install
    count = len(calls)
    with pytest.raises(IntegrationError, match="approve its update"):
        installer.install(host)
    assert len(calls) == count
    first_plan = installer.install(host, dry_run=True)
    project = tmp_path / "different-project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert installer.install(host, dry_run=True)["path"] == first_plan["path"]
    result = installer.install(host, upgrade=True)
    assert result["updated"] and result["path"] != original["path"]
    assert actual["path"] == result["path"]
    assert Path(original["path"]).exists()
    previous = Path(original["path"]).parent / "previous.json"
    assert json.loads(previous.read_text())["path"] == original["path"]
    count = len(calls)
    assert installer.install(host, upgrade=True)["unchanged"]
    assert len(calls) == count
    assert all(cmd[4] == original["marketplace"] for cmd in calls if cmd[2:4] == ["marketplace", "remove"])


def test_update_failure_restores_previous_registration_and_receipt(host_install):
    host, original, actual, _, fail = host_install
    receipt = Path(original["path"]).parent / "installed.json"
    before = receipt.read_bytes()
    fail["next_install"] = True
    with pytest.raises(IntegrationError, match="previous plugin was restored"):
        installer.install(host, upgrade=True)
    assert actual["path"] == original["path"] and actual["enabled"]
    assert receipt.read_bytes() == before


@pytest.mark.parametrize("change", ["bundle", "registration", "disabled", "symlink"])
def test_changed_install_is_not_overwritten(host_install, change):
    host, original, actual, calls, _ = host_install
    if change == "bundle":
        (Path(original["path"]) / "user-file.txt").write_text("preserve me")
    elif change == "registration":
        actual["path"] = "/someone/elses/plugin"
    elif change == "disabled":
        actual["enabled"] = False
    else:
        (Path(original["path"]) / "linked").symlink_to("/tmp")
    count = len(calls)
    with pytest.raises(IntegrationError):
        installer.install(host, upgrade=True)
    assert len(calls) == count


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_installed_host_update_in_isolated_home(host, tmp_path, monkeypatch):
    if os.getenv("OV_TEST_NATIVE_INSTALL_HOSTS") != "1":
        pytest.skip("Opt in to installed host CLI checks with isolated HOME")
    assert installer.detect(host), "Requested host check cannot silently skip a missing CLI"
    home = tmp_path.resolve()
    (home / "claude").mkdir()
    (home / "codex").mkdir()
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(home / "claude"),
               CODEX_HOME=str(home / "codex"), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
    monkeypatch.setenv("OPENVEGAS_INTEGRATION_PROFILE", str(home / "profile"))
    monkeypatch.setenv("OPENVEGAS_BACKEND_URL", "http://127.0.0.1:8000")
    original = installer.bundle_files
    first = installer.install(host, env=env)
    try:
        monkeypatch.setattr(installer, "bundle_files", lambda *a: {
            **original(*a), "plugins/openvegas/revision.txt": "approved update"})
        second = installer.install(host, upgrade=True, env=env)
        assert second["updated"] and second["path"] != first["path"]
        assert installer.install(host, upgrade=True, env=env)["unchanged"]
        actual = installer.registration(installer.detect(host), second["marketplace"], env=env)
        assert actual["path"] == second["path"] and actual["enabled"]
        # Check what the host actually loads, not only our source receipt.
        listing = json.loads(installer._run([second["executable"], "plugin", "list", "--json"], env=env).stdout)
        rows = listing if host == "claude" else listing["installed"]
        entry = next(r for r in rows if r.get("id", r.get("pluginId")) == "openvegas@" + second["marketplace"])
        loaded = Path(entry.get("readFromFolder") or entry.get("installPath") or entry["source"]["path"])
        assert (loaded / "revision.txt").read_text() == "approved update"
        if host == 'codex':
            detail = asyncio.run(codex_plugin_details(
                second['executable'], Path(second['path']) / '.agents/plugins/marketplace.json', env))
            assert detail['mcpServers'] == ['openvegas']
            assert len(detail['skills']) == 1
        print(json.dumps({"host": host, "version": second["version"], "isolated_update": True, "repeat": True}))
    finally:
        installer.uninstall(host, env=env)
