"""Consumed proof checks against actual SQL settlement; synthetic provider only."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from openvegas.agent.native_envelope import _digest
from openvegas.agent.native_handoff_source import assemble_task_tx
from openvegas.contracts.errors import ContractError
from openvegas.contracts.native_scope import NativeContinuationRef
from server.services import native_handoff_provenance as provenance
from server.services.inference_replay import InferenceReplayService, ReplayClaim
from server.services.native_handoff_service import HandoffSelection
from tests.integration.test_native_handoff_dispatch_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_dispatch_postgres import first, infer
from tests.integration.test_native_handoff_dispatch_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_dispatch_postgres import (
    handoff_db as handoff_db,  # noqa: PLC0414
)

pytestmark = pytest.mark.asyncio


async def settled(c, **changes):
    d = await first(c, **changes)
    result = await infer(c, d)
    row = await c.db.fetchrow('SELECT idempotency_key FROM inference_route_commands WHERE id=$1::uuid',
                             d.claim.route_command_id)
    claim = ReplayClaim(c.user, d.claim.route_command_id, row['idempotency_key'], d.claim.command_hash,
                        d.claim.gateway_key, d.claim.owner_token, native_claim=d.claim)

    async def append(_tx):
        pass

    await InferenceReplayService(c.db).complete(claim, gateway_request_id=result.inference_request_id,
        response={'text': result.text, 'native_generation': {'inference_request_id': result.inference_request_id}},
        append=append)
    return d, result


async def verify(c, d, **changes):
    async with c.db.transaction() as tx:
        return await provenance.verify_consumed_handoff_tx(tx, user_id=changes.get('user_id', c.user),
                                                          scope=changes.get('scope', d.scope))


async def test_consumed_ancestry_uses_settled_wire_and_original_claim_not_current_reviews(handoff_db, monkeypatch):
    c = handoff_db
    d, result = await settled(c)
    before = await c.db.fetchval('SELECT count(*) FROM inference_usage')
    original = await verify(c, d)
    assert original.provenance() == d.request._native_handoff_binding.provenance()
    assert original.first_request_id == result.inference_request_id
    assert original.ancestor_handoff_ids == (d.preview.handoff_id,)
    assert len(original.document.values()['tasks']) == 1
    monkeypatch.setenv('OPENVEGAS_MODEL_REVIEWS_JSON', '{}')
    c.db = await c.sandbox.reconnect()
    assert await verify(c, d) == original
    assert await c.db.fetchval('SELECT count(*) FROM inference_usage') == before
    assert len(d.calls) == 1
    assert 'Synthetic' not in repr(original)


@pytest.mark.parametrize('change', ['owner', 'session', 'unsettled_route', 'unsettled_billing',
                                    'wire', 'inputs', 'route_owner', 'proof', 'missing_envelope', 'gate'])
async def test_changed_consumption_or_private_history_cannot_be_reused(handoff_db, monkeypatch, change):
    c = handoff_db
    d, result = await settled(c)
    args = {}
    if change == 'owner':
        args['user_id'] = str(uuid4())
    elif change == 'session':
        args['scope'] = d.scope.model_copy(update={'runtime_session_id': str(uuid4())})
    elif change == 'unsettled_route':
        await c.db.execute("UPDATE inference_route_commands SET status='processing',response_status=NULL WHERE id=$1::uuid",
                           d.claim.route_command_id)
    elif change == 'unsettled_billing':
        await c.db.execute("UPDATE inference_preauthorizations SET status='reserved' WHERE request_id=$1",
                           result.inference_request_id)
    elif change in {'wire', 'inputs'}:
        column, digest_column = (('request_payload_json', 'request_sha256') if change == 'wire'
                                 else ('history_inputs_json', 'inputs_sha256'))
        raw = await c.db.fetchval(f'SELECT {column} FROM native_generation_envelopes WHERE request_id=$1::uuid',
                                 result.inference_request_id)
        value = json.loads(raw)
        if change == 'wire':
            value['messages'][0]['content'] = 'private-tamper-canary'
        else:
            value['incoming_handoff']['document_sha256'] = 'f' * 64
        raw = json.dumps(value)
        # Simulated corrupted storage with its self-hash rewritten must still
        # fail the independent immutable first-dispatch commitment.
        await c.db.execute(f'UPDATE native_generation_envelopes SET {column}=$2,{digest_column}=$3 '
                           'WHERE request_id=$1::uuid', result.inference_request_id, raw, _digest(raw))
    elif change == 'route_owner':
        body = json.loads(await c.db.fetchval('SELECT response_body_text FROM inference_route_commands WHERE id=$1::uuid',
                                             d.claim.route_command_id))
        body['owner_token'] = str(uuid4())
        await c.db.execute('UPDATE inference_route_commands SET response_body_text=$2 WHERE id=$1::uuid',
                           d.claim.route_command_id, json.dumps(body))
    elif change == 'proof':
        original = provenance.store._owned

        async def unproven(*args, **kwargs):
            return replace(await original(*args, **kwargs), first_dispatch_json=None)

        monkeypatch.setattr(provenance.store, '_owned', unproven)
    elif change == 'missing_envelope':
        await c.db.execute('DELETE FROM native_generation_envelopes WHERE request_id=$1::uuid', result.inference_request_id)
    else:
        monkeypatch.setenv('OPENVEGAS_NATIVE_TASK_HANDOFF', '0')
    with pytest.raises(ContractError) as error:
        await verify(c, d, **args)
    assert 'private-tamper-canary' not in str(error.value)
    assert len(d.calls) == 1


async def test_route_not_completed_cannot_be_promoted_to_consumed_history(handoff_db):
    c = handoff_db
    d = await first(c)
    with pytest.raises(ContractError):
        await verify(c, d)
    await infer(c, d)
    with pytest.raises(ContractError):
        await verify(c, d)


async def test_concurrent_consumed_reads_do_not_rebill_or_deadlock(handoff_db):
    c = handoff_db
    d, _ = await settled(c)
    results = await asyncio.wait_for(asyncio.gather(*(verify(c, d) for _ in range(4))), 5)
    assert all(row == results[0] for row in results)
    assert len(d.calls) == 1


@pytest.mark.parametrize('foreign_owner', [False, True])
async def test_corrupted_reverse_link_rejects_without_locking_unrelated_route(handoff_db, foreign_owner):
    c = handoff_db
    d, result = await settled(c)
    owner, route = (str(uuid4()) if foreign_owner else c.user), str(uuid4())
    if foreign_owner:
        await c.db.execute('INSERT INTO auth.users(id) VALUES($1::uuid)', owner)
    await c.db.execute('INSERT INTO inference_route_commands '
        '(id,user_id,idempotency_key,payload_hash,status,response_body_text) '
        "VALUES($1::uuid,$2::uuid,$3,$4,'processing','{}')", route, owner, 'unrelated-route', 'f' * 64)
    await c.db.execute('UPDATE inference_requests SET native_route_command_id=$2::uuid WHERE id=$1::uuid',
                       result.inference_request_id, route)
    async with c.db.transaction() as blocker:
        await blocker.fetchrow('SELECT id FROM inference_route_commands WHERE id=$1::uuid FOR UPDATE', route)
        # The unrelated lock stays held. Waiting on it is a regression, even if
        # validation would eventually reject after that lock was released.
        with pytest.raises(ContractError):
            await asyncio.wait_for(verify(c, d), 2)
    assert len(d.calls) == 1


async def assembled(c, d, result):
    async with c.db.transaction() as tx:
        return await assemble_task_tx(tx, user_id=c.user, scope=d.scope,
            source_ref=NativeContinuationRef(previous_inference_request_id=result.inference_request_id,
                                             expected_history_revision=0))


async def test_successive_boundaries_preserve_each_task_once_without_private_state(handoff_db):
    c = handoff_db
    second, result = await settled(c)
    source = await assembled(c, second, result)
    tasks = source.document.values()['tasks']
    assert len(tasks) == 2
    assert tasks[0]['user_text'] == 'Keep the exact public task.\r\nNo truncation.'
    assert tasks[1]['user_text'] == 'Now compare the answer.'
    # A new source boundary needs new prepare/confirm keys. Keep existing
    # helper semantics but scope its two keys to this distinct boundary.
    from unittest.mock import patch

    from tests.integration import test_native_handoff_dispatch_postgres as initial
    from tests.integration.test_native_handoff_service_postgres import confirm, prepare
    from tests.integration.test_openrouter_postgres import MODELS

    c.source_scope = second.scope
    c.source_ref = NativeContinuationRef(previous_inference_request_id=result.inference_request_id,
                                         expected_history_revision=0)

    async def next_prepare(context, **kwargs):
        return await prepare(context, idempotency_key='second-preview', **kwargs)

    async def next_confirm(context, preview, scope, **kwargs):
        return await confirm(context, preview, scope, idempotency_key='second-confirm', **kwargs)

    with patch.object(initial, 'prepare', next_prepare), patch.object(initial, 'confirm', next_confirm):
        third, final = await settled(c, selection=HandoffSelection(MODELS[0], max_tokens=100),
                                    key='third-private-destination')
    final_source = await assembled(c, third, final)
    final_tasks = final_source.document.values()['tasks']
    assert len(final_tasks) == 3 and final_tasks[:2] == tasks
    assert (await verify(c, third)).ancestor_handoff_ids == (second.preview.handoff_id, third.preview.handoff_id)
    assert 'opaque-private-fixture' not in final_source.document.to_json()
    assert len(second.calls) == len(third.calls) == 1
    assert await c.db.fetchval('SELECT count(*) FROM inference_usage') == 3
