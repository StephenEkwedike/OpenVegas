"""Export a credential-free, public-source candidate. Never commits, uploads or deploys."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKS = {
    "beat-maker",
    "bicycle-finish",
    "pixel-courier",
    "skyline-dunk",
    "three-point-glow",
    "visor-explorer",
}
PRIVATE_UI = {"naruto-shadow-clone", "lebron-court", "q50-jewelry", "scenes"}
TOP = {"Dockerfile", ".dockerignore", "pyproject.toml", "requirements.lock", "README.md"}
SCRIPTS = {
    "start.sh",
    "migrate.py",
    "frozen_entry.py",
    "build_cli_candidate.py",
    "verify_emote_release.py",
    "ci/check_emote_ui_assets.py",
}
SECRET = re.compile(
    rb"(?:sk-ant-[A-Za-z0-9_-]{40,}|(?:sk|rk|rkcs)_(?:live|test)_[A-Za-z0-9]{40,}|"
    rb"whsec_[A-Za-z0-9]{24,}|npm_[A-Za-z0-9]{24,}|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def include(path: Path) -> bool:
    parts = path.parts
    if len(parts) == 1:
        return path.name in TOP
    if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in parts):
        return False
    if parts[0] in {"openvegas", "server"}:
        if path.suffix == ".py":
            return True
        if parts[:3] == ("openvegas", "emotes", "assets"):
            return (
                len(parts) == 5
                and parts[3] in PACKS
                and parts[4] in {"manifest.json", "sheet.png", "provenance.json"}
            )
        if parts[:3] == ("openvegas", "integrations", "assets"):
            return len(parts) == 4 and path.suffix in {".js", ".md"}
        return parts[:3] == ("openvegas", "tv", "assets") and path.suffix == ".json"
    if parts[0] == "ui":
        if parts[:3] == ("ui", "assets", "tv"):
            return False
        if (
            parts[:4] == ("ui", "assets", "emotes", "previews")
            and len(parts) > 4
            and parts[4] in PRIVATE_UI
        ):
            return False
        return path.suffix.lower() in {
            ".html",
            ".css",
            ".js",
            ".json",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".ico",
            ".woff",
            ".woff2",
        }
    if parts[:2] == ("supabase", "migrations"):
        return len(parts) == 3 and path.suffix == ".sql"
    if parts[0] == "scripts":
        return path.relative_to("scripts").as_posix() in SCRIPTS
    if parts[0] == "npm-cli":
        return path.as_posix() in {"npm-cli/package.json", "npm-cli/bin/openvegas.js"}
    return False


def export(root: Path, output: Path):
    if output.exists() or output.is_symlink():
        raise ValueError("Use a new candidate directory; nothing will be overwritten")
    files = []
    # Only inspect approved source roots; never read .env, private notes or evidence.
    for top in sorted(TOP | {"openvegas", "server", "ui", "supabase", "scripts", "npm-cli"}):
        source = root / top
        candidates = source.rglob("*") if source.is_dir() else [source]
        for item in candidates:
            relative = item.relative_to(root)
            if not include(relative):
                continue
            if item.is_symlink() or any(p.is_symlink() for p in item.parents if p != root.parent):
                raise ValueError("Symlinked release input: " + str(relative))
            if not item.is_file():
                continue
            data = item.read_bytes()
            if item.suffix in {".py", ".js", ".md", ".json", ".html", ".sql"} and SECRET.search(
                data
            ):
                raise ValueError(
                    "Potential credential in release input (value withheld): " + str(relative)
                )
            files.append((item, relative, hashlib.sha256(data).hexdigest()))
    output.mkdir(parents=True, mode=0o700)
    for source, relative, _digest in files:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        shutil.copymode(source, target)
    inventory = {str(relative): digest for _, relative, digest in sorted(files)}
    (output / "release-inputs.json").write_text(
        json.dumps(
            {
                "files": inventory,
                "private_art_excluded": True,
                "deployed": False,
                "certified": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"candidate": str(output), "files": len(files), "deployed": False, "certified": False}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(ROOT, args.output.absolute())
