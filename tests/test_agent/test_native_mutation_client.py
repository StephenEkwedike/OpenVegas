"""Offline client preparation and actual-file dispatch. No server/provider calls."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import socket
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from openvegas.agent import native_mutation_client as bridge
from openvegas.agent.native_mutation import build_mutation_plan
from openvegas.client import APIError
from openvegas.contracts.errors import APIErrorCode, ContractError


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS","1")
    def forbidden(*a,**kw): pytest.fail("unexpected external/legacy execution")
    for name in ("run","Popen","call","check_output","check_call"):
        monkeypatch.setattr(subprocess,name,forbidden)
    monkeypatch.setattr(socket,"create_connection",forbidden)
    monkeypatch.setattr(socket.socket,"connect",forbidden)


@pytest.fixture
def scope(tmp_path):
    root = tmp_path.resolve()
    (root/"a.txt").write_bytes(b"first\r\nlast")
    return {"run_id":str(uuid4()), "runtime_session_id":str(uuid4()),
        "expected_run_version":7,"expected_valid_actions_signature":"sha256:"+"a"*64,
        "idempotency_key":"prepare-1", "workspace_root":str(root),
        "call":{"tool_name":"Write","arguments":{"filepath":"a.txt","content":"  target\r\nno final newline\t","write_mode":"replace"},
            "shell_mode":"mutating","timeout_sec":30,"provider_call_id":"call-original",
            "native_inference_request_id":str(uuid4())}}


def response_for(call,snapshot):
    projection = {k:v for k,v in call.items() if k != "native_inference_request_id"}
    plan = build_mutation_plan(projection,snapshot).document()
    prep = str(uuid4())
    marker = {k:plan[k] for k in ("version","relative_path","before_exists","before_sha256","before_bytes","after_sha256","after_bytes")}
    marker.update(preparation_id=prep,contract_sha256=plan["contract_sha256"])
    return {"preparation_id":prep,"contract_sha256":plan["contract_sha256"],"tool_name":"fs_apply_patch",
        "shell_mode":"mutating","timeout_sec":min(5,call.get("timeout_sec",30)),
        "arguments":{"native_mutation":marker,"patch":plan["patch"]},"diff":plan["patch"],
        **{k:plan[k] for k in ("before_exists","before_sha256","before_bytes","after_sha256","after_bytes","no_change")},
        "expires_at":(datetime.now(UTC)+timedelta(minutes=10)).isoformat(),
        "evidence_kind":"runtime_observed_file_v1"}


class Client:
    def __init__(self,call,change=None,error=None):
        self.call = copy.deepcopy(call)
        self.change,self.error = change,error
        self.calls = []
        self.response = None

    async def agent_native_mutation_prepare(self,**kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.error: raise self.error
        self.response = response_for(self.call,kwargs["observed_source"])
        if self.change: self.change(self.response)
        return self.response


def prepare(scope, client=None):
    client = client or Client(scope["call"])
    return asyncio.run(bridge.prepare_native_mutation(client,**scope))


def test_prepare_just_in_time_exact_references_bytes_and_hardcap(scope):
    client = Client(scope["call"])
    saved = copy.deepcopy(scope)
    runtime,plan = prepare(scope,client)
    assert scope == saved
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request == {k:v for k,v in scope.items() if k not in {"workspace_root","call"}} | {
        "native_inference_request_id":scope["call"]["native_inference_request_id"],
        "native_provider_call_id":"call-original","plan_mode":False,
        "observed_source":{"exists":True,"content_utf8":"first\r\nlast"}}
    assert runtime["arguments"] == client.response["arguments"]
    assert runtime["timeout_sec"] == 5 and plan["original_call"]["timeout_sec"] == 30
    assert runtime["provider_call_id"] == scope["call"]["provider_call_id"]
    assert runtime["native_inference_request_id"] == scope["call"]["native_inference_request_id"]
    assert "native_inference_request_id" not in plan["original_call"]
    assert "native_mutation_preparation_id" not in runtime
    assert plan["content_utf8"] == "  target\r\nno final newline\t"
    assert "original_call" not in request and "patch" not in request
    client.response["arguments"]["patch"] = "tampered later"
    assert runtime["arguments"]["patch"] == plan["patch"]


@pytest.mark.parametrize("timeout",[1,2,5,30,300])
def test_timeout_cap_matches_existing_runtime_policy(scope,timeout):
    scope["call"]["timeout_sec"] = timeout
    runtime,plan = prepare(scope)
    assert runtime["timeout_sec"] == min(5,timeout)
    assert plan["original_call"]["timeout_sec"] == timeout


def test_only_route_reference_excluded_from_pure_commit(scope):
    scope["call"]["type"] = "tool_call"
    _,plan = prepare(scope)
    assert plan["original_call"] == {k:v for k,v in scope["call"].items() if k != "native_inference_request_id"}


@pytest.mark.parametrize("field,value",[("tool_name","shell_exec"),("shell_mode","read_only"),("timeout_sec",30),
    ("timeout_sec",True),("contract_sha256","b"*64),("diff","changed"),("before_exists",1),
    ("before_sha256",None),("before_bytes",True),("after_sha256","b"*64),("after_bytes",3),
    ("no_change",True),("evidence_kind","server_verified"),("preparation_id","bad"),
    ("expires_at","2020-01-01T00:00:00+00:00"),("expires_at","2090-01-01T00:00:00"),
    ("expires_at",False),("extra",None)])
def test_changed_response_rejected_without_write(scope,field,value):
    client = Client(scope["call"],change=lambda r:r.update({field:value}))
    with pytest.raises(ContractError): prepare(scope,client)
    assert len(client.calls) == 1
    from pathlib import Path
    assert (Path(scope["workspace_root"])/"a.txt").read_bytes() == b"first\r\nlast"


@pytest.mark.parametrize("field,value",[("patch","changed\n"),("extra","command")])
def test_changed_executable_arguments_rejected(scope,field,value):
    client = Client(scope["call"],change=lambda r:r["arguments"].update({field:value}))
    with pytest.raises(ContractError): prepare(scope,client)


@pytest.mark.parametrize("field,value",[("version",True),("relative_path","other.txt"),("before_exists",1),
    ("before_sha256","a"*64),("after_sha256","a"*64),("after_bytes",0),("before_bytes",False),
    ("preparation_id",str(uuid4())),("contract_sha256","c"*64),("extra",1)])
def test_marker_mismatch_rejected(scope,field,value):
    client = Client(scope["call"],change=lambda r:r["arguments"]["native_mutation"].update({field:value}))
    with pytest.raises(ContractError): prepare(scope,client)


@pytest.mark.parametrize("field,value",[("run_id","bad"),("runtime_session_id","0"*36),
    ("expected_run_version",True),("expected_run_version",-1),("expected_valid_actions_signature","bad"),
    ("idempotency_key","bad\nkey")])
def test_bad_scope_before_capture_or_api(scope,field,value,monkeypatch):
    scope[field] = value
    client = Client(scope["call"])
    def forbidden(*a): pytest.fail("invalidscope touched filesystem")
    monkeypatch.setattr(bridge,"capture_source",forbidden)
    with pytest.raises(ContractError): prepare(scope,client)
    assert not client.calls


@pytest.mark.parametrize("field,value",[("native_inference_request_id",None),("native_inference_request_id","bad"),
    ("provider_call_id","bad\nid"),("tool_name","shell_exec"),("unexpected","private"),
    ("timeout_sec",True)])
def test_bad_call_never_calls_api(scope,field,value):
    scope["call"][field] = value
    client = Client(scope["call"])
    with pytest.raises(ContractError): prepare(scope,client)
    assert not client.calls


@pytest.mark.parametrize("setting",[None,"0","true","yes"," 1"])
def test_explicit_gate_before_any_io(scope,setting,monkeypatch):
    if setting is None: monkeypatch.delenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS")
    else: monkeypatch.setenv("OPENVEGAS_CHAT_NATIVE_MUTATIONS",setting)
    client = Client(scope["call"])
    with pytest.raises(ContractError): prepare(scope,client)
    assert not client.calls
    with pytest.raises(ContractError): asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],{}))


def test_api_error_redacted_and_not_retried(scope):
    client = Client(scope["call"],error=APIError(409,"secret source",{"private":"target text"}))
    with pytest.raises(APIError) as error: prepare(scope,client)
    assert error.value.status == 409 and error.value.data == {}
    assert "secret" not in str(error.value) and "target" not in str(error.value)
    assert len(client.calls) == 1


def test_transport_error_redacted_and_not_retried(scope):
    client = Client(scope["call"],error=OSError("private connection text"))
    with pytest.raises(ContractError) as error: prepare(scope,client)
    assert "private" not in str(error.value) and len(client.calls) == 1


def test_current_snapshot_not_cached(scope):
    from pathlib import Path
    client = Client(scope["call"])
    _,first = prepare(scope,client)
    (Path(scope["workspace_root"])/"a.txt").write_bytes(b"changed\r\n")
    scope["idempotency_key"] = "prepare-2"
    _,second = prepare(scope,client)
    assert first["before_sha256"] != second["before_sha256"]
    assert client.calls[-1]["observed_source"]["content_utf8"] == "changed\r\n"


@pytest.mark.parametrize("same",[True,False])
def test_execute_actual_file_and_private_free_result(scope,same,capsys):
    from pathlib import Path
    if same: scope["call"]["arguments"]["content"] = "first\r\nlast"
    _,plan = prepare(scope)
    result = asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
    assert result.result_status == "succeeded" and result.stdout == result.stderr == ""
    assert set(result.result_payload) == {"native_mutation_proof"}
    proof = result.result_payload["native_mutation_proof"]
    assert proof["outcome"] == ("no_change" if same else "applied")
    assert proof["observed_after"]["sha256"] == hashlib.sha256((Path(scope["workspace_root"])/"a.txt").read_bytes()).hexdigest()
    encoded = json.dumps(result.result_payload)
    assert "first" not in encoded and "content_utf8" not in encoded and "patch" not in encoded
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("outcome",["not_applied","unknown"])
def test_failure_result_maps_without_output(scope,outcome,monkeypatch):
    _,plan = prepare(scope)
    monkeypatch.setattr(bridge,"execute_native_mutation",lambda *a:{"outcome":outcome,"reason":"native_runtime_io"})
    result = asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
    assert result.result_status == "failed" and result.stdout == result.stderr == ""
    assert result.result_payload["native_mutation_proof"]["outcome"] == outcome


def test_worker_exception_is_uncertain_no_retry(scope,monkeypatch):
    _,plan = prepare(scope)
    calls=[]
    def fail(*a): calls.append(1); raise OSError("private contents")
    monkeypatch.setattr(bridge,"execute_native_mutation",fail)
    with pytest.raises(ContractError) as error:
        asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
    assert error.value.code == APIErrorCode.MUTATION_UNCERTAIN
    assert "private" not in str(error.value) and calls == [1]


def test_tampered_private_plan_never_reaches_writer(scope,monkeypatch):
    _,plan = prepare(scope); plan["patch"] = "tampered"
    def fail(*a): pytest.fail("invalid plan executed")
    monkeypatch.setattr(bridge,"execute_native_mutation",fail)
    with pytest.raises(ContractError): asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))


def test_prepare_cancellation_propagates_and_makes_one_api_call(scope):
    class Cancelling(Client):
        async def agent_native_mutation_prepare(self,**kw):
            self.calls.append(kw)
            raise asyncio.CancelledError()
    client = Cancelling(scope["call"])
    with pytest.raises(asyncio.CancelledError): prepare(scope,client)
    assert len(client.calls) == 1


def test_execution_cancellation_does_not_claim_worker_killed(scope,monkeypatch):
    _,plan = prepare(scope)
    started,release,finished = threading.Event(),threading.Event(),threading.Event()
    calls=[]
    real = bridge.execute_native_mutation
    def worker(*args):
        calls.append(1); started.set()
        assert release.wait(3)
        try: return real(*args)
        finally: finished.set()
    monkeypatch.setattr(bridge,"execute_native_mutation",worker)
    async def run():
        task = asyncio.create_task(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
        while not started.is_set(): await asyncio.sleep(0)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError): await task
        finally: release.set()
        while not finished.is_set(): await asyncio.sleep(0)
    asyncio.run(run())
    assert calls == [1]
    from pathlib import Path
    assert (Path(scope["workspace_root"])/"a.txt").read_bytes() == plan["content_utf8"].encode()


def test_capture_and_execute_off_event_loop(scope,monkeypatch):
    main = threading.get_ident()
    capture,execute = bridge.capture_source,bridge.execute_native_mutation
    observed=[]
    def captured(*a):
        observed.append(threading.get_ident()); return capture(*a)
    def executed(*a):
        observed.append(threading.get_ident()); return execute(*a)
    monkeypatch.setattr(bridge,"capture_source",captured)
    monkeypatch.setattr(bridge,"execute_native_mutation",executed)
    _,plan = prepare(scope)
    asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
    assert len(observed) == 2 and all(i != main for i in observed)


def test_real_service_public_contract_matches_without_db(scope):
    from openvegas.agent.native_mutation_service import _public
    class ServiceClient(Client):
        async def agent_native_mutation_prepare(self,**kwargs):
            self.calls.append(kwargs)
            projection = {k:v for k,v in self.call.items() if k != "native_inference_request_id"}
            plan = build_mutation_plan(projection,kwargs["observed_source"])
            return _public({"id":uuid4(),"expires_at":datetime.now(UTC)+timedelta(minutes=10)},plan)
    client = ServiceClient(scope["call"])
    runtime,plan = prepare(scope,client)
    assert runtime["arguments"]["native_mutation"]["contract_sha256"] == plan["contract_sha256"]
    assert runtime["timeout_sec"] == 5 and plan["original_call"]["timeout_sec"] == 30


@pytest.mark.parametrize("field",sorted(bridge._PUBLIC))
def test_missing_public_field_is_rejected(scope,field):
    client = Client(scope["call"],change=lambda r:r.pop(field))
    with pytest.raises(ContractError): prepare(scope,client)
    assert len(client.calls) == 1


@pytest.mark.parametrize("field,value",[("provider_call_id","other"),("timeout_sec",60),("shell_mode","read_only"),("type","tool_call")])
def test_server_projection_change_rejected_even_if_target_same(scope,field,value):
    altered = copy.deepcopy(scope["call"]); altered[field] = value
    client = Client(altered)
    with pytest.raises(ContractError): prepare(scope,client)


def test_target_whitespace_change_rejected(scope):
    altered = copy.deepcopy(scope["call"])
    altered["arguments"]["content"] = altered["arguments"]["content"].strip()
    with pytest.raises(ContractError): prepare(scope,Client(altered))


def test_original_call_frozen_before_capture_await(scope,monkeypatch):
    original = copy.deepcopy(scope["call"])
    client = Client(original)
    real = bridge.capture_source
    def capture(*args):
        snapshot = real(*args)
        scope["call"]["arguments"]["content"] = "changed during capture"
        scope["call"]["provider_call_id"] = "changed-call"
        return snapshot
    monkeypatch.setattr(bridge,"capture_source",capture)
    runtime,plan = prepare(scope,client)
    assert runtime["provider_call_id"] == original["provider_call_id"]
    assert plan["content_utf8"] == original["arguments"]["content"]


def test_capture_cancellation_never_sends_snapshot(scope,monkeypatch):
    client = Client(scope["call"])
    started,release,finished = threading.Event(),threading.Event(),threading.Event()
    real = bridge.capture_source
    def capture(*args):
        started.set()
        assert release.wait(3)
        try: return real(*args)
        finally: finished.set()
    monkeypatch.setattr(bridge,"capture_source",capture)
    async def run():
        task = asyncio.create_task(bridge.prepare_native_mutation(client,**scope))
        while not started.is_set(): await asyncio.sleep(0)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError): await task
        finally: release.set()
        while not finished.is_set(): await asyncio.sleep(0)
    asyncio.run(run())
    assert not client.calls


@pytest.mark.parametrize("kind",["oversized","cycle","wrong_type","surrogate"])
def test_bad_private_shape_bounded_and_safe(scope,kind):
    if kind == "oversized": scope["call"]["arguments"]["content"] = "x"*524289
    elif kind == "cycle": scope["call"]["arguments"]["cycle"] = scope["call"]
    elif kind == "wrong_type": scope["call"]["arguments"]["content"] = ["private"]
    else: scope["call"]["arguments"]["content"] = "\ud800"
    class NoClient:
        async def agent_native_mutation_prepare(self,**kw): pytest.fail("bad shape dispatched")
    with pytest.raises(ContractError) as error: prepare(scope,NoClient())
    assert error.value.detail == "Native mutation preparation validation failed; no automatic retry is permitted."


def test_mutation_plan_frozen_before_worker_execution(scope,monkeypatch):
    _,plan = prepare(scope)
    original_target = plan["content_utf8"]
    real = bridge.execute_native_mutation
    def execute(root,private_document):
        plan["content_utf8"] = "mutated caller document"
        plan["patch"] = "mutated caller patch"
        assert private_document["content_utf8"] == original_target
        return real(root,private_document)
    monkeypatch.setattr(bridge,"execute_native_mutation",execute)
    result = asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
    assert result.result_status == "succeeded"


@pytest.mark.parametrize("name,args",[("InsertAtEnd",{"content":"  tail\r\n"}),
    ("FindAndReplace",{"old_string":"last","new_string":"LAST"})])
def test_each_transform_roundtrips_in_real_file(scope,name,args):
    scope["call"]["tool_name"] = name
    scope["call"]["arguments"] = {"filepath":"a.txt",**args}
    runtime,plan = prepare(scope)
    result = asyncio.run(bridge.execute_prepared_mutation(scope["workspace_root"],plan))
    assert runtime["tool_name"] == "fs_apply_patch" and result.result_status == "succeeded"
    assert plan["original_call"]["tool_name"] == name
