"""Offline evidence tests: authored assets, strict ANSI parsing, and actual PTYs."""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("emote_demo", ROOT / "scripts/record_emote_demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


@pytest.mark.parametrize("name", demo.PACKS)
def test_original_pack_timeline_and_native_geometry(name):
    _, _, _, load_pack, fit_frame, _ = demo.runtime()
    pack = load_pack(demo.ASSETS / name)
    assert "original" in pack.manifest.tags
    cols, rows = demo.dimensions(pack, "native")
    assert fit_frame(pack.frame(0), max_columns=cols, max_rows=rows - 4).size == (
        pack.manifest.width,
        pack.manifest.height,
    )
    steps = demo.timeline(pack, 10)
    waiting = [step for step in steps if step["cycle"]]
    if "dance" in pack.manifest.tags:
        assert [s["frame"] for s in waiting] == list(
            pack.manifest.animations["waiting"].frames
        ) * 10
        assert [s["cycle"] for s in waiting] == [n for n in range(1, 11) for _ in range(8)]
    else:
        assert waiting == []
    complete = [s for s in steps if s["state"] == "complete"]
    assert [s["frame"] for s in complete] == list(pack.manifest.animations["complete"].frames)
    assert sum(s["seconds"] for s in complete) == pytest.approx(
        pack.manifest.animations["complete"].duration
    )
    assert sum("COMPLETE" in s["actions"] for s in steps) == 1
    assert steps[0]["state"] == steps[-1]["state"] == "idle"


@pytest.mark.parametrize("bad", [0, -1, 101, True, 1.5])
def test_timeline_bounds(bad):
    with pytest.raises(ValueError):
        demo.timeline(None, bad)


def test_environment_does_not_forward_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "canary-private-value")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "canary-private-value")
    monkeypatch.setenv("PYTHONPATH", "/private/customer")
    monkeypatch.setenv("NO_COLOR", "1")
    env = demo.clean_environment(tmp_path)
    assert (
        not {"OPENAI_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "PYTHONPATH", "NO_COLOR"} & env.keys()
    )
    assert "canary" not in json.dumps(env)
    assert env["HOME"] == str(tmp_path)


@pytest.mark.parametrize(
    "attempt",
    [
        "open('.env')",
        "open('.env.local')",
        "open('ENV.md')",
        "socket.socket()",
        "subprocess.run(['echo', 'blocked'])",
    ],
)
def test_child_guard_blocks_network_processes_and_dotenv(tmp_path, attempt):
    code = (
        "import runpy,socket,subprocess; "
        f"d=runpy.run_path({str(ROOT / 'scripts/record_emote_demo.py')!r}); "
        f"d['install_guard'](); {attempt}"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        env=demo.clean_environment(tmp_path),
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert b"Offline demo guard blocked" in result.stderr


@pytest.mark.parametrize(
    "ansi",
    [
        "\x1b]52;c;ZXhmaWw=\x07",
        "\x1b[5m",
        "\x1b[38;2;999;0;0m",
        "\x1b[1A",
        "\x1b[999;1H",
        "\t",
        "\x1b[",
        "\x00",
        "\x1b[38;2;1m",
    ],
)
def test_emulator_rejects_unsupported_controls(ansi):
    with pytest.raises(ValueError):
        demo.Terminal(80, 24, "dark").feed(ansi)


def test_emulator_half_blocks_and_themes():
    import io

    from PIL import Image

    terminal = demo.Terminal(2, 1, "light")
    terminal.feed("\x1b[38;2;12;34;56m\u2580\x1b[0m\u2584")
    image = Image.open(io.BytesIO(terminal.png()))
    assert image.size == (16, 16)
    assert image.getpixel((0, 0)) == (12, 34, 56)
    assert image.getpixel((0, 15)) == demo.THEMES["light"][1]
    assert image.getpixel((8, 15)) == demo.THEMES["light"][0]
    with pytest.raises(ValueError, match="overflow"):
        terminal.feed("x")


def test_screen_boundaries_can_cross_reads():
    ansi = demo.BEGIN + "hello" + demo.END + demo.RESTORE
    events = [[i / 10, "o", c] for i, c in enumerate(ansi)]
    assert demo.screens(events) == [
        (pytest.approx((len(ansi) - len(demo.RESTORE) - 1) / 10), demo.BEGIN + "hello" + demo.END)
    ]
    with pytest.raises(ValueError, match="restoration"):
        demo.screens([[0, "o", ansi[:-1]]])


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only evidence, not Windows certification")
@pytest.mark.parametrize(
    "theme,geometry,name",
    [
        ("dark", "native", "pixel-courier"),
        ("light", "fit-80x24", "bicycle-finish"),
    ],
)
def test_actual_controlling_pty_capture_and_replay(tmp_path, monkeypatch, theme, geometry, name):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sensitive-canary-never-forward")
    (tmp_path / "frames").mkdir()
    session = demo.capture_session(tmp_path, name, theme, geometry, cycles=1, fast=True)
    assert session["attestation"]["isatty"] == [True] * 3
    assert session["attestation"]["controlling_tty"]
    assert session["attestation"]["rich_is_terminal"]
    assert session["attestation"]["color_system"] == "truecolor"
    assert "ANTHROPIC_API_KEY" not in session["attestation"]["environment_keys"]
    assert session["timing"] == "accelerated-test"
    assert session["rendered_pixels"] == ([64, 80] if geometry == "native" else [53, 40])
    raw = gzip.decompress((tmp_path / session["ansi"]).read_bytes())
    assert b"sensitive-canary" not in raw
    assert b"SIMULATION" in raw and b"\x1b[38;2;" in raw
    assert raw.endswith(demo.RESTORE.encode())
    assert not list(tmp_path.glob(".private-home-*"))
    demo.write_replay(tmp_path, [session])
    page = (tmp_path / "index.html").read_text()
    assert "data:image/png;base64," in page
    assert "data:application/gzip;base64," in page
    assert "connect-src 'none'" in page
    assert "__DATA__" not in page and "https://" not in page


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only evidence")
def test_pty_timeout_and_failure_cleanup(tmp_path):
    command = [sys.executable, "-I", "-B", "-c", "import time;time.sleep(10)"]
    with pytest.raises(TimeoutError):
        demo.read_pty(command, cols=80, rows=24, home=tmp_path, timeout=0.15)
    with pytest.raises(RuntimeError, match="child failed"):
        demo.read_pty(
            [sys.executable, "-I", "-B", "-c", "raise SystemExit(2)"],
            cols=80,
            rows=24,
            home=tmp_path,
        )


def test_checked_in_evidence_matrix_and_hashes():
    report_path = demo.OUTPUT / "evidence.json"
    assert report_path.is_file(), "Generate the evidence matrix with the documented capture command"
    report = json.loads(report_path.read_bytes())
    assert report["full_matrix"] is True
    assert len(report["sessions"]) == 24
    for relative, digest in report["files"].items():
        assert demo.sha((demo.OUTPUT / relative).read_bytes()) == digest, relative
    for relative, digest in report["sources"].items():
        assert demo.sha((ROOT / relative).read_bytes()) == digest, relative
    for name in report["sessions"]:
        session = json.loads((demo.OUTPUT / f"{name}.json").read_bytes())
        assert session["timing"] == "wall-paced"
        assert session["completion_plays"] == 1
        assert session["waiting_cycles"] == (10 if session["native_pixels"] == [64, 80] else 0)
        assert session["duration"] >= session["fixture_duration"] - 0.1
        if session["geometry"] == "native":
            assert session["native_pixels"] == session["rendered_pixels"]


def test_embedded_replay_assets_match_recordings():
    page = (demo.OUTPUT / "index.html").read_text()
    payload = json.loads(
        re.search(
            r'<script id="evidence" type="application/json">(.*?)</script>', page, re.DOTALL
        ).group(1)
    )
    assert len(payload["sessions"]) == 24
    for name, uri in {**payload["images"], **payload["downloads"]}.items():
        assert base64.b64decode(uri.split(",", 1)[1]) == (demo.OUTPUT / name).read_bytes()
    for session in payload["sessions"]:
        assert session == json.loads((demo.OUTPUT / (session["id"] + ".json")).read_bytes())


def test_verifier_rejects_corrupt_artifact(tmp_path):
    (tmp_path / "asset.png").write_bytes(b"corrupt")
    (tmp_path / "evidence.json").write_text(
        json.dumps({"files": {"asset.png": "0" * 64}, "sources": {}, "sessions": []})
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        demo.verify(tmp_path)


def test_replay_controls_without_browser_automation():
    """Run the replay's JS against tiny DOM test doubles, not a browser or UI."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node-based replay control unit test requires Node")
    page = (demo.OUTPUT / "index.html").read_text()
    data = re.search(
        r'<script id="evidence" type="application/json">(.*?)</script>', page, re.DOTALL
    ).group(1)
    script = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)[0]
    harness = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,{value:'',style:{},listeners:{},
 append(o){if(!this.value)this.value=o.value},addEventListener(k,f){this.listeners[k]=f},
 removeAttribute(k){delete this[k]}});return elements.get(id)}
element('evidence').textContent=input.data;element('speed').value='1';element('zoom').value='1';
let callback;
vm.runInNewContext(input.script,{document:{getElementById:element,createElement:()=>({})},
 performance:{now:()=>0},requestAnimationFrame:f=>{callback=f}});
assert.match(element('geometry').textContent,/native, no downsampling/);
element('next').onclick();assert.equal(element('seek').value,1);
element('back').onclick();assert.equal(element('seek').value,0);
element('zoom').value='0.5';element('zoom').onchange();assert.equal(element('screen').style.width,'320px');
element('play').onclick();callback(999999);assert.equal(element('play').textContent,'Play');
assert.equal(element('seek').value,Number(element('seek').max));
element('pack').value='bicycle-finish';element('theme').value='light';element('size').value='fit-80x24';
element('size').listeners.change();assert.match(element('geometry').textContent,/NOT native-size art/);
assert.match(element('cast').href,/^data:application\/gzip;base64,/);
element('seek').value='5';element('seek').oninput();assert.match(element('status').textContent,/frame 6\//);
"""
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps({"data": data, "script": script}),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
