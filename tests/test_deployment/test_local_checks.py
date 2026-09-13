"""Offline contracts for the read-only local checker; no application imports."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_local.py"
SPEC = importlib.util.spec_from_file_location("openvegas_check_local", SCRIPT)
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class LocalCheckTests(unittest.TestCase):
    def test_help_does_not_import_application_or_need_pytest(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(SCRIPT), "--help"],
                cwd=temporary, env=checks.clean_env(Path(temporary)), capture_output=True,
                text=True, timeout=5, check=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("probe", result.stdout)
        self.assertIn("fast", result.stdout)

    def test_environment_is_allowlisted(self):
        with patch.dict(os.environ, {"DATABASE_URL": "private", "OPENAI_API_KEY": "private", "HTTP_PROXY": "private", "PYTHONPATH": "private", "PYTEST_ADDOPTS": "private"}):
            env = checks.clean_env(Path("/synthetic/home"), Path("/synthetic/source"))
        for key in ("DATABASE_URL", "OPENAI_API_KEY", "HTTP_PROXY", "PYTHONPATH", "PYTEST_ADDOPTS"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HOME"], "/synthetic/home")
        self.assertEqual(env["OPENVEGAS_ROOT"], "/synthetic/source")
        self.assertEqual(env["PYTHON_KEYRING_BACKEND"], "keyring.backends.null.Keyring")
        self.assertEqual(env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"], "1")

    def test_snapshot_excludes_secrets_notes_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "input", root / "snapshot"
            for relative in ("pyproject.toml", "server/main.py", "openvegas/cli.py", "ui/index.html", ".env", "ui/.env", "notes.md", "tests/.env.py", "tests/private.pem", "ui/assets/notes.md", "scripts/helper.py"):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic data", encoding="utf-8")
            external = root / "external.py"
            external.write_text("synthetic data", encoding="utf-8")
            (source / "openvegas/link.py").symlink_to(external)
            checks.snapshot_source(source, target, checks.clean_env(root), time.monotonic() + 5)
            names = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        self.assertEqual(names, {"pyproject.toml", "server/main.py", "openvegas/cli.py", "ui/index.html", "scripts/helper.py"})

    def test_tracked_snapshot_does_not_include_untracked_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "input", root / "snapshot"
            for relative in (".git", "pyproject.toml", "server/main.py", "scripts/private.py"):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic", encoding="utf-8")
            fake = subprocess.CompletedProcess([], 0, b"pyproject.toml\0server/main.py\0", b"")
            with patch.object(checks.subprocess, "run", return_value=fake):
                checks.snapshot_source(source, target, checks.clean_env(root), time.monotonic() + 5)
            self.assertFalse((target / "scripts/private.py").exists())

    def test_default_probe_target_is_loopback(self):
        self.assertEqual(checks.parser().parse_args(["probe"]).base_url, "http://127.0.0.1:8000")
        self.assertEqual(checks.normalize_base_url("http://localhost:8000/", False), "http://127.0.0.1:8000")
        self.assertEqual(checks.normalize_base_url("http://[::1]:8000", False), "http://[::1]:8000")

    def test_remote_requires_opt_in_and_rejects_credentials(self):
        for value in ("https://example.invalid", "http://127.0.0.1.example.invalid", "http://2130706433", "file:///tmp/data", "http://user:pass@127.0.0.1", "http://127.0.0.1/?secret=value", "http://127.0.0.1/path", "http://127.0.0.1/#fragment"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checks.normalize_base_url(value, False)
        self.assertEqual(checks.normalize_base_url("https://example.invalid", True), "https://example.invalid")
        with self.assertRaises(ValueError):
            checks.normalize_base_url("https://user:pass@example.invalid", True)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(checks.RejectRedirects().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_probe_only_uses_allowlisted_read_only_paths(self):
        paths = [path for _, path, _ in checks.PROBES]
        self.assertIn("/health/ready", paths)
        self.assertIn("/inference/mode", paths)
        self.assertFalse(any(part in path for path in paths for part in ("bootstrap", "checkout", "signup", "migration")))

    def test_probe_status_contracts_do_not_accept_fake_readiness(self):
        self.assertTrue(checks.response_ok("ready", 200, "application/json", b'{"status":"ready","mode":"runtime"}'))
        self.assertFalse(checks.response_ok("ready", 200, "application/json", b'{"status":"ready","mode":"test"}'))
        self.assertFalse(checks.response_ok("ready", 500, "application/json", b'{"status":"ready"}'))
        self.assertFalse(checks.response_ok("ready", 200, "text/html", b'<html>error</html>'))
        self.assertTrue(checks.response_ok("auth", 401, "", b""))
        self.assertTrue(checks.response_ok("auth", 403, "", b""))
        self.assertFalse(checks.response_ok("auth", 200, "", b""))
        self.assertFalse(checks.response_ok("auth", 404, "", b""))

    def test_probe_uses_get_and_suppresses_response_details(self):
        requests = []

        class Response:
            code = 503

            def __init__(self):
                self.headers = {"Content-Type": "text/plain"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                return b"synthetic-private-response-do-not-echo"

        class Opener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return Response()

        output = io.StringIO()
        with patch.object(checks.urllib.request, "build_opener", return_value=Opener()), redirect_stdout(output):
            code = checks.probe_checks("http://127.0.0.1:8000", 1)
        self.assertEqual(code, 1)
        self.assertEqual(len(requests), len(checks.PROBES))
        self.assertTrue(all(request.get_method() == "GET" and 0 < timeout <= 1 for request, timeout in requests))
        self.assertNotIn("synthetic-private", output.getvalue())
        self.assertEqual(output.getvalue().count("FAIL:"), len(checks.PROBES))

    def test_offline_guard_blocks_connections_during_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap = root / "bootstrap.py"
            bootstrap.write_text(checks.BOOTSTRAP, encoding="utf-8")
            (root / "pytest.py").write_text(
                'import socket\n'
                'def main(args):\n'
                '    operations = [(socket.getaddrinfo, ("example.invalid", 80)), '
                '(socket.create_connection, (("127.0.0.1", 1),)), '
                '(socket.socket().connect, (("127.0.0.1", 1),))]\n'
                '    for operation, arguments in operations:\n'
                '        try:\n'
                '            operation(*arguments)\n'
                '        except RuntimeError as error:\n'
                '            assert "offline checks prohibit" in str(error)\n'
                '        else:\n'
                '            return 1\n'
                '    return 0\n', encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(bootstrap), str(root)],
                cwd=root, env=checks.clean_env(root), capture_output=True, timeout=5, check=False,
            )
        self.assertEqual(result.returncode, 0)

    def test_timeout_cannot_exceed_thirty_seconds(self):
        for value in ("0", "-1", "31", "nan", "inf", "invalid"):
            with self.subTest(value=value), self.assertRaises(checks.argparse.ArgumentTypeError):
                checks.timeout_arg(value)
        self.assertEqual(checks.timeout_arg("0.1"), 0.1)

    def test_child_process_deadline_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(TimeoutError):
                checks.run_bounded([sys.executable, "-I", "-B", "-c", "import time; time.sleep(5)"], cwd=root, env=checks.clean_env(root), timeout=0.1)

    def test_offline_guard_is_installed_before_pytest_collection(self):
        guard = checks.BOOTSTRAP
        self.assertLess(guard.index('setattr(socket, name, deny_network)'), guard.index('import pytest'))
        for operation in ("connect", "connect_ex", "sendto", "getaddrinfo"):
            self.assertIn('"' + operation + '"', guard)


if __name__ == "__main__":
    unittest.main()
