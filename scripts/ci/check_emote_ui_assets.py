"""Check the Emotes page's static asset closure in a checkout or extracted image.

Standard library only; never imports the app, executes JS, reads configuration or
uses the network. Checks HTML resources, CSS url/@import and literal ES imports
(including re-exports and dynamic import()). Runtime API-provided preview URLs
and computed JS asset URLs still require the staging smoke test. Run against a
clean checkout/image: files present only in a dirty worktree can mask omissions.
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit


class Reference(NamedTuple):
    url: str
    line: int
    module: bool = False


# Keep quoted strings and comments opaque rather than finding imports inside them.
JS_TOKENS = re.compile(
    r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
    r"""|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    r"|(?P<template>`(?:\\.|[^`\\])*`)"
    r"|(?P<word>[A-Za-z_$][\w$]*)|(?P<punct>[^\s])"
)
JS_REGEX_LITERAL = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+/[a-z]*")
CSS_REFS = re.compile(
    r"/\*[\s\S]*?\*/"
    r"""|@import\s+(?P<import>"[^"\n]*"|'[^'\n]*')"""
    r"""|url\(\s*(?P<url>"[^"\n]*"|'[^'\n]*'|[^\s)'";]+)\s*\)"""
    r"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""",
    re.IGNORECASE,
)


def css_references(source: str) -> list[Reference]:
    refs = []
    for match in CSS_REFS.finditer(source):
        value = match.group("import") or match.group("url")
        if value:
            refs.append(Reference(value.strip("\"'"), source.count("\n", 0, match.start()) + 1))
    return refs


def js_references(source: str) -> list[Reference]:
    tokens = []
    skip_until = 0
    for match in JS_TOKENS.finditer(source):
        if match.start() < skip_until or match.lastgroup == "comment":
            continue
        if match.group() == "/" and (
            not tokens
            or tokens[-1].group()
            in {"=", "(", "[", "{", ",", ":", ";", "!", "?", "return", "case", "throw"}
        ):
            literal = JS_REGEX_LITERAL.match(source, match.start())
            if literal:
                skip_until = literal.end()
                tokens.append(literal)
                continue
        tokens.append(match)
    refs = []
    for index, token in enumerate(tokens):
        word = token.group()
        if token.lastgroup != "word" or word not in {"import", "export"}:
            continue
        if index and tokens[index - 1].group() == ".":
            continue  # import.meta and object.import() are not module imports.
        rest = tokens[index + 1 :]
        target = None
        if word == "import" and rest and rest[0].lastgroup == "string":
            target = rest[0]
        elif word == "import" and len(rest) > 1 and rest[0].group() == "(":
            if rest[1].lastgroup == "string" and len(rest) > 2 and rest[2].group() in {",", ")"}:
                target = rest[1]
            else:
                raise ValueError("computed import() cannot be validated; use a literal module URL")
        elif rest and (word == "import" or rest[0].group() in {"*", "{"}):
            for offset, part in enumerate(rest):
                if part.group() == ";":
                    break
                if part.group() == "from" and offset + 1 < len(rest):
                    if rest[offset + 1].lastgroup == "string":
                        target = rest[offset + 1]
                    break
        if target:
            value = target.group()[1:-1]
            if "\\" in value:
                raise ValueError("escaped module URL cannot be validated; use a literal path")
            refs.append(Reference(value, source.count("\n", 0, token.start()) + 1, True))
    return refs


class AssetHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[Reference] = []
        self.inline: tuple[str, int, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        line = self.getpos()[0]
        if tag == "base" and values.get("href"):
            raise ValueError("<base href> is not supported by the static asset check")
        if tag in {
            "script",
            "img",
            "source",
            "video",
            "audio",
            "track",
            "embed",
            "input",
        } and values.get("src"):
            self.references.append(Reference(values["src"], line))
        if tag == "link" and values.get("href"):
            rel = set((values.get("rel") or "").lower().split())
            if rel & {
                "stylesheet",
                "icon",
                "apple-touch-icon",
                "preload",
                "modulepreload",
                "manifest",
            }:
                self.references.append(Reference(values["href"], line))
        if tag == "video" and values.get("poster"):
            self.references.append(Reference(values["poster"], line))
        if tag in {"img", "source"} and values.get("srcset"):
            # Each candidate starts with its URL; data URLs may contain commas.
            for candidate in re.finditer(r"(data:[^\s]+|[^,\s]+)(?:\s+[^,]*)?", values["srcset"]):
                self.references.append(Reference(candidate[1].rstrip(","), line))
        if values.get("style"):
            self.references.extend(css_references(values["style"]))
        if tag == "style" or (
            tag == "script"
            and not values.get("src")
            and (values.get("type") or "").lower()
            in {"", "module", "text/javascript", "application/javascript"}
        ):
            self.inline = (tag, line, [])

    def handle_data(self, data: str) -> None:
        if self.inline:
            self.inline[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.inline and self.inline[0] == tag:
            kind, line, parts = self.inline
            parser = css_references if kind == "style" else js_references
            self.references.extend(
                Reference(ref.url, ref.line + line - 1, ref.module)
                for ref in parser("".join(parts))
            )
            self.inline = None


def resolve_reference(root: Path, source: Path, ref: Reference) -> Path | None:
    value = ref.url.strip()
    url = urlsplit(value)
    if not value or value.startswith("#") or url.scheme or url.netloc:
        return None  # External resources are never fetched.
    path = unquote(url.path)
    if not path:
        return None
    if "\\" in path or "\x00" in path:
        raise ValueError("invalid local asset path")
    if ref.module and not path.startswith(("/", "./", "../")):
        raise ValueError("bare module specifier requires an import map; not a local asset")
    if path.startswith("/"):
        if not path.startswith("/ui/"):
            raise ValueError("asset URL is outside /ui/")
        candidate = root / path.removeprefix("/ui/")
    else:
        candidate = source.parent / path
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("asset path escapes UI root")
    return resolved


def check_assets(ui_root: Path) -> tuple[set[Path], list[str]]:
    """Return visited files and actionable failures; roots are mounted at /ui/."""
    root = ui_root.resolve()
    visited: set[Path] = set()
    errors: list[str] = []
    pending = [(root / "emotes.html", "entry ui/emotes.html")]
    while pending:
        candidate, origin = pending.pop()
        path = candidate.resolve()
        if not path.is_relative_to(root):
            errors.append(f"{origin}: asset path escapes UI root")
            continue
        if not path.is_file():
            errors.append(f"{origin}: missing file ui/{path.relative_to(root).as_posix()}")
            continue
        if path in visited:
            continue
        visited.add(path)
        suffix = path.suffix.lower()
        if suffix not in {".html", ".js", ".mjs", ".css"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            if suffix == ".html":
                parser = AssetHTMLParser()
                parser.feed(source)
                parser.close()
                refs = parser.references
            else:
                refs = css_references(source) if suffix == ".css" else js_references(source)
        except (OSError, ValueError) as exc:
            errors.append(f"ui/{path.relative_to(root).as_posix()}: {exc}")
            continue
        for ref in refs:
            origin = f"ui/{path.relative_to(root).as_posix()}:{ref.line} ({ref.url!r})"
            try:
                target = resolve_reference(root, path, ref)
                if target is not None:
                    pending.append((target, origin))
            except (OSError, ValueError) as exc:
                errors.append(f"{origin}: {exc}")
    return visited, sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-root", type=Path, default=Path(__file__).resolve().parents[2] / "ui")
    args = parser.parse_args(argv)
    visited, errors = check_assets(args.ui_root)
    if errors:
        print("FAIL Emotes UI static assets:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print(f"PASS Emotes UI static assets: {len(visited)} local files checked (no network).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
