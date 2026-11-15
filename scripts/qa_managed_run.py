"""Firewalled Windows ConPTY; emit only enumerated checkpoints and aggregate counts."""
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
import threading
import time


def need(ok):
    if not ok:raise ValueError('Managed QA checkpoint')


def launch_child():
    exe, codex, port = sys.argv[2:]
    need(sys.platform=='win32' and sys.flags.isolated and Path(exe).name=='ov-qa-managed.exe'
         and Path(codex).name=='codex.exe' and port.isdecimal() and 0<int(port)<65536
         and all(os.isatty(i) for i in range(3)))
    print('OV-QA-READY',flush=True);need(input()=='OV-QA-START')
    return subprocess.run([exe,codex,port],check=False,timeout=360).returncode


def drive(lane,f,port,output):
    from scripts import verify_windows_conpty as pty
    from scripts.windows_codex_editor import enter
    from scripts.windows_managed_startup import environment
    from openvegas.emotes._windows_state import private_windows_directory
    from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY
    pty.require_hosted_lane()
    root=Path(os.environ['RUNNER_TEMP']).resolve(strict=True)/('ov-startup-'+token_hex(16))
    need(not root.exists())
    codex=Path(os.environ['OV_QA_CODEX_BUNDLE_EXE']).resolve(strict=True)
    def stage(s):output['stage']=s
    with ExitStack() as stack:
        for rel in ('','work','tmp','codex','claude','.openvegas','AppData','AppData/Local','AppData/Roaming','observer'):
            folder=stack.enter_context(private_windows_directory(root/rel));need(not folder.names(1))
        obs=folder
        def read():
            try:r=json.loads(obs.read('counters.json',2048))
            except FileNotFoundError:return {}
            need(all(type(v) is int and 0<=v<1000000 for v in r.values()))
            output['counters']=r;return r
        env=environment('codex',root,codex,None,os.environ,managed=False)
        env.update(OPENAI_API_KEY=SYNTHETIC_KEY,OPENVEGAS_LOAD_PROJECT_ENV='0')
        (root/'codex/auth.json').write_text(json.dumps({'OPENAI_API_KEY':SYNTHETIC_KEY}),encoding='ascii')
        (root/'codex/config.toml').write_text(
            'model_provider="openai"\nmodel="gpt-6-astra"\nmodel_reasoning_effort="low"\n'
            'cli_auth_credentials_store="file"\nsandbox_mode="read-only"\napproval_policy="on-request"\n'
            'web_search="disabled"\ncheck_for_update_on_startup=false\n[projects.'+
            json.dumps(str(root/'work'))+']\ntrust_level="trusted"\n[tui]\nscreen_reader_detection_done=true\n',encoding='ascii')
        args=['child',os.environ['OV_QA_MANAGED'],str(codex),str(port)]
        try:
            with pty.isolated_console(Path(__file__).resolve(),args,env,str(root/'work')) as term:
                stage('managed-banner');term.expect('Windows managed Codex','owned native launcher',timeout=75)
                stage('composer');term.expect(r'OpenAI\s+Codex|for shortcuts','native UI',timeout=60,diagnose='claude-start')
                for i,mode in enumerate(('host','v','host')):
                    if i:
                        stage('switch-'+mode)
                        enter(term,'openvegas use '+mode,stage,'switch-'+mode)
                        pattern=r'Payer:\s*'+mode+r'\.\s*No prompt sent\.|Control check:\s*[a-z-]+\.'
                        verdict=term.expect(pattern,'native control',timeout=30)
                        if 'Control check:' in verdict:
                            k=verdict.split(':')[-1].strip(' .')
                            stage(k if k in ('native-route','native-inventory','native-root','native-model','native-workspace','native-fence','session-lease','wallet-prepare','wallet-outcome','request-boundary') else 'control-failed')
                            raise ValueError('Control refused')
                        f.switch_acks+=1
                    stage('payer-status')
                    enter(term,'openvegas status',stage,'turn-0' if i==0 else 'status')
                    term.expect(r'Payer:\s*'+mode+r';\s*\$V\s*(?:73\.25|73\.24)\.','verified native payer')
                    f.status_checks+=1
                    stage('root-input')
                    enter(term,f.prompts[i],stage,'turn-'+str(i),before_submit=lambda:f.arm(i))
                    if lane=='retry' and i==1:
                        stage('wallet-interrupt');need(f.interrupted.wait(35))
                        stage('await-retry-fence')
                        end=time.monotonic()+85
                        while time.monotonic()<end:
                            term.read();r=read()
                            need(not f.repeat_v and not f.repeat_host and not f.unmatched)
                            if r.get('ambiguous_v') and (r['turn_held'] or r['unattributed'] or r['unbound']):break
                            time.sleep(.06)
                        else:raise ValueError('No observed fence')
                        # Do not submit a replacement, change payer, or reconcile a hold.
                        stage('retry-fenced')
                        return
                    stage('root-render');term.expect(re.escape(f.answers[i]),'rendered synthetic reply',timeout=45)
                    f.rendered+=1;need(f.sent[f.answers[i]].wait(5))
                    time.sleep(.9)
                stage('exit');enter(term,'/exit',stage,'exit');term.require_success();f.clean_exit=True
                stage('title-policy')
                r=read();need(r['pipe'] and r['ingress'] and r['origin'] and (r['title_claim'] or r['title_denial']))
                need(f.rows==['host','v','host'] and f.sealed and f.rendered==3 and not f.extra_root
                     and not f.denied_wallet and not f.bad and r['issue']==r['revoke']==1 and not r['bank_bad'] and r['exit_zero'])
                stage('roundtrip-confirmed')
        finally: read()


async def lane_main(lane):
    import uvicorn
    from scripts.windows_switch_rehearsal import Fixture,LIMIT
    f=Fixture('codex',lane=='retry')
    out={'stage':'setup','counters':{}}
    ok=False
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));sock.listen(8)
            srv=uvicorn.Server(uvicorn.Config(f,lifespan='off',access_log=False,log_config=None,log_level='critical',ws_max_size=LIMIT,timeout_graceful_shutdown=3))
            task=asyncio.create_task(srv.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(8):
                    while not srv.started:await asyncio.sleep(.02)
                await asyncio.to_thread(drive,lane,f,sock.getsockname()[1],out)
                r=out['counters']
                need(r['owner'] and r['pipe'] and r['ingress'] and not f.denied_wallet and not f.excess_titles and not f.bad)
                if lane=='retry':
                    need(f.interrupted.is_set() and f.rows==['host'] and r['issue']==1 and not r['bank_bad']
                         and r['ambiguous_v'] and (r['turn_held'] or r['unbound'] or r['unattributed']))
                ok=True
            finally:
                srv.should_exit=True
                await asyncio.wait_for(task,8)
    except Exception: pass
    vals={k:out['counters'].get(k,0) for k in ('boot','upstream','owner','pipe','ingress','origin','title_claim','title_denial','retired_title','unknown_title','turn_held','unbound','unattributed','ambiguous_v','issue','revoke','bank_bad','exit_zero')}
    vals.update(rendered=f.rendered,links=max(0,len(f.rows)-1),switches=f.switch_acks,statuses=f.status_checks,title_host=f.denied_titles-f.denied_wallet,title_wallet=f.denied_wallet,cap=f.excess_titles,drop=int(f.interrupted.is_set()),repeat_v=f.repeat_v,repeat_host=f.repeat_host)
    print('::notice::managed '+lane+' stage='+out['stage']+' pass='+str(int(ok))+' '+' '.join(k+'='+str(v) for k,v in vals.items()),flush=True)
    return 0 if ok else 1

if __name__=='__main__':
    if sys.argv[1:2]==['child']:sys.exit(launch_child())
    assert sys.argv[1:] in (['roundtrip'],['retry'])
    sys.exit(asyncio.run(lane_main(sys.argv[1])))
