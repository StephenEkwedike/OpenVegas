"""Install the exact unpublished QA tarballs in a disposable directory. No model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ARCHIVE = "c8f92bbc21b4dc04e7f03772b1c9aaecf2f74b50bc547367a2812e8329191376"
INDEX = "9fa6b306ce5016adf3568d5a65719dad4a955b89f9932139d568d0d7fd75c884"
LAUNCHER = "437a0ca08f9a4459c716b1fba541b6a450e659d54f4aca5aa3dee9c9f66f6d40"
VERSION = "0.3.19"
TARGETS = ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-x64")


def require(ok, message):
    if not ok:
        raise SystemExit(message)


def digest(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(block)
    return h.hexdigest()


def pinned(path, expected):
    with path.open("rb") as f:
        require(digest(f) == expected, f"Wrong bytes for {path.name}; stopped.")


def check(archive, root, expected_target):
    exe = shutil.which("node")
    require(exe, "Node.js 20+ with npm is required.")
    node = Path(exe).resolve(strict=True)
    npm = next((p for p in (node.parent/"node_modules/npm/bin/npm-cli.js",
                            node.parent.parent/"lib/node_modules/npm/bin/npm-cli.js",
                            Path(shutil.which("npm") or root).resolve())
                if p.name == "npm-cli.js" and p.is_file()), None)
    require(npm, "Could not locate npm alongside Node.js.")
    home, tmp = root/"home", root/"tmp"
    home.mkdir()
    tmp.mkdir()
    config = root/"empty.npmrc"
    config.touch()
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "ComSpec", "PATHEXT") if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home), LOCALAPPDATA=str(home),
               TMP=str(tmp), TEMP=str(tmp), TMPDIR=str(tmp), CI="1",
               NPM_CONFIG_USERCONFIG=str(config), NPM_CONFIG_GLOBALCONFIG=str(root/"no-global.npmrc"),
               NPM_CONFIG_CACHE=str(root/"cache"), NPM_CONFIG_UPDATE_NOTIFIER="false",
               NPM_CONFIG_OFFLINE="true", NPM_CONFIG_REGISTRY="http://127.0.0.1:1")

    def run(args):
        proc = subprocess.run(args, cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=120, text=True, encoding="utf-8", errors="replace")
        require(proc.returncode == 0, f"{Path(args[0]).name} {args[-1]} failed (exit {proc.returncode}).")
        return proc.stdout.strip()

    target = run([node, "-p", "process.platform+'-'+process.arch"])
    require(target in TARGETS and (not expected_target or target == expected_target), f"Unexpected target: {target}")
    require(int(run([node,"-p","process.versions.node.split('.')[0]"])) >= 20, "Node.js 20+ is required.")
    main = f"openvegas-ai-{VERSION}.tgz"
    platform = f"lordesteph-openvegas-{target}-{VERSION}.tgz"
    with archive.open("rb") as stream:
        require(digest(stream) == ARCHIVE, "Wrong installer download; nothing installed.")
        stream.seek(0)
        with zipfile.ZipFile(stream) as z:
            for name in (main, platform):
                with z.open(name) as src, (root/name).open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    prefix = root/"prefix"
    prefix.mkdir()
    run([node,npm,"install","--offline","--ignore-scripts","--no-audit","--no-fund","--no-package-lock",
         "--prefix",prefix,str(root/platform),str(root/main)])
    mods = prefix/"node_modules"
    package = mods/"openvegas-ai"
    js = package/"bin/openvegas.js"
    index = package/"release.json"
    pinned(js, LAUNCHER)
    pinned(index, INDEX)
    data = json.loads(index.read_bytes())["platforms"][target]
    pinned(mods/data["package"]/"bin"/data["binary"], data["sha256"])
    for alias in ("openvegas-ai", "openvegas", "ov"):
        if os.name == "nt":
            # Only the fixed, npm-generated .cmd name goes through the shell; never the user's path.
            command = [str(Path(env["SystemRoot"])/"System32/cmd.exe"),"/d","/c",
                       f"prefix\\node_modules\\.bin\\{alias}.cmd"]
        else:
            link = mods/".bin"/alias
            require(link.is_symlink() and link.resolve(strict=True) == js, "Wrong npm entry point.")
            command = [str(link)]
        require(VERSION in run([*command, "--version"]).split(), f"{alias} returned a different version.")
        if alias == "ov":
            run([*command, "--verify-bundle"])
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", type=Path, default=Path("customer-tarballs-unpublished.zip"))
    parser.add_argument("--expect-target", choices=TARGETS)
    args = parser.parse_args()
    require(args.archive.is_file(), "Pass the path to customer-tarballs-unpublished.zip from the QA download.")
    with tempfile.TemporaryDirectory(prefix="ov-install-") as temp:
        target = check(args.archive.resolve(strict=True), Path(temp).resolve(strict=True), args.expect_target)
    print(f"PASS {target} OpenVegas {VERSION}: offline npm install, three launchers, bundle verified; temporary install removed.")
