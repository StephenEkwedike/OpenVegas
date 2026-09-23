"""Real-file, offline local writer checks. Not uncooperative-writer CAS claims."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from openvegas.agent import runtime_write as rw
from openvegas.agent.native_mutation import NativeMutationError, build_mutation_plan


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("writer attempted subprocess/network")
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


def document(root, new="new", path="a.txt", name="Write", **arguments):
    args = {"filepath": path, **arguments}
    if name == "Write":
        args.update(content=new, write_mode="replace")
    call = {"type": "tool_call", "tool_name": name, "arguments": args,
            "provider_call_id": "call_1", "shell_mode": "mutating", "timeout_sec": 30}
    return build_mutation_plan(call, rw.capture_source(str(root), path)).document()


def execute(root, plan):
    return rw.execute_native_mutation(str(root), plan)


def no_temp(root):
    assert not list(root.rglob(".openvegas-native-*.tmp"))


@pytest.mark.parametrize("old,new", [(None,""), (None,"new"), ("",""), ("a","a"),
    ("a",""), ("a","b"), ("a\r\nb\n","x\r\ny\n"), ("last","no newline"),
    ("\ufeffcafe\u0301","\ufeffnext\r\n"), ("\t\r","\t\n"), ("x","z" * 32768)])
def test_exact_bytes_and_independent_proof(root, old, new):
    leaf = root / "a.txt"
    if old is not None:
        leaf.write_bytes(old.encode())
    plan = document(root, new)
    out = execute(root, plan)
    assert out["outcome"] == ("no_change" if old == new else "applied"), out
    assert leaf.read_bytes() == new.encode()
    assert out["observed_after"] == {"exists": True, "bytes": len(new.encode()),
        "sha256": hashlib.sha256(leaf.read_bytes()).hexdigest()}
    assert out["contract_sha256"] == plan["contract_sha256"]
    assert not {"patch", "content_utf8", "original_call"} & out.keys()
    no_temp(root)


@pytest.mark.parametrize("mode", [0o600,0o640,0o644,0o660,0o755])
def test_preserves_ordinary_mode_without_chmod_original(root, mode, monkeypatch):
    leaf = root / "a.txt"
    leaf.write_text("old")
    leaf.chmod(mode)
    original_inode = leaf.stat().st_ino
    real = os.fchmod
    def fchmod(fd, target):
        assert os.fstat(fd).st_ino != original_inode
        return real(fd, target)
    monkeypatch.setattr(os, "fchmod", fchmod)
    assert execute(root, document(root))["outcome"] == "applied"
    assert stat.S_IMODE(leaf.stat().st_mode) == mode


def test_missing_distinct_from_empty(root):
    assert rw.capture_source(str(root), "a.txt") == {"exists": False,"content_utf8":None}
    (root/"a.txt").touch()
    assert rw.capture_source(str(root), "a.txt") == {"exists": True,"content_utf8":""}


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", "a\0b", "a//b", "./a", "a\\b"])
def test_bad_paths(root, path):
    with pytest.raises(NativeMutationError):
        rw.capture_source(str(root), path)


@pytest.mark.parametrize("kind", ["leaf_symlink","parent_symlink","hardlink","directory","fifo"])
def test_unsafe_nodes_never_read_or_block(root, kind):
    (root / "a.txt").write_text("private")
    path = "a.txt"
    if kind == "leaf_symlink":
        (root/"link").symlink_to(root/"a.txt"); path = "link"
    elif kind == "parent_symlink":
        (root/"link").symlink_to(root, target_is_directory=True); path = "link/a.txt"
    elif kind == "hardlink":
        os.link(root/"a.txt", root/"hard")
    elif kind == "directory":
        (root/"directory").mkdir(); path = "directory"
    else:
        os.mkfifo(root/"fifo"); path = "fifo"
    start = time.monotonic()
    with pytest.raises(NativeMutationError):
        rw.capture_source(str(root), path)
    assert time.monotonic()-start < 1


@pytest.mark.parametrize("mode", [0o444,0o666,0o4755,0o2755,0o1755])
def test_unsafe_modes(root, mode):
    (root/"a.txt").write_text("x"); (root/"a.txt").chmod(mode)
    with pytest.raises(NativeMutationError, match="native_runtime_unsafe_file"):
        rw.capture_source(str(root), "a.txt")


@pytest.mark.parametrize("data", [b"x"*32769,b"\xff",b"a\0b",b"\x1b[31m", "a\u202eb".encode()])
def test_bounded_strict_text(root, data):
    (root/"a.txt").write_bytes(data)
    with pytest.raises(NativeMutationError):
        rw.capture_source(str(root), "a.txt")


@pytest.mark.parametrize("change", ["bytes","delete","create","replace_inode"])
def test_stale_snapshot(root, change):
    leaf = root/"a.txt"
    if change != "create": leaf.write_text("old")
    plan = document(root)
    if change in {"bytes","create"}: leaf.write_text("other")
    elif change == "delete": leaf.unlink()
    else:
        # Content-level plan binds bytes, not a prior capture inode: identical
        # replacement BEFORE execution is intentionally valid.
        leaf.unlink(); leaf.write_text("old")
    expected = "applied" if change == "replace_inode" else "not_applied"
    out = execute(root, plan)
    assert out["outcome"] == expected, out
    if expected == "not_applied": assert out["reason"] == "native_runtime_source_changed"
    no_temp(root)


def test_create_collision_preserves_other_writer(root, monkeypatch):
    plan = document(root)
    real = os.link
    def collide(*args, **kwargs):
        (root/"a.txt").write_text("competitor")
        return real(*args, **kwargs)
    monkeypatch.setattr(os,"link",collide)
    monkeypatch.setattr(rw,"_supported",lambda:None)  # wrapper changes supports_dir_fd membership
    out = execute(root,plan)
    assert out["outcome"] == "not_applied" and out["reason"] == "native_runtime_create_collision", out
    assert (root/"a.txt").read_text() == "competitor"
    no_temp(root)


def test_cooperating_threads_one_winner(root):
    (root/"a.txt").write_text("old")
    plan = document(root)
    barrier = threading.Barrier(2)
    def run():
        barrier.wait()
        return execute(root,plan)
    with ThreadPoolExecutor(2) as pool:
        one,two = pool.submit(run),pool.submit(run)
        outcomes = [one.result(timeout=5),two.result(timeout=5)]
    assert sorted(x["outcome"] for x in outcomes) == ["applied","not_applied"],outcomes
    no_temp(root)


def test_short_writes_are_completed(root, monkeypatch):
    plan = document(root,"long text")
    real = os.write
    monkeypatch.setattr(os,"write",lambda fd,data:real(fd,data[:2]))
    assert execute(root,plan)["outcome"] == "applied"
    assert (root/"a.txt").read_text() == "long text"


@pytest.mark.parametrize("fault", ["write","fsync","fchmod"])
def test_precommit_failure_retains_original(root, monkeypatch, fault):
    (root/"a.txt").write_text("old")
    plan = document(root)
    def fail(*args): raise OSError("content that must never leak")
    monkeypatch.setattr(os,fault,fail)
    out = execute(root,plan)
    assert out["outcome"] == "not_applied",out
    assert "never leak" not in json.dumps(out)
    assert (root/"a.txt").read_text() == "old"
    no_temp(root)


@pytest.mark.parametrize("fault", ["raise_after_replace","readback_changed","directory_fsync","readback_error","interrupt"])
def test_possible_commit_is_unknown_no_rollback(root, monkeypatch, fault):
    (root/"a.txt").write_text("old")
    plan = document(root)
    replace,fsync,read = os.replace,os.fsync,rw._read
    committed = False
    def replacement(*args,**kwargs):
        nonlocal committed
        replace(*args,**kwargs); committed = True
        if fault == "raise_after_replace": raise OSError("private")
        if fault == "interrupt": raise KeyboardInterrupt()
        if fault == "readback_changed": (root/"a.txt").write_text("interloper")
    def sync(fd):
        if committed and fault == "directory_fsync": raise OSError("private")
        return fsync(fd)
    def reading(anchor):
        if committed and fault == "readback_error": raise OSError("private")
        return read(anchor)
    monkeypatch.setattr(os,"replace",replacement)
    monkeypatch.setattr(os,"fsync",sync)
    monkeypatch.setattr(rw,"_read",reading)
    out = execute(root,plan)
    assert out["outcome"] == "unknown",out
    assert (root/"a.txt").read_text() == ("interloper" if fault == "readback_changed" else "new")
    if fault == "readback_changed":
        assert out["observed_after"]["sha256"] == hashlib.sha256(b"interloper").hexdigest()
    no_temp(root)


def test_parent_rename_stops_before_commit(root, monkeypatch):
    (root/"folder").mkdir(); (root/"folder/a.txt").write_text("old")
    plan = document(root,path="folder/a.txt")
    real = rw._write_all
    def moved(fd,data):
        real(fd,data)
        (root/"folder").rename(root/"moved")
        (root/"folder").mkdir(); (root/"folder/a.txt").write_text("other")
    monkeypatch.setattr(rw,"_write_all",moved)
    out = execute(root,plan)
    assert out["outcome"] == "not_applied" and out["reason"] == "native_runtime_parent_changed",out
    assert (root/"moved/a.txt").read_text() == "old"
    assert (root/"folder/a.txt").read_text() == "other"
    no_temp(root)


def test_never_cleans_someone_elses_temp(root, monkeypatch):
    (root/"a.txt").write_text("old")
    plan = document(root)
    real = rw._write_all
    def substituted(fd,data):
        real(fd,data)
        temp = next(root.glob(".openvegas-native-*.tmp"))
        temp.unlink(); temp.write_text("foreign")
    monkeypatch.setattr(rw,"_write_all",substituted)
    out = execute(root,plan)
    assert out["outcome"] == "not_applied" and out["reason"] == "native_runtime_temp_changed",out
    assert (root/"a.txt").read_text() == "old"
    assert next(root.glob(".openvegas-native-*.tmp")).read_text() == "foreign"


def test_windows_explicitly_unsupported(root, monkeypatch):
    plan = document(root)
    monkeypatch.setattr(rw.sys,"platform","win32")
    out = execute(root,plan)
    assert out["reason"] == "native_runtime_unsupported"
    with pytest.raises(NativeMutationError,match="native_runtime_unsupported"):
        rw.capture_source(str(root),"a.txt")


def test_tampered_plan_no_effect(root):
    plan = document(root)
    plan["content_utf8"] = "forged"
    out = execute(root,plan)
    assert out["reason"] == "native_runtime_plan_invalid" and out["outcome"] == "not_applied"
    assert not (root/"a.txt").exists()
    no_temp(root)


def test_missing_parent_not_created(root):
    plan = build_mutation_plan({"tool_name":"Write","arguments":{"filepath":"missing/a.txt","content":"x"}},
        {"exists":False,"content_utf8":None}).document()
    assert execute(root,plan)["outcome"] == "not_applied"
    assert not (root/"missing").exists()


def test_directory_flock_serializes_distinct_handles(root, monkeypatch):
    import fcntl
    plan = document(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(rw,"LOCK_TIMEOUT_SEC",0.04)
        out = execute(root,plan)
        assert out["outcome"] == "not_applied" and out["reason"] == "native_runtime_busy",out
        assert not (root/"a.txt").exists()
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN); os.close(fd)
    assert execute(root,plan)["outcome"] == "applied"


def test_root_symlink_rejected(root):
    alias = root.parent / (root.name + "-alias")
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(NativeMutationError):
        rw.capture_source(str(alias),"a.txt")


def test_root_rename_detected(root, monkeypatch):
    (root/"a.txt").write_text("old")
    plan = document(root)
    moved = root.parent / (root.name + "-moved")
    real = rw._write_all
    def moving(fd,data):
        real(fd,data); root.rename(moved); root.mkdir()
    monkeypatch.setattr(rw,"_write_all",moving)
    out = execute(root,plan)
    assert out["outcome"] == "not_applied" and out["reason"] == "native_runtime_parent_changed",out
    assert (moved/"a.txt").read_text() == "old"
    assert not (root/"a.txt").exists()
    no_temp(moved)


def test_changed_during_temp_write_detected(root, monkeypatch):
    (root/"a.txt").write_text("old")
    plan = document(root)
    real = rw._write_all
    def change(fd,data):
        real(fd,data); (root/"a.txt").write_text("other")
    monkeypatch.setattr(rw,"_write_all",change)
    out = execute(root,plan)
    assert out["reason"] == "native_runtime_source_changed" and out["outcome"] == "not_applied",out
    assert (root/"a.txt").read_text() == "other"
    no_temp(root)


def test_temp_content_tampering_detected(root, monkeypatch):
    plan = document(root)
    real = rw._write_all
    def wrong(fd,data): real(fd,b"bad")
    monkeypatch.setattr(rw,"_write_all",wrong)
    out = execute(root,plan)
    assert out["reason"] == "native_runtime_temp_changed" and out["outcome"] == "not_applied",out
    assert not (root/"a.txt").exists()
    no_temp(root)


def test_zero_byte_write_fails_not_hangs(root, monkeypatch):
    plan = document(root)
    monkeypatch.setattr(os,"write",lambda *a:0)
    out = execute(root,plan)
    assert out["reason"] == "native_runtime_io" and out["outcome"] == "not_applied",out
    no_temp(root)


def test_nochange_reread_detects_race(root, monkeypatch):
    (root/"a.txt").write_text("old")
    plan = document(root,"old")
    real = rw._read
    n = 0
    def change(anchor):
        nonlocal n
        n += 1
        if n == 2: (root/"a.txt").write_text("other")
        return real(anchor)
    monkeypatch.setattr(rw,"_read",change)
    out = execute(root,plan)
    assert out["reason"] == "native_runtime_source_changed" and out["outcome"] == "not_applied",out
    assert out["observed_after"]["sha256"] == hashlib.sha256(b"other").hexdigest()


def test_create_link_then_exception_unknown_no_rollback(root, monkeypatch):
    plan = document(root)
    link = os.link
    def lost(*args,**kwargs):
        link(*args,**kwargs)
        raise OSError("lost acknowledgement")
    monkeypatch.setattr(os,"link",lost)
    monkeypatch.setattr(rw,"_supported",lambda:None)
    out = execute(root,plan)
    assert out["outcome"] == "unknown",out
    assert (root/"a.txt").read_text() == "new"
    assert (root/"a.txt").stat().st_nlink == 1
    no_temp(root)


def test_metadata_rejection_has_safe_error(root,monkeypatch):
    (root/"a.txt").write_text("private text")
    def reject(fd): raise rw.RuntimeWriteError("native_runtime_metadata")
    monkeypatch.setattr(rw,"_metadata",reject)
    with pytest.raises(NativeMutationError) as error:
        rw.capture_source(str(root),"a.txt")
    assert str(error.value) == "native_runtime_metadata"


def test_newfile_owner_only_and_no_parent_creation(root):
    assert execute(root,document(root))["outcome"] == "applied"
    assert stat.S_IMODE((root/"a.txt").stat().st_mode) == 0o600


def test_samefile_append_and_replace_match_pure_plan(root):
    (root/"a.txt").write_bytes(b"a\r\nb")
    first = document(root,name="InsertAtEnd",content="tail")
    assert execute(root,first)["outcome"] == "applied"
    assert (root/"a.txt").read_bytes() == b"a\r\nbtail"
    second = document(root,name="FindAndReplace",old_string="btail",new_string="end")
    assert execute(root,second)["outcome"] == "applied"
    assert (root/"a.txt").read_bytes() == b"a\r\nend"


def test_cancellation_after_commit_is_unknown(root,monkeypatch):
    from asyncio import CancelledError
    (root/"a.txt").write_text("old")
    plan = document(root)
    real = os.replace
    def cancelled(*args,**kwargs):
        real(*args,**kwargs)
        raise CancelledError()
    monkeypatch.setattr(os,"replace",cancelled)
    out = execute(root,plan)
    assert out["outcome"] == "unknown" and out["reason"] == "native_runtime_interrupted",out
    assert (root/"a.txt").read_text() == "new"
    no_temp(root)


def test_actual_xattr_rejected_not_silently_discarded(root):
    import ctypes
    (root/"a.txt").write_text("old")
    fd = os.open(root/"a.txt",os.O_RDWR)
    try:
        if rw.sys.platform == "darwin":
            libc = ctypes.CDLL(None,use_errno=True)
            libc.fsetxattr.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
            libc.fsetxattr.restype = ctypes.c_int
            assert libc.fsetxattr(fd,b"org.openvegas.fixture",b"x",1,0,0) == 0
        else:
            os.setxattr(fd,"user.openvegas_fixture",b"x")
    finally:
        os.close(fd)
    with pytest.raises(NativeMutationError,match="native_runtime_metadata"):
        rw.capture_source(str(root),"a.txt")
    assert (root/"a.txt").read_text() == "old"
