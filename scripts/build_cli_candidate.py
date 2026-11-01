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
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}


def pack_inputs(*, include_review_packs=False):
    inventory = runpy.run_path(str(ROOT / 'scripts/frozen_entry.py'))
    names = inventory['REQUIRED_PACKS'] | (inventory['REVIEW_PACKS'] if include_review_packs else set())
    arguments = []
    for name in sorted(names):
        source = ROOT / 'openvegas/emotes/assets' / name
        if not source.is_dir() or any(path.is_symlink() for path in (source, *source.rglob('*'))):
            raise ValueError('Missing or symlinked release pack')
        arguments.extend(['--add-data', str(source) + ':openvegas/emotes/assets/' + name])
    return arguments


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
        if path.is_file() and path.suffix in {".py", ".json", ".png", ".js", ".md"}
    )
    # Windows Path ordering folds case, unlike POSIX. Hash identical relative
    # names in identical order on every runner, without normalizing file bytes.
    for path in sorted(files, key=lambda item: item.relative_to(ROOT).as_posix()):
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


def npm_launcher(binary_name, operating_system, architecture, digest):
    """Generate the same interactive onboarding contract as the source npm shim."""
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
const args = process.argv.slice(2);
if (!args.length && process.stdin.isTTY && process.stdout.isTTY && !process.env.CI) args.push('go');
const result = spawnSync(binary, args, {stdio:'inherit', shell:false});
if (result.error) process.exit(1);
if (result.signal) { process.kill(process.pid, result.signal); process.exit(1); }
process.exit(result.status ?? 1);
"""
    for key, value in {"BINARY": "./" + binary_name, "PLATFORM": operating_system,
                       "ARCH": architecture, "DIGEST": digest}.items():
        launcher = launcher.replace(key, json.dumps(value))
    return launcher


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-review-packs", action="store_true",
                        help="Explicit private artwork test only, never public release approval")
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
        "--collect-submodules",
        "openvegas.emotes",
        "--collect-all",
        "openvegas.integrations",
        "--collect-all",
        "sounddevice",
        "--collect-submodules",
        "keyring.backends",
        "--copy-metadata",
        "openvegas",
        str(ROOT / "scripts/frozen_entry.py"),
    ]
    # Explicit allowlist, not collect-all: private fan-art must never slip into
    # the default installer just because it exists in a developer checkout.
    command[-1:-1] = pack_inputs(include_review_packs=args.include_review_packs)
    if operating_system == "darwin":
        # collect-all includes submodules/data/binaries for lazy PyObjC frameworks.
        command[-1:-1] = [
            "--collect-all", "objc",
            "--collect-all", "Foundation",
            "--collect-all", "Security",
            "--collect-all", "LocalAuthentication",
        ]
    with tempfile.TemporaryDirectory(prefix="openvegas-build-home-") as build_home:
        run(command, cwd=ROOT, env=clean_environment(build_home))
    binary = output / "bin" / (name + (".exe" if operating_system == "win32" else ""))
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("Native builder did not produce the expected executable")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    terminal_profile_verified = False
    with tempfile.TemporaryDirectory(prefix="openvegas-candidate-home-") as home:
        # Clean home/cwd: do not accidentally validate against the developer's config.
        env = clean_environment(home)
        bundle_args = ["--verify-bundle", *(["--include-review-packs"] if args.include_review_packs else [])]
        for arguments in (bundle_args, ["emote", "doctor"], ["--help"]):
            result = subprocess.run(
                [str(binary), *arguments],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            # Keep clean-environment failure diagnostics before raising, not just successes.
            (output / (arguments[-1].replace("--", "") + ".txt")).write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            result.check_returncode()
            if arguments == bundle_args and operating_system == 'darwin':
                profile = json.loads(result.stdout)['terminal_profile']
                probe = subprocess.run(
                    [profile['path'], *profile['args'], '-c',
                     'test -z "$TERM_PROGRAM" && test "$TERM" = openvegas-sixel && printf PROFILE_SHELL_OK'],
                    cwd=home, env={**env, 'TERM_PROGRAM': 'vscode'},
                    capture_output=True, text=True, timeout=30, check=True)
                if probe.stdout != 'PROFILE_SHELL_OK':
                    raise ValueError('Packaged terminal profile did not execute the shell')
                (output / 'terminal-profile.txt').write_text(probe.stdout + '\n')
                terminal_profile_verified = True
    package = output / "npm"
    (package / "bin").mkdir(parents=True)
    shutil.copy2(binary, package / "bin" / binary.name)
    manifest = {
        "name": "openvegas-native-candidate",
        "version": version,
        "private": True,
        "type": "module",
        "bin": {"openvegas": "bin/openvegas.js", "ov": "bin/openvegas.js"},
        "os": [operating_system],
        "cpu": [architecture],
        "files": ["bin"],
    }
    (package / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    launcher = npm_launcher(binary.name, operating_system, architecture, digest)
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
        for host in ('claude', 'codex'):
            run([node, str(installed_entry), host, '--dry-run'], cwd=installed, env=env)
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
                "includes_private_review_packs": args.include_review_packs,
                "publication_approved": False,
                "signed_release": False,
                "native_ux_verified": False,
                "npm_install_verified": True,
                "terminal_profile_command_verified": terminal_profile_verified,
                "scope": "unpublished single-platform frozen and npm candidate",
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
