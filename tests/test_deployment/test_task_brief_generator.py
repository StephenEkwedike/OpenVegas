"""Offline contract tests; no app imports, agents, credentials or network calls."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/generate_task_brief.py"
loader = importlib.util.spec_from_file_location("task_brief_generator", SCRIPT)
brief = importlib.util.module_from_spec(loader)
loader.loader.exec_module(brief)


@pytest.fixture
def spec():
    return brief.scaffold("Example feature", "Implement a safe local feature.")


def test_scaffold_preserves_request_and_starts_unchecked(spec):
    rendered = brief.render_brief(spec)
    assert "Execution status: NOT STARTED" in rendered
    assert "recon -> implement -> verify" in rendered
    assert "Implement a safe local feature." in rendered
    assert "- [x]" not in rendered
    assert "at most 3 concurrent bounded workers" in rendered
    assert rendered == brief.render_brief(spec)


def test_fence_cannot_be_closed_by_request(spec):
    spec["request"] = "Keep this code:\n````python\nprint('hello')\n````\n" + "a `code` span"
    rendered = brief.render_brief(spec)
    assert "`````text\n" + spec["request"] + "\n`````" in rendered


@pytest.mark.parametrize("workers", [0, 4, True, "3", 2.5])
def test_invalid_worker_limits(spec, workers):
    spec["max_workers"] = workers
    with pytest.raises(ValueError, match="max_workers"):
        brief.render_brief(spec)


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_invalid_versions(spec, version):
    spec["version"] = version
    with pytest.raises(ValueError, match="version"):
        brief.render_brief(spec)


@pytest.mark.parametrize("field", ["title", "objective", "workstreams"])
def test_missing_required_fields(spec, field):
    del spec[field]
    with pytest.raises(ValueError):
        brief.render_brief(spec)


@pytest.mark.parametrize("field", ["owner", "paths", "deliverables", "checks"])
def test_missing_workstream_contract(spec, field):
    del spec["workstreams"][0][field]
    with pytest.raises(ValueError):
        brief.render_brief(spec)


def test_topological_order_not_input_order(spec):
    spec["workstreams"].reverse()
    assert brief.validate_spec(spec) == ["recon", "implement", "verify"]


def test_independent_workstreams(spec):
    branch = copy.deepcopy(spec["workstreams"][1])
    branch["id"] = "independent"
    spec["workstreams"].append(branch)
    assert brief.validate_spec(spec) == ["recon", "implement", "independent", "verify"]


@pytest.mark.parametrize("dependency", ["unknown", "verify", "recon"])
def test_bad_dependencies(spec, dependency):
    spec["workstreams"][0]["depends_on"] = [dependency]
    with pytest.raises(ValueError, match="unknown dependencies|cycle"):
        brief.render_brief(spec)


def test_duplicate_id(spec):
    spec["workstreams"].append(copy.deepcopy(spec["workstreams"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        brief.render_brief(spec)


@pytest.mark.parametrize(
    "sections", [None, ["bad"], [{"title": "x"}], [{"title": "x\ny", "body": "body"}]]
)
def test_invalid_sections(spec, sections):
    spec["sections"] = sections
    with pytest.raises((ValueError, TypeError)):
        brief.render_brief(spec)


def test_invalid_workstream_type(spec):
    spec["workstreams"] = ["bad"]
    with pytest.raises(TypeError):
        brief.render_brief(spec)


def invoke(tmp_path, *args, stdin=None):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *map(str, args)],
        cwd=tmp_path,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=False,
    )


def test_cli_runs_from_any_directory_and_preserves_unicode(tmp_path):
    request = tmp_path / "request.txt"
    request.write_text("Show a caf\u00e9 sprite and `code` safely.", encoding="utf-8")
    output = tmp_path / "nested" / "task.md"
    result = invoke(tmp_path, "--title", "Art", "--request-file", request, "--output", output)
    assert result.returncode == 0, result.stderr
    assert "caf\u00e9" in output.read_text(encoding="utf-8")
    assert str(output) in result.stdout


def test_stdin_and_no_shell_execution(tmp_path):
    output = tmp_path / "task.md"
    request = "Do not execute: $(touch SHOULD_NOT_EXIST)"
    result = invoke(
        tmp_path, "--title", "Safety", "--request-file", "-", "--output", output, stdin=request
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert request in output.read_text()


def test_existing_progress_is_never_overwritten(tmp_path):
    output = tmp_path / "task.md"
    output.write_text("- [x] Important existing progress\n")
    result = invoke(
        tmp_path, "--title", "New", "--request-file", "-", "--output", output, stdin="request"
    )
    assert result.returncode == 2
    assert output.read_text() == "- [x] Important existing progress\n"


def test_dangling_symlink_is_not_followed(tmp_path):
    output = tmp_path / "task.md"
    target = tmp_path / "must-not-exist.md"
    try:
        output.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this platform")
    result = invoke(
        tmp_path, "--title", "New", "--request-file", "-", "--output", output, stdin="request"
    )
    assert result.returncode == 2
    assert not target.exists()


@pytest.mark.parametrize(
    "arguments,stdin",
    [
        (["--request-file", "-"], "request"),
        (["--title", "Empty", "--request-file", "-"], ""),
        (["--title", "Missing", "--request-file", "missing.txt"], None),
    ],
)
def test_invalid_cli_does_not_create_output(tmp_path, arguments, stdin):
    output = tmp_path / "nested" / "task.md"
    result = invoke(tmp_path, *arguments, "--output", output, stdin=stdin)
    assert result.returncode == 2
    assert not output.parent.exists()


def test_malformed_toml(tmp_path):
    source = tmp_path / "bad.toml"
    source.write_text("version = [")
    result = invoke(tmp_path, "--spec", source, "--output", tmp_path / "out.md")
    assert result.returncode == 2
    assert not (tmp_path / "out.md").exists()


def test_non_markdown_output_rejected(tmp_path):
    result = invoke(
        tmp_path, "--title", "Task", "--request-file", "-", "--output", "out.py", stdin="request"
    )
    assert result.returncode == 2
    assert not (tmp_path / "out.py").exists()


def test_real_spec_covers_all_requested_work():
    source = ROOT / "docs/task-briefs/specs/emotes-models-marketing.toml"
    spec = brief.tomllib.loads(source.read_text(encoding="utf-8"))
    rendered = brief.render_brief(spec)
    assert set(brief.validate_spec(spec)) == {
        "recon",
        "contracts",
        "art",
        "runtime",
        "commerce",
        "models",
        "marketing",
        "release",
    }
    for required in (
        "openvegas emote",
        "LeBron",
        "Ronaldo",
        "Curry",
        "Stripe",
        "Mistral",
        "Docker",
        "scrollback",
    ):
        assert required in rendered


def test_real_spec_cli(tmp_path):
    source = ROOT / "docs/task-briefs/specs/emotes-models-marketing.toml"
    output = tmp_path / "generated.md"
    result = invoke(tmp_path, "--spec", source, "--output", output)
    assert result.returncode == 0, result.stderr
    expected = brief.render_brief(brief.tomllib.loads(source.read_text(encoding="utf-8")))
    assert output.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    "gate,owner,requirements",
    [
        ("SPR-ART-01", "art", ["explicit art approval", "actual terminal capture"]),
        ("SPR-DANCE-02", "art", ["at least six authored frames", "same static image"]),
        ("SPR-LIFE-03", "runtime", ["different event IDs", "after cancellation"]),
        ("SPR-TERM-04", "runtime", ["one compositor/render owner", "snap-to-bottom"]),
    ],
)
def test_sprite_remediation_gates_survive_generation(gate, owner, requirements):
    source = ROOT / "docs/task-briefs/specs/emotes-models-marketing.toml"
    spec = brief.tomllib.loads(source.read_text(encoding="utf-8"))
    rendered = brief.render_brief(spec)
    section = next(
        item for item in spec["sections"] if item["title"] == "Mandatory Sprite Remediation Gates"
    )
    stream = next(item for item in spec["workstreams"] if item["id"] == owner)
    assert f"### {gate}:" in section["body"]
    assert any(gate in check for check in stream["checks"])
    for requirement in requirements:
        assert requirement in section["body"]
    assert section["body"].strip() in rendered
    assert "- [x]" not in rendered


def test_spec_cannot_silently_accept_title_override(tmp_path):
    source = ROOT / "docs/task-briefs/specs/emotes-models-marketing.toml"
    result = invoke(tmp_path, "--spec", source, "--title", "Wrong", "--output", "out.md")
    assert result.returncode == 2
    assert not (tmp_path / "out.md").exists()
