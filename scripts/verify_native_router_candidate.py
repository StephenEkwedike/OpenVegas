"""Freeze/inspect private native-router candidates without executing or publishing them.

Use the existing build_cli_candidate.py inside the frozen cli directory. This
sidecar deliberately does not infer native UX acceptance from packaging success.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import marshal
import platform
import re
import sys
import tarfile
import tomllib
import types
from pathlib import Path, PurePosixPath

ROUTER_MODULES = tuple(
    "openvegas.integrations." + name
    for name in (
        "inference_router", "inference_router_adapter", "inference_router_controls",
        "inference_router_session", "inference_router_state",
        "inference_router_managed", "inference_router_wallet",
        "inference_router_request_boundary", "inference_router_claude",
        "inference_router_shutdown_receipt", "inference_router_fixture",
    )
)
TRANSPORT_MODULES = (
    "httpx", "uvicorn", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl", "websockets.asyncio.client",
)
PLATFORMS = ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-x64")
FROZEN_ENTRY = "scripts/frozen_entry.py"
NATIVE_GATES = (
    "managed_launch_local_controls", "native_model_effort_picker",
    "permission_and_tool_continuity", "cancellation_and_once_only_input",
    "background_retry_boundary", "native_terminal_balance_display",
)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def checked_path(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlinked or traversing input/output path")
    return path


def relative_name(name: str) -> str:
    part = PurePosixPath(name)
    if (
        not name or part.is_absolute() or part.as_posix() != name or "\\" in name
        or any(p in {".", ".."} or p.startswith(".") for p in part.parts)
    ):
        raise ValueError("Unsafe frozen input name")
    return name


def read(root: Path, name: str) -> bytes:
    return checked_path(root / relative_name(name)).read_bytes()


def load_pinned(path: Path, expected: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("Expected an explicit SHA256")
    raw = checked_path(path).read_bytes()
    if digest(raw) != expected:
        raise ValueError("Pinned receipt changed")
    return json.loads(raw)


def inventory(root: Path, hashes: dict[str, str]) -> dict[str, bytes]:
    if not hashes:
        raise ValueError("Empty source inventory")
    result = {name: read(root, name) for name in hashes}
    for name, raw in result.items():
        if digest(raw) != hashes[name]:
            raise ValueError("Frozen source hash mismatch: " + name)
    return result


def overlay_name(name: str) -> str:
    relative_name(name)
    if name == FROZEN_ENTRY:
        return name
    if not name.startswith("openvegas/integrations/") or not name.endswith((".py", ".js")):
        raise ValueError("Only explicit native integration overlays are allowed")
    return name


def module_path(module: str, blobs: dict[str, bytes]) -> str | None:
    path = module.replace(".", "/")
    return next((p for p in (path + ".py", path + "/__init__.py") if p in blobs), None)


def validate_imports(name: str, raw: bytes, blobs: dict[str, bytes], source: Path | None = None) -> None:
    if not name.endswith(".py"):
        return
    package = name.removesuffix(".py").split("/")[:-1]
    for node in ast.walk(ast.parse(raw, filename=name)):
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = package[:len(package) - node.level + 1] if node.level else []
            module = ".".join([*prefix, *([node.module] if node.module else [])])
            modules = [module]
            # from package import submodule can also import an attribute. Only
            # require siblings when their module files are explicitly selected.
            modules += [module + "." + a.name for a in node.names
                        if module_path(module + "." + a.name, blobs)
                        or source is not None and any((source / p).is_file() for p in (
                            (module + "." + a.name).replace(".", "/") + ".py",
                            (module + "." + a.name).replace(".", "/") + "/__init__.py"))]
        else:
            continue
        for module in modules:
            if module.startswith("openvegas.") and module_path(module, blobs) is None:
                raise ValueError("Missing frozen import: " + module)


def freeze(
    source: Path, baseline: Path, baseline_sha256: str, output: Path,
    overlays: list[str], *, build_inputs_sha256: str | None = None,
) -> dict:
    source, baseline, output = map(checked_path, (source, baseline, output))
    if output.exists():
        raise ValueError("Never overwrite an existing candidate")
    receipt = load_pinned(baseline / "freeze.json", baseline_sha256)
    if receipt.get("frozen") is not True:
        raise ValueError("Baseline is not frozen")
    blobs = inventory(baseline / "cli", receipt["inputs"]["cli"])
    if build_inputs_sha256:
        build = load_pinned(baseline / "build-inputs.json", build_inputs_sha256)
        if build.get("freeze_sha256") != baseline_sha256:
            raise ValueError("Build metadata belongs to a different freeze")
        metadata = build.get("build_metadata", {})
        if any(".dist-info/" not in name or name.startswith("openvegas/") for name in metadata):
            raise ValueError("Unexpected build metadata inventory")
        if blobs.keys() & metadata.keys():
            raise ValueError("Build metadata may not replace source")
        blobs.update(inventory(baseline / "cli", metadata))
    selected = {overlay_name(name) for name in overlays}
    selected.update(module.replace(".", "/") + ".py" for module in ROUTER_MODULES)
    selected.add(FROZEN_ENTRY)
    changes = {name: read(source, name) for name in sorted(selected)}
    blobs.update(changes)
    for name, raw in changes.items():
        validate_imports(name, raw, blobs, source)
    project = tomllib.loads(blobs["pyproject.toml"].decode())
    version = project["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+-rc\.\d+", version):
        raise ValueError("Use a retained private prerelease, never the published baseline")
    if version != receipt.get("version"):
        raise ValueError("Baseline package version does not match its receipt")
    result = {
        "format": "openvegas-native-router-candidate-v1", "version": version,
        "frozen": True, "published": False, "publication_approved": False,
        "baseline_freeze_sha256": baseline_sha256,
        "baseline_build_inputs_sha256": build_inputs_sha256,
        "inputs": {"cli": {n: digest(b) for n, b in sorted(blobs.items())}},
        "checkout_native_inputs": {n: digest(b) for n, b in changes.items()},
        "freezer_sha256": digest(Path(__file__).read_bytes()),
        "native_acceptance": {gate: "not_run" for gate in NATIVE_GATES},
    }
    output.mkdir(parents=True, mode=0o700)
    for name, raw in sorted(blobs.items()):
        target = output / "cli" / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as handle:
            handle.write(raw)
    # A drifted snapshot is retained for diagnosis, never marked frozen.
    if any(read(source, n) != raw for n, raw in changes.items()):
        raise ValueError("Source changed during freeze; no receipt created")
    write_new(output / "freeze.json", result)
    return result


def write_new(path: Path, value: dict) -> None:
    path = checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def source_digest(blobs: dict[str, bytes]) -> str:
    names = {"pyproject.toml", "requirements.lock", "scripts/frozen_entry.py",
             "scripts/build_cli_candidate.py"}
    names.update(n for n in blobs if n.startswith("openvegas/")
                 and PurePosixPath(n).suffix in {".py", ".json", ".png", ".js", ".md"})
    h = hashlib.sha256()
    for name in sorted(names):
        h.update(name.encode() + b"\0")
        h.update(hashlib.sha256(blobs[name]).digest())
    return h.hexdigest()


def verify_cache(path: Path, root: Path, blobs: dict[str, bytes]) -> str:
    path = checked_path(path)
    if path.suffix != ".pyc" or path.parent.name != "__pycache__":
        raise ValueError("Unexpected frozen source file")
    source = Path(importlib.util.source_from_cache(str(path)))
    name = source.relative_to(root).as_posix()
    if name not in blobs:
        raise ValueError("Bytecode source is not bound by the freeze")
    raw = path.read_bytes()
    if raw[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError("Use the same Python version to inspect candidate bytecode")
    expected = compile(blobs[name], str(source), "exec", dont_inherit=True, optimize=0)
    cached = marshal.loads(raw[16:])
    if (not isinstance(cached, types.CodeType) or cached.co_filename != str(source)
            or canonical_code(cached) != canonical_code(expected)):
        raise ValueError("Generated bytecode differs from frozen source")
    return digest(raw)


def canonical_code(code: types.CodeType) -> tuple:
    """Compare code without path-dependent filenames or marshal reference flags."""
    def constant(value):
        if isinstance(value, types.CodeType):
            return ("code", canonical_code(value))
        if isinstance(value, tuple):
            return ("tuple", tuple(constant(v) for v in value))
        if isinstance(value, frozenset):
            return ("frozenset", tuple(sorted((constant(v) for v in value), key=repr)))
        return (type(value).__name__, repr(value))
    return (
        code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
        code.co_nlocals, code.co_stacksize, code.co_flags, code.co_code,
        tuple(constant(v) for v in code.co_consts), code.co_names, code.co_varnames,
        code.co_freevars, code.co_cellvars, code.co_name, code.co_qualname,
        code.co_firstlineno, code.co_linetable, code.co_exceptiontable,
    )


def archive_modules(binary: Path) -> tuple[object, set[str]]:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(checked_path(binary)))
    embedded = [name for name, row in archive.toc.items() if row[-1] == "z"]
    if len(embedded) != 1:
        raise ValueError("Expected one PyInstaller module archive")
    pyz = archive.open_embedded_archive(embedded[0])
    return pyz, set(pyz.toc)


def verify_binary(binary: Path, blobs: dict[str, bytes], selected: dict[str, str]) -> dict:
    pyz, modules = archive_modules(binary)
    missing = sorted(set(ROUTER_MODULES + TRANSPORT_MODULES) - modules)
    if missing:
        raise ValueError("Missing packaged router/transport modules: " + ", ".join(missing))
    bound = {}
    for name in sorted(selected):
        if not name.endswith(".py") or name == FROZEN_ENTRY:
            continue
        module = name.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        if module not in modules:
            raise ValueError("Overlay absent from executable: " + module)
        code = pyz.extract(module)
        expected = compile(blobs[name], name, "exec", dont_inherit=True, optimize=0)
        if not isinstance(code, types.CodeType) or canonical_code(code) != canonical_code(expected):
            raise ValueError("Packaged code differs from frozen source: " + module)
        bound[module] = {
            "source_sha256": digest(blobs[name]),
            "code_sha256": digest(marshal.dumps(canonical_code(code))),
        }
    return {"module_count": len(modules), "exact_overlay_code": bound,
            "transport_modules": list(TRANSPORT_MODULES)}


def verify_entry(binary: Path, blobs: dict[str, bytes]) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(checked_path(binary)))
    if "frozen_entry" not in archive.toc or archive.toc["frozen_entry"][-1] != "s":
        raise ValueError("Missing compiled frozen entry point")
    code = marshal.loads(archive.extract("frozen_entry"))
    expected = compile(blobs[FROZEN_ENTRY], FROZEN_ENTRY, "exec", dont_inherit=True, optimize=0)
    if not isinstance(code, types.CodeType) or canonical_code(code) != canonical_code(expected):
        raise ValueError("Packaged entry point differs from frozen source")
    return {"source_sha256": digest(blobs[FROZEN_ENTRY]),
            "code_sha256": digest(marshal.dumps(canonical_code(code)))}


def verify_assets(binary: Path, blobs: dict[str, bytes], selected: dict[str, str]) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(checked_path(binary)))
    result = {}
    for name in sorted(selected):
        if name.endswith(".py"):
            continue
        if name not in archive.toc or archive.extract(name) != blobs[name]:
            raise ValueError("Packaged native asset differs from frozen source: " + name)
        result[name] = digest(blobs[name])
    return result


def expected_launcher(blobs: dict[str, bytes], name: str, os_name: str, arch: str, sha256: str) -> bytes:
    """Use only the reviewed local builder, never execute receipt-supplied code."""
    builder = checked_path(Path(__file__).with_name("build_cli_candidate.py"))
    if builder.read_bytes() != blobs["scripts/build_cli_candidate.py"]:
        raise ValueError("Frozen builder differs from reviewed local tooling")
    spec = importlib.util.spec_from_file_location("router_candidate_builder", builder)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    launcher = module.npm_launcher(name, os_name, arch, sha256)
    # The frozen builder uses write_text with native newline translation.
    # Reconstruct target bytes; never normalize the archive under inspection.
    if os_name == "win32":
        launcher = launcher.replace("\n", "\r\n")
    return launcher.encode()


def verify_npm(archive: Path, binary_name: str, binary_sha256: str, *, launcher: bytes) -> dict:
    members, modes, seen = {}, {}, set()
    expected_members = {"package/package.json", "package/bin/openvegas.js", "package/bin/" + binary_name}
    with tarfile.open(checked_path(archive), "r:gz") as tar:
        for entry in tar:
            name = relative_name(entry.name.rstrip("/"))
            if name in seen:
                raise ValueError("Nonregular or duplicate npm archive member")
            seen.add(name)
            if entry.isdir():
                if name not in {"package", "package/bin"}:
                    raise ValueError("Unexpected npm payload directory")
                continue
            if not entry.isfile():
                raise ValueError("Nonregular or duplicate npm archive member")
            if entry.name not in expected_members:
                raise ValueError("Unexpected npm payload inventory")
            if entry.size > 512 * 1024 * 1024:
                raise ValueError("Oversized npm member")
            members[entry.name] = tar.extractfile(entry).read()
            modes[entry.name] = entry.mode
    if set(members) != expected_members:
        raise ValueError("Unexpected npm payload inventory")
    package = json.loads(members["package/package.json"])
    if package.get("private") is not True or package.get("name") != "openvegas-native-candidate":
        raise ValueError("Require the existing private candidate npm package")
    if (set(package) != {"name", "version", "private", "type", "bin", "os", "cpu", "files"}
            or package.get("type") != "module" or package.get("files") != ["bin"]
            or package.get("bin") != {"openvegas": "bin/openvegas.js", "ov": "bin/openvegas.js"}):
        raise ValueError("Unexpected npm installation contract")
    if digest(members["package/bin/" + binary_name]) != binary_sha256:
        raise ValueError("npm archive contains a different executable")
    if members["package/bin/openvegas.js"] != launcher:
        raise ValueError("npm launcher differs from reviewed builder")
    for name, mode in modes.items():
        expected_modes = {0o644} if name == "package/package.json" else {0o755}
        if package.get("os") == ["win32"] and name in {
            "package/bin/" + binary_name, "package/bin/openvegas.js",
        }:
            expected_modes = {0o644, 0o755}
        if mode not in expected_modes:
            raise ValueError("Unexpected npm payload permissions")
    return {"sha256": digest(archive.read_bytes()), "package": package,
            "launcher_sha256": digest(launcher), "installation_contract_verified": True}


def verify(frozen: Path, freeze_sha256: str, candidate: Path) -> dict:
    frozen, candidate = map(checked_path, (frozen, candidate))
    receipt = load_pinned(frozen / "freeze.json", freeze_sha256)
    if receipt.get("format") != "openvegas-native-router-candidate-v1" or receipt.get("frozen") is not True:
        raise ValueError("Require a router candidate freeze")
    blobs = inventory(frozen / "cli", receipt["inputs"]["cli"])
    selected = receipt["checkout_native_inputs"]
    required = {module.replace(".", "/") + ".py" for module in ROUTER_MODULES}
    required.add(FROZEN_ENTRY)
    if not required <= selected.keys():
        raise ValueError("Missing mandatory router source binding")
    for name, sha256 in selected.items():
        overlay_name(name)
        if name not in blobs or digest(blobs[name]) != sha256:
            raise ValueError("Overlay receipt differs from frozen source")
    observed = {p.relative_to(frozen / "cli").as_posix() for p in (frozen / "cli").rglob("*") if p.is_file()}
    caches = {name: verify_cache(frozen / "cli" / name, frozen / "cli", blobs)
              for name in sorted(observed - set(blobs))}
    raw = checked_path(candidate / "candidate.json").read_bytes()
    built = json.loads(raw)
    if built.get("published") is not False or built.get("publication_approved") is not False:
        raise ValueError("Not an unpublished private candidate")
    if built.get("version") != receipt["version"] or built.get("source_sha256") != source_digest(blobs):
        raise ValueError("Builder receipt does not match frozen inputs")
    target = built.get("os", "") + "-" + built.get("arch", "")
    if target not in PLATFORMS:
        raise ValueError("Unknown candidate platform")
    name = relative_name(built["binary"])
    if "/" in name:
        raise ValueError("Expected a binary basename")
    binary = checked_path(candidate / "bin" / name)
    binary_sha = digest(binary.read_bytes())
    if binary_sha != built.get("sha256"):
        raise ValueError("Candidate binary hash mismatch")
    packaged = verify_binary(binary, blobs, receipt["checkout_native_inputs"])
    packaged["exact_entry_code"] = verify_entry(binary, blobs)
    packaged["exact_overlay_assets"] = verify_assets(binary, blobs, receipt["checkout_native_inputs"])
    archives = sorted((candidate / "npm").glob("*.tgz"))
    if len(archives) != 1:
        raise ValueError("Expected one private npm archive")
    launcher = expected_launcher(blobs, name, built["os"], built["arch"], binary_sha)
    npm = verify_npm(archives[0], name, binary_sha, launcher=launcher)
    if npm["package"].get("version") != receipt["version"]:
        raise ValueError("npm version differs from frozen version")
    if npm["package"].get("os") != [built["os"]] or npm["package"].get("cpu") != [built["arch"]]:
        raise ValueError("npm platform differs from builder receipt")
    return {
        "packaging_verified": True, "published": False, "full_native_acceptance": False,
        "freeze_sha256": freeze_sha256, "binary_sha256": binary_sha,
        "source_sha256": built["source_sha256"], "candidate_receipt_sha256": digest(raw),
        "verifier_sha256": digest(Path(__file__).read_bytes()),
        "runtime": {"python": sys.version, "platform": platform.platform()},
        "verified_generated_caches": caches,
        "packaged": packaged, "npm": npm,
        "clean_install": "builder_reported_pass" if built.get("npm_install_verified") is True else "not_run",
        "platforms": {p: "packaging_verified" if p == target else "not_run" for p in PLATFORMS},
        "native_acceptance": {gate: "not_run" for gate in NATIVE_GATES},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("freeze")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--baseline", type=Path, required=True)
    snapshot.add_argument("--baseline-sha256", required=True)
    snapshot.add_argument("--build-inputs-sha256")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--overlay", action="append", default=[])
    inspect = commands.add_parser("verify")
    inspect.add_argument("--frozen", type=Path, required=True)
    inspect.add_argument("--freeze-sha256", required=True)
    inspect.add_argument("--candidate", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze(args.source, args.baseline, args.baseline_sha256, args.output,
                            args.overlay, build_inputs_sha256=args.build_inputs_sha256)
            print(json.dumps({"frozen": str(args.output), "version": result["version"],
                              "overlay_count": len(result["checkout_native_inputs"])}))
        else:
            result = verify(args.frozen, args.freeze_sha256, args.candidate)
            write_new(args.output, result)
            print(json.dumps({"packaging_verified": True, "full_native_acceptance": False,
                              "evidence": str(args.output)}))
    except (KeyError, OSError, ValueError, SyntaxError) as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__, "reason": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
