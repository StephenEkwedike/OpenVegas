"""Build one Windows-only QA package from the public assembly's verified frozen inputs."""
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import zipfile
from pathlib import Path

BASE="c1f97e3e91fa45b0d784184b157dcd3398c5c294"
OLD="c3685dc856a19eb8aea14c8dac45e864728411370fa1cf959a591013c63be570"
NEW="844eddd4b98c3f9f84df6aef24d96af8b3282f805b71141fd5d99c3db592213f"
MOD="openvegas/integrations/windows_billing_host.py"
OLD_MOD="22163229ebdd6bc09b8c31797ae333f8b8722d54208fd5dbeed1a3ce52d87456"
NEW_MOD="8d249cb1aa71c9488bfdeae08c7d7072b10819c29e4e1e3d072759b1704f56fb"
PATCH="9fe4fef8218d4420f8497cbf39f9eb0eed152275546e1f83dd9d2c3fc00a9988"
FREEZE="cee8e8074f92a34c06a81ad4f19f408abc4c77100dc17dc62f3b969200e25cb5"
MAIN="openvegas-ai-0.3.19.tgz"
WIN="lordesteph-openvegas-win32-x64-0.3.19.tgz"

def need(ok):
    if not ok: raise ValueError("Windows QA build invariant failed")

def sha(path):
    with path.open("rb") as f: return hashlib.file_digest(f,"sha256").hexdigest()

def files(root):
    return {p.relative_to(root).as_posix():sha(p) for p in root.rglob("*") if p.is_file()}

def stage():
    repo=Path.cwd()
    work=Path(os.environ["RUNNER_TEMP"])/"ov-one-module"
    work.mkdir()
    need(subprocess.check_output(["git","-C","baseline","rev-parse","HEAD"],text=True).strip()==BASE)
    zipname=work/"public.zip"
    subprocess.run(["git","-C","baseline","-c","core.autocrlf=false","archive","--format=zip","--output",zipname,"HEAD"],check=True)
    src=work/"public"
    with zipfile.ZipFile(zipname) as z: z.extractall(src)
    need(sha(src/"native-router-freeze.json")==FREEZE)
    subprocess.run([sys.executable,"-IB",src/"scripts/ci_prepare_native_frozen.py",src,work/"frozen"],check=True)
    tree=work/"frozen/cli"
    expected=json.loads((work/"frozen/freeze.json").read_bytes())["inputs"]["cli"]
    need(len(expected)==331 and files(tree)==expected and expected[MOD]==OLD_MOD)
    builder=runpy.run_path(str(tree/"scripts/build_cli_candidate.py"))
    need(builder["source_digest"]()==OLD)
    patch=subprocess.check_output(["git","show","HEAD:scripts/windows-fixture-only.patch"])
    need(hashlib.sha256(patch).hexdigest()==PATCH)
    subprocess.run(["git","-c","core.autocrlf=false","apply","--no-index","--unidiff-zero","-"],input=patch,cwd=tree,check=True)
    need(files(tree)=={**expected,MOD:NEW_MOD} and builder["source_digest"]()==NEW)
    subprocess.run([sys.executable,"-m","pip","--isolated","install","--no-cache-dir","-c",tree/"requirements.lock",
                    "-r",src/"ci-build-requirements.txt"],cwd=work,check=True)

def constants(code):
    # Compare the full code object recursively, excluding only its path.
    return code.replace(co_filename="",co_consts=tuple(constants(c) if isinstance(c,types.CodeType) else c
                                                      for c in code.co_consts))

def build():
    from PyInstaller.archive.readers import CArchiveReader
    repo=Path.cwd()
    work=Path(os.environ["RUNNER_TEMP"])/"ov-one-module"
    tree=work/"frozen/cli"
    b=runpy.run_path(str(tree/"scripts/build_cli_candidate.py"))
    expected=json.loads((work/"frozen/freeze.json").read_bytes())["inputs"]["cli"]
    need(files(tree)=={**expected,MOD:NEW_MOD} and b["source_digest"]()==NEW)
    name="openvegas-win32-x64"
    cmd=[sys.executable,"-m","PyInstaller","--clean","--noconfirm","--onefile","--name",name,
         "--distpath",str(work/"bin"),"--workpath",str(work/"obj"),"--specpath",str(work/"spec"),
         "--paths",str(tree),"--collect-submodules","openvegas.emotes","--collect-all","openvegas.integrations",
         "--collect-all","sounddevice","--collect-submodules","keyring.backends","--copy-metadata","openvegas",
         *b["pack_inputs"](),str(tree/"scripts/frozen_entry.py")]
    with tempfile.TemporaryDirectory() as home:
        subprocess.run(cmd,cwd=tree,env=b["clean_environment"](home),check=True,timeout=1100)
    pe=work/"bin"/(name+".exe")
    need(pe.read_bytes()[:2]==b"MZ")
    exe_archive=CArchiveReader(str(pe))
    archives=[k for k in exe_archive.toc if k.endswith(".pyz")]
    need(len(archives)==1)
    pyz=exe_archive.open_embedded_archive(archives[0])
    verified=0
    for path in (MOD,"openvegas/integrations/windows_customer.py","openvegas/integrations/windows_customer_codex.py",
                 "openvegas/integrations/windows_customer_claude.py","openvegas/integrations/inference_router_adapter.py"):
        member=path[:-3].replace("/",".")
        code=pyz.extract(member)
        need(isinstance(code,types.CodeType) and constants(code)==constants(compile((tree/path).read_bytes(),path,"exec")))
        verified+=1
    from scripts.windows_managed_startup import unpack
    original=json.loads((repo/"qa-image-pins.json").read_bytes())
    old=work/"old/node_modules"
    unpack(repo/"packs",old,original)
    index=json.loads((old/"openvegas-ai/release.json").read_bytes())
    pehash=sha(pe)
    need(pehash!=original["pe"] and index["source_sha256"]==OLD)
    index.update(source_sha256=NEW,platforms={"win32-x64":{**index["platforms"]["win32-x64"],"sha256":pehash}})
    out=repo/"fixture-artifacts"
    out.mkdir()
    for filename,override,data in [(MAIN,"package/release.json",(json.dumps(index,indent=2)+"\n").encode()),
                                     (WIN,"package/bin/"+pe.name,pe.read_bytes())]:
        with tarfile.open(repo/"packs"/filename,"r:gz") as orig, tarfile.open(out/filename,"w:gz") as dest:
            changed=0
            for m in orig.getmembers():
                need(m.isfile())
                if m.name==override:
                    m.size=len(data); dest.addfile(m,io.BytesIO(data)); changed+=1
                else:
                    with orig.extractfile(m) as s: dest.addfile(m,s)
            need(changed==1)
    pins={**original,"source":NEW,"pe":pehash,MAIN:sha(out/MAIN),WIN:sha(out/WIN)}
    unpack(out,repo/"installed/node_modules",pins)
    proof=dict(base_assembly=43747860497,qa_commit=os.environ["GITHUB_SHA"],old_pe=original["pe"],pe=pehash,
               launcher=original["launcher"],old_source=OLD,source=NEW,product_module=NEW_MOD,
               frozen_inputs=331,changed_inputs=[MOD],compiled_modules_verified=verified,main=pins[MAIN],platform=pins[WIN])
    (out/"identity.json").write_text(json.dumps(proof,indent=2)+"\n")
    print("::notice::fixture-candidate "+" ".join(k+"="+proof[k] for k in ("source","pe","launcher","main","platform")),flush=True)
    with open(os.environ["GITHUB_ENV"],"a",encoding="utf-8") as dst: print("OV_FIXTURE_PE_HASH="+pehash,file=dst)

if __name__=="__main__":
    need(sys.platform=="win32" and sys.argv[1:] in (["stage"],["build"]))
    (stage if sys.argv[1]=="stage" else build)()
