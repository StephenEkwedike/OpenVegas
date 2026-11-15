"""Separate offline Windows QA executable; not the customer entry point."""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from openvegas.emotes._windows_state import private_windows_directory
from openvegas.integrations import inference_router_state as ledger
from openvegas.integrations import windows_customer as customer
from openvegas.integrations import windows_customer_codex as native
from openvegas.integrations.codex_title_origin import TitleOrigins
from openvegas.integrations.inference_router_fixture import SYNTHETIC_KEY
from openvegas.integrations.windows_native_image import pinned_windows_image
from openvegas.integrations.windows_terminal import console_modes
from qa_managed_bank import Bank, LocalSupplier, bundled_catalog


def need(ok):
    if not ok:
        raise ValueError("Private QA precondition")


async def exercise(root, exe, port):
    env = customer.environment(dict(os.environ))
    cwd = str(root / 'work')
    bank = Bank('codex', bundled_catalog(exe, env=env, cwd=cwd))
    broker = bank.broker()  # MockTransport only, synthetic device reference.
    owner = None
    with private_windows_directory(root / 'observer') as private:
        def save(**extra):
            row.update(extra)
            raw = json.dumps(row,sort_keys=True).encode('ascii')
            if raw != last[0]:
                private.atomic_write('counters.json',raw)
                last[0] = raw
        row = dict(boot=1, upstream=0, owner=0, pipe=0, ingress=0, origin=0, title_claim=0,
                   title_denial=0, retired_title=0, unknown_title=0, turn_held=0, unbound=0, unattributed=0, ambiguous_v=0,
                   issue=0, revoke=0, bank_bad=0, v=0, generation=0, exit_zero=0)
        last=[None];save()
        upstream = await native.original_upstream(exe, env, cwd)
        need(upstream=='https://api.openai.com/v1');save(upstream=1)
        owner = customer.Customer('codex', broker, upstream, cwd)
        s=owner.session; inner=s.router.inner; origins=inner.title_origins
        need(native.root_bounded(s) and type(origins) is TitleOrigins and origins.state is s.state
             and inner.fixture is None and inner.auth_mode=='peer')
        inner.fixture = LocalSupplier(bank,port)  # Only the validated plan's physical sink is loopback.
        save(owner=1)
        def observe(frame,event,arg):
            code=frame.f_code
            if code is ledger._refuse.__code__ and event=='call' and s.state.mode=='v':
                name=frame.f_locals['code']; caller=frame.f_back
                if caller.f_code is ledger.RouterState.begin.__code__ and caller.f_locals['self'] is s.state:
                    prior=caller.f_locals.get('prior')
                    scope=caller.f_locals.get('turn_scope')
                    if (name=='turn_held' and scope and scope[0]==owner.sid and prior and prior.mode=='v' and
                            s.state._attempts[prior.attempt_id].outcome=='ambiguous'):
                        row['turn_held']+=1
                    if name in ('unbound_retry','unattributed_response') and any(
                        a.operation.mode=='v' and a.outcome=='ambiguous' for a in s.state._attempts.values()):
                        row['unbound' if name=='unbound_retry' else 'unattributed']+=1
            elif code is native.Pipe.send.__code__ and event=='call':
                if frame.f_locals['self'].title_origins is origins:row['pipe']=1
            elif code is native.RootIngress.__call__.__code__ and event=='call':
                if frame.f_locals['self'] is s.router:row['ingress']=1
            elif code is TitleOrigins.claim.__code__ and event=='return' and isinstance(arg,str):
                if frame.f_locals['self'] is origins and arg in origins.origins:row['title_claim']+=1
            elif code is origin_need and event=='call' and not frame.f_locals.get('ok',True):
                caller=frame.f_back
                if (caller.f_code is TitleOrigins.claim.__code__ and caller.f_locals['self'] is origins
                        and caller.f_locals.get('meta',{}).get('thread_source')=='thread_title'):
                    row['title_denial']+=1
                    sid=caller.f_locals.get('sid')
                    if sid not in origins.origins:row['unknown_title']+=1
                    elif origins.origins[sid] is None or origins.origins[sid]!=origins.stamp():row['retired_title']+=1
        import openvegas.integrations.codex_title_origin as titles
        origin_need=titles._need.__code__
        def sample():
            save(origin=len(origins.origins),issue=bank.events.count('issue'),revoke=bank.events.count('revoke'),
                 bank_bad=bank.events.count('unexpected'),generation=s.state.generation,v=int(s.state.mode=='v'),
                 ambiguous_v=sum(a.operation.mode=='v' and a.outcome=='ambiguous' for a in s.state._attempts.values()))
        async def poll():
            while True:
                sample()
                await asyncio.sleep(.08)
        task=asyncio.create_task(poll())
        try:
            sys.setprofile(observe)
            rc=await native.run(owner,exe,env)
        finally:
            sys.setprofile(None);task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            try:await owner.close()
            finally:
                await broker.close();sample()
        save(exit_zero=int(rc==0));return rc


def main():
    root=Path(os.environ['USERPROFILE']).resolve(strict=True)
    need(sys.platform=='win32' and getattr(sys,'frozen',False) and all(os.isatty(i) for i in range(3))
         and re.fullmatch(r'ov-startup-[0-9a-f]{32}',root.name) and Path.cwd()==root/'work'
         and not any(Path.cwd().iterdir()) and Path(os.environ['CODEX_HOME'])==root/'codex'
         and os.environ['OPENAI_API_KEY']==SYNTHETIC_KEY and os.environ['OPENVEGAS_LOAD_PROJECT_ENV']=='0'
         and not any(k.upper().startswith(('GITHUB_','ACTIONS_')) or k.upper().endswith('_PROXY') for k in os.environ))
    exe,port=sys.argv[1:];need(0<int(port)<65536)
    with console_modes(), pinned_windows_image('codex',exe,'0.166.0') as pinned:
        return asyncio.run(exercise(root,pinned,int(port)))

if __name__=='__main__':
    try:code=main()
    except Exception:code=26
    sys.exit(code)
