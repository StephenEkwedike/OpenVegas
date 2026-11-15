import hashlib
import json
import os
import subprocess
from pathlib import PureWindowsPath

import pytest

from openvegas.integrations import windows_native_image as image
from openvegas.integrations.credentials import IntegrationError
from tests.test_emotes.test_windows_resources import FakeAPI


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_normal_npm_layouts_never_return_shell_shims(host):
    global_shim = rf"C:\apps\{host}.cmd"
    local_shim = rf"C:\apps\node_modules\.bin\{host}.ps1"
    paths = image._locations(host, global_shim)
    assert paths == image._locations(host, local_shim)
    assert len(set(paths)) == 2
    assert all(p.name == host + ".exe" and "node_modules" in p.parts for p in paths)


@pytest.mark.parametrize(
    "entry",
    [
        r"C:codex.exe",
        r"\\host\share\codex.exe",
        r"C:\a\..\codex.exe",
        r"C:\a:stream\codex.exe",
        r"C:\wrong.bat",
        r"C:\aux\codex.exe",
    ],
)
def test_invalid_images_are_not_launchable(entry):
    with pytest.raises((IntegrationError, OSError)):
        image._locations("codex", entry)


def test_image_and_ancestors_remain_held_through_launch():
    api = FakeAPI(data=b"a" * (1024 * 1024 + 1))
    path = PureWindowsPath(r"C:\fixture\codex.exe")
    with image._held_image(api, path, len(api.data), hashlib.sha256(api.data).hexdigest()) as exe:
        assert exe == str(path)
        assert len(api.opened) == 3 and not api.closed
        assert max(api.reads) == 1024 * 1024
    assert api.closed == api.opened[::-1]


@pytest.mark.parametrize("bad", ["wrong digest", "wrong size", "reparse", "truncated"])
def test_no_callback_on_unreviewed_bytes_or_reparse(bad):
    api = FakeAPI()
    size = len(api.data)
    digest = hashlib.sha256(api.data).hexdigest()
    if bad == "wrong digest":
        digest = "0" * 64
    elif bad == "wrong size":
        size += 1
    elif bad == "reparse":
        api.override[2] = {"attributes": image.win.FILE_ATTRIBUTE_REPARSE_POINT}
    else:
        api.override[3] = {"size": size + 1}
        size += 1
    with (
        pytest.raises((OSError, IntegrationError)),
        image._held_image(api, r"C:\fixture\codex.exe", size, digest),
    ):
        pytest.fail("launch callback reached")
    assert api.opened and api.closed == api.opened[::-1]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
@pytest.mark.parametrize(("host", "version"), [("codex", "0.166.0"), ("claude", "2.1.322")])
def test_launches_official_windows_image(host, version, tmp_path):
    key = "OV_WINDOWS_" + host.upper() + "_EXE"
    if key not in os.environ and not os.environ.get("CI"):
        pytest.skip(f"requires installed native package ({key})")
    entry = os.environ[key]
    env = {k: os.environ[k] for k in ("SystemRoot", "TEMP", "TMP") if k in os.environ}
    env.update(
        {
            "USERPROFILE": str(tmp_path),
            "HOME": str(tmp_path),
            "CODEX_HOME": str(tmp_path / "codex"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    with image.pinned_windows_image(host, entry, version) as exe:
        result = subprocess.run(
            [exe, "--version"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
            check=True,
        )
        assert version in result.stdout
        if host == "codex":
            from openvegas.integrations.codex_catalog_schema import valid_native_row

            # Bundled means no sign-in, model request, or network refresh.
            catalog = subprocess.run(
                [exe, "debug", "models", "--bundled"],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=40,
                check=True,
            )
            rows = json.loads(catalog.stdout)["models"]
            for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra"):
                (row,) = [row for row in rows if row["slug"] == model]
                assert valid_native_row(row) and row["visibility"] == "list"
                assert {"low", "high"} <= {v["effort"] for v in row["supported_reasoning_levels"]}
