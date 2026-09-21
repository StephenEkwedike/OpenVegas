"""No app imports, credentials, subprocess agents or network."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/task_completion.py"
spec = importlib.util.spec_from_file_location("task_completion", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "plan.md").write_text("# Plan\n- [ ] Build\n- [x] Test\n")
    (tmp_path / "proof.txt").write_text("A human-reviewed test result\n")
    (tmp_path / "code.py").write_text("x = 1\n")
    manifest = tmp_path / "state.json"
    assert (
        gate.main(
            ["init", "--manifest", str(manifest), "--root", str(tmp_path), "--checklist", "plan.md"]
        )
        == 0
    )
    return tmp_path, manifest


def record(project, task=1, *extra):
    root, manifest = project
    tasks = gate.parse_checklist(root, "plan.md")
    return gate.main(
        [
            "record",
            "--manifest",
            str(manifest),
            "--id",
            tasks[task]["id"],
            "--status",
            "passed",
            "--evidence",
            "proof.txt",
            "--subject",
            "code.py",
            "--summary",
            "verified result",
            *extra,
        ]
    )


def report(project):
    root, manifest = project
    _, data = gate.load_manifest(manifest)
    return gate.audit(root, data)


def test_checked_but_unevidenced_cannot_pass(project):
    assert report(project)["counts"] == {"open": 1, "unverified": 1}
    assert gate.main(["audit", "--manifest", str(project[1])]) == 1


@pytest.mark.parametrize("value", [[], None, 1, {}, {"version": True, "requirements": {}}])
def test_invalid_manifest_shape_refused_without_traceback(project, value, capsys):
    project[1].write_text(json.dumps(value))
    assert gate.main(["audit", "--manifest", str(project[1])]) == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "key,value",
    [
        ("evidence", "not-a-list"),
        ("subjects", [None]),
        ("evidence", [{"path": "proof.txt", "sha256": []}]),
        ("status", []),
        ("summary", {}),
        ("requirement", None),
    ],
)
def test_malformed_record_cannot_pass_or_crash(project, key, value):
    data = json.loads(project[1].read_text())
    next(iter(data["requirements"].values()))[key] = value
    project[1].write_text(json.dumps(data))
    assert gate.main(["audit", "--manifest", str(project[1])]) == 2


def test_record_never_ticks_box(project):
    assert record(project, 0) == 0
    assert "- [ ] Build" in (project[0] / "plan.md").read_text()
    assert not report(project)["complete"]


def test_all_checked_and_evidenced_pass(project):
    root, manifest = project
    (root / "plan.md").write_text("# Plan\n- [x] Build\n- [x] Test\n")
    assert record(project, 0) == record(project, 1) == 0
    assert gate.main(["audit", "--manifest", str(manifest), "--json"]) == 0
    assert report(project)["counts"] == {"passed": 2}


@pytest.mark.parametrize("name", ["proof.txt", "code.py"])
def test_changed_evidence_or_source_fails(project, name):
    assert record(project) == 0
    (project[0] / name).write_text("changed")
    assert report(project)["counts"]["stale"] == 1


def test_missing_evidence_fails(project):
    assert record(project) == 0
    (project[0] / "proof.txt").unlink()
    assert report(project)["counts"]["stale"] == 1


def test_deleting_or_editing_requirement_cannot_hide_it(project):
    (project[0] / "plan.md").write_text("# Plan\n- [x] Test\n- [ ] Easier requirement\n")
    assert report(project)["counts"] == {"unverified": 1, "untracked": 1, "scope_missing": 1}
    assert gate.main(["sync", "--manifest", str(project[1])]) == 0
    assert report(project)["counts"]["scope_missing"] == 1


def test_blocker_is_not_completion(project):
    task_id = gate.parse_checklist(project[0], "plan.md")[0]["id"]
    args = ["record", "--manifest", str(project[1]), "--id", task_id, "--status", "blocked"]
    assert gate.main(args) == 2
    assert (
        gate.main(
            [
                *args,
                "--reason",
                "Needs native test host",
                "--owner",
                "operator",
                "--next-step",
                "Provide Windows runner",
            ]
        )
        == 0
    )
    assert report(project)["counts"]["blocked"] == 1
    assert report(project)["complete"] is False


def test_parser_ignores_fences_quotes_and_non_tasks(tmp_path):
    (tmp_path / "plan.md").write_text(
        "# Real\n- [ ] first\n  continued here\n\n```md\n- [ ] fake\n```\n"
        "> - [ ] quoted\n~~~text\n- [x] fake again\n~~~\n  - [X] second\n"
    )
    tasks = gate.parse_checklist(tmp_path, "plan.md")
    assert [(t["text"], t["checked"]) for t in tasks] == [
        ("first continued here", False),
        ("second", True),
    ]


def test_ids_stable_across_line_numbers_and_checkbox_state(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n- [ ] first\n  second\n- [ ] duplicate\n- [ ] duplicate\n")
    before = gate.parse_checklist(tmp_path, "plan.md")
    path.write_text("\n\n# Plan\n- [x] first second\n- [ ] duplicate\n- [ ] duplicate\n")
    after = gate.parse_checklist(tmp_path, "plan.md")
    assert [t["id"] for t in before] == [t["id"] for t in after]
    assert after[1]["id"] != after[2]["id"]


def test_init_does_not_overwrite_progress(project):
    before = project[1].read_bytes()
    assert (
        gate.main(
            [
                "init",
                "--manifest",
                str(project[1]),
                "--root",
                str(project[0]),
                "--checklist",
                "plan.md",
            ]
        )
        == 2
    )
    assert project[1].read_bytes() == before


@pytest.mark.parametrize(
    "name", ["../outside", "/tmp/outside", ".env", "env.md", "test-accounts.md"]
)
def test_outside_and_secrets_refused(project, name):
    with pytest.raises(ValueError):
        gate.local_file(project[0], name)


def test_symlink_refused(project):
    root, _ = project
    (root / "link").symlink_to(root / "proof.txt")
    with pytest.raises(ValueError, match="Symlink"):
        gate.local_file(root, "link")


def test_empty_scope_cannot_pass(tmp_path):
    (tmp_path / "empty.md").write_text("# Nothing\n")
    assert (
        gate.main(
            [
                "init",
                "--manifest",
                str(tmp_path / "state.json"),
                "--root",
                str(tmp_path),
                "--checklist",
                "empty.md",
            ]
        )
        == 2
    )


def test_concurrent_update_and_lock_preserve_progress(project):
    _, path = project
    before = path.read_bytes()
    _, data = gate.load_manifest(path)
    with pytest.raises(ValueError, match="concurrently"):
        gate.write_manifest(path, data, expected="old")
    assert path.read_bytes() == before
    lock = path.with_name(path.name + ".lock")
    lock.write_text("")
    assert record(project) == 2
    assert lock.exists()
    assert path.read_bytes() == before


def test_record_requires_explicit_evidence(project):
    root, path = project
    task_id = gate.parse_checklist(root, "plan.md")[0]["id"]
    assert (
        gate.main(["record", "--manifest", str(path), "--id", task_id, "--status", "passed"]) == 2
    )
    assert json.loads(path.read_text())["requirements"][task_id]["status"] == "open"
