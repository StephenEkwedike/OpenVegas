"""Actual owned history/receipts/settlement on disposable SQL, synthetic supplier."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from openvegas.agent.native_handoff_source import assemble_task_tx
from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from tests.integration.test_native_continuation_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_continuation_postgres import followup, payload, post, setup_media
from tests.integration.test_native_history_postgres import (
    Source,
    callback,
    projection,
    propose_and_start,
)

pytestmark = pytest.mark.asyncio


async def accept_reads(c, generation):
    observations = []
    for index, call in enumerate(generation["tool_calls"]):
        source = Source(generation["native_generation"]["inference_request_id"], generation["provider_request_id"], call, {})
        tool = await propose_and_start(c.service, c.run, source)
        output = f"content {index}\n"
        result = {"ok": True, "path": "notes.txt", "content": output, "truncated": False,
                  "content_truncated": False, "bytes_read": len(output.encode()), "max_bytes": 262144}
        assert (await callback(c.service, c.run, tool, payload=result, stdout=output)).status_code == 200
        observations.append({"tool_name": "Read", "arguments": {"path": "notes.txt"},
                             "result": {"status": "succeeded", "payload": result, "stdout": output, "stderr": ""}})
    return observations


async def completed(c, *, original=True):
    initial = {"native_user_text": "Read my notes exactly.\r\nThen explain."} if original else {}
    first = payload(await post(c, **initial))
    observations = await accept_reads(c, first)
    c.emit_calls = False
    final = payload(await c.client.post("/inference/ask", json=await followup(c, first)))
    return first, final, observations


async def assemble(c, final, **changes):
    scope = NativeInferenceScope.model_validate({"run_id": c.run.run_id,
        "runtime_session_id": c.run.runtime_session_id, **await projection(c.service, c.run),
        **changes.pop("scope", {})})
    ref = NativeContinuationRef(previous_inference_request_id=final["native_generation"]["inference_request_id"],
                                expected_history_revision=final["native_generation"]["history_revision"])
    async with c.db.transaction() as tx:
        return await assemble_task_tx(tx, user_id=changes.pop("user_id", c.user), scope=scope,
                                      source_ref=changes.pop("source_ref", ref))


async def test_complete_public_chain_preserves_order_excludes_private_vendor_state_and_never_dispatches(continuation_db):
    c = continuation_db
    first, final, observations = await completed(c)
    before = len(c.calls)
    result = await assemble(c, final)
    task = result.document.values()["tasks"][0]
    assert task == {"user_text": "Read my notes exactly.\r\nThen explain.", "attachment_refs": [],
                    "generations": [{"assistant_text": first["text"], "observations": observations},
                                    {"assistant_text": final["text"], "observations": []}]}
    assert result.request_id == final["native_generation"]["inference_request_id"]
    assert result.history_revision == 1
    for private in ("opaque-private-fixture", "reasoning_details", "execution_token", "provider_call_id",
                    "source_snapshot", "settings", "Available tools"):
        assert private not in result.document.to_json()
    assert len(c.calls) == before == 2
    assert (await assemble(c, final)).document.sha256 == result.document.sha256
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 2


@pytest.mark.parametrize("change", ["owner", "session", "projection", "revision", "request", "legacy",
                                    "cancel", "route_unsettled", "billing_unsettled", "missing_receipt",
                                    "chain_gap", "text_hash", "tool_payload", "truncated_output",
                                    "uncertain_write", "cancelled_tool", "unfinished_tool"])
async def test_unsafe_or_unowned_history_cannot_be_assembled(continuation_db, change):
    c = continuation_db
    first, final, _ = await completed(c, original=change != "legacy")
    args = {}
    if change == "owner":
        args["user_id"] = str(uuid4())
    elif change == "session":
        args["scope"] = {"runtime_session_id": str(uuid4())}
    elif change == "projection":
        args["scope"] = {"expected_run_version": 999}
    elif change in {"revision", "request"}:
        args["source_ref"] = NativeContinuationRef(
            previous_inference_request_id=str(uuid4()) if change == "request" else first["native_generation"]["inference_request_id"],
            expected_history_revision=0 if change == "revision" else 1)
    elif change == "cancel":
        await c.db.execute("UPDATE agent_runs SET cancel_requested_at=now() WHERE id=$1::uuid", c.run.run_id)
    elif change == "route_unsettled":
        await c.db.execute("UPDATE inference_route_commands SET status='processing',response_status=NULL WHERE native_run_id=$1::uuid", c.run.run_id)
    elif change == "billing_unsettled":
        await c.db.execute("UPDATE inference_preauthorizations SET status='reserved'")
    elif change == "missing_receipt":
        await c.db.execute("DELETE FROM agent_chat_turns WHERE run_id=$1::uuid", c.run.run_id)
    elif change == "chain_gap":
        await c.db.execute("UPDATE inference_route_commands SET previous_native_request_id=gateway_request_id WHERE native_history_revision=1")
    elif change == "text_hash":
        await c.db.execute("UPDATE native_generation_envelopes SET history_inputs_json='{}'")
    elif change == "tool_payload":
        await c.db.execute("UPDATE agent_run_tool_calls SET result_payload=$1::jsonb", json.dumps({"private": "canary"}))
    elif change == "truncated_output":
        await c.db.execute("UPDATE agent_run_tool_calls SET stdout_truncated=true")
    elif change == "uncertain_write":
        await c.db.execute("UPDATE agent_run_tool_calls SET commit_state='commit_unknown'")
    elif change == "cancelled_tool":
        await c.db.execute("UPDATE agent_run_tool_calls SET status='cancelled'")
    elif change == "unfinished_tool":
        await c.db.execute("UPDATE agent_run_tool_calls SET status='started' WHERE id = "
                           "(SELECT id FROM agent_run_tool_calls ORDER BY id LIMIT 1)")
    with pytest.raises(ContractError) as error:
        await assemble(c, final, **args)
    assert error.value.code == APIErrorCode.HANDOFF_BLOCKED
    assert "canary" not in str(error.value)
    assert len(c.calls) == 2


async def test_tool_call_turn_is_not_a_finished_task(continuation_db):
    c = continuation_db
    first = payload(await post(c, native_user_text="Read notes."))
    with pytest.raises(ContractError):
        await assemble(c, first)
    assert len(c.calls) == 1


async def test_single_generation_final_answer_is_eligible_without_tools(continuation_db):
    c = continuation_db
    c.emit_calls = False
    final = payload(await post(c, native_user_text="Hello."))
    document = (await assemble(c, final)).document
    assert document.values()["tasks"] == [{"user_text": "Hello.", "attachment_refs": [],
        "generations": [{"assistant_text": final["text"], "observations": []}]}]
    assert len(c.calls) == 1


async def test_three_generation_history_includes_every_accepted_observation_in_order(continuation_db):
    c = continuation_db
    first = payload(await post(c, native_user_text="Read two rounds then explain."))
    observations1 = await accept_reads(c, first)
    middle = payload(await c.client.post("/inference/ask", json=await followup(c, first)))
    observations2 = await accept_reads(c, middle)
    c.emit_calls = False
    final = payload(await c.client.post("/inference/ask", json=await followup(c, middle)))
    result = await assemble(c, final)
    assert result.history_revision == 2
    assert result.document.values()["tasks"][0]["generations"] == [
        {"assistant_text": first["text"], "observations": observations1},
        {"assistant_text": middle["text"], "observations": observations2},
        {"assistant_text": final["text"], "observations": []},
    ]
    assert len(c.calls) == 3


async def test_source_reader_observes_cancellation_committed_before_run_lock_acquired(continuation_db):
    c = continuation_db
    _, final, _ = await completed(c)
    async with c.db.transaction() as tx:
        await tx.fetchrow("SELECT id FROM agent_runs WHERE id=$1::uuid FOR UPDATE", c.run.run_id)
        reader = asyncio.create_task(assemble(c, final))
        try:
            done, _ = await asyncio.wait({reader}, timeout=0.05)
            assert not done
            await tx.execute("UPDATE agent_runs SET cancel_requested_at=now() WHERE id=$1::uuid", c.run.run_id)
        except BaseException:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
    with pytest.raises(ContractError):
        await asyncio.wait_for(reader, timeout=5)
    assert len(c.calls) == 2


@pytest.mark.parametrize("web,attachment", [(True, False), (False, True), (True, True)])
async def test_owned_file_references_and_public_web_citations_survive_source_assembly(continuation_db, monkeypatch, web, attachment):
    c = continuation_db
    await setup_media(c, monkeypatch, web=web, attachment=attachment)
    first = payload(await post(c, native_user_text="Read the full file and find evidence."))
    await accept_reads(c, first)
    c.emit_calls = False
    if web:
        c.provider_body["choices"][0]["finish_reason"] = "stop"
        del c.provider_body["choices"][0]["message"]["tool_calls"]
    final = payload(await c.client.post("/inference/ask", json=await followup(c, first)))
    document = (await assemble(c, final)).document
    task = document.values()["tasks"][0]
    if web:
        assert all(g["web_search_used"] for g in task["generations"])
        assert all(g["web_search_sources"] == ["https://example.com/evidence"] for g in task["generations"])
    if attachment:
        import hashlib

        assert task["attachment_refs"] == [{"file_id": c.command["attachments"][0],
            "sha256": hashlib.sha256(b"Complete owned synthetic content, not a preview.").hexdigest()}]
        assert "Complete owned synthetic content" not in document.to_json()
    assert "opaque-private-fixture" not in document.to_json()
    assert len(c.calls) == 2


@pytest.mark.parametrize("change", ["replace", "delete", "replace_accounting", "delete_accounting", "inject"])
async def test_web_citations_must_match_original_annotations_and_settled_evidence(continuation_db, monkeypatch, change):
    c = continuation_db
    if change != "inject":
        await setup_media(c, monkeypatch, web=True, attachment=False)
        c.provider_body["choices"][0]["finish_reason"] = "stop"
        del c.provider_body["choices"][0]["message"]["tool_calls"]
    c.emit_calls = False
    final = payload(await post(c, native_user_text="Find public evidence."))
    row = await c.db.fetchrow("SELECT id,response_body_text FROM inference_requests")
    public = json.loads(row["response_body_text"])
    if change in {"delete", "delete_accounting"}:
        public.update(web_search_used=False, web_search_sources=[])
        if change == "delete_accounting":
            public.pop("managed_web_accounting")
    else:
        public.update(web_search_used=True, web_search_sources=["https://example.org/substituted"])
        if change == "replace_accounting":
            public["managed_web_accounting"]["annotations"][0]["url_citation"]["url"] = "https://example.org/substituted"
    await c.db.execute("UPDATE inference_requests SET response_body_text=$1 WHERE id=$2::uuid",
                       json.dumps(public), str(row["id"]))
    with pytest.raises(ContractError) as error:
        await assemble(c, final)
    assert error.value.code == APIErrorCode.HANDOFF_BLOCKED
    assert len(c.calls) == 1


@pytest.mark.parametrize("count", [None, True, 1, "0", [], {}])
async def test_nonweb_explicit_search_count_cannot_be_coerced_or_invented(continuation_db, count):
    c = continuation_db
    c.emit_calls = False
    final = payload(await post(c, native_user_text="Hello."))
    row = await c.db.fetchrow("SELECT id,response_body_text FROM inference_requests")
    public = json.loads(row["response_body_text"])
    assert "web_search_requests" not in public
    assert (await assemble(c, final)).document
    public["web_search_requests"] = count
    await c.db.execute("UPDATE inference_requests SET response_body_text=$1 WHERE id=$2::uuid",
                       json.dumps(public), str(row["id"]))
    with pytest.raises(ContractError):
        await assemble(c, final)
    assert len(c.calls) == 1
