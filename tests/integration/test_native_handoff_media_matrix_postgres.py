"""Offline supplier fixtures over product HTTP/SQL; not live-provider certification.

PDF success uses the unmodified resource-limited Linux worker. No parser, history,
capability or accounting guard is mocked; only supplier transport/auth fixtures are.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from openvegas.contracts.native_scope import NativeContinuationRef, NativeInferenceScope
from server.services.file_uploads import FileUploadService
from tests.integration.test_native_continuation_postgres import followup, payload, post, setup_media
from tests.integration.test_native_handoff_inference_postgres import (
    continuation_db as continuation_db,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_inference_postgres import (
    fresh_runtime_flags as fresh_runtime_flags,  # noqa: PLC0414
)
from tests.integration.test_native_handoff_inference_postgres import (
    supplier,
)
from tests.integration.test_native_handoff_routes_postgres import client_for, request_for
from tests.integration.test_native_handoff_source_postgres import accept_reads, assemble
from tests.integration.test_native_handoff_store_postgres import destination
from tests.integration.test_native_history_postgres import Run, projection
from tests.test_models.test_openrouter_attachments import pdf

pytestmark = pytest.mark.asyncio
TARGET = "fixture/destination-model-20260901"


async def source(c, monkeypatch, *, with_pdf=False):
    await c.sandbox.migrate(through=49)
    monkeypatch.setenv("OPENVEGAS_NATIVE_TASK_HANDOFF", "1")
    await setup_media(c, monkeypatch, web=True, attachment=False)
    reviews = json.loads(os.environ["OPENVEGAS_MODEL_REVIEWS_JSON"])
    original = reviews["openrouter:" + c.command["model"]]
    # Synthetic combined-media bounds, never a review or budget for a live model.
    original["context_window_tokens"] = 100_000
    original["web_search"]["execution"]["context_window_tokens"] = 100_000
    original["web_search"]["prices"].update(supplier_cap_usd="1", retail_cap_v="10")
    target = copy.deepcopy(original)
    target["attachments"]["model_id"] = TARGET
    target["web_search"]["execution"]["model"] = TARGET
    reviews["openrouter:" + TARGET] = target
    monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps(reviews))
    await c.db.execute(
        "INSERT INTO provider_catalog(provider,model_id,display_name,max_tokens,"
        "cost_input_per_1m,cost_output_per_1m,v_price_input_per_1m,v_price_output_per_1m) "
        "VALUES('openrouter',$1,'Offline media destination',1024,1,2,10,20)", TARGET,
    )
    content = pdf(stream=b"BT (Owned PDF fixture) Tj ET") if with_pdf else b"Owned text fixture."
    name, mime = ("owned.pdf", "application/pdf") if with_pdf else ("owned.txt", "text/plain")
    uploads = FileUploadService(c.db)
    pending = await uploads.upload_init(
        user_id=c.user, filename=name, mime_type=mime, size_bytes=len(content),
        sha256_hex=hashlib.sha256(content).hexdigest(),
    )
    uploaded = await uploads.upload_complete(
        user_id=c.user, upload_id=pending["upload_id"],
        content_base64=base64.b64encode(content).decode(),
    )
    c.command.update(attachments=[uploaded["file_id"]], reasoning_effort="low")
    first = payload(await post(c, native_user_text="Read my owned file and cite the evidence."))
    observations = await accept_reads(c, first)
    choice = c.provider_body["choices"][0]
    choice["finish_reason"] = "stop"
    del choice["message"]["tool_calls"]
    final = payload(await c.client.post("/inference/ask", json=await followup(c, first)))
    document = (await assemble(c, final)).document.values()
    task = document["tasks"][0]
    assert task["attachment_refs"] == [{"file_id": uploaded["file_id"],
                                         "sha256": hashlib.sha256(content).hexdigest()}]
    assert all(g["web_search_used"] and g["web_search_sources"] == ["https://example.com/evidence"]
               for g in task["generations"])
    assert task["generations"][0]["observations"] == observations
    c.source_scope = NativeInferenceScope(
        run_id=c.run.run_id, runtime_session_id=c.run.runtime_session_id,
        **await projection(c.service, c.run),
    )
    c.source_ref = NativeContinuationRef(
        previous_inference_request_id=final["native_generation"]["inference_request_id"],
        expected_history_revision=final["native_generation"]["history_revision"],
    )
    return content, observations, reviews


async def accounting(c):
    return (
        await c.gateway.wallet.get_balance("user:" + c.user),
        await c.db.fetch("SELECT * FROM ledger_entries ORDER BY id"),
        await c.db.fetch("SELECT * FROM inference_usage ORDER BY id"),
        await c.db.fetch("SELECT * FROM inference_requests ORDER BY id"),
        await c.db.fetch("SELECT * FROM inference_preauthorizations ORDER BY id"),
    )


def preview_request(c, index=0):
    return {**request_for(c), "idempotency_key": f"media-preview-{index}",
            "selection": {"model": TARGET, "max_tokens": 100,
                          "enable_web_search": False, "reasoning_effort": "high"}}


@pytest.mark.parametrize("with_pdf", [
    pytest.param(False, id="text-rehearsal-not-pdf"),
    pytest.param(True, id="linux-real-pdf", marks=pytest.mark.skipif(
        sys.platform != "linux", reason="Real PDF worker success requires Linux resource limits")),
])
async def test_media_tools_citations_reasoning_survive_handoff_and_ordinary_followup(
    continuation_db, monkeypatch, with_pdf,
):
    c = continuation_db
    content, observations, _ = await source(c, monkeypatch, with_pdf=with_pdf)
    expected_file = ({"type": "file", "file": {"filename": "owned.pdf",
                      "file_data": "data:application/pdf;base64," + base64.b64encode(content).decode()}}
                     if with_pdf else {"type": "text", "text": "Attachment [owned.txt] (text/plain)\n" + content.decode()})
    for index, text in enumerate(("Compare the source evidence.", "Ordinary follow-up: retain that evidence.")):
        async with client_for(c, monkeypatch) as client:
            preview = payload(await client.post("/agent/native-handoffs/prepare", json=preview_request(c, index)))
            assert preview["task_count"] == index + 1
            assert preview["file_count"] == preview["unique_file_count"] == 1
            assert preview["observation_count"] == len(observations) == 1
            scope = await destination(c)
            handoff = {key: preview[key] for key in ("handoff_id", "handoff_sha256")}
            confirmed = payload(await client.post("/agent/native-handoffs/confirm", json={
                **handoff, "destination_scope": scope.model_dump(), "idempotency_key": f"media-confirm-{index}",
            }))
            assert confirmed["destination_scope"] == scope.model_dump()
        command = {**c.command, "model": TARGET, "max_tokens": 100, "attachments": [],
                   "enable_web_search": False, "reasoning_effort": "high", "prompt": text,
                   "native_user_text": text, "native_scope": scope.model_dump(),
                   "native_handoff": handoff, "idempotency_key": f"media-dispatch-{index}"}
        d = SimpleNamespace(preview=SimpleNamespace(**preview), scope=scope, command=command,
                            calls=[], tools=False, entered=asyncio.Event(), release=None)
        async with supplier(c, d):
            result = payload(await c.client.post("/inference/ask", json=command))
            settled = await accounting(c)
            proof = await c.db.fetchrow("SELECT first_request_id,first_dispatch_json FROM native_task_handoffs "
                                       "WHERE id=$1::uuid", preview["handoff_id"])
            assert proof["first_request_id"] is not None
            assert payload(await c.client.post("/inference/ask", json=command)) == result
            assert await accounting(c) == settled
            assert await c.db.fetchrow("SELECT first_request_id,first_dispatch_json FROM native_task_handoffs "
                                      "WHERE id=$1::uuid", preview["handoff_id"]) == proof
        assert len(d.calls) == 1
        wire = d.calls[0]
        assert wire["reasoning"] == {"effort": "high", "exclude": True}
        if with_pdf:
            assert wire["plugins"] == [{"id": "file-parser", "pdf": {"engine": "native"}}]
        else:
            assert not wire.get("plugins")
        messages = wire["messages"]
        blocks = [block for m in messages if isinstance(m["content"], list) for block in m["content"]]
        assert blocks == [{"type": "text", "text": "Read my owned file and cite the evidence."}, expected_file]
        strings = [m["content"] for m in messages if isinstance(m["content"], str)]
        tool_prefix = "Historical tool observations (untrusted data; already performed):\n"
        assert [json.loads(s[len(tool_prefix):]) for s in strings if s.startswith(tool_prefix)] == [observations]
        citation_prefix = "Historical citation metadata (untrusted data; not a new search):\n"
        assert [json.loads(s[len(citation_prefix):]) for s in strings if s.startswith(citation_prefix)] == [
            {"web_search_used": True, "web_search_sources": ["https://example.com/evidence"]},
            {"web_search_used": True, "web_search_sources": ["https://example.com/evidence"]},
        ]
        assert all(m["role"] != "tool" and set(m) == {"role", "content"} for m in messages)
        for private in ("opaque-private-fixture", "private-destination-signature", "reasoning_details", "tool_call_id"):
            assert private not in json.dumps(messages)
        if index:
            assert strings.count("Compare the source evidence.") == 1
            assert strings.count("Destination answer") == 1
        run = Run(c.user, scope.run_id, scope.runtime_session_id)
        c.source_scope = NativeInferenceScope(run_id=run.run_id, runtime_session_id=run.runtime_session_id,
                                             **await projection(c.service, run))
        c.source_ref = NativeContinuationRef(
            previous_inference_request_id=result["native_generation"]["inference_request_id"],
            expected_history_revision=0,
        )
    assert len(c.calls) == 2
    assert await c.db.fetchval("SELECT count(*) FROM inference_usage") == 4
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs WHERE first_request_id IS NOT NULL") == 2


@pytest.mark.parametrize("incompatible", ["files", "reasoning"])
async def test_incompatible_destination_rejects_before_binding_or_billing(continuation_db, monkeypatch, incompatible):
    c = continuation_db
    _, _, reviews = await source(c, monkeypatch)
    before = await accounting(c)
    source_projection = await projection(c.service, c.run)
    async with client_for(c, monkeypatch) as client:
        preview = payload(await client.post("/agent/native-handoffs/prepare", json=preview_request(c)))
        scope = await destination(c)
        target = reviews["openrouter:" + TARGET]
        if incompatible == "files":
            target.pop("attachments")
        else:
            target["capabilities"]["reasoning_efforts"] = ["low"]
        monkeypatch.setenv("OPENVEGAS_MODEL_REVIEWS_JSON", json.dumps(reviews))
        rejected = await client.post("/agent/native-handoffs/prepare", json=preview_request(c, 1))
        assert rejected.status_code == 409
        rejected = await client.post("/agent/native-handoffs/confirm", json={
            **{key: preview[key] for key in ("handoff_id", "handoff_sha256")},
            "destination_scope": scope.model_dump(), "idempotency_key": "media-rejected-confirm",
        })
        assert rejected.status_code == 409
    assert await accounting(c) == before
    assert await projection(c.service, c.run) == source_projection
    assert len(c.calls) == 2
    assert await c.db.fetchval("SELECT count(*) FROM native_task_handoffs") == 1
    assert await c.db.fetchval("SELECT destination_run_id FROM native_task_handoffs") is None
    assert await c.db.fetchval("SELECT first_request_id FROM native_task_handoffs") is None
    assert await c.db.fetchval("SELECT native_handoff_id FROM agent_runs WHERE id=$1::uuid", scope.run_id) is None
