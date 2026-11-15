"""Existing two managed lanes, normal installed CLI, no replacement PE or injected counters."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
from secrets import token_hex
import socket
import subprocess
import sys
import time



def need(ok):
    if not ok: raise ValueError("Stock managed QA checkpoint")


def inputs():
    node, app, pe, codex = [Path(os.environ[k]).resolve(strict=True) for k in
                           ("OV_QA_NODE","OV_QA_APP","OV_QA_PE","OV_QA_CODEX_BUNDLE_EXE")]
    need(node.name.lower()=="node.exe" and app.parts[-4:]==("node_modules","openvegas-ai","bin","openvegas.js"))
    need(pe==app.parents[2]/"@lordesteph/openvegas-win32-x64/bin/openvegas-win32-x64.exe")
    need(codex.name=="codex.exe" and codex.parent.name=="bin" and codex.parent.parent.name=="publisher-codex")
    return node,app,pe,codex


def isolation(paths):
    from scripts import verify_windows_conpty as pty
    pty.require_hosted_lane()
    # Readback of the existing workflow's executable-specific deny-all rules.
    # Do not install/alter a firewall rule from the test.
    ps = r'''
$ErrorActionPreference='Stop'
$inputObject=ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
$programs=@($inputObject.programs)
$profiles=@(Get-NetFirewallProfile -ErrorAction Stop)
if (!$programs.Count -or !$profiles.Count -or @($profiles | Where-Object {$_.Enabled -ne 'True'}).Count) {exit 2}
$n=0
foreach($exe in $programs) {
 $n++
 $rules=@(Get-NetFirewallRule -PolicyStore ActiveStore -Name "OpenVegas-QA-helper-$n" -ErrorAction Stop)
 if ($rules.Count -ne 1) {exit 3}
 $r=$rules[0]
 $p=@($r | Get-NetFirewallApplicationFilter -ErrorAction Stop)
 $a=@($r | Get-NetFirewallAddressFilter -ErrorAction Stop)
 if ($r.Enabled -ne 'True' -or $r.Direction -ne 'Outbound' -or $r.Action -ne 'Block' -or
     $r.Profile -ne 'Any' -or $p.Count -ne 1 -or $p[0].Program -ine $exe -or
     $a.Count -ne 1 -or (@($a[0].RemoteAddress) -join ',') -ne 'Any') {exit 4}
}
exit 0
'''
    system = Path(os.environ["SystemRoot"])
    result = subprocess.run([str(system/"System32/WindowsPowerShell/v1.0/powershell.exe"),
                             "-NoProfile","-NonInteractive","-Command",ps],
                            input=json.dumps({"programs": sorted(set(map(str,paths)))}),text=True,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=40)
    need(result.returncode==0)


def launch_child():
    node, app, port = sys.argv[2:]
    root = Path(os.environ["USERPROFILE"]).resolve(strict=True)
    need(sys.platform=="win32" and sys.flags.isolated and all(os.isatty(i) for i in range(3)))
    need(re.fullmatch("ov-startup-[0-9a-f]{32}",root.name) and Path.cwd()==root/"work"
         and not any(Path.cwd().iterdir()))
    need(Path(node).is_absolute() and Path(node).name.lower()=="node.exe"
         and Path(app).parts[-4:]==("node_modules","openvegas-ai","bin","openvegas.js"))
    need(port.isascii() and port.isdecimal() and 0<int(port)<65536)
    allowed={"OPENVEGAS_WINDOWS_MANAGED_REHEARSAL":"1","OPENVEGAS_LOAD_PROJECT_ENV":"0",
             "OPENVEGAS_BACKEND_URL":"http://127.0.0.1:"+port}
    need({k:v for k,v in os.environ.items() if k.startswith(("OPENVEGAS_","OV_"))}==allowed)
    need(not any(k.upper().startswith(("GITHUB_","ACTIONS_")) or k.upper().endswith("_PROXY")
                 for k in os.environ))
    need(not any(k.upper() in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","CODEX_AUTH_TOKEN",
                               "CLAUDE_CODE_OAUTH_TOKEN") for k in os.environ))
    print("OV-QA-READY",flush=True); need(input()=="OV-QA-START")
    args=[node,app,"--local","integration","terminal","--attach","--host","codex","--windows-fixture-port",port]
    return subprocess.run(args,shell=False,check=False,timeout=360).returncode


def drive(lane, app, output, node, launcher, codex):
    from scripts.qa_stock_managed_fixture import verdict
    from scripts.qa_stock_managed_bank import bundled_catalog
    from scripts import verify_windows_conpty as pty
    from scripts.windows_codex_editor import enter
    from scripts.windows_managed_startup import environment
    from openvegas.emotes._windows_state import private_windows_directory
    root=Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)/("ov-startup-"+token_hex(16))
    need(not root.exists())
    f=app.fixture
    def stage(s): output["stage"]=s
    with ExitStack() as stack:
        for rel in ("","work","tmp","codex","claude",".openvegas","AppData","AppData/Local","AppData/Roaming"):
            folder=stack.enter_context(private_windows_directory(root/rel)); need(not folder.names(1))
        env=environment("codex",root,codex,None,os.environ,managed=False)
        # No native auth/profile writes here: the installed PE owns its fresh rehearsal root.
        env.update(OPENVEGAS_WINDOWS_MANAGED_REHEARSAL="1",OPENVEGAS_LOAD_PROJECT_ENV="0",
                   OPENVEGAS_BACKEND_URL="http://127.0.0.1:"+str(app.port))
        stage("bundled-catalog")
        app.bank.catalog=bundled_catalog(codex,env=env,cwd=str(root/"work"))
        args=["child",str(node),str(launcher),str(app.port)]
        with pty.isolated_console(Path(__file__).resolve(),args,env,str(root/"work")) as term:
            stage("stock-managed-banner"); term.expect("Windows managed Codex","stock managed owner",timeout=75)
            stage("composer"); term.expect(r"OpenAI\s+Codex|for shortcuts","native UI",timeout=60,diagnose="claude-start")
            for i,mode in enumerate(("host","v","host")):
                if i:
                    stage("switch-"+mode)
                    enter(term,"openvegas use "+mode,stage,"switch-"+mode)
                    result=term.expect(r"Payer:\s*"+mode+r"\.\s*No prompt sent\.|Control check:\s*[a-z-]+\.",
                                       "native control",timeout=30)
                    need("Control check:" not in result); f.switch_acks+=1
                stage("payer-status")
                enter(term,"openvegas status",stage,"status")
                term.expect(r"Payer:\s*"+mode+r";\s*\$V\s*(?:73\.25|73\.24)\.","native payer")
                f.status_checks+=1
                stage("root-input")
                enter(term,f.prompts[i],stage,"turn-"+str(i),before_submit=lambda:f.arm(i))
                if lane=="retry" and i==1:
                    stage("wallet-interrupt"); need(f.interrupted.wait(35))
                    stage("native-retry-outcome")
                    end=time.monotonic()+85
                    while time.monotonic()<end:
                        term.read(); frame=term.frame()
                        need(f.answers[1] not in frame and not (f.repeat_v or f.repeat_host or f.unmatched or f.bad or app.bad))
                        f.native_reconnect |= bool(re.search(r"reconnecting\W+\d+\s*/\s*\d+",frame,re.I))
                        f.native_error |= bool(re.search(
                            r"stream disconnected before completion|exceeded retry limit|"
                            r"unexpected status (?:400|403|409|502)|stream closed before response.completed",frame,re.I))
                        time.sleep(.06)
                    need(f.native_error and f.native_reconnect)
                    stage("retry-fenced-observation"); verdict(app,lane)
                    return  # No third prompt, payer change, synthetic revoke or clean-exit claim.
                stage("root-render"); term.expect(re.escape(f.answers[i]),"rendered synthetic reply",timeout=45)
                f.rendered+=1; need(f.sent[f.answers[i]].wait(5)); time.sleep(.9)
            stage("exit"); enter(term,"/exit",stage,"exit"); term.require_success(); f.clean_exit=True
            verdict(app,lane); stage("roundtrip-confirmed")


def firewall_programs():
    node,launcher,pe,codex=inputs()
    import winpty
    helpers=[*codex.parent.parent.rglob("*.exe"),*Path(winpty.__file__).parent.rglob("*.exe")]
    git=Path(os.environ["ProgramFiles"])/"Git"
    git_tools=[git/"bin/bash.exe",git/"usr/bin/bash.exe",git/"usr/bin/sh.exe",git/"cmd/git.exe"]
    need(all(p.is_file() for p in git_tools))
    return sorted(set(map(str,[Path(sys.executable),node,pe,codex,*helpers,*git_tools])))


async def lane_main(lane):
    from scripts.qa_stock_managed_fixture import Service, verdict, snapshot
    from scripts.windows_switch_rehearsal import LIMIT
    import uvicorn
    node,launcher,pe,codex=inputs()
    isolation(firewall_programs())
    app=Service(lane=="retry"); output={"stage":"setup"}; passed=False; counts={}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); sock.listen(8); app.port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,lifespan="off",access_log=False,log_config=None,
                             log_level="critical",ws_max_size=LIMIT,timeout_graceful_shutdown=3))
        task=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(8):
                while not server.started:
                    if task.done(): await task
                    await asyncio.sleep(.02)
            await asyncio.to_thread(drive,lane,app,output,node,launcher,codex)
            counts=verdict(app,lane); passed=True
        except Exception:
            pass  # No raw prompts, paths, headers, frames or exception text.
        finally:
            server.should_exit=True
            await asyncio.wait_for(task,8)
    if not counts:
        counts=snapshot(app)
    print("::notice::stock-managed "+lane+" stage="+output["stage"]+" pass="+str(int(passed))+
          " "+" ".join(k+"="+str(v) for k,v in counts.items()),flush=True)
    return 0 if passed else 1


if __name__=="__main__":
    try:
        need(not sys.flags.optimize)
        if sys.argv[1:2]==["child"]:
            code=launch_child()
        else:
            need(sys.argv[1:] in (["roundtrip"],["retry"]))
            code=asyncio.run(lane_main(sys.argv[1]))
    except Exception:
        print("::notice::stock-managed pass=0 stage=preflight-or-cleanup",flush=True)
        code=1
    sys.exit(code)
