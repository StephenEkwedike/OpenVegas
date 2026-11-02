"""Explicit isolated-host acceptance, never personal VS Code or real inference."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv('OV_TEST_NATIVE_RESUME') != '1', reason='Native fixture requires opt-in')
def test_installed_codex_same_provider_context_and_original_auth_return():
    if sys.platform != 'darwin' or not shutil.which('codex') or not Path('/usr/bin/sandbox-exec').exists():
        pytest.skip('macOS native Codex and external-network-denial policy required')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / 'scripts/verify_codex_native_resume.py')],
        cwd=root, env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True, text=True, timeout=100, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'host return with original auth' in result.stdout
    assert 'resume_no_daemon_sanitized True restore_args_preserved True' in result.stdout
    assert 'native_model_menu_reviewed_only True' in result.stdout
    assert 'native_effort_menu_verified True menu_provider_calls 0' in result.stdout
    print(result.stdout)
