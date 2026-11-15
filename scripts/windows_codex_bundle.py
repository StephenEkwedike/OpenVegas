"""Unpack the independently pinned publisher CLI layout, only into a new QA directory."""
import hashlib
import re
import shutil
import sys
import tarfile
from pathlib import Path, PureWindowsPath

ARCHIVE = "d559bb88e74d471265857b93b7f19bc250dae527673b9191b33534478093f6bc"
CODEX = "9d563ee4e0441ff9d59d612013e0460ddf8e5709735c2d9f6b98b7ef8ca4eb42"


def check(value):
    if not value:
        raise ValueError("Unexpected publisher package")


def sha(p):
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def extract(src, dst):
    check(sha(src) == ARCHIVE)
    dst.mkdir(exist_ok=False)
    seen, n, total = set(), 0, 0
    with tarfile.open(src, "r|gz") as tar:
        for m in tar:
            parts = m.name.split("/")
            check(m.isfile() or m.isdir())
            check(all(re.fullmatch(r"[a-zA-Z0-9._-]+", p) and p not in (".", "..") and not p.endswith(".")
                      and not PureWindowsPath(p).is_reserved() for p in parts))
            check(m.name.casefold() not in seen)
            seen.add(m.name.casefold())
            out = dst.joinpath(*parts)
            if m.isdir():
                out.mkdir(exist_ok=True)
                continue
            n += 1
            total += m.size
            check(n <= 46 and total <= 586895585 and 0 < m.size <= 371735856)
            with tar.extractfile(m) as stream, out.open("xb") as f:
                shutil.copyfileobj(stream, f)
    check(n == 46 and total == 586895585 and sha(dst / "bin/codex.exe") == CODEX)
    check(all((dst / p).is_file() for p in ("bin/codex-code-mode-host.exe", "bin/codex-exec-server.exe",
                                            "codex-resources/codex-command-runner.exe", "codex-path/rg.exe")))


if __name__ == "__main__":
    extract(*map(Path, sys.argv[1:]))
