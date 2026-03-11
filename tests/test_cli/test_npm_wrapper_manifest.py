from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_npm_wrapper_has_single_openvegas_bin():
    package_json = ROOT / "npm-cli" / "package.json"
    data = json.loads(package_json.read_text())
    assert "bin" in data
    assert isinstance(data["bin"], dict)
    assert list(data["bin"].keys()) == ["openvegas"]
    assert data["bin"]["openvegas"] == "bin/openvegas.js"
