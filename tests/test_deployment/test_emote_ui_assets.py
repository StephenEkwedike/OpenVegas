"""Offline static deployment closure checks; no application/config imports."""

import runpy
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/ci/check_emote_ui_assets.py"
CHECK = runpy.run_path(str(CHECKER))


def put(root, name, text=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def check(root):
    visited, errors = CHECK["check_assets"](root)
    return {path.relative_to(root).as_posix() for path in visited}, errors


def test_real_emotes_page_has_static_dependency_closure():
    visited, errors = check(ROOT / "ui")
    assert not errors, "\n".join(errors)
    assert {
        "emotes.html",
        "assets/emotes.js",
        "assets/emotes.css",
        "assets/emote-view.js",
    } <= visited


@pytest.mark.parametrize("missing", ["emotes.js", "emotes.css", "emote-view.js"])
def test_each_release_omission_is_caught(tmp_path, missing):
    # Use the actual page and modules; a worktree-only file must not mask a clean
    # build artifact's missing dependency.
    ui = ROOT / "ui"
    visited, errors = CHECK["check_assets"](ui)
    assert not errors
    for path in visited:
        relative = path.relative_to(ui)
        if relative == Path("assets") / missing:
            continue
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    _, errors = check(tmp_path)
    assert len(errors) == 1, errors
    assert f"missing file ui/assets/{missing}" in errors[0]


def test_recursive_modules_css_queries_cycles_and_inline_modules(tmp_path):
    put(
        tmp_path,
        "emotes.html",
        """
      <script type="module" src="/ui/assets/app.js?v=3#x"></script>
      <script type="module">import './inline.js';</script>
      <link rel="stylesheet" href="assets/main.css?v=2&amp;theme=dark">
      <a href="/ui/balance">Not a static asset</a>
      <img src="assets/a%20b.png" srcset="assets/a%20b.png 1x, assets/b.png 2x">
      <video poster="assets/poster.jpg"><source src="assets/demo.mp4"></video>
    """,
    )
    put(
        tmp_path,
        "assets/app.js",
        """
      import {
        value as alias
      } from './child.mjs?v=2';
      export * from './reexport.js';
      const lazy = import('./lazy.js', {with: {type: 'javascript'}});
    """,
    )
    put(tmp_path, "assets/child.mjs", 'import "./app.js";')
    put(tmp_path, "assets/reexport.js", 'export {value} from "./child.mjs";')
    put(tmp_path, "assets/lazy.js", 'import "./child.mjs";')
    put(tmp_path, "inline.js")
    put(
        tmp_path,
        "assets/main.css",
        """
      @import "./colors.css";
      @import url('./more.css');
      .a { background: url('./a%20b.png?v=2'); }
    """,
    )
    put(tmp_path, "assets/colors.css", "@font-face {src:url('./font.woff2')}")
    put(tmp_path, "assets/more.css", '@import "main.css";')
    for name in ["a b.png", "b.png", "poster.jpg", "demo.mp4", "font.woff2"]:
        put(tmp_path, "assets/" + name)
    visited, errors = check(tmp_path)
    assert not errors, errors
    assert len(visited) == 14
    (tmp_path / "assets/child.mjs").unlink()
    assert any("missing file ui/assets/child.mjs" in error for error in check(tmp_path)[1])


def test_skip_remote_data_fragment_comments_and_import_lookalikes(tmp_path):
    put(
        tmp_path,
        "emotes.html",
        """
      <!-- <script src="missing.js"></script> -->
      <link rel="preconnect" href="https://fonts.example">
      <link rel="stylesheet" href="//cdn.example/style.css">
      <img src="data:image/png;base64,abcd">
      <script type="application/json">{"text": "import './missing.js'"}</script>
      <script src="assets/app.js"></script>
      <style>/* url(missing.png) */ .x {background:url(data:image/png;base64,abcd)}</style>
    """,
    )
    put(
        tmp_path,
        "assets/app.js",
        """
      // import './missing.js';
      /* export * from './missing.js'; */
      const text = "import './missing.js';";
      const template = `import './missing.js';`;
      const regex = /import("missing.js")/;
      const properties = object.import('./not-a-module.js');
      import 'https://cdn.example/module.js';
      import '//cdn.example/other.js';
      import 'data:text/javascript,export default 1';
    """,
    )
    visited, errors = check(tmp_path)
    assert not errors, errors
    assert visited == {"emotes.html", "assets/app.js"}


def test_srcset_without_spaces_and_with_data_url(tmp_path):
    put(
        tmp_path,
        "emotes.html",
        """
      <img srcset="a.png,b.png">
      <source srcset="data:image/png;base64,abcd 1x, b.png 2x">
    """,
    )
    put(tmp_path, "a.png")
    put(tmp_path, "b.png")
    visited, errors = check(tmp_path)
    assert not errors, errors
    assert visited == {"emotes.html", "a.png", "b.png"}


@pytest.mark.parametrize(
    "url", ["../secret.js", "/outside.js", "/ui/%2e%2e/secret.js", "/ui/../../secret.js"]
)
def test_refuses_ui_root_escape(tmp_path, url):
    put(tmp_path, "emotes.html", f'<script src="{url}"></script>')
    _, errors = check(tmp_path)
    assert len(errors) == 1
    assert "outside /ui/" in errors[0] or "escapes UI root" in errors[0]


def test_rejects_symlink_escape_without_reading_target(tmp_path):
    ui = tmp_path / "ui"
    outside = put(tmp_path, "outside.js", "SECRET_SENTINEL")
    put(ui, "emotes.html", '<script src="leak.js"></script>')
    try:
        (ui / "leak.js").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    _, errors = check(ui)
    assert len(errors) == 1 and "escapes UI root" in errors[0]
    assert "SECRET_SENTINEL" not in str(errors)


@pytest.mark.parametrize(
    "source", ["import(name)", "import('./a' + name)", "import 'package'", r"import './\u0061.js'"]
)
def test_cannot_silently_pass_unresolved_modules(tmp_path, source):
    put(tmp_path, "emotes.html", f'<script type="module">{source};</script>')
    assert check(tmp_path)[1]


def test_missing_nested_css_resource_reports_referrer(tmp_path):
    put(tmp_path, "emotes.html", '<link rel="stylesheet" href="main.css">')
    put(tmp_path, "main.css", '@import "nested.css";')
    put(tmp_path, "nested.css", '.x {background:url("missing.png")}')
    _, errors = check(tmp_path)
    assert len(errors) == 1
    assert "ui/nested.css:1" in errors[0] and "missing file ui/missing.png" in errors[0]


def test_command_exit_codes_and_explicit_ui_root(tmp_path, capsys):
    assert CHECK["main"](["--ui-root", str(tmp_path)]) == 1
    assert "missing file ui/emotes.html" in capsys.readouterr().err
    put(tmp_path, "emotes.html", '<img src="#inline">')
    assert CHECK["main"](["--ui-root", str(tmp_path)]) == 0
    assert "1 local files checked (no network)" in capsys.readouterr().out
