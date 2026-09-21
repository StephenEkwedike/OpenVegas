"""Offline launcher contracts: fail closed, preserve data and isolate the CLI."""

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

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stack = load("openvegas_local_stack", ROOT / "scripts/local_stack.py")
local = load("openvegas_run_local", ROOT / "scripts/run_local.py")


class LocalStackTests(unittest.TestCase):
    def launch(self, *, fail_at=None, cancel_at=None, build=True, configured=True):
        steps = []

        class FakeRunner:
            def step(self, name, command, **kwargs):
                steps.append((name, command, kwargs))
                if len(steps) == fail_at:
                    raise stack.StartupError("synthetic failure")
                if len(steps) == cancel_at:
                    raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            if configured:
                (root / ".env.local").touch()
            with (
                patch.object(stack, "ROOT", root),
                patch.object(stack, "local_config"),
                patch.object(stack, "Runner", return_value=FakeRunner()),
                patch.object(stack, "prerequisites"),
                patch.object(stack, "show_menu") as menu,
                redirect_stdout(io.StringIO()),
            ):
                code = stack.main([] if build else ["--no-build"])
                shown = menu.called
        return code, shown, steps

    def test_order_and_health_gate(self):
        code, shown, steps = self.launch()
        self.assertEqual(code, 0)
        self.assertTrue(shown)
        self.assertEqual(len(steps), 5)
        self.assertEqual(steps[0][1][:2], ["docker", "info"])
        self.assertEqual(steps[1][1][:2], ["supabase", "start"])
        self.assertIn("--check", steps[2][1])
        self.assertIn("--wait", steps[3][1])
        self.assertIn("--wait-timeout", steps[3][1])
        self.assertIn("--build", steps[3][1])
        self.assertEqual(steps[4][1][-1], "probe")

    def test_any_failed_phase_stops_sequence_and_hides_menu(self):
        for phase in range(1, 6):
            with self.subTest(phase=phase):
                code, shown, steps = self.launch(fail_at=phase)
                self.assertEqual(code, 1)
                self.assertFalse(shown)
                self.assertEqual(len(steps), phase)

    def test_ctrl_c_during_supabase_never_starts_compose(self):
        code, shown, steps = self.launch(cancel_at=2)
        self.assertEqual(code, 130)
        self.assertFalse(shown)
        self.assertEqual(len(steps), 2)

    def test_resume_reuses_image_when_requested(self):
        code, shown, steps = self.launch(build=False)
        self.assertEqual(code, 0)
        self.assertTrue(shown)
        self.assertIn("--no-build", steps[3][1])
        self.assertNotIn("--build", steps[3][1])

    def test_only_missing_configuration_is_created(self):
        _, _, existing = self.launch()
        _, _, new = self.launch(configured=False)
        self.assertFalse(any("setup_local.py" in " ".join(s[1]) for s in existing))
        self.assertTrue(any("setup_local.py" in " ".join(s[1]) for s in new))

    def test_compose_pins_project_file_and_local_env(self):
        command = stack.compose("up")
        self.assertIn(str(ROOT / ".env.local"), command)
        self.assertIn(str(ROOT / "compose.yaml"), command)
        self.assertEqual(command[command.index("-p") + 1], "ov-restoration")
        self.assertNotIn(str(ROOT / ".env"), command)

    def test_tool_environment_does_not_inherit_app_or_compose_settings(self):
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "private",
                "OPENAI_API_KEY": "private",
                "COMPOSE_FILE": "unrelated.yaml",
                "COMPOSE_PROJECT_NAME": "other",
            },
        ):
            env = stack.tool_environment()
        for key in ("DATABASE_URL", "OPENAI_API_KEY", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
            self.assertNotIn(key, env)

    def test_runner_keeps_credential_bearing_output_private(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(stack, "ROOT", Path(folder)):
            runner = stack.Runner()
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(stack.StartupError):
                runner.step(
                    "Failure",
                    [sys.executable, "-c", "print('synthetic-secret'); raise SystemExit(7)"],
                    timeout=5,
                )
            log = next(runner.logs.glob("*.log"))
            self.assertIn("synthetic-secret", log.read_text())
            self.assertNotIn("synthetic-secret", output.getvalue())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_runner_timeout_returns_control(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(stack, "ROOT", Path(folder)),
            redirect_stdout(io.StringIO()),
        ):
            runner = stack.Runner()
            started = time.monotonic()
            with self.assertRaisesRegex(stack.StartupError, "timed out"):
                runner.step(
                    "Hung child",
                    [sys.executable, "-c", "import time; time.sleep(100)"],
                    timeout=0.1,
                )
            self.assertLess(time.monotonic() - started, 5)

    def test_stop_retains_data_and_is_project_scoped(self):
        with (
            patch.object(stack, "Runner") as runner,
            patch.object(stack, "prerequisites"),
            patch.object(stack, "show_menu") as menu,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(stack.main(["--stop"]), 0)
        calls = runner.return_value.step.call_args_list
        self.assertEqual(calls[0].args[1], stack.compose("stop"))
        self.assertEqual(calls[1].args[1], ["supabase", "stop", "--workdir", str(ROOT / "dev")])
        self.assertFalse(menu.called)

    def test_status_does_not_start_or_migrate(self):
        with (
            patch.object(stack, "Runner") as runner,
            patch.object(stack, "prerequisites"),
            patch.object(stack, "local_config"),
            patch.object(stack, "show_menu") as menu,
        ):
            self.assertEqual(stack.main(["--status"]), 0)
        self.assertEqual(runner.return_value.step.call_count, 1)
        self.assertEqual(runner.return_value.step.call_args.args[1][-1], "probe")
        self.assertTrue(menu.called)

    def config(self, folder, extra=""):
        text = (
            (ROOT / ".env.local.example")
            .read_text()
            .replace(
                "DATABASE_URL=\n",
                "DATABASE_URL=postgresql://postgres:local@127.0.0.1:54322/postgres\n",
            )
            .replace("SUPABASE_URL=\n", "SUPABASE_URL=http://127.0.0.1:54321\n")
            .replace("SUPABASE_ANON_KEY=\n", "SUPABASE_ANON_KEY=synthetic-local-key\n")
        )
        path = Path(folder) / ".env.local"
        path.write_text(text + extra)
        return path

    def test_local_configuration_and_cli_profile_are_isolated(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(local, "ROOT", Path(folder)),
            patch.object(stack, "ROOT", Path(folder)),
            patch.dict(sys.modules, {"run_local": local}),
        ):
            path = self.config(folder)
            env = local.local_environment(path)
            stack.local_config()
            self.assertEqual(env["OPENVEGAS_BACKEND_URL"], "http://127.0.0.1:8000")
            self.assertEqual(env["HOME"], str(Path(folder) / ".local/home"))
            self.assertEqual(env["OPENVEGAS_ENABLE_TOUCHID"], "0")

    def test_launcher_refuses_remote_or_wrong_port_configuration(self):
        for extra in (
            "SUPABASE_PUBLIC_URL=https://example.invalid\n",
            "DATABASE_URL=postgresql://postgres:local@127.0.0.1:5432/postgres\n",
            "OPENVEGAS_BACKEND_URL=https://app.openvegas.ai\n",
        ):
            with (
                self.subTest(extra=extra),
                tempfile.TemporaryDirectory() as folder,
                patch.object(local, "ROOT", Path(folder)),
                patch.object(stack, "ROOT", Path(folder)),
                patch.dict(sys.modules, {"run_local": local}),
            ):
                self.config(folder, extra)
                with self.assertRaises(stack.StartupError):
                    stack.local_config()

    def test_wrappers_work_from_other_directories_and_preserve_arguments(self):
        for name, suffix in (
            ("start", ["scripts/local_stack.py"]),
            ("openvegas", ["scripts/run_local.py", "cli"]),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / "repo with spaces"
                (root / "dev").mkdir(parents=True)
                (root / ".venv/bin").mkdir(parents=True)
                wrapper = root / "dev" / name
                wrapper.write_text((ROOT / "dev" / name).read_text())
                python = root / ".venv/bin/python"
                python.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n')
                python.chmod(0o700)
                result = subprocess.run(
                    ["sh", str(wrapper), "argument with spaces"],
                    cwd=folder,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn(f"<{root / suffix[0]}>", result.stdout)
                self.assertIn("<argument with spaces>", result.stdout)
                if name == "openvegas":
                    self.assertIn("<cli>", result.stdout)

    def test_menu_contains_user_and_developer_access_points_without_secrets(self):
        output = io.StringIO()
        with redirect_stdout(output):
            stack.show_menu()
        text = output.getvalue()
        for value in (
            "http://127.0.0.1:8000/ui",
            "http://127.0.0.1:54324",
            "./dev/openvegas login",
            "./dev/openvegas ui",
            "./dev/openvegas chat",
            "./dev/openvegas ops diagnostics",
            "./dev/start --stop",
        ):
            self.assertIn(value, text)
        for value in ("postgres:postgres", "SUPABASE_ANON_KEY=", ":54323"):
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
