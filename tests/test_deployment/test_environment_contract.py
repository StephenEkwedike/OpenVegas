"""Environment isolation contracts using synthetic files, never the real .env."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import click
import dotenv
import pytest

ROOT = Path(__file__).resolve().parents[2]


def script_module(name):
    spec = importlib.util.spec_from_file_location(f"environment_contract_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli_loader(fake_root):
    tree = ast.parse((ROOT / "openvegas/cli.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_load_openvegas_env_defaults_from_dotenv")
    namespace = {"os": os, "Path": Path, "click": click, "__file__": str(fake_root / "openvegas/cli.py"), "_ENV_DEFAULTS_BOOTSTRAPPED": False}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<cli-env-contract>", "exec"), namespace)  # noqa: S102 -- Whitelisted repository function, no app import.
    return namespace[function.name]


def server_environment_bootstrap(fake_root):
    # Execute only actual environment bootstrap nodes, not routes, auth, or lifespan.
    tree = ast.parse((ROOT / "server/main.py").read_text())
    functions = {"_early_truthy", "_early_resolve_root_dir", "_early_dotenv_override", "_environment_file", "_resolve_root_dir", "_env_truthy", "_dotenv_override_enabled"}
    selected = []
    for node in tree.body:
        is_helper = isinstance(node, ast.FunctionDef) and node.name in functions
        is_root = isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"EARLY_ROOT_DIR", "ROOT_DIR"} for target in node.targets)
        is_load = isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "load_dotenv"
        if is_helper or is_root or is_load:
            selected.append(node)
    touched = []

    def values(path, **kwargs):
        touched.append(Path(path).resolve())
        return dotenv.dotenv_values(path, **kwargs)

    def load(path, **kwargs):
        touched.append(Path(path).resolve())
        return dotenv.load_dotenv(path, **kwargs)

    namespace = {"os": os, "Path": Path, "dotenv_values": values, "load_dotenv": load, "__file__": str(fake_root / "server/main.py")}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "<server-env-contract>", "exec"), namespace)  # noqa: S102 -- Execute only reviewed bootstrap nodes against temporary files.
    return touched


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)
    for name in ("server/main.py", "ui/index.html", "openvegas/cli.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture")
    (tmp_path / ".env").write_text("OPENVEGAS_ROOT_ONLY_SENTINEL=must-not-load\nROOT_ONLY_SENTINEL=must-not-load\nOPENVEGAS_DOTENV_OVERRIDE=1\n")
    return tmp_path


def selected_environment(root, content="OPENVEGAS_SELECTED_SENTINEL=selected\nPLAIN_SELECTED_SENTINEL=selected\n"):
    selected = root / "isolated.env"
    selected.write_text(content)
    os.environ["OPENVEGAS_ENV_FILE"] = str(selected)
    return selected


@pytest.mark.parametrize("surface", ["cli", "server"])
def test_explicit_file_never_loads_root_dotenv(fake_root, surface):
    selected = selected_environment(fake_root)
    if surface == "cli":
        cli_loader(fake_root)()
        assert "PLAIN_SELECTED_SENTINEL" not in os.environ
    else:
        touched = server_environment_bootstrap(fake_root)
        assert touched and set(touched) == {selected.resolve()}
        assert os.environ["PLAIN_SELECTED_SENTINEL"] == "selected"
    assert os.environ["OPENVEGAS_SELECTED_SENTINEL"] == "selected"
    assert "OPENVEGAS_ROOT_ONLY_SENTINEL" not in os.environ
    assert "ROOT_ONLY_SENTINEL" not in os.environ


@pytest.mark.parametrize("surface", ["cli", "server"])
@pytest.mark.parametrize("value", ["missing.env", ""])
def test_missing_explicit_file_fails_without_root_fallback(fake_root, surface, value):
    os.environ["OPENVEGAS_ENV_FILE"] = value
    action = cli_loader(fake_root) if surface == "cli" else lambda: server_environment_bootstrap(fake_root)
    with pytest.raises((RuntimeError, click.ClickException), match="OPENVEGAS_ENV_FILE"):
        action()
    assert "OPENVEGAS_ROOT_ONLY_SENTINEL" not in os.environ


@pytest.mark.parametrize("surface", ["cli", "server"])
def test_explicit_process_override_off_preserves_shell(fake_root, surface):
    selected_environment(fake_root, "OPENVEGAS_SELECTED_SENTINEL=file\n")
    os.environ.update(OPENVEGAS_DOTENV_OVERRIDE="0", OPENVEGAS_SELECTED_SENTINEL="process")
    if surface == "cli":
        cli_loader(fake_root)()
    else:
        server_environment_bootstrap(fake_root)
    assert os.environ["OPENVEGAS_SELECTED_SENTINEL"] == "process"


@pytest.mark.parametrize("surface", ["cli", "server"])
def test_explicit_process_override_on_uses_selected_file(fake_root, surface):
    selected_environment(fake_root, "OPENVEGAS_SELECTED_SENTINEL=file\n")
    os.environ.update(OPENVEGAS_DOTENV_OVERRIDE="1", OPENVEGAS_SELECTED_SENTINEL="process")
    if surface == "cli":
        cli_loader(fake_root)()
    else:
        server_environment_bootstrap(fake_root)
    assert os.environ["OPENVEGAS_SELECTED_SENTINEL"] == "file"
    assert "OPENVEGAS_ROOT_ONLY_SENTINEL" not in os.environ


def test_root_override_setting_cannot_affect_explicit_server_file(fake_root):
    selected_environment(fake_root, "OPENVEGAS_SELECTED_SENTINEL=file\n")
    os.environ.update(OPENVEGAS_RUNTIME_ENV="production", OPENVEGAS_SELECTED_SENTINEL="process")
    server_environment_bootstrap(fake_root)
    assert os.environ["OPENVEGAS_SELECTED_SENTINEL"] == "process"
    assert "OPENVEGAS_DOTENV_OVERRIDE" not in os.environ


def test_selected_server_file_cannot_redirect_second_load_to_root_dotenv(fake_root):
    selected = selected_environment(fake_root, f"OPENVEGAS_ENV_FILE={fake_root / '.env'}\nOPENVEGAS_SELECTED_SENTINEL=selected\n")
    os.environ["OPENVEGAS_DOTENV_OVERRIDE"] = "1"
    touched = server_environment_bootstrap(fake_root)
    assert touched and set(touched) == {selected.resolve()}
    assert "OPENVEGAS_ROOT_ONLY_SENTINEL" not in os.environ


def test_cli_explicit_loader_is_idempotent(fake_root):
    selected = selected_environment(fake_root)
    load = cli_loader(fake_root)
    load()
    selected.write_text("OPENVEGAS_SELECTED_SENTINEL=changed\n")
    load()
    assert os.environ["OPENVEGAS_SELECTED_SENTINEL"] == "selected"


@pytest.mark.parametrize("module", ["openvegas.cli", "server.main"])
def test_full_import_uses_only_explicit_synthetic_environment(fake_root, module):
    selected = selected_environment(fake_root)
    script = r'''
import importlib
import os
import socket
import sys

def denied(*args, **kwargs):
    raise RuntimeError("Environment import contracts prohibit network access")

for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
    setattr(socket, name, denied)
for name in ("connect", "connect_ex", "sendto", "sendmsg"):
    if hasattr(socket.socket, name):
        setattr(socket.socket, name, denied)
sys.path.insert(0, sys.argv[1])
importlib.import_module(sys.argv[2])
assert os.environ["OPENVEGAS_SELECTED_SENTINEL"] == "selected"
assert "OPENVEGAS_ROOT_ONLY_SENTINEL" not in os.environ
assert "ROOT_ONLY_SENTINEL" not in os.environ
print("isolated-import-ok")
'''
    env = {
        "HOME": str(fake_root), "USERPROFILE": str(fake_root),
        "XDG_CONFIG_HOME": str(fake_root / "config"),
        "OPENVEGAS_ROOT": str(fake_root), "OPENVEGAS_ENV_FILE": str(selected),
        "OPENVEGAS_DOTENV_OVERRIDE": "0", "OPENVEGAS_RUNTIME_ENV": "test",
        "OPENVEGAS_TEST_MODE": "1", "OPENVEGAS_QR_AUTO_INSTALL": "0",
        "OPENVEGAS_ENABLE_TOUCHID": "0", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(ROOT), module],
        cwd=fake_root, env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "isolated-import-ok" in result.stdout


def local_config(root, database="postgresql://127.0.0.1:54322/local"):
    path = root / ".env.local"
    path.write_text(f"DATABASE_URL={database}\nSUPABASE_URL=http://127.0.0.1:54321\nOPENVEGAS_BACKEND_URL=http://127.0.0.1:8000\nREDIS_URL=redis://127.0.0.1:6379/0\n")
    return path


def test_local_launcher_uses_private_home_and_drops_shell_configuration(fake_root, monkeypatch):
    module = script_module("run_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    os.environ.update(DATABASE_URL="must-not-inherit", OPENAI_API_KEY="must-not-inherit", HTTP_PROXY="must-not-inherit", PYTHONPATH="must-not-inherit")
    path = local_config(fake_root)
    env = module.local_environment(path)
    assert env["HOME"] == str(fake_root / ".local/home")
    assert env["USERPROFILE"] == env["HOME"]
    assert env["OPENVEGAS_ENV_FILE"] == str(path.resolve())
    assert env["OPENVEGAS_ROOT"] == str(fake_root)
    assert env["OPENVEGAS_DOTENV_OVERRIDE"] == "0"
    assert env["OPENVEGAS_TEST_MODE"] == "0"
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    assert env["DATABASE_URL"].startswith("postgresql://127.0.0.1:")
    for name in ("OPENAI_API_KEY", "HTTP_PROXY", "PYTHONPATH", "ROOT_ONLY_SENTINEL"):
        assert name not in env


@pytest.mark.parametrize("key", ["DATABASE_URL", "SUPABASE_URL", "OPENVEGAS_BACKEND_URL", "REDIS_URL"])
def test_local_launcher_rejects_remote_service(fake_root, monkeypatch, key):
    module = script_module("run_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    path = local_config(fake_root)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(f"{key}=http://example.invalid" if line.startswith(key + "=") else line for line in lines))
    with pytest.raises(ValueError, match=key):
        module.local_environment(path)
    assert not (fake_root / ".local/home").exists()


def test_local_launcher_rejects_database_query_host_override(fake_root, monkeypatch):
    module = script_module("run_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    path = local_config(fake_root, "postgresql://127.0.0.1/local?host=example.invalid")
    with pytest.raises(ValueError):
        module.local_environment(path)


def test_local_launcher_rejects_remote_fallback_in_multihost_database_url(fake_root, monkeypatch):
    module = script_module("run_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    path = local_config(fake_root, "postgresql://127.0.0.1:5432,example.invalid:5432/local")
    with pytest.raises(ValueError):
        module.local_environment(path)


def test_local_launcher_does_not_interpolate_ambient_values(fake_root, monkeypatch):
    module = script_module("run_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    path = local_config(fake_root)
    path.write_text(path.read_text() + "OPENVEGAS_SYNTHETIC_VALUE=${AMBIENT_MARKER}\n")
    os.environ["AMBIENT_MARKER"] = "must-not-inherit"
    assert module.local_environment(path)["OPENVEGAS_SYNTHETIC_VALUE"] == "${AMBIENT_MARKER}"


def test_setup_never_overwrites_existing_local_configuration(fake_root, monkeypatch):
    module = script_module("setup_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    target = fake_root / ".env.local"
    target.write_text("synthetic-existing-file")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not invoke Supabase when configuration exists"))
    assert module.main() == 1
    assert target.read_text() == "synthetic-existing-file"


@pytest.mark.parametrize("remote", [False, True])
def test_setup_uses_only_synthetic_local_status(fake_root, monkeypatch, remote, capsys):
    module = script_module("setup_local")
    monkeypatch.setattr(module, "ROOT", fake_root)
    (fake_root / ".env.local.example").write_text("DATABASE_URL=\nSUPABASE_URL=\nSUPABASE_ANON_KEY=\nSUPABASE_JWT_SECRET=\n")
    status = {"DB_URL": "postgresql://127.0.0.1:54322/local", "API_URL": "http://127.0.0.1:54321", "ANON_KEY": "synthetic-marker", "JWT_SECRET": "synthetic-signing-marker"}
    if remote:
        status["DB_URL"] = "postgresql://example.invalid/local"
    calls = []

    def status_command(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(status), "")

    monkeypatch.setattr(module.subprocess, "run", status_command)
    original = (fake_root / ".env").read_text()
    assert module.main() == (1 if remote else 0)
    assert calls[0][0] == ["supabase", "status", "--workdir", str(fake_root / "dev"), "-o", "json"]
    assert calls[0][1]["timeout"] <= 30
    assert (fake_root / ".env").read_text() == original
    target = fake_root / ".env.local"
    assert target.exists() is not remote
    if not remote:
        assert target.stat().st_mode & 0o777 == 0o600
        assert dotenv.dotenv_values(target, interpolate=False)["SUPABASE_ANON_KEY"] == "synthetic-marker"
    assert "synthetic-marker" not in capsys.readouterr().out
