"""Native-only Claude controls. Only green submits a synthetic loopback turn."""
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from contextlib import ExitStack
from secrets import token_hex

CASES = ("profile", "green-empty", "green")
PIN = "345996b4bdbae21d817f750fae2f34cbf8d81224a5444c78136091d121beb298"


def need(ok):
    if not ok:
        raise ValueError("Publisher startup lab precondition failed")


def child(image):
    root = Path(os.environ["USERPROFILE"])
    need(sys.flags.isolated and sys.platform == "win32" and Path.cwd() == root / "work"
         and re.fullmatch("ov-lab-[0-9a-f]{32}", root.name) and not any(Path.cwd().iterdir()))
    need(all(os.isatty(fd) for fd in (0,1,2)) and Path(image).is_absolute()
         and Path(image).name == "claude.exe" and not any(k.startswith(("GITHUB_","ACTIONS_")) for k in os.environ))
    with open(image,"rb") as src:
        need(hashlib.file_digest(src,"sha256").hexdigest() == PIN)
    print("OV-QA-READY",flush=True)
    need(input() == "OV-QA-START")
    rc = subprocess.run([image], shell=False, timeout=125).returncode
    print("OV-QA-FG-EXIT-" + {0:"zero",1:"one",2:"two"}.get(rc,"other"), flush=True)
    time.sleep(.5)
    return rc


def run(case, fixture=None, port=None):
    from openvegas.emotes._windows_state import private_windows_directory
    from scripts import verify_windows_conpty as pty

    pty.require_hosted_lane()
    need(case in CASES and os.environ["GITHUB_REPOSITORY"] == "StephenEkwedike/OpenVegas")
    root = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True) / ("ov-lab-" + token_hex(16))
    system, image, bash = (Path(os.environ[k]).resolve(strict=True) for k in ("SystemRoot","OV_WINDOWS_CLAUDE_EXE","OV_WINDOWS_BASH_EXE"))
    need(not root.exists() and image.name == "claude.exe" and bash.name == "bash.exe")
    with ExitStack() as held:
        ephemeral = held.enter_context(socket.socket())
        ephemeral.bind(("127.0.0.1",0))  # Bound but deliberately not listening.
        dirs = ["", "work", "claude", "codex", "roaming", "local", "tmp"]
        folders = case != "profile"
        if folders:
            dirs += ["AppData","AppData/Local","AppData/Roaming"]
        for rel in dirs:
            d = held.enter_context(private_windows_directory(root / rel))
            need(not d.names(1))
        env = dict(SystemRoot=str(system),WINDIR=str(system),ComSpec=str(system/"System32/cmd.exe"),
                   PATH=str(system/"System32"), USERPROFILE=str(root),HOME=str(root),
                   APPDATA=str(root/("AppData/Roaming" if folders else "roaming")),
                   LOCALAPPDATA=str(root/("AppData/Local" if folders else "local")),
                   TEMP=str(root/"tmp"),TMP=str(root/"tmp"),CLAUDE_CONFIG_DIR=str(root/"claude"),
                   CODEX_HOME=str(root/"codex"),DISABLE_TELEMETRY="1",DISABLE_AUTOUPDATER="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1",
                   ANTHROPIC_API_KEY="invalid-local-lab-"+token_hex(12),
                   ANTHROPIC_BASE_URL="http://127.0.0.1:"+str(ephemeral.getsockname()[1]))
        if case == "profile":
            (root/"claude/.claude.json").write_text(json.dumps({"hasCompletedOnboarding":True,"theme":"dark",
                "customApiKeyResponses":{"approved":[env["ANTHROPIC_API_KEY"][-20:]]},
                "projects":{(root/"work").as_posix():{"hasTrustDialogAccepted":True}}}), encoding="ascii")
        if case in ("green","green-empty"):
            from scripts.windows_managed_startup import environment
            url=env["ANTHROPIC_BASE_URL"]
            env = environment("claude",root,image,bash,os.environ,managed=case=="green")
            if case=="green":
                need(fixture is not None and type(port) is int and 0 < port < 65536)
                url="http://127.0.0.1:"+str(port)+"/_host"
            else:
                env.update(ANTHROPIC_API_KEY="ov-qa-invalid-"+token_hex(24),ANTHROPIC_MODEL="claude-sonnet-4-6",
                           CLAUDE_CODE_GIT_BASH_PATH=str(bash))
                env["PATH"]+=os.pathsep+str(bash.parent.parent/"cmd")
            env["ANTHROPIC_BASE_URL"]=url
        with pty.isolated_console(Path(__file__).resolve(), ["child",str(image)], env, str(root/"work")) as term:
            try:
                term.expect(r"Claude\s+Code|Sonnet\s+4\.6|\? for shortcuts|Type your message|shortcuts for help",
                            "historical native title or composer",timeout=90, diagnose="claude-start")
            except pty.CheckpointMiss as exc:
                return exc.reason.value
            if case == "green":
                from scripts.windows_codex_editor import enter
                fixture.arm(0)
                try:
                    enter(term,fixture.prompts[0],lambda _: None,"green",host="claude")
                    term.expect(re.escape(fixture.answers[0]),"rendered loopback answer",timeout=25,diagnose="claude-start")
                except pty.CheckpointMiss as exc:
                    return "after-title-"+exc.reason.value
                need(fixture.rows == ["host"] and not fixture.bad)
                return "native-loopback-answer"
            return "native-title-or-composer"

if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="child":
        sys.exit(child(sys.argv[2]))
    need(len(sys.argv)==1)
    for case in CASES:
        try:
            if case == "green":
                from scripts.windows_switch_rehearsal import run as serve
                closed=[]
                def drive(_,checkpoint,fixture,port):
                    closed.append(run("green",fixture,port))
                asyncio.run(serve("claude", drive, lambda _: None, {}))
                outcome,=closed
            else:
                outcome=run(case)
        except Exception:
            outcome="setup-unconfirmed"
        print("::notice::publisher-lab claude case="+case+" outcome="+outcome, flush=True)
