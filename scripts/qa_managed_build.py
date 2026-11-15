"""Build a separate QA PE and compare embedded code with the actual pinned customer PE."""
import ast
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile


def main():
    from PyInstaller.archive.readers import CArchiveReader
    from scripts import windows_fixture_build as old
    root=Path.cwd();work=Path(os.environ['RUNNER_TEMP'])/'ov-title-candidate'; tree=work/'frozen/cli'
    assert sys.platform=='win32' and old.files(tree)==json.loads((work/'frozen/freeze.json').read_bytes())['inputs']['cli']
    assert old.sha(work/'frozen/freeze.json')==old.NEW_FREEZE
    src=Path(__file__).resolve().parent; kit=work/'qa-src';kit.mkdir()
    # Only fake remote endpoints, not the older QA helper's monkeypatch entry point.
    data=subprocess.check_output(['git','cat-file','blob','HEAD:scripts/verify_windows_customer.py']).decode()
    keep=set('MODELS DEVICE GRANT BALANCE AFTER_WALLET SESSION Bank bundled_catalog LocalSupplier'.split())
    bits=[]
    for node in ast.parse(data).body:
        if isinstance(node,(ast.Import,ast.ImportFrom)) or getattr(node,'name',None) in keep or (
            isinstance(node,ast.Assign) and node.targets[0].id in keep):
            bits.append(ast.get_source_segment(data,node))
    (kit/'qa_managed_bank.py').write_text('\n\n'.join(bits)+'\n',encoding='utf-8')
    entry=kit/'qa_managed_entry.py';shutil.copyfile(src/entry.name,entry)
    helper=runpy.run_path(str(tree/'scripts/build_cli_candidate.py'))
    assert helper['source_digest']()==old.NEW
    with tempfile.TemporaryDirectory() as home:
        args=[sys.executable,'-m','PyInstaller','--clean','--noconfirm','--onefile','--console','--name','ov-qa-managed',
              '--paths',str(tree),'--collect-all','openvegas.integrations','--collect-submodules','keyring.backends',
              '--copy-metadata','openvegas','--distpath',str(work/'qa-bin'),'--workpath',str(work/'qa-work'),
              '--specpath',str(work/'qa-spec'),str(entry)]
        subprocess.run(args,env=helper['clean_environment'](home),cwd=tree,check=True,timeout=1000,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    exe=work/'qa-bin/ov-qa-managed.exe'
    installed=root/'installed/node_modules/@lordesteph/openvegas-win32-x64/bin/openvegas-win32-x64.exe'
    assert old.sha(installed)=='7bd197486075db9de0eae806ec3e6c77b27bac156f468e614c0505a4c62cc8b1'
    def arch(path):
        c=CArchiveReader(str(path));n,=[k for k,v in c.toc.items() if v[-1]=='z'];return c.open_embedded_archive(n)
    a,b=arch(installed),arch(exe)
    names=sorted(n for n in b.toc if n.startswith('openvegas.integrations.') or n in ('openvegas.emotes._windows_state','openvegas.emotes._windows_resources'))
    required=set('windows_customer windows_customer_codex windows_native_image windows_terminal session_ownership windows_session_lease inference_router_session inference_router_state inference_router inference_router_adapter inference_router_windows inference_router_wallet codex_title_origin codex_stable_transport inference_router_managed'.split())
    assert {'openvegas.integrations.'+n for n in required} <=set(names)
    for n in names:
        assert n in a.toc and old.constants(b.extract(n))==old.constants(a.extract(n))
    with open(os.environ['GITHUB_ENV'],'a',encoding='utf-8') as f:print('OV_QA_MANAGED='+str(exe),file=f)
    print(f'::notice::managed-build pe={old.sha(exe)} product={old.sha(installed)} verified_modules={len(names)}',flush=True)

if __name__=='__main__':main()
