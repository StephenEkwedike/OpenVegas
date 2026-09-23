"""Execute the real emotes ES module with deterministic, network-free browser fakes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "emotes-preview-race.js"
CASES = (
    "response-race",
    "decode-race",
    "closed-modal",
    "stale-error",
    "cached-reopen",
    "queued-close",
    "pagehide",
    "independent-cards",
    "compatibility-empty",
    "compatibility-text",
    "compatibility-bounds",
)


@pytest.mark.parametrize("case", CASES)
def test_preview_and_compatibility_in_javascript(case, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the executable emotes browser regression")
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [node, "--experimental-vm-modules", str(FIXTURE), str(ROOT), case],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"PASS {case}"
