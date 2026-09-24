"""Private first-dispatch primitive: real SQL/wallet, synthetic provider only.

Public routes, continuation and successive handoffs stay disabled. This does
not certify paid providers, the picker, CLI UX or a released model switch.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from openvegas.agent.native_continuation import frozen_settings
from openvegas.agent.native_envelope import history_inputs
from openvegas.agent.native_generation import NativeGenerationClaim, scope_document
from openvegas.agent.native_handoff_source import assemble_task_tx
from openvegas.contracts.errors import ContractError
from openvegas.gateway.inference import InferenceRequest
from server.services import native_handoff_dispatch as dispatch
from server.services.inference_replay import _KIND, _gateway_key, command_fingerprint
from tests.integration.test_native_handoff_service_postgres import (
    confirm,
    destination,
    prepare,
)
from tests.integration.test_native_handoff_service_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414 - pytest fixture registration
)
from tests.integration.test_native_handoff_service_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414 - pytest fixture registration
)
from tests.integration.test_native_handoff_service_postgres import (
    handoff_db as handoff_db,  # noqa: PLC0414 - pytest fixture registration
)

pytestmark = pytest.mark.asyncio


async def first(c, *, selection=None, current_files=()):
    preview, scope = await prepare(c, **({'selection': selection} if selection else {})), await destination(c)
    await confirm(c, preview, scope)
    key, route_id, owner = 'private-destination', str(uuid4()), str(uuid4())
    gateway_key = _gateway_key(c.user, key)
    command = {**c.command, 'model': preview.selection.model, 'prompt': 'Now compare the answer.',
               'native_user_text': 'Now compare the answer.', 'native_scope': scope.model_dump(),
               'attachments': list(current_files), 'enable_web_search': preview.selection.enable_web_search}
    digest = command_fingerprint(command)
    async with c.db.transaction() as tx:
        run = await tx.fetchrow('SELECT * FROM agent_runs WHERE id=$1::uuid FOR UPDATE', scope.run_id)
        scope_json = scope_document(scope, run)
        await tx.execute('''INSERT INTO inference_route_commands
            (id,user_id,idempotency_key,payload_hash,status,response_body_text,native_run_id,native_scope,native_history_revision)
            VALUES($1::uuid,$2::uuid,$3,$4,'processing',$5,$6::uuid,$7::jsonb,0)''',
            route_id,c.user,key,digest,json.dumps({'kind':_KIND,'state':'processing',
                'owner_token':owner,'gateway_key':gateway_key}),scope.run_id,scope_json)
        await tx.execute('UPDATE agent_runs SET native_generation_claim_id=$2::uuid,native_history_revision=0 WHERE id=$1::uuid',
                         scope.run_id,route_id)
    claim = NativeGenerationClaim(c.user,route_id,scope,scope_json,owner,digest,gateway_key,0)
    request = InferenceRequest('user:'+c.user,'openrouter',preview.selection.model,
        [{'role':'system','content':'Fresh destination instructions.'},
         {'role':'user','content':command['prompt']}],max_tokens=100,enable_tools=True,idempotency_key=gateway_key,
         enable_web_search=preview.selection.enable_web_search)
    request._native_generation_claim = claim
    refs = []
    if current_files:
        from server.services.openrouter_attachment_request import prepare_attachment_request
        request.messages,request._managed_attachment_context,refs = await prepare_attachment_request(
            history=request.messages[:-1],prompt=command['prompt'],file_ids=list(current_files),user_id=c.user,
            model_id=request.model,model_config=dict(await c.db.fetchrow(
                'SELECT * FROM provider_catalog WHERE provider=$1 AND model_id=$2','openrouter',request.model)),
            upload_service=dispatch.FileUploadService(c.db))
    settings = frozen_settings(SimpleNamespace(**{**command,'reasoning_effort':None}),max_tokens=100)
    request._native_history_inputs = history_inputs(attachment_refs=refs,settings=settings)
    prepared = await dispatch.prepare_first_dispatch(c.db,request=request,
        handoff_id=preview.handoff_id,handoff_sha256=preview.handoff_sha256)
    return SimpleNamespace(preview=preview,scope=scope,claim=claim,raw=request,request=prepared,calls=[])


def supplier(c, d, *, fail=False):
    async def handle(request):
        row = await c.db.fetchrow('SELECT * FROM native_task_handoffs WHERE id=$1::uuid',d.preview.handoff_id)
        assert row['first_request_id'] is not None and row['first_dispatch_json'] is not None
        assert str(row['first_route_command_id']) == d.claim.route_command_id
        assert await c.db.fetchval('SELECT status FROM inference_preauthorizations WHERE request_id=$1',
                                    str(row['first_request_id'])) == 'reserved'
        payload = json.loads(request.content)
        proof = json.loads(row['first_dispatch_json'])
        assert proof['payload_sha256'] == dispatch._hash(payload)
        assert proof['document_sha256'] == d.request._native_handoff_binding.document_sha256
        assert 'opaque-private-fixture' not in request.content.decode()
        assert 'Historical' not in payload['messages'][-1]['content']
        d.calls.append(payload)
        if fail: raise httpx.ReadTimeout('synthetic uncertain outcome')
        if d.request.enable_web_search:
            from tests.test_models.test_openrouter_web_gateway import response
            return httpx.Response(200,json=response())
        return httpx.Response(200,json={'id':'gen-private-destination','model':d.request.model,
            'choices':[{'message':{'role':'assistant','content':'Compared using the public history.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':8,'total_tokens':108,'cost':0.0001}})
    return handle


async def infer(c,d,**kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(supplier(c,d,**kwargs))) as http:
        c.gateway.http_client=http
        return await c.gateway.infer(d.request)


async def test_first_dispatch_recomposes_and_atomically_consumes_before_transport(handoff_db):
    c=handoff_db;d=await first(c)
    assert await c.db.fetchval('SELECT first_dispatch_json FROM native_task_handoffs') is None
    result=await infer(c,d)
    assert result.text=='Compared using the public history.' and len(d.calls)==1
    texts=[m['content'] for m in d.calls[0]['messages']]
    assert texts[0]=='Fresh destination instructions.'
    assert 'Keep the exact public task.\r\nNo truncation.' in texts
    assert 'Synthetic native answer' in texts and texts[-1]=='Now compare the answer.'
    assert await c.db.fetchval('SELECT count(*) FROM inference_usage')==2
    row=await c.db.fetchrow('SELECT * FROM native_generation_envelopes WHERE request_id=$1::uuid',result.inference_request_id)
    assert json.loads(row['history_inputs_json'])['incoming_handoff']==d.request._native_handoff_binding.provenance()
    assert dispatch._hash(json.loads(row['request_payload_json']))==d.request._native_handoff_binding.payload_sha256
    with pytest.raises(ContractError): await infer(c,d)
    assert len(d.calls)==1


@pytest.mark.parametrize('change',['messages','settings','claim','tools','review','price','source','binding','gate','inputs'])
async def test_change_before_reservation_cannot_charge_or_dispatch(handoff_db,monkeypatch,change):
    c=handoff_db;d=await first(c)
    if change=='messages':d.request.messages[-1]['content']='substituted'
    elif change=='settings':d.request.max_tokens=99
    elif change=='claim':d.request._native_generation_claim=replace(d.claim,owner_token=str(uuid4()))
    elif change=='tools':
        from openvegas.gateway import openrouter
        original=openrouter.local_tool_definitions
        monkeypatch.setattr(openrouter,'local_tool_definitions',lambda model:[*original(model),{'unexpected':'tool'}])
    elif change=='review':monkeypatch.setenv('OPENVEGAS_MODEL_REVIEWS_JSON','{}')
    elif change=='price':await c.db.execute('UPDATE provider_catalog SET v_price_input_per_1m=999 WHERE model_id=$1',d.request.model)
    elif change=='source':await c.db.execute('UPDATE agent_runs SET version=version+1 WHERE id=$1::uuid',c.run.run_id)
    elif change=='binding':d.request._native_handoff_binding=None
    elif change=='gate':monkeypatch.setenv('OPENVEGAS_NATIVE_TASK_HANDOFF','0')
    else:d.request._native_history_inputs=d.raw._native_history_inputs
    with pytest.raises(ContractError):await infer(c,d)
    assert not d.calls
    assert await c.db.fetchval('SELECT count(*) FROM inference_requests')==1
    assert await c.db.fetchval('SELECT first_request_id FROM native_task_handoffs') is None


async def test_wallet_failure_rolls_back_consumption_and_gateway_linkage(handoff_db,monkeypatch):
    c=handoff_db;d=await first(c)
    async def fail(*args,**kwargs):raise RuntimeError('synthetic wallet failure')
    monkeypatch.setattr(c.gateway.wallet,'reserve',fail)
    with pytest.raises(RuntimeError,match='synthetic wallet failure'):await infer(c,d)
    assert not d.calls
    assert await c.db.fetchval('SELECT first_dispatch_json FROM native_task_handoffs') is None
    assert await c.db.fetchval('SELECT count(*) FROM inference_requests')==1
    assert await c.db.fetchval('SELECT gateway_request_id FROM inference_route_commands WHERE id=$1::uuid',d.claim.route_command_id) is None


async def test_expiry_while_wallet_waits_rolls_back_without_transport(handoff_db,monkeypatch):
    c=handoff_db;d=await first(c)
    real=c.gateway.wallet.reserve
    async def wait(*args,**kwargs):
        await real(*args,**kwargs)
        class Clock:
            @staticmethod
            def now(tz):return d.request._native_handoff_binding.expires_at+timedelta(seconds=1)
        monkeypatch.setattr(dispatch,'datetime',Clock)
    monkeypatch.setattr(c.gateway.wallet,'reserve',wait)
    with pytest.raises(ContractError):await infer(c,d)
    assert not d.calls
    assert await c.db.fetchval('SELECT first_request_id FROM native_task_handoffs') is None
    assert await c.db.fetchval('SELECT count(*) FROM inference_requests')==1


async def test_uncertain_transport_keeps_consumption_one_way(handoff_db):
    c=handoff_db;d=await first(c)
    with pytest.raises(ContractError):await infer(c,d,fail=True)
    before=await c.db.fetchval('SELECT first_dispatch_json FROM native_task_handoffs')
    assert before is not None and len(d.calls)==1
    with pytest.raises(ContractError):await infer(c,d)
    assert await c.db.fetchval('SELECT first_dispatch_json FROM native_task_handoffs')==before
    assert len(d.calls)==1


async def test_no_retroactive_proof_or_successive_switch(handoff_db):
    import asyncpg

    from openvegas.agent import native_handoff_store as store
    from openvegas.contracts.native_scope import NativeContinuationRef
    c=handoff_db;d=await first(c)
    result=await infer(c,d)
    with pytest.raises(asyncpg.CheckViolationError):
        await c.db.execute('UPDATE native_task_handoffs SET first_dispatch_json=NULL')
    async with c.db.transaction() as tx:
        with pytest.raises(ContractError):
            await assemble_task_tx(tx,user_id=c.user,scope=d.scope,source_ref=NativeContinuationRef(
                previous_inference_request_id=result.inference_request_id,expected_history_revision=0))
        row=await store.load_handoff_tx(tx,user_id=c.user,handoff_id=d.preview.handoff_id)
        proof=json.loads(row.first_dispatch_json)
        replay=await store.consume_first_generation_tx(tx,user_id=c.user,handoff_id=row.handoff_id,
            handoff_sha256=row.handoff_sha256,target=row.target,route_command_id=row.first_route_command_id,
            request_id=row.first_request_id,dispatch_proof=proof)
        assert replay==row
        with pytest.raises(ContractError):
            await store.consume_first_generation_tx(tx,user_id=c.user,handoff_id=row.handoff_id,
                handoff_sha256=row.handoff_sha256,target=row.target,route_command_id=row.first_route_command_id,
                request_id=row.first_request_id,dispatch_proof=None)


async def test_concurrent_private_dispatch_consumes_and_calls_only_once(handoff_db):
    c=handoff_db;d=await first(c)
    async with httpx.AsyncClient(transport=httpx.MockTransport(supplier(c,d))) as http:
        c.gateway.http_client=http
        results=await asyncio.gather(c.gateway.infer(d.request),c.gateway.infer(d.request),return_exceptions=True)
    assert sum(isinstance(item,ContractError) for item in results)==1
    assert len(d.calls)==1
    assert await c.db.fetchval('SELECT count(*) FROM inference_usage')==2


@pytest.mark.parametrize('phase',['linkage','wallet'])
async def test_binding_removal_or_deadline_replacement_cannot_skip_consumption(handoff_db,monkeypatch,phase):
    from openvegas.agent import native_generation
    c=handoff_db;d=await first(c)
    snapshot=c.gateway._snapshot_handoff_request
    def track(req):
        d.active=snapshot(req)
        return d.active
    monkeypatch.setattr(c.gateway,'_snapshot_handoff_request',track)
    if phase=='linkage':
        original=native_generation.link_gateway_tx
        async def mutate(tx,claim,request_id):
            await original(tx,claim,request_id)
            d.active._native_handoff_binding=None
            d.active._native_history_inputs=d.raw._native_history_inputs
        monkeypatch.setattr(native_generation,'link_gateway_tx',mutate)
    else:
        original=c.gateway.wallet.reserve
        async def mutate(*args,**kwargs):
            await original(*args,**kwargs)
            binding=d.active._native_handoff_binding
            d.active._native_handoff_binding=replace(binding,expires_at=binding.expires_at+timedelta(days=1))
        monkeypatch.setattr(c.gateway.wallet,'reserve',mutate)
    with pytest.raises(ContractError):await infer(c,d)
    assert not d.calls
    assert await c.db.fetchval('SELECT first_request_id FROM native_task_handoffs') is None
    assert await c.db.fetchval('SELECT count(*) FROM inference_requests')==1


async def test_transport_rejects_tool_payload_change_after_consumption(handoff_db,monkeypatch):
    from openvegas.gateway import openrouter
    c=handoff_db;d=await first(c)
    real=c.gateway.wallet.reserve
    original=openrouter.local_tool_definitions
    async def mutate(*args,**kwargs):
        await real(*args,**kwargs)
        monkeypatch.setattr(openrouter,'local_tool_definitions',lambda model:[*original(model),{'unexpected':'tool'}])
    monkeypatch.setattr(c.gateway.wallet,'reserve',mutate)
    with pytest.raises(ContractError):await infer(c,d)
    assert not d.calls
    # The original reservation committed, but the changed wire payload was not
    # sent. Its one-way handoff stays consumed, rather than being retried blindly.
    assert await c.db.fetchval('SELECT first_request_id FROM native_task_handoffs') is not None
    assert await c.db.fetchval("SELECT count(*) FROM inference_requests WHERE status='failed'")==1


@pytest.mark.parametrize('current,web',[(False,False),(True,False),(True,True)])
async def test_inherited_and_current_media_keep_association_and_native_provenance(continuation_db,monkeypatch,current,web):
    from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
    from server.services.native_handoff_service import HandoffSelection
    from tests.integration.test_native_continuation_postgres import (
        payload,
        post,
        projection,
        setup_media,
    )
    c=continuation_db
    await c.sandbox.migrate(through=49)
    await setup_media(c,monkeypatch,web=False,attachment=True)
    c.emit_calls=False
    source=payload(await post(c,native_user_text='Remember the owned file.'))
    c.source_scope=NativeInferenceScope(run_id=c.run.run_id,runtime_session_id=c.run.runtime_session_id,
                                      **await projection(c.service,c.run))
    c.source_ref=NativeContinuationRef(previous_inference_request_id=source['native_generation']['inference_request_id'],
                                      expected_history_revision=0)
    monkeypatch.setenv('OPENVEGAS_NATIVE_TASK_HANDOFF','1')
    d=await first(c,selection=HandoffSelection(c.command['model'],max_tokens=100,enable_web_search=web),
                  current_files=c.command['attachments'] if current else ())
    result=await infer(c,d)
    assert result.inference_request_id and len(d.calls)==1
    context=d.request._managed_attachment_context.prepared
    assert context.file_ids==tuple(c.command['attachments'])*(2 if current else 1)
    assert isinstance(d.calls[0]['messages'][2]['content'],list)
    assert isinstance(d.calls[0]['messages'][-1]['content'],list) is current
    inputs=d.request._native_history_inputs.values()
    assert len(inputs['attachment_refs'])==(1 if current else 0)
    assert inputs['incoming_handoff']==d.request._native_handoff_binding.provenance()
