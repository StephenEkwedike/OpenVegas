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

ROOT = Path.cwd().resolve(strict=True)
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


def linux_audio_inputs(stage):
    """Use the reviewed ALSA-only pair; never collect the distro JACK build."""
    stage.mkdir()
    root = Path(os.environ["OPENVEGAS_LINUX_AUDIO"]).resolve(strict=True)
    args = []
    loader = {"aarch64": "ld-linux-aarch64.so.1", "x86_64": "ld-linux-x86-64.so.2"}[platform.machine()]
    base = {"libc.so.6", "libm.so.6", "libpthread.so.0", "librt.so.1", "libdl.so.2", loader}
    for name in ("libportaudio.so.2", "libasound.so.2"):
        path = root / name
        dynamic = subprocess.check_output(
            ["readelf", "-d", path], text=True, env={"PATH": os.defpath, "LC_ALL": "C"}
        ).splitlines()
        needed = {line.split("[")[1].split("]")[0] for line in dynamic if "(NEEDED)" in line}
        expected = {"libasound.so.2"} if name == "libportaudio.so.2" else set()
        if not needed or needed - base != expected:
            raise ValueError(f"Unexpected dependencies for {name}: {sorted(needed)}")
        if not any("(SONAME)" in line and f"[{name}]" in line for line in dynamic):
            raise ValueError(f"Missing SONAME: {name}")
        args.extend(["--add-binary", f"{path}:."])

    required = ["alsa/alsa.conf", "notices/build-audio.sh", "notices/packages.tsv",
                "notices/libasound2/copyright", "notices/libportaudio2/copyright", "notices/LGPL-2.1"]
    if not all((root / p).is_file() for p in required):
        raise ValueError("Incomplete Linux audio configuration or notices")
    for package in ("alsa-lib", "portaudio19"):
        if len(list((root / "source").glob(package + "_*.dsc"))) != 1:
            raise ValueError(f"Missing matching distribution source for {package}")
    subprocess.run(["sha256sum", "--strict", "--check", "notices/SHA256SUMS"], cwd=root, check=True)
    for src, dest in [("alsa", "linux-audio-alsa"), ("notices", "linux-audio-notices"),
                      ("source", "linux-audio-source")]:
        args.extend(["--add-data", f"{root / src}:{dest}"])
    # The upstream PyInstaller hook otherwise also collects system PortAudio + JACK.
    (stage / "hook-sounddevice.py").write_text("# Linux audio inputs supplied explicitly.\n")
    args.extend(["--additional-hooks-dir", str(stage)])
    # Linux sounddevice asks find_library before dlopen. A SONAME bundled by
    # itself is invisible to find_library on a consumer without ldconfig entries.
    hook = stage / "linux_audio_hook.py"
    hook.write_text("""def _install():
    import ctypes.util
    import os
    import sys
    find = ctypes.util.find_library
    def lookup(name):
        if name != 'portaudio':
            return find(name)
        if not os.path.isfile('/usr/share/alsa/alsa.conf') and 'ALSA_CONFIG_PATH' not in os.environ:
            os.environ.setdefault('ALSA_CONFIG_DIR', os.path.join(sys._MEIPASS, 'linux-audio-alsa'))
        return os.path.join(sys._MEIPASS, 'libportaudio.so.2')
    ctypes.util.find_library = lookup
_install()
""")
    return [*args, "--runtime-hook", str(hook)]


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


def verify_version_output(output, version):
    if not output.strip().endswith(f", version {version}"):
        raise ValueError("Native runtime version differs from release package version")


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
    if runpy.run_path(str(ROOT / "openvegas/__init__.py"))["__version__"] != version:
        raise ValueError("Source runtime version differs from release package version")
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
    if operating_system == "linux":
        command[-1:-1] = linux_audio_inputs(output / "linux-audio")
    if operating_system == "darwin":
        # collect-all includes submodules/data/binaries for lazy PyObjC frameworks.
        command[-1:-1] = [
            "--collect-all", "objc",
            "--collect-all", "Foundation",
            "--collect-all", "Security",
        ]
    with tempfile.TemporaryDirectory(prefix="openvegas-build-home-") as build_home:
        run(command, cwd=ROOT, env=clean_environment(build_home))
    binary = output / "bin" / (name + (".exe" if operating_system == "win32" else ""))
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("Native builder did not produce the expected executable")
    if operating_system == "linux":
        from PyInstaller.archive.readers import CArchiveReader

        archive = CArchiveReader(str(binary))
        root = Path(os.environ["OPENVEGAS_LINUX_AUDIO"])
        for name in ("libportaudio.so.2", "libasound.so.2"):
            if archive.extract(name) != (root / name).read_bytes():
                raise ValueError(f"Frozen audio library differs from reviewed input: {name}")
        if any(Path(name).name.startswith(("libjack.so", "libdb-5.")) for name in archive.toc):
            raise ValueError("Unexpected JACK/Berkeley DB in frozen Linux candidate")
        for path in (root / "source").iterdir():
            if archive.extract("linux-audio-source/" + path.name) != path.read_bytes():
                raise ValueError(f"Missing or changed corresponding source: {path.name}")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    terminal_profile_verified = False
    with tempfile.TemporaryDirectory(prefix="openvegas-candidate-home-") as home:
        # Clean home/cwd: do not accidentally validate against the developer's config.
        env = clean_environment(home)
        bundle_args = ["--verify-bundle", *(["--include-review-packs"] if args.include_review_packs else [])]
        for arguments in (bundle_args, ["emote", "doctor"], ["--help"], ["--version"]):
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
            if arguments == ["--version"]:
                verify_version_output(result.stdout, version)
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
    if sources != source_digest():
        raise ValueError("Source changed during native build")
    vf = runpy.run_path(str(Path(__file__).with_name("verify_native_router_candidate.py")))
    freeze = ROOT.parent / "freeze.json"
    receipt = vf["load_pinned"](freeze, os.environ["FREEZE_PIN"])
    blobs = vf["inventory"](ROOT, receipt["inputs"]["cli"])
    selected = receipt["checkout_native_inputs"]
    assert sources == vf["source_digest"](blobs) == os.environ["SOURCE_PIN"]
    assert operating_system + "-" + architecture == os.environ["TARGET"]
    assert not args.include_review_packs and version == "0.3.21"
    assert selected["openvegas/integrations/runtime_directory.py"]
    for path, pin in selected.items():
        assert hashlib.sha256(blobs[path]).hexdigest() == pin
        vf["validate_imports"](path, blobs[path], blobs)
    exact = vf["verify_binary"](binary, blobs, selected)
    exact.update(exact_entry=vf["verify_entry"](binary, blobs), exact_assets=vf["verify_assets"](binary, blobs, selected),
                 source_sha256=sources, freeze_sha256=os.environ["FREEZE_PIN"], binary_sha256=digest)
    (output / "independent-verified.json").write_text(json.dumps(exact,sort_keys=True))
    record = dict(os=operating_system, arch=architecture, binary=binary.name, version=version,
                  sha256=digest, source_sha256=sources, includes_private_review_packs=False, published=False,
                  publication_approved=False, native_source_verified=True, npm_install_verified=False,
                  terminal_profile_command_verified=terminal_profile_verified, native_ux_verified=False)
    (output / "candidate.json").write_text(json.dumps(record,sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
