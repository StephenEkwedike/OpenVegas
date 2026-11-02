"""Include offline Node terminal regressions in the regular packaging gate."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_public_launcher_loading_terminal_regressions(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the public launcher terminal regressions")
    suite = Path(__file__).with_suffix(".mjs")
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--test", str(suite)],
        cwd=tmp_path,
        env={"HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
