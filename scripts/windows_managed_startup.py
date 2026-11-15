"""Windows-only, network-denied stock launcher test. Never send inference or switch payer."""
import asyncio
import hashlib
import json
import os
import re
from secrets import token_hex
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import ExitStack
from pathlib import Path

MAIN = "openvegas-ai-0.3.19.tgz"
WIN = "lordesteph-openvegas-win32-x64-0.3.19.tgz"
PACKAGE = "@lordesteph/openvegas-win32-x64"
BIN = "openvegas-win32-x64.exe"
FILES = {
    MAIN: ("openvegas-ai", ("bin/openvegas.js", "package.json", "release.json", "README.md")),
    WIN: (PACKAGE, ("bin/" + BIN, "package.json")),
}
HOSTS = ("codex", "claude")
REF = "refs/heads/codex/qa-win-foreground-8005302f"


def require(ok):
    if not ok:
        raise ValueError("Windows QA precondition not met")


def digest(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def unpack(packs, modules, pins):
    modules.mkdir(parents=True, exist_ok=False)
    for filename, (subdir, files) in FILES.items():
        archive = packs / filename
        require(digest(archive) == pins[filename])
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            require(sorted(m.name for m in members) == sorted("package/" + s for s in files))
            for rel in files:
                member = tar.getmember("package/" + rel)
                require(member.isfile() and 0 < member.size <= 192 * 1024 * 1024)
                out = modules / subdir / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, out.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    app = modules / "openvegas-ai"
    index = json.loads((app / "release.json").read_bytes())
    require(index["version"] == "0.3.19" and index["source_sha256"] == pins["source"])
    require(index["platforms"]["win32-x64"] == {"package": PACKAGE, "binary": BIN, "sha256": pins["pe"]})
    require(digest(app / "bin/openvegas.js") == pins["launcher"])
    require(digest(modules / PACKAGE / "bin" / BIN) == pins["pe"])


def child(host, node, js, port=None):
    root = Path(os.environ["USERPROFILE"])
    require(sys.platform == "win32" and sys.flags.isolated and host in HOSTS)
    require(all(os.isatty(i) for i in (0, 1, 2)) and Path.cwd() == root / "work")
    require(re.fullmatch(r"ov-startup-[0-9a-f]{32}", root.name) and not any(Path.cwd().iterdir()))
    require(Path(node).is_absolute() and Path(node).name.lower() == "node.exe")
    require(Path(js).is_absolute() and Path(js).parts[-4:] == ("node_modules", "openvegas-ai", "bin", "openvegas.js"))
    require(not any(k.upper().startswith(("GITHUB_", "ACTIONS_", "OPENVEGAS_", "OV_")) for k in os.environ))
    require(not any(k.upper().endswith("_PROXY") for k in os.environ))
    print("OV-QA-READY", flush=True)
    require(input() == "OV-QA-START")
    mode = ["--managed-billing"]
    if port is not None:
        require(port.isascii() and port.isdigit() and 0 < int(port) < 65536)
        mode = ["--windows-fixture-port", port]
    rc = subprocess.run([node, js, "integration", "terminal", "--attach", "--host", host, *mode],
                          shell=False, timeout=440).returncode
    bucket = {0: "zero", 1: "one", 2: "two", 127: "notfound", 3221225781: "dllmissing"}.get(rc, "other")
    print("OV-QA-FG-EXIT-" + bucket, flush=True)
    time.sleep(1)
    return rc



def environment(host, root, image, bash, parent, *, managed=True):
    require(host in HOSTS)
    system = Path(parent["SystemRoot"])
    work = root / "work"
    env = {k: parent[k] for k in ("PROGRAMFILES", "PROGRAMDATA", "PROGRAMFILES(X86)", "PROGRAMW6432",
                                    "SYSTEMDRIVE", "ALLUSERSPROFILE", "COMMONPROGRAMFILES",
                                    "COMMONPROGRAMFILES(X86)") if k in parent}
    env.update(SystemRoot=str(system), WINDIR=str(system), ComSpec=str(system / "System32/cmd.exe"),
               PATH=os.pathsep.join(map(str, [image.parent, system / "System32"])),
               HOME=str(root), USERPROFILE=str(root), HOMEDRIVE=root.drive, HOMEPATH=str(root)[len(root.drive):],
               LOCALAPPDATA=str(root / "AppData/Local"), APPDATA=str(root / "AppData/Roaming"),
               TEMP=str(root / "tmp"), TMP=str(root / "tmp"), CODEX_HOME=str(root / "codex"),
               CLAUDE_CONFIG_DIR=str(root / "claude"), TERM="xterm-256color", PYTHON_DOTENV_DISABLED="1",
               DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1")
    if not managed:
        # Only the PE may create/configure its inner rehearsal environment.
        return env
    # Generated here, scoped to one fresh profile. No server accepts this value.
    identity = "ov-qa-invalid-" + token_hex(24)
    if host == "codex":
        env["OPENAI_API_KEY"] = identity
        (root / "codex/auth.json").write_text(json.dumps({"OPENAI_API_KEY": identity}), encoding="ascii")
        (root / "codex/config.toml").write_text(
            'model_provider="openai"\nmodel="gpt-6-astra"\nmodel_reasoning_effort="low"\n'
            'cli_auth_credentials_store="file"\nsandbox_mode="read-only"\napproval_policy="on-request"\n'
            'web_search="disabled"\ncheck_for_update_on_startup=false\n'
            '[projects.' + json.dumps(str(work)) + ']\ntrust_level="trusted"\n'
            '[tui]\nscreen_reader_detection_done=true\n', encoding="ascii")
    else:
        env.update(ANTHROPIC_API_KEY=identity, ANTHROPIC_MODEL="claude-sonnet-4-6",
                   CLAUDE_CODE_GIT_BASH_PATH=str(bash))
        env["PATH"] += os.pathsep + str(bash.parent.parent / "cmd")
        (root / "claude/.claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "customApiKeyResponses": {"approved": [identity[-20:]]},
            "projects": {work.as_posix(): {"hasTrustDialogAccepted": True}},
        }), encoding="ascii")
    return env


def observe(host, term, checkpoint):
    from scripts import verify_windows_conpty as pty

    checkpoint("managed-banner")
    banner = term.expect(r"Windows managed (?:Codex|Claude)\.|Windows native ownership, selection or routing "
                         r"could not be verified\.|Managed Windows billing unavailable\.", "managed launch", timeout=65)
    require(banner.casefold() == ("Windows managed " + host + ".").casefold())
    checkpoint("native-ui")
    pattern = r"OpenAI\s+Codex" if host == "codex" else r"Claude\s+Code|Sonnet\s+4\.6"
    term.expect(pattern, "native UI", timeout=90, diagnose="claude-start")
    if host == "claude":
        checkpoint("command-registration")
        term.expect(r"OpenVegas billing controls (?:ready|registered;\s*readiness unconfirmed)\.",
                    "local command registration", timeout=90, diagnose="claude-start")
    checkpoint("local-status")
    require(not pty.screen_failure("claude-start", term.frame()))
    from scripts.windows_codex_editor import enter
    require(host == "codex")
    enter(term, "openvegas status", checkpoint, "status")
    term.expect(r"Payer:\s*host;\s*balance\s+unavailable\.", "unfunded local status", timeout=35, diagnose="claude-control")
    checkpoint("native-exit")
    term.type_line("/exit")
    term.require_success()


def drive(host, checkpoint, fixture=None, port=None):
    from openvegas.emotes._windows_state import private_windows_directory
    from scripts import verify_windows_conpty as pty

    pty.require_hosted_lane()
    require(os.environ["GITHUB_REF"] == REF and os.environ["GITHUB_REPOSITORY"] == "StephenEkwedike/OpenVegas")
    root = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True) / ("ov-startup-" + token_hex(16))
    require(not root.exists())
    exe = Path(os.environ["OV_WINDOWS_" + host.upper() + "_EXE"]).resolve(strict=True)
    if fixture is not None and host == "codex":
        exe = Path(os.environ["OV_QA_CODEX_BUNDLE_EXE"]).resolve(strict=True)
        require(exe.name == "codex.exe" and exe.parent.name == "bin"
                and exe.parent.parent.name == "publisher-codex")
    args = ["child", host, os.environ["OV_QA_NODE"], os.environ["OV_QA_APP"]]
    with ExitStack() as held:
        for rel in ("", "codex", "claude", "work", "tmp", ".openvegas", "AppData", "AppData/Local", "AppData/Roaming"):
            d = held.enter_context(private_windows_directory(root / rel))
            require(not d.names(1))
        env = environment(host, root, exe, Path(os.environ["OV_WINDOWS_BASH_EXE"]), os.environ,
                          managed=fixture is None)
        if fixture is not None:
            require(type(port) is int and 0 < port < 65536)
            args.append(str(port))
        checkpoint("conpty")
        with pty.isolated_console(Path(__file__).resolve(), args, env, str(root / "work")) as term:
            if fixture is None:
                observe(host, term, checkpoint)
            else:
                from scripts.windows_switch_rehearsal import interact, payer

                def control(sid, action, expected):
                    payer(args[2], args[3], env, str(root / "work"), sid, action, expected)

                interact(host, term, fixture, control, checkpoint)


def main(args):
    if len(args) in (4, 5) and args[0] == "child":
        return child(*args[1:])
    if args == ["unpack"]:
        unpack(Path("packs"), Path("installed/node_modules"), json.loads(Path("qa-image-pins.json").read_bytes()))
        return 0
    require(args == ["rehearsal"] and sys.platform == "win32")
    lane = args[0]
    ok = True
    for host in HOSTS:
        stages = ["private-root"]
        stats = {}
        try:
            if lane == "managed":
                drive(host, stages.append)
            else:
                from scripts.windows_switch_rehearsal import run

                asyncio.run(run(host, drive, stages.append, stats))
        except Exception as exc:
            from scripts.verify_windows_conpty import CheckpointMiss

            reason = exc.reason.value if isinstance(exc, CheckpointMiss) else "unconfirmed"
            counts = "".join(f" {k}={stats.get(k, 'unobserved')}" for k in ("supplier_answers", "linked", "rendered", "switch_acks", "status_checks", "rejected")) if lane == "rehearsal" else ""
            print(f"::error::{lane} {host} NOT-PASSED stage={stages[-1]} reason={reason}{counts}", flush=True)
            ok = False
        else:
            result = "offline-status-clean-exit" if lane == "managed" else "three-linked-replies-host-v-host-clean-exit"
            print(f"::notice::{lane} {host} PASS stage={result}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
