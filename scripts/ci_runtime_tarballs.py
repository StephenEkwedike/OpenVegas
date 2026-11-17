"""Offline six-tarball assembly and clean layout QA. Does not run a package manager."""
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

P = Path
TARGETS = {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-x64"}
VERSION = "0.3.22"
MAIN = "openvegas-ai"
MAIN_FILES = ["README.md", "bin/openvegas.js", "package.json", "release.json"]
def sha(p):
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()

def name(t):
    return MAIN if t == MAIN else "lordesteph-openvegas-" + t

def tgz(t):
    return name(t) + "-" + VERSION + ".tgz"

def binary(t):
    return "openvegas-" + t + (".exe" if t.startswith("win32") else "")

def members(t):
    return MAIN_FILES if t == MAIN else ["bin/" + binary(t), "package.json"]

def pack():
    dst = P("dist/customer-tarballs")
    dst.mkdir()
    sums = []
    for t in sorted(TARGETS | {MAIN}):
        root = P("dist/npm-assembly-check") / t
        assert {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == set(members(t))
        p = dst / tgz(t)
        with p.open("xb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz, tarfile.open(mode="w|", fileobj=gz, format=tarfile.USTAR_FORMAT) as a:
            for n in members(t):
                src = root / n
                assert not src.is_symlink() and src.is_file()
                h = tarfile.TarInfo("package/" + n)
                h.mode = 0o755 if n.startswith("bin/") else 0o644
                h.size = src.stat().st_size
                with src.open("rb") as body:
                    a.addfile(h, body)
        sums.append(sha(p) + "  " + p.name)
    (dst / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode())

def unpack(t):
    assert t in TARGETS
    source = P("packs")
    rows = (source / "SHA256SUMS").read_text().splitlines()
    sums = {n:h for h,n in (r.split("  ") for r in rows)}
    assert len(rows) == 6 and set(sums) == {tgz(x) for x in TARGETS | {MAIN}}
    for n,h in sums.items():
        assert re.fullmatch("[0-9a-f]{64}", h) and sha(source / n) == h
    modules = P("installed/node_modules")
    modules.mkdir(parents=True, exist_ok=False)
    for x in [t, MAIN]:
        dest = modules / (MAIN if x == MAIN else "@lordesteph/openvegas-" + x)
        with tarfile.open(source / tgz(x), "r:gz") as a:
            rows = a.getmembers()
            assert sorted(m.name for m in rows) == sorted("package/" + n for n in members(x))
            for m in rows:
                n = m.name.removeprefix("package/")
                assert m.isfile() and 0 < m.size <= (200*1024*1024 if n.startswith("bin/openvegas-") else 1024*1024)
                mode = 0o755 if n.startswith("bin/") else 0o644
                assert m.mode == mode
                p = dest / n
                p.parent.mkdir(parents=True, exist_ok=True)
                with a.extractfile(m) as src, p.open("xb") as f:
                    shutil.copyfileobj(src, f)
                p.chmod(mode)
        meta = json.loads((dest / "package.json").read_bytes())
        assert meta["version"] == VERSION and "scripts" not in meta
        assert meta["name"] == (MAIN if x == MAIN else "@lordesteph/openvegas-" + x)
    index = json.loads((modules / MAIN / "release.json").read_bytes())
    assert set(index["platforms"]) == TARGETS and index["version"] == VERSION
    p = index["platforms"][t]
    assert p["package"] == "@lordesteph/openvegas-" + t and p["binary"] == binary(t)
    assert sha(modules / p["package"] / "bin" / binary(t)) == p["sha256"]

def hosts(t):
    assert t in TARGETS
    triple = {"linux-x64":"x86_64-unknown-linux-musl","linux-arm64":"aarch64-unknown-linux-musl",
              "darwin-arm64":"aarch64-apple-darwin","darwin-x64":"x86_64-apple-darwin","win32-x64":"x86_64-pc-windows-msvc"}[t]
    ext = ".exe" if t == "win32-x64" else ""
    dest = P("hosts")
    dest.mkdir()
    def fetch(url, target):
        curl = "curl.exe" if os.name == "nt" else "curl"
        subprocess.run([curl, "-qfsSL", "--proto", "=https", "--proto-redir", "=https", "--retry", "2",
                        "--max-time", "180", "--max-filesize", "700000000", "-o", str(target), url], check=True)
    cx = dest / ("codex" + ext)
    cc = dest / ("claude" + ext)
    url = "https://github.com/openai/codex/releases/download/rust-v0.169.0/codex-" + triple
    if ext:
        fetch(url + ext, cx)
    else:
        archive = dest / "host.tgz"
        fetch(url + ".tar.gz", archive)
        with tarfile.open(archive, "r:gz") as a:
            matches = [m for m in a if P(m.name).name in ("codex", "codex-" + triple) and m.isfile()]
            m, = matches
            assert 0 < m.size < 700000000
            with a.extractfile(m) as src, cx.open("xb") as f:
                shutil.copyfileobj(src, f)
    fetch("https://downloads.claude.ai/claude-code-releases/2.1.322/" + t + "/claude" + ext, cc)
    # The caller authenticates the pinned bytes before any executable runs.
    cx.chmod(0o755)
    cc.chmod(0o755)

if __name__ == "__main__":
    if sys.argv[1:] == ["pack"]:
        pack()
    elif sys.argv[1:] == ["unpack"]:
        unpack(os.environ["TARGET"])
    elif sys.argv[1:] == ["hosts"]:
        hosts(os.environ["TARGET"])
    else:
        raise SystemExit(2)
