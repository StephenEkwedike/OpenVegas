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

BASE='add579ab7211283c0b7a18173616ff4310f5d230'
OLD='146604cd6eb6a9538ca303287ebaf27f024b48571ad7c10f439b0703ef36ee01'
NEW='8005302f9fa777343fc1252534b6fb0552605d5974945011097a49d4faa3c434'
PINS={
    "openvegas/integrations/codex_stable_transport.py": "f2000295be52bbe26474f080f8880eebebce1159a610389979b803e84e148533",
    "openvegas/integrations/codex_title_origin.py": "cf1cc4b2c99d6c9677a3f277c0eff4424f28b281e2009901e5558807db13bfef",
    "openvegas/integrations/inference_router.py": "d03a94b61fb641eb18f6de7e74eb76e7f15fa536abd95386fc69475341fd8394",
    "openvegas/integrations/inference_router_managed.py": "ea677def6f231abfb42c5a49e94e1ff6a8da61a8bee87a9f41d8703b165f8de4",
    "openvegas/integrations/inference_router_state.py": "e4529b9221b4d8a63dc6d960f5d595767f59d6dd0b4e3b3b1a885c368f7a724c",
    "openvegas/integrations/windows_customer.py": "1ffafff3a0e894addc80cd324ded216e23c2ccedf5ded55e8ffae31bd1bd4aad",
    "openvegas/integrations/windows_customer_codex.py": "408d538159891ce5dfb986415d9157becc8c858065e5678e3dd5aa7e578b8163"
}
FREEZE='23fac8190de7dc03016a01c72be360383d8801483aef3766ed20c1a32dcfcf06'
NEW_FREEZE='8c213c291cd3caa60779d200b3121c98178b233677d2d8b414e6c377d41f019d'
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
    work=Path(os.environ["RUNNER_TEMP"])/"ov-title-candidate"
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
    need(files(tree)==expected)
    builder=runpy.run_path(str(tree/"scripts/build_cli_candidate.py"))
    need(builder["source_digest"]()==OLD)
    for path,h in PINS.items():
        data=subprocess.check_output(["git","cat-file","blob","HEAD:qa-frozen/"+path])
        need(hashlib.sha256(data).hexdigest()==h and expected.get(path)!=h)
        (tree/path).write_bytes(data)
    need(files(tree)==expected|PINS and builder["source_digest"]()==NEW)
    fr=json.loads((work/"frozen/freeze.json").read_bytes())
    fr["inputs"]["cli"].update(PINS)
    fr["checkout_native_inputs"].update(PINS)
    raw=(json.dumps(fr,sort_keys=True,indent=2)+"\n").encode()
    need(hashlib.sha256(raw).hexdigest()==NEW_FREEZE)
    (work/"frozen/freeze.json").write_bytes(raw)
    subprocess.run([sys.executable,"-m","pip","--isolated","install","--no-cache-dir","-c",tree/"requirements.lock",
                    "-r",src/"ci-build-requirements.txt"],cwd=work,check=True)

def constants(code):
    # Compare the full code object recursively, excluding only its path.
    return code.replace(co_filename="",co_consts=tuple(constants(c) if isinstance(c,types.CodeType) else c
                                                      for c in code.co_consts))

def build():
    from PyInstaller.archive.readers import CArchiveReader
    repo=Path.cwd()
    work=Path(os.environ["RUNNER_TEMP"])/"ov-title-candidate"
    tree=work/"frozen/cli"
    b=runpy.run_path(str(tree/"scripts/build_cli_candidate.py"))
    expected=json.loads((work/"frozen/freeze.json").read_bytes())["inputs"]["cli"]
    need(sha(work/"frozen/freeze.json")==NEW_FREEZE and files(tree)==expected and b["source_digest"]()==NEW)
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
    for path in sorted(PINS):
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
    proof=dict(base_assembly=43755105032,base_commit=BASE,freeze_sha256=NEW_FREEZE,qa_commit=os.environ["GITHUB_SHA"],old_pe=original["pe"],pe=pehash,
               launcher=original["launcher"],old_source=OLD,source=NEW,product_delta=PINS,
               frozen_inputs=len(expected),changed_inputs=sorted(PINS),compiled_modules_verified=verified,main=pins[MAIN],platform=pins[WIN])
    (out/"identity.json").write_text(json.dumps(proof,indent=2)+"\n")
    print("::notice::fixture-candidate "+" ".join(k+"="+proof[k] for k in ("source","pe","launcher","main","platform")),flush=True)
    with open(os.environ["GITHUB_ENV"],"a",encoding="utf-8") as dst: print("OV_FIXTURE_PE_HASH="+pehash,file=dst)

if __name__=="__main__":
    need(sys.platform=="win32" and sys.argv[1:] in (["stage"],["build"]))
    (stage if sys.argv[1]=="stage" else build)()
