"""Record the real emote renderer on a POSIX PTY; never launch an external CLI.

The controller clock/events are deterministic SIMULATION fixtures. ANSI bytes and
read timestamps come from a real PTY. PNGs are a deliberately small, strict ANSI
emulator's output, NOT native-OS screenshots. No new artwork is generated.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import errno
import gzip
import hashlib
import importlib.metadata
import io
import itertools
import json
import math
import os
import re
import selectors
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "openvegas/emotes/assets"
OUTPUT = ROOT / "creatives/emotes/terminal-demo"
PACKS = (
    "pixel-courier",
    "visor-explorer",
    "beat-maker",
    "bicycle-finish",
    "three-point-glow",
    "skyline-dunk",
)
THEMES = {"dark": ((228, 234, 240), (16, 20, 26)), "light": ((30, 36, 44), (247, 248, 250))}
GEOMETRIES = ("native", "fit-80x24")
BEGIN = "\x1b[0m\x1b[H\x1b[2J"
END = "\x1b[0m\x1b[?25l"
RESTORE = "\x1b[0m\x1b[?25h"
CELL = (8, 16)
NOTICE = (
    "Actual POSIX PTY renderer output. SIMULATED lifecycle and controller clock. "
    "Replay PNGs are deterministic ANSI-emulator renderings, NOT native-OS screenshots. "
    "No native desktop scroll/selection/voice certification, external host certification, "
    "user final art approval, ownership, or sale approval."
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def runtime():
    # -I prevents ambient PYTHONPATH/site-user customization; use this snapshot only.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from openvegas.emotes.controller import EmoteController
    from openvegas.emotes.events import Event, Phase
    from openvegas.emotes.manifest import load_pack
    from openvegas.emotes.render import fit_frame, rich_frame

    return EmoteController, Event, Phase, load_pack, fit_frame, rich_frame


def clean_environment(home: Path) -> dict[str, str]:
    home = Path(home)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TMPDIR": str(home),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "OPENVEGAS_DOTENV_OVERRIDE": "0",
        "OPENVEGAS_TEST_MODE": "1",
    }


def install_guard():
    def audit(event, args):
        forbidden = event.startswith("socket.") or event in {
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.exec",
        }
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = Path(os.fsdecode(args[0])).name.lower()
            forbidden |= name == "env.md" or name == ".env" or name.startswith(".env.")
        if forbidden:
            raise RuntimeError("Offline demo guard blocked a forbidden operation")

    sys.addaudithook(audit)


def dimensions(pack, geometry: str) -> tuple[int, int]:
    if geometry == "fit-80x24":
        return 80, 24
    if geometry != "native":
        raise ValueError("Unknown geometry")
    return max(80, pack.manifest.width + 2), math.ceil(pack.manifest.height / 2) + 5


def timeline(pack, cycles: int) -> list[dict]:
    if type(cycles) is not int or not 1 <= cycles <= 100:
        raise ValueError("Cycles must be an integer in 1..100")
    clips = pack.manifest.animations
    steps = []

    def add(state, frame, seconds, actions=(), cycle=None):
        steps.append(
            {
                "state": state,
                "frame": frame,
                "seconds": seconds,
                "actions": list(actions),
                "cycle": cycle,
            }
        )

    add("idle", clips["idle"].frames[0], 0.35)
    if "dance" in pack.manifest.tags:
        for cycle in range(1, cycles + 1):
            for slot, frame in enumerate(clips["waiting"].frames):
                add(
                    "active",
                    frame,
                    clips["waiting"].frame_ms / 1000,
                    ("START",) if cycle == 1 and slot == 0 else (),
                    cycle,
                )
        add("paused", clips["idle"].frames[0], 0.35, ("PAUSE",))
        complete_actions = ("RESUME", "COMPLETE")
    else:
        add("active", clips["waiting"].frames[0], 0.35, ("START",))
        complete_actions = ("COMPLETE",)
    for slot, frame in enumerate(clips["complete"].frames):
        add(
            "complete",
            frame,
            clips["complete"].frame_ms / 1000,
            complete_actions if slot == 0 else (),
        )
    add("idle", clips["idle"].frames[0], 0.35)
    return steps


def worker(args) -> None:
    import fcntl
    import termios

    install_guard()
    Controller, Event, Phase, load_pack, fit_frame, rich_frame = runtime()
    from rich.console import Console

    # Popen starts a new session; explicitly acquire our owned slave as its ctty.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    sys.stdout.reconfigure(encoding="utf-8")
    pack = load_pack(ASSETS / args.pack)
    cols, rows = dimensions(pack, args.geometry)
    if not all(os.isatty(fd) for fd in (0, 1, 2)):
        raise RuntimeError("The renderer must be attached to a real PTY")
    actual = os.get_terminal_size(1)
    if (actual.columns, actual.lines) != (cols, rows):
        raise RuntimeError("PTY geometry mismatch")
    console = Console(
        file=sys.stdout, color_system="truecolor", highlight=False, legacy_windows=False
    )
    if not console.is_terminal or (console.width, console.height) != (cols, rows):
        raise RuntimeError("Rich did not detect the real PTY geometry")
    telemetry = os.fdopen(args.meta_fd, "w", encoding="ascii", buffering=1)
    telemetry.write(
        json.dumps(
            {
                "kind": "attestation",
                "isatty": [os.isatty(fd) for fd in (0, 1, 2)],
                "controlling_tty": os.tcgetpgrp(0) == os.getpgrp(),
                "cols": cols,
                "rows": rows,
                "rich_is_terminal": console.is_terminal,
                "color_system": console.color_system,
                "environment_keys": sorted(os.environ),
                "guard": "network/process/dotenv blocked",
            }
        )
        + "\n"
    )
    clock = [0.0]
    controller = Controller(
        pack,
        source="demo-fixture",
        session_id="simulated-local",
        clock=lambda: clock[0],
        active_lease_seconds=300,
    )
    sequence = 0
    elapsed = 0.0
    deadline = time.monotonic()
    fg, bg = THEMES[args.theme]
    default = f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m"
    try:
        for ordinal, step in enumerate(timeline(pack, args.cycles)):
            clock[0] = round(elapsed, 9)
            for action in step["actions"]:
                phase = Phase[action]
                event = Event(
                    "demo-fixture",
                    "simulated-local",
                    "fixture-turn",
                    f"fixture-event-{sequence}",
                    phase,
                    0,
                    sequence,
                    "success" if phase == Phase.COMPLETE else None,
                )
                if not controller.handle(event):
                    raise RuntimeError("Fixture lifecycle event was rejected")
                sequence += 1
            # Sample just inside the frame, avoiding floating-point boundary ambiguity.
            clock[0] += 0.000001
            if (controller.current_state, controller.frame_index) != (step["state"], step["frame"]):
                raise RuntimeError("Production controller diverged from fixture timeline")
            frame = fit_frame(controller.current_frame(), max_columns=cols, max_rows=rows - 4)
            if args.geometry == "native" and frame.size != (
                pack.manifest.width,
                pack.manifest.height,
            ):
                raise RuntimeError("Native frame was unexpectedly downsampled")
            # Only this compositor writes stdout. Rich serializes the actual renderer Text.
            sys.stdout.write(BEGIN + default + "\x1b[2J")
            labels = [
                "OpenVegas | SIMULATION | public art preview",
                f"{pack.manifest.display_name} | {args.theme} | {args.geometry} | scale=1",
                f"{step['state'].upper()} | fixture events, not external CLI footage",
                "No final art approval or external host certification",
            ]
            for label in labels:
                sys.stdout.write(label + "\n")
            style = "#{:02x}{:02x}{:02x} on #{:02x}{:02x}{:02x}".format(*fg, *bg)
            console.print(
                rich_frame(frame, scale=1, background=bg), end="", soft_wrap=True, style=style
            )
            sys.stdout.write(END)
            sys.stdout.flush()
            telemetry.write(
                json.dumps(
                    dict(
                        kind="frame",
                        ordinal=ordinal,
                        fixture_seconds=round(elapsed, 6),
                        frame_pixels=list(frame.size),
                        **step,
                    )
                )
                + "\n"
            )
            elapsed += step["seconds"]
            deadline += step["seconds"]
            if not args.fast:
                time.sleep(max(0, deadline - time.monotonic()))
    finally:
        controller.close()
        sys.stdout.write(RESTORE)
        sys.stdout.flush()
        telemetry.close()


def read_pty(command, *, cols, rows, home, timeout=180):
    """Raw output only; never record user input or inherit a shell/environment."""
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    meta_read, meta_write = os.pipe()
    process = None
    streams = selectors.DefaultSelector()
    output, metadata = [], bytearray()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    started = time.monotonic()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        process = subprocess.Popen(
            [*command, "--meta-fd", str(meta_write)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=home,
            env=clean_environment(home),
            pass_fds=(meta_write,),
            start_new_session=True,
        )
        os.close(slave)
        slave = -1
        os.close(meta_write)
        meta_write = -1
        streams.register(master, selectors.EVENT_READ, "pty")
        streams.register(meta_read, selectors.EVENT_READ, "meta")
        total = 0
        while streams.get_map():
            if time.monotonic() - started > timeout:
                raise TimeoutError("PTY demo exceeded its bounded deadline")
            for key, _ in streams.select(0.1):
                try:
                    data = os.read(key.fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO or key.data != "pty":
                        raise
                    data = b""
                if not data:
                    streams.unregister(key.fd)
                    continue
                total += len(data)
                if total > 128 * 1024 * 1024:
                    raise RuntimeError("PTY demo exceeded its output bound")
                if key.data == "meta":
                    metadata.extend(data)
                else:
                    chunk = decoder.decode(data)
                    if chunk:
                        output.append([round(time.monotonic() - started, 6), "o", chunk])
        tail = decoder.decode(b"", final=True)
        if tail:
            output.append([round(time.monotonic() - started, 6), "o", tail])
        if process.wait(timeout=5) != 0:
            # Do not echo child tracebacks, paths, or arbitrary data into public reports.
            raise RuntimeError("PTY renderer child failed; no evidence was published")
        return output, [json.loads(line) for line in metadata.splitlines()]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        streams.close()
        for fd in (master, slave, meta_read, meta_write):
            if fd >= 0:
                os.close(fd)


class Terminal:
    """Strict emulator of this recorder's ANSI subset, with explicit cell bounds.

    Half-blocks are painted geometrically (8x16 cells, two 8x8 halves), not using
    a browser font. Unsupported controls fail rather than silently making art up.
    """

    CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")

    def __init__(self, cols, rows, theme):
        self.cols, self.rows = cols, rows
        self.default_fg, self.default_bg = THEMES[theme]
        self.fg, self.bg = self.default_fg, self.default_bg
        self.x = self.y = 0
        self.cells = [(" ", self.fg, self.bg)] * (cols * rows)

    def feed(self, data):
        i = 0
        while i < len(data):
            char = data[i]
            if char == "\x1b":
                match = self.CSI.match(data, i)
                if not match:
                    raise ValueError("Unsupported or incomplete ANSI control")
                raw, command = match.groups()
                i = match.end()
                if raw == "?25" and command in "hl":
                    continue
                if command == "m":
                    self.sgr([int(n or 0) for n in raw.split(";")])
                elif command == "H":
                    values = [int(n or 1) for n in raw.split(";")] if raw else [1, 1]
                    self.y, self.x = values[0] - 1, (values[1] if len(values) > 1 else 1) - 1
                    self.check_bounds()
                elif command == "J" and raw == "2":
                    self.cells = [(" ", self.fg, self.bg)] * (self.cols * self.rows)
                else:
                    raise ValueError("Unsupported ANSI control")
                continue
            i += 1
            if char == "\r":
                self.x = 0
            elif char == "\n":
                self.y += 1
                self.check_bounds()
            elif char == " " or 33 <= ord(char) <= 126 or char in "\u2580\u2584":
                self.check_bounds()
                self.cells[self.y * self.cols + self.x] = (char, self.fg, self.bg)
                self.x += 1
            else:
                raise ValueError("Unsupported terminal glyph/control")

    def check_bounds(self):
        if not (0 <= self.x < self.cols and 0 <= self.y < self.rows):
            raise ValueError("Terminal overflow/wrap/scroll is not allowed in this evidence")

    def sgr(self, codes):
        i = 0
        while i < len(codes):
            code = codes[i]
            i += 1
            if code == 0:
                self.fg, self.bg = self.default_fg, self.default_bg
            elif code == 39:
                self.fg = self.default_fg
            elif code == 49:
                self.bg = self.default_bg
            elif code in (38, 48) and codes[i : i + 1] == [2] and len(codes[i : i + 4]) == 4:
                color = tuple(codes[i + 1 : i + 4])
                if any(not 0 <= n <= 255 for n in color):
                    raise ValueError("Invalid truecolor SGR")
                if code == 38:
                    self.fg = color
                else:
                    self.bg = color
                i += 4
            else:
                raise ValueError("Unsupported SGR")

    def png(self):
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (self.cols * CELL[0], self.rows * CELL[1]))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=11)
        for index, (char, fg, bg) in enumerate(self.cells):
            x, y = index % self.cols * CELL[0], index // self.cols * CELL[1]
            draw.rectangle((x, y, x + 7, y + 15), fill=bg)
            if char in "\u2580\u2584":
                top = y if char == "\u2580" else y + 8
                draw.rectangle((x, top, x + 7, top + 7), fill=fg)
            elif char != " ":
                draw.text((x, y + 1), char, fill=fg, font=font)
        result = io.BytesIO()
        image.save(result, format="PNG", optimize=False)
        return result.getvalue()


def screens(events):
    """Use observed PTY read times, not the synthetic controller's timeline."""
    pending = ""
    found = []
    for timestamp, kind, text in events:
        if kind != "o":
            raise ValueError("Only output events are supported")
        pending += text
        while END in pending:
            screen, pending = pending.split(END, 1)
            if not screen.startswith(BEGIN):
                raise ValueError("Invalid screen boundary")
            found.append((timestamp, screen + END))
    if pending != RESTORE or not found:
        raise ValueError("Missing screen, incomplete capture, or absent cursor restoration")
    return found


def assert_pixels(terminal, frame):
    """Compare every decoded emote cell to the existing authored RGBA frame."""
    bg = terminal.default_bg

    def composite(x, y):
        if y >= frame.height:
            return bg
        r, g, b, alpha = frame.getpixel((x, y))
        return tuple((c * alpha + b * (255 - alpha) + 127) // 255 for c, b in zip((r, g, b), bg))

    for y in range(math.ceil(frame.height / 2)):
        for x in range(frame.width):
            glyph, fg, back = terminal.cells[(y + 4) * terminal.cols + x]
            actual = ((fg if glyph == "\u2580" else back), (fg if glyph == "\u2584" else back))
            if glyph not in " \u2580\u2584" or actual != (
                composite(x, y * 2),
                composite(x, y * 2 + 1),
            ):
                raise ValueError("Captured terminal pixels differ from authored frame")


def source_hashes():
    paths = [Path(__file__).resolve(), *sorted((ROOT / "openvegas/emotes").glob("*.py"))]
    for pack in PACKS:
        paths.extend(
            ASSETS / pack / name for name in ("manifest.json", "sheet.png", "provenance.json")
        )
    return {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in paths}


def capture_session(output, pack_name, theme, geometry, cycles=10, fast=False):
    _, _, _, load_pack, fit_frame, _ = runtime()
    pack = load_pack(ASSETS / pack_name)
    cols, rows = dimensions(pack, geometry)
    name = f"{pack_name}-{theme}-{geometry}"
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "_worker",
        "--pack",
        pack_name,
        "--theme",
        theme,
        "--geometry",
        geometry,
        "--cycles",
        str(cycles),
    ]
    if fast:
        command.append("--fast")
    with tempfile.TemporaryDirectory(prefix=".private-home-", dir=output) as home:
        events, telemetry = read_pty(
            command,
            cols=cols,
            rows=rows,
            home=home,
            timeout=sum(s["seconds"] for s in timeline(pack, cycles)) + 60,
        )
    attestation, *frames = telemetry
    if (
        attestation["isatty"] != [True] * 3
        or not attestation["controlling_tty"]
        or not attestation["rich_is_terminal"]
    ):
        raise ValueError("PTY attestation failed")
    captured = screens(events)
    if len(captured) != len(frames) or len(frames) != len(timeline(pack, cycles)):
        raise ValueError("Frame coverage mismatch")
    replay = []
    image_cache = {}
    for (timestamp, ansi), frame_info in zip(captured, frames):
        key = (ansi, frame_info["frame"])
        if key not in image_cache:
            terminal = Terminal(cols, rows, theme)
            terminal.feed(ansi)
            frame = fit_frame(pack.frame(frame_info["frame"]), max_columns=cols, max_rows=rows - 4)
            assert_pixels(terminal, frame)
            png = terminal.png()
            image_name = f"frames/{sha(png)}.png"
            image_path = output / image_name
            if not image_path.exists():
                image_path.write_bytes(png)
            image_cache[key] = image_name
        image_name = image_cache[key]
        replay.append(dict(t=timestamp, image=image_name, **frame_info))
    header = {
        "version": 2,
        "width": cols,
        "height": rows,
        "title": name,
        "env": {"TERM": "xterm-256color", "COLORTERM": "truecolor"},
        "duration": events[-1][0],
        "openvegas_notice": NOTICE,
    }
    cast = (
        b"\n".join(json.dumps(row, ensure_ascii=True).encode() for row in [header, *events]) + b"\n"
    )
    raw = "".join(event[2] for event in events).encode("utf-8")
    (output / f"{name}.cast.gz").write_bytes(gzip.compress(cast, mtime=0))
    (output / f"{name}.ansi.gz").write_bytes(gzip.compress(raw, mtime=0))
    session = {
        "id": name,
        "pack": pack_name,
        "display_name": pack.manifest.display_name,
        "version": pack.manifest.version,
        "theme": theme,
        "geometry": geometry,
        "cols": cols,
        "rows": rows,
        "native_pixels": [pack.manifest.width, pack.manifest.height],
        "rendered_pixels": frames[0]["frame_pixels"],
        "scale": 1,
        "waiting_cycles": cycles if "dance" in pack.manifest.tags else 0,
        "completion_plays": 1,
        "completion_duration": pack.manifest.animations["complete"].duration,
        "duration": events[-1][0],
        "fixture_duration": sum(s["seconds"] for s in timeline(pack, cycles)),
        "timing": "accelerated-test" if fast else "wall-paced",
        "attestation": attestation,
        "cast": f"{name}.cast.gz",
        "ansi": f"{name}.ansi.gz",
        "raw_ansi_sha256": sha(raw),
        "frames": replay,
    }
    (output / f"{name}.json").write_bytes(json_bytes(session))
    return session


PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>OpenVegas / Recorded Terminal Evidence</title>
<style>
:root{color-scheme:light;--paper:#ece9e0;--ink:#202a2d;--accent:#a63f27}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(at top right,#fff9e9,transparent 65%),var(--paper);color:var(--ink);font:16px Georgia,serif}
header,main,footer{max-width:1500px;margin:auto;padding:24px}header{border-bottom:1px solid #bdbaaf}
.eyebrow{font:12px monospace;letter-spacing:.14em;color:var(--accent)}h1{font-size:clamp(28px,4vw,48px);font-weight:normal;margin:12px 0}p{line-height:1.5;max-width:1000px}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:end;margin:16px 0}label{display:grid;gap:6px;font:12px monospace}
button,select{font:14px monospace;padding:10px;border:1px solid #788488;border-radius:2px;background:#fffdf6;color:var(--ink)}button{cursor:pointer}button:disabled{opacity:.4}
#viewport{overflow:auto;max-height:76vh;background:#394347;border:1px solid #5b6265}#screen{display:block;max-width:none;image-rendering:pixelated}
#seek{width:100%}#status,#geometry{font:13px/1.7 monospace}a{color:#853e2b}.notice{border-left:4px solid var(--accent);padding-left:14px}footer{font-size:14px}
</style>
<header><div class="eyebrow">OPENVEGAS / LOCAL EVIDENCE / NOT FOR SALE</div>
<h1>Original art. Recorded terminal output.</h1>
<p class="notice">Actual POSIX PTY + production renderer. <strong>SIMULATED lifecycle and controller clock.</strong>
These replay images are deterministic ANSI-emulator renderings, not native-OS screenshots or external CLI footage.
No native desktop scroll, selection, or voice certification. No final art approval or external host certification is implied.</p></header>
<main><div class="controls">
<label>PACK<select id="pack"></select></label><label>THEME<select id="theme"></select></label>
<label>GEOMETRY<select id="size"></select></label><label>DISPLAY ZOOM<select id="zoom"><option value="1">1x / 8 x 16 px cells</option><option value="0.5">0.5x / scaled review</option></select></label>
<button id="play">Play</button><button id="back" aria-label="Previous frame">Previous</button><button id="next" aria-label="Next frame">Next</button>
<label>PLAYBACK<select id="speed"><option value="1">1x recorded timing</option><option value="2">2x review</option><option value="0.5">0.5x review</option></select></label></div>
<p id="geometry"></p><div id="viewport"><img id="screen" alt="Recorded ANSI output emulated into terminal cells"></div>
<label>RECORDED FRAME<input id="seek" type="range" min="0" value="0"></label><p id="status" aria-live="polite"></p>
<p><a id="cast">Download raw asciicast (gzip)</a> / <a id="ansi">Download raw ANSI (gzip)</a></p>
<p>Native means no source-pixel downsampling: 64 x 80 companions occupy 64 x 40 cells; 160 x 120 sports occupy 160 x 60 cells.
The 80 x 24 recordings are explicitly fitted. At 1x display zoom, narrow screens scroll rather than silently resize.
Companions run ten waiting dance cycles; each success animation plays once, then returns to idle. Sports waiting frames are static, not dances.</p></main>
<footer>Offline, self-contained replay. Text uses Pillow's bundled font; block glyphs use exact cell halves.
No network, paid model, customer session, terminal input, or external host footage was used. Raw recordings and hashes remain independently inspectable.</footer>
<script id="evidence" type="application/json">__DATA__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById('evidence').textContent);
const $=id=>document.getElementById(id);let session,index=0,playing=false,origin=0,position=0;
for(const [id,key] of [['pack','pack'],['theme','theme'],['size','geometry']]){
 for(const value of [...new Set(data.sessions.map(s=>s[key]))]){const o=document.createElement('option');o.value=value;o.textContent=value;$(id).append(o)}
 $(id).addEventListener('change',choose);
}
function stop(){playing=false;$('play').textContent='Play'}
function show(){const f=session.frames[index];$('screen').src=data.images[f.image];$('screen').style.width=(session.cols*8*Number($('zoom').value))+'px';$('seek').value=index;
 $('status').textContent=`${session.timing} | ${f.t.toFixed(3)}s | frame ${index+1}/${session.frames.length} | ${f.state} | source frame ${f.frame}`+(f.cycle?` | waiting cycle ${f.cycle}/${session.waiting_cycles}`:'');
 $('back').disabled=index===0;$('next').disabled=index===session.frames.length-1;
}
function choose(){stop();session=data.sessions.find(s=>s.pack===$('pack').value&&s.theme===$('theme').value&&s.geometry===$('size').value);
 if(!session){$('status').textContent='This combination was not captured';$('screen').removeAttribute('src');$('play').disabled=true;return}
 $('play').disabled=false;index=0;$('seek').max=session.frames.length-1;
 $('geometry').textContent=`${session.cols} x ${session.rows} PTY | source ${session.native_pixels.join(' x ')} px | rendered ${session.rendered_pixels.join(' x ')} px | ${session.geometry==='native'?'native, no downsampling':'integer fit, NOT native-size art'} | ${session.timing}`;
 for(const kind of ['cast','ansi']){$(kind).href=data.downloads[session[kind]];$(kind).download=session[kind]}show();
}
$('play').onclick=()=>{if(playing){stop();return}if(index===session.frames.length-1)index=0;position=session.frames[index].t;origin=performance.now();playing=true;$('play').textContent='Pause';requestAnimationFrame(tick)};
function tick(now){if(!playing)return;const t=position+(now-origin)/1000*Number($('speed').value);let changed=false;
 while(index+1<session.frames.length&&session.frames[index+1].t<=t){index++;changed=true}if(changed)show();
 if(t>=session.duration){stop();return}requestAnimationFrame(tick)}
$('seek').oninput=()=>{stop();index=Number($('seek').value);show()};
for(const [id,delta] of [['back',-1],['next',1]])$(id).onclick=()=>{stop();index=Math.max(0,Math.min(session.frames.length-1,index+delta));show()};
$('zoom').onchange=()=>{if(session)show()};$('speed').onchange=stop;
choose();
</script></html>
"""


def write_replay(output, sessions):
    names = sorted({f["image"] for s in sessions for f in s["frames"]})
    images = {
        name: "data:image/png;base64," + base64.b64encode((output / name).read_bytes()).decode()
        for name in names
    }
    downloads = {
        s[kind]: "data:application/gzip;base64,"
        + base64.b64encode((output / s[kind]).read_bytes()).decode()
        for s in sessions
        for kind in ("cast", "ansi")
    }
    payload = json.dumps(
        {"sessions": sessions, "images": images, "downloads": downloads}, ensure_ascii=True
    ).replace("<", "\\u003c")
    (output / "index.html").write_text(PAGE.replace("__DATA__", payload), encoding="utf-8")


def verify(output):
    report = json.loads((output / "evidence.json").read_bytes())
    for relative, digest in report["files"].items():
        path = (output / relative).resolve()
        if not path.is_relative_to(output.resolve()) or sha(path.read_bytes()) != digest:
            raise ValueError("Artifact checksum mismatch")
    for relative, digest in report["sources"].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or sha(path.read_bytes()) != digest:
            raise ValueError("Staging source checksum mismatch; evidence is stale")
    _, _, _, load_pack, fit_frame, _ = runtime()
    frame_count = 0
    for name in report["sessions"]:
        session = json.loads((output / f"{name}.json").read_bytes())
        rows = [
            json.loads(line)
            for line in gzip.decompress((output / session["cast"]).read_bytes()).splitlines()
        ]
        header, *events = rows
        raw = gzip.decompress((output / session["ansi"]).read_bytes())
        if raw != "".join(e[2] for e in events).encode() or sha(raw) != session["raw_ansi_sha256"]:
            raise ValueError("ANSI and asciicast disagree")
        if [header["width"], header["height"]] != [session["cols"], session["rows"]]:
            raise ValueError("Cast geometry mismatch")
        if any(a[0] > b[0] for a, b in itertools.pairwise(events)):
            raise ValueError("Non-monotonic PTY timestamps")
        pack = load_pack(ASSETS / session["pack"])
        expected = timeline(pack, session["waiting_cycles"] or 1)
        captured = screens(events)
        if len(captured) != len(session["frames"]) or len(expected) != len(captured):
            raise ValueError("Incomplete recorded frame sequence")
        checked = set()
        for (timestamp, ansi), record, step in zip(captured, session["frames"], expected):
            if any(record[key] != value for key, value in step.items()) or timestamp != record["t"]:
                raise ValueError("Fixture coverage or timestamp mismatch")
            key = (ansi, record["frame"], record["image"])
            frame_count += 1
            if key in checked:
                continue
            terminal = Terminal(session["cols"], session["rows"], session["theme"])
            terminal.feed(ansi)
            frame = fit_frame(
                pack.frame(record["frame"]),
                max_columns=session["cols"],
                max_rows=session["rows"] - 4,
            )
            assert_pixels(terminal, frame)
            if terminal.png() != (output / record["image"]).read_bytes():
                raise ValueError("Replay PNG did not derive from recorded ANSI")
            checked.add(key)
    return {
        "sessions": len(report["sessions"]),
        "frames": frame_count,
        "checked_files": len(report["files"]),
        "checked_sources": len(report["sources"]),
        "result": "PASS",
        "notice": NOTICE,
    }


def capture(args):
    output = args.output.resolve()
    if not output.is_relative_to(OUTPUT):
        raise ValueError("Output must stay inside creatives/emotes/terminal-demo")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.glob("*.json")) or (output / "index.html").exists():
        raise ValueError("Refusing to overwrite evidence; choose a new output subdirectory")
    (output / "frames").mkdir(exist_ok=True)
    sources = source_hashes()
    sessions = []
    for pack in args.packs:
        for theme in args.themes:
            for geometry in args.geometries:
                session = capture_session(output, pack, theme, geometry, args.cycles, args.fast)
                sessions.append(session)
                print(f"Captured {session['id']}: {len(session['frames'])} frames", flush=True)
    if source_hashes() != sources:
        raise ValueError("Source changed during recording; evidence not certified")
    write_replay(output, sessions)
    names = {"index.html"}
    for session in sessions:
        names.update((session["cast"], session["ansi"], session["id"] + ".json"))
        names.update(f["image"] for f in session["frames"])
    files = {name: sha((output / name).read_bytes()) for name in sorted(names)}
    report = {
        "schema_version": 1,
        "notice": NOTICE,
        "sources": sources,
        "files": files,
        "sessions": [s["id"] for s in sessions],
        "renderer": "openvegas.emotes.render.rich_frame",
        "controller": "openvegas.emotes.controller.EmoteController",
        "cell_pixels": list(CELL),
        "runtime": {name: importlib.metadata.version(name) for name in ("Pillow", "rich")},
        "python": ".".join(map(str, sys.version_info[:3])),
        "full_matrix": (
            set(args.packs) == set(PACKS)
            and set(args.themes) == set(THEMES)
            and set(args.geometries) == set(GEOMETRIES)
            and args.cycles == 10
            and not args.fast
        ),
    }
    (output / "evidence.json").write_bytes(json_bytes(report))
    result = verify(output)
    (output / "verification.json").write_bytes(json_bytes(result))
    print(json.dumps(result))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    record = subs.add_parser("capture", help="Capture all six packs with ten dance cycles")
    record.add_argument("--output", type=Path, default=OUTPUT)
    record.add_argument("--packs", choices=PACKS, nargs="+", default=list(PACKS))
    record.add_argument("--themes", choices=THEMES, nargs="+", default=list(THEMES))
    record.add_argument("--geometries", choices=GEOMETRIES, nargs="+", default=list(GEOMETRIES))
    record.add_argument("--cycles", type=int, default=10)
    record.add_argument(
        "--fast", action="store_true", help="Accelerated TEST evidence, not real-time"
    )
    check = subs.add_parser("verify", help="Re-decode ANSI and check every PNG, asset, and hash")
    check.add_argument("--output", type=Path, default=OUTPUT)
    child = subs.add_parser("_worker", help=argparse.SUPPRESS)
    child.add_argument("--pack", choices=PACKS, required=True)
    child.add_argument("--theme", choices=THEMES, required=True)
    child.add_argument("--geometry", choices=GEOMETRIES, required=True)
    child.add_argument("--cycles", type=int, default=10)
    child.add_argument("--meta-fd", type=int, required=True)
    child.add_argument("--fast", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("Actual POSIX PTY capture is required; Windows is not certified")
    if args.command == "_worker":
        worker(args)
    elif args.command == "capture":
        capture(args)
    else:
        print(json.dumps(verify(args.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
