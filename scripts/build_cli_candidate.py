"""Build and test one native, unpublished CLI/npm candidate. Never signs or uploads.

Run in a dedicated build venv with .[audio], PyInstaller and Node installed.
The resulting npm tarball is deliberately platform-specific and private; it is
test evidence, not a replacement for a reviewed universal npm release.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}


def source_digest():
    digest = hashlib.sha256()
    files = [
        ROOT / "pyproject.toml",
        ROOT / "requirements.lock",
        ROOT / "scripts/frozen_entry.py",
        ROOT / "scripts/build_cli_candidate.py",
    ]
    files.extend(
        path
        for path in (ROOT / "openvegas").rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".png"}
    )
    for path in sorted(files):
        if path.is_symlink():
            raise ValueError("Symlinked source is not a release input")
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def run(args, *, cwd, env=None, timeout=1200):
    subprocess.run(args, cwd=cwd, env=env, check=True, timeout=timeout)


def clean_environment(home):
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ}
    env.update(
        {
            key: str(home)
            for key in (
                "HOME",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
            )
        }
    )
    env.update(
        {
            "PATH": os.defpath,
            "OPENVEGAS_DOTENV_OVERRIDE": "0",
            "OPENVEGAS_ENABLE_TOUCHID": "0",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "LANG": "C.UTF-8",
        }
    )
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error("Use a new output directory; existing artifacts are never overwritten")
    operating_system = PLATFORMS[platform.system()]
    architecture = ARCHES[platform.machine()]
    name = f"openvegas-{operating_system}-{architecture}"
    sources = source_digest()
    output.mkdir(parents=True, mode=0o700)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        name,
        "--distpath",
        str(output / "bin"),
        "--workpath",
        str(output / "work"),
        "--specpath",
        str(output / "spec"),
        "--paths",
        str(ROOT),
        "--collect-all",
        "openvegas.emotes",
        "--collect-all",
        "sounddevice",
        "--collect-submodules",
        "keyring.backends",
        "--copy-metadata",
        "openvegas",
        str(ROOT / "scripts/frozen_entry.py"),
    ]
    with tempfile.TemporaryDirectory(prefix="openvegas-build-home-") as build_home:
        run(command, cwd=ROOT, env=clean_environment(build_home))
    binary = output / "bin" / (name + (".exe" if operating_system == "win32" else ""))
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("Native builder did not produce the expected executable")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="openvegas-candidate-home-") as home:
        # Clean home/cwd: do not accidentally validate against the developer's config.
        env = clean_environment(home)
        for arguments in (["--verify-bundle"], ["emote", "doctor"], ["--help"]):
            result = subprocess.run(
                [str(binary), *arguments],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            (output / (arguments[-1].replace("--", "") + ".txt")).write_text(result.stdout)
    package = output / "npm"
    (package / "bin").mkdir(parents=True)
    shutil.copy2(binary, package / "bin" / binary.name)
    manifest = {
        "name": "openvegas-native-candidate",
        "version": version,
        "private": True,
        "type": "module",
        "bin": {"openvegas": "bin/openvegas.js"},
        "os": [operating_system],
        "cpu": [architecture],
        "files": ["bin"],
    }
    (package / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    launcher = """#!/usr/bin/env node
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const binary = fileURLToPath(new URL(BINARY, import.meta.url));
if (process.platform !== PLATFORM || process.arch !== ARCH ||
    createHash('sha256').update(readFileSync(binary)).digest('hex') !== DIGEST) {
  console.error('OpenVegas candidate platform/checksum mismatch.'); process.exit(1);
}
const result = spawnSync(binary, process.argv.slice(2), {stdio:'inherit', shell:false});
if (result.error) process.exit(1);
if (result.signal) { process.kill(process.pid, result.signal); process.exit(1); }
process.exit(result.status ?? 1);
"""
    for key, value in {
        "BINARY": "./" + binary.name,
        "PLATFORM": operating_system,
        "ARCH": architecture,
        "DIGEST": digest,
    }.items():
        launcher = launcher.replace(key, json.dumps(value))
    (package / "bin/openvegas.js").write_text(launcher)
    (package / "bin/openvegas.js").chmod(0o755)
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise ValueError("Node and npm required for the actual npm candidate gate")
    # npm's CLI is JS; avoid a Windows .cmd shell when packing.
    npm_cli = Path(npm).resolve()
    if operating_system == "win32":
        npm_cli = Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
    with tempfile.TemporaryDirectory(prefix="openvegas-npm-pack-home-") as pack_home:
        run(
            [node, str(npm_cli), "pack", "--ignore-scripts"],
            cwd=package,
            env=clean_environment(pack_home),
        )
    tarballs = list(package.glob("*.tgz"))
    if len(tarballs) != 1:
        raise ValueError("Expected one npm candidate archive")
    with tempfile.TemporaryDirectory(prefix="openvegas-npm-install-") as installed:
        # The binary-test HOME above has been removed. Keep this separate clean
        # HOME alive for both the launcher and installed-package checks.
        clean_home = Path(installed) / "home"
        clean_home.mkdir(mode=0o700)
        env = clean_environment(clean_home)
        run([node, str(package / "bin/openvegas.js"), "emote", "doctor"], cwd=installed, env=env)
        run(
            [
                node,
                str(npm_cli),
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefix",
                installed,
                str(tarballs[0]),
            ],
            cwd=installed,
            env=env,
        )
        installed_entry = (
            Path(installed) / "node_modules/openvegas-native-candidate/bin/openvegas.js"
        )
        run([node, str(installed_entry), "emote", "doctor"], cwd=installed, env=env)
    if sources != source_digest():
        raise ValueError("Source changed during build; candidate is not certifiable")
    (output / "candidate.json").write_text(
        json.dumps(
            {
                "version": version,
                "os": operating_system,
                "arch": architecture,
                "binary": binary.name,
                "sha256": digest,
                "source_sha256": sources,
                "published": False,
                "signed_release": False,
                "native_ux_verified": False,
                "npm_install_verified": True,
                "scope": "unpublished single-platform frozen and npm candidate",
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
