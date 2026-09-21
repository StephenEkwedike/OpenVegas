"""Offline, fail-closed checklist completion gate. Never executes task/evidence text."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

CHECK = re.compile(r"^( {0,3})(?:[-+*]|\d+[.)])\s+\[([ xX])\]\s+(.+)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FORBIDDEN = {"env.md", "test-accounts.md", "test-account.md"}
MAX_BYTES = 4_000_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_file(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Paths must stay relative to the manifest's repository root")
    if any(p.startswith(".env") or p.lower() in FORBIDDEN for p in path.parts):
        raise ValueError("Secret-bearing files cannot be checklist evidence")
    target = root / path
    if any(p.is_symlink() for p in [target, *target.parents] if p != root):
        raise ValueError("Symlinks are not accepted")
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Missing regular file: {name}")
    return target


def read_small(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Checklist/manifest exceeds the size bound")
    return data


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            value.update(chunk)
    return value.hexdigest()


def parse_checklist(root: Path, name: str) -> list[dict]:
    lines = read_small(local_file(root, name)).decode("utf-8").splitlines()
    tasks = []
    headings: list[tuple[int, str]] = []
    occurrences: Counter = Counter()
    fence = None
    active = None
    for number, line in enumerate(lines, 1):
        marker = FENCE.match(line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker:
            fence = marker[1]
            active = None
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading[1])
            headings = [item for item in headings if item[0] < level]
            headings.append((level, heading[2]))
            active = None
            continue
        check = CHECK.match(line)
        if check:
            active = {
                "file": name,
                "line": number,
                "heading": " / ".join(h[1] for h in headings),
                "text": check[3].strip(),
                "checked": check[2].lower() == "x",
            }
            tasks.append(active)
        elif (
            active
            and line.startswith("  ")
            and line.strip()
            and not line.lstrip().startswith(("- ", "* ", "+ "))
        ):
            active["text"] += " " + line.strip()
        else:
            active = None
    for task in tasks:
        key = "\0".join((task["file"], task["heading"], " ".join(task["text"].split())))
        occurrences[key] += 1
        task["id"] = digest(f"{key}\0{occurrences[key]}".encode())[:20]
    return tasks


def collect(root: Path, files: list[str]) -> list[dict]:
    if not files or len(set(files)) != len(files):
        raise ValueError("Checklists must be nonempty and unique")
    tasks = [task for name in files for task in parse_checklist(root, name)]
    if not tasks:
        raise ValueError("No checklist tasks found; empty scope cannot pass")
    return tasks


def load_manifest(path: Path) -> tuple[Path, dict]:
    if path.is_symlink():
        raise ValueError("Manifest cannot be a symlink")
    data = json.loads(read_small(path))
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("root"), str)
        or not data["root"]
        or not isinstance(data.get("checklists"), list)
        or not data["checklists"]
        or any(not isinstance(name, str) or not name for name in data["checklists"])
        or not isinstance(data.get("requirements"), dict)
        or not data["requirements"]
    ):
        raise ValueError("Unsupported completion manifest")
    for task_id, record in data["requirements"].items():
        if (
            not re.fullmatch(r"[0-9a-f]{20}", task_id)
            or not isinstance(record, dict)
            or not isinstance(record.get("requirement"), dict)
            or record.get("status") not in ("open", "passed", "blocked")
            or any(
                not isinstance(record.get(key, ""), str)
                for key in ("summary", "reason", "owner", "next_step")
            )
        ):
            raise ValueError("Invalid completion record")
        for key in ("evidence", "subjects"):
            values = record.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                for item in values
            ):
                raise ValueError("Invalid completion evidence metadata")
    root = (path.parent / data["root"]).resolve()
    return root, data


def write_manifest(
    path: Path, data: dict, *, exclusive: bool = False, expected: str | None = None
) -> None:
    if path.is_symlink():
        raise ValueError("Manifest cannot be a symlink")
    # Refuse a concurrent writer; no lost progress from two coordinator processes.
    lock = path.with_name(path.name + ".lock")
    with lock.open("x"):
        try:
            if expected is not None and file_hash(path) != expected:
                raise ValueError("Manifest changed concurrently; reload before recording")
            if exclusive:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(data, indent=2) + "\n")
            else:
                temporary = path.with_name(path.name + ".tmp")
                try:
                    with temporary.open("x", encoding="utf-8") as stream:
                        stream.write(json.dumps(data, indent=2) + "\n")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        finally:
            lock.unlink(missing_ok=True)


def audit(root: Path, manifest: dict) -> dict:
    current = {task["id"]: task for task in collect(root, manifest["checklists"])}
    records = manifest["requirements"]
    rows = []
    for task_id in dict.fromkeys([*current, *records]):
        task = current.get(task_id)
        record = records.get(task_id, {})
        row = dict(task or record.get("requirement", {}), id=task_id)
        row["status"] = "open"
        row["reason"] = "Acceptance is not checked and evidenced"
        if task is None:
            row.update(
                status="scope_missing",
                reason="A recorded requirement was removed or edited; reconcile scope explicitly",
            )
        elif task_id not in records:
            row.update(status="untracked", reason="New requirement needs manifest sync")
        elif record.get("status") == "blocked":
            row.update(
                status="blocked",
                reason=record.get("reason", ""),
                owner=record.get("owner", ""),
                next_step=record.get("next_step", ""),
            )
            if not all(row.get(key) for key in ("reason", "owner", "next_step")):
                row.update(status="invalid", reason="Blocker needs reason, owner and next step")
        elif task["checked"]:
            row.update(status="unverified", reason="Checked item needs recorded passing evidence")
            if (
                record.get("status") == "passed"
                and record.get("summary")
                and record.get("evidence")
            ):
                valid = True
                for evidence in record["evidence"]:
                    try:
                        valid &= file_hash(local_file(root, evidence["path"])) == evidence["sha256"]
                    except (OSError, ValueError, KeyError):
                        valid = False
                for subject in record.get("subjects", []):
                    try:
                        valid &= file_hash(local_file(root, subject["path"])) == subject["sha256"]
                    except (OSError, ValueError, KeyError):
                        valid = False
                row.update(
                    status="passed" if valid else "stale",
                    reason="Evidence hashes verified"
                    if valid
                    else "Evidence is missing or changed; rerun verification",
                )
        rows.append(row)
    counts = dict(Counter(row["status"] for row in rows))
    actionable = [r for r in rows if r["status"] not in {"passed", "blocked"}]
    return {
        "complete": all(r["status"] == "passed" for r in rows),
        "counts": counts,
        "next": (actionable or [r for r in rows if r["status"] == "blocked"])[:5],
        "requirements": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "sync", "record", "audit"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "init":
            command.add_argument("--root", type=Path, default=Path.cwd())
            command.add_argument("--checklist", action="append", required=True)
        if name == "record":
            command.add_argument("--id", action="append", required=True)
            command.add_argument("--status", choices=("passed", "blocked", "open"), required=True)
            command.add_argument("--evidence", action="append", default=[])
            command.add_argument(
                "--subject",
                action="append",
                default=[],
                help="Bind verification to source/test/asset file hashes",
            )
            command.add_argument("--summary", default="")
            command.add_argument("--reason", default="")
            command.add_argument("--owner", default="")
            command.add_argument("--next-step", default="")
        if name == "audit":
            command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = args.manifest.absolute()
        if args.command == "init":
            import os

            root = args.root.resolve()
            tasks = collect(root, args.checklist)
            data = {
                "version": 1,
                "root": os.path.relpath(root, path.parent),
                "checklists": args.checklist,
                "requirements": {t["id"]: {"requirement": t, "status": "open"} for t in tasks},
            }
            write_manifest(path, data, exclusive=True)
        else:
            original = file_hash(path)
            root, data = load_manifest(path)
            if args.command == "audit":
                report = audit(root, data)
                if args.json:
                    print(json.dumps(report, indent=2))
                else:
                    print(
                        "COMPLETE"
                        if report["complete"]
                        else "INCOMPLETE - continue actionable work"
                    )
                    print(" | ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
                    for row in report["next"]:
                        print(
                            f"{row['id']} [{row['status']}] {row.get('file')}:{row.get('line')} {row.get('text')}"
                        )
                        if row.get("next_step"):
                            print(f"  Next: {row['next_step']} ({row['owner']})")
                return 0 if report["complete"] else 1
            if args.command == "sync":
                for task in collect(root, data["checklists"]):
                    data["requirements"].setdefault(
                        task["id"], {"requirement": task, "status": "open"}
                    )
            elif args.command == "record":
                if any(task_id not in data["requirements"] for task_id in args.id):
                    raise ValueError("Unknown requirement ID")
                if args.status == "passed" and (not args.evidence or not args.summary.strip()):
                    raise ValueError("Passing requires evidence files and a verification summary")
                if args.status == "blocked" and not all(
                    x.strip() for x in (args.reason, args.owner, args.next_step)
                ):
                    raise ValueError("Blocker requires reason, owner and next step")
                evidence = [
                    {"path": name, "sha256": file_hash(local_file(root, name))}
                    for name in args.evidence
                ]
                subjects = [
                    {"path": name, "sha256": file_hash(local_file(root, name))}
                    for name in args.subject
                ]
                for task_id in args.id:
                    data["requirements"][task_id].update(
                        status=args.status,
                        evidence=evidence,
                        subjects=subjects,
                        summary=args.summary.strip(),
                        reason=args.reason.strip(),
                        owner=args.owner.strip(),
                        next_step=args.next_step.strip(),
                    )
            write_manifest(path, data, expected=original)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"completion gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
