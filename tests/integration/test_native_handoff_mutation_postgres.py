"""Handoff projection of actual approved writes, not seeded success receipts."""
from __future__ import annotations

import pytest

from openvegas.contracts.errors import ContractError
from tests.integration.test_native_continuation_postgres import payload
from tests.integration.test_native_handoff_source_postgres import assemble
from tests.integration.test_native_mutation_lifecycle_postgres import (
    PRIVATE,
    call,
    continuation_db,
    continue_native,
    emit,
    lifecycle,
    mutation_db,
    ready,
    require_owned_database,
    result,
    write,
)

__all__ = ["continuation_db", "lifecycle", "mutation_db", "require_owned_database"]
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation", [
    call(), call("InsertAtEnd", filepath="notes.txt", content="tail"),
    call("FindAndReplace", filepath="notes.txt", old_string="before", new_string="after"),
])
async def test_real_approved_committed_write_retains_original_arguments_and_public_proof(lifecycle, operation):
    c = lifecycle
    c.command["native_user_text"] = "Update my notes."
    await emit(c, [operation])
    c.command.pop("native_user_text")
    prepared, tool = await ready(c)
    proof = await write(c, prepared)
    assert (await result(c, tool, proof)).status_code == 200
    final = payload(await continue_native(c))
    document = (await assemble(c, final)).document
    observation = document.values()["tasks"][0]["generations"][0]["observations"][0]
    assert observation == {"tool_name": operation["tool_name"], "arguments": operation["arguments"],
        "result": {"status": "succeeded", "payload": {"native_mutation_proof": proof},
                   "stdout": "", "stderr": ""}}
    for private in (PRIVATE, "observed_source", "content_utf8", "execution_token", "source_snapshot"):
        assert private not in document.to_json()
    assert len(c.calls) == 2


@pytest.mark.parametrize("change", ["missing_observation", "missing_approval", "changed_commitment", "uncertain",
                                    "approval_state", "approval_context", "approval_actor"])
async def test_post_completion_mutation_receipt_damage_blocks_handoff(lifecycle, change):
    c = lifecycle
    c.command["native_user_text"] = "Update my notes."
    await emit(c)
    c.command.pop("native_user_text")
    prepared, tool = await ready(c)
    assert (await result(c, tool, await write(c, prepared))).status_code == 200
    final = payload(await continue_native(c))
    if change == "missing_observation":
        await c.db.execute("DELETE FROM native_mutation_observations")
    elif change == "missing_approval":
        await c.db.execute("UPDATE native_mutation_preparations SET approval_id=NULL")
    elif change == "changed_commitment":
        await c.db.execute("UPDATE native_mutation_preparations SET contract_sha256=repeat('0',64)")
    elif change == "approval_state":
        await c.db.execute("UPDATE agent_tool_approvals SET decision_state='revoked'")
    elif change == "approval_context":
        await c.db.execute("UPDATE agent_tool_approvals SET approval_context_hash=repeat('0',64)")
    elif change == "approval_actor":
        await c.db.execute("UPDATE agent_tool_approvals SET decision_actor_id=gen_random_uuid()")
    else:
        await c.db.execute("UPDATE agent_run_tool_calls SET commit_state='commit_unknown'")
    with pytest.raises(ContractError):
        await assemble(c, final)
    assert len(c.calls) == 2
