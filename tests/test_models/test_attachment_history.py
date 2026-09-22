import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openvegas.contracts.errors import ContractError
from server.services.attachment_history import references, resolve_retained, validate_refs
from server.services.provider_threads import ProviderThreadService

IDENT = "00000000-0000-4000-8000-000000000001"
REF = {"file_id": IDENT, "sha256": hashlib.sha256(b"fixture").hexdigest()}
FILE = {"file_id": IDENT, "content_bytes": b"fixture"}


def test_references_are_content_committed_and_do_not_contain_file_contents():
    assert references([FILE]) == [REF]
    assert "content_bytes" not in references([FILE])[0]


@pytest.mark.parametrize(
    "refs",
    [
        None,
        [],
        [REF, REF],
        [{"file_id": "../x", "sha256": "a" * 64}],
        [{"file_id": IDENT, "sha256": "x" * 64}],
        [dict(REF, url="https://example.org")],
    ],
)
def test_invalid_or_client_url_references_rejected(refs):
    with pytest.raises(ContractError):
        validate_refs(refs)


@pytest.mark.asyncio
async def test_reload_rechecks_owner_and_exact_original_contents():
    service = SimpleNamespace(resolve_uploaded_for_inference=AsyncMock(return_value=[FILE]))
    assert await resolve_retained([REF], user_id="owner", file_service=service) == [FILE]
    service.resolve_uploaded_for_inference.assert_awaited_once_with(
        user_id="owner", file_ids=[IDENT]
    )
    service.resolve_uploaded_for_inference.return_value = [dict(FILE, content_bytes=b"changed")]
    with pytest.raises(ContractError, match="changed"):
        await resolve_retained([REF], user_id="owner", file_service=service)


@pytest.mark.asyncio
async def test_attachment_only_prompt_is_retained(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    db = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[{"role": "user", "content": {"text": "", "attachment_refs": [REF]}}]
        )
    )
    result, loaded, skipped = await ProviderThreadService(db).get_recent_messages_with_stats(
        thread_id=IDENT
    )
    assert result == [{"role": "user", "content": "", "attachment_refs": [REF]}]
    assert loaded == 1 and skipped == 0


@pytest.mark.asyncio
async def test_old_file_beyond_history_window_is_not_silently_lost(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    db = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"role": "user", "content": {"text": "recent"}}] * 21),
        fetchval=AsyncMock(return_value=True),
    )
    with pytest.raises(ContractError, match="No files were dropped"):
        await ProviderThreadService(db).get_recent_messages_with_stats(thread_id=IDENT, limit=20)


@pytest.mark.asyncio
async def test_visible_file_answer_keeps_json_and_code_not_heuristically_dropped(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    code = '```json\n{"arguments": "part of a code example"}\n```'
    rows = [
        {"role": "assistant", "content": {"text": code, "assistant_kind": "visible_text"}},
        {"role": "user", "content": {"text": "", "attachment_refs": [REF]}},
    ]
    result, loaded, skipped = await ProviderThreadService(
        SimpleNamespace(fetch=AsyncMock(return_value=rows))
    ).get_recent_messages_with_stats(thread_id=IDENT)
    assert loaded == 2 and skipped == 0
    assert result[1] == {"role": "assistant", "content": code}


@pytest.mark.asyncio
async def test_native_tool_request_never_omitted_from_file_conversation(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_CONTEXT_ENABLED", "1")
    rows = [
        {"role": "assistant", "content": {"text": "", "assistant_kind": "native_tool_request"}},
        {"role": "user", "content": {"text": "", "attachment_refs": [REF]}},
    ]
    with pytest.raises(ContractError, match="native tool continuation"):
        await ProviderThreadService(
            SimpleNamespace(fetch=AsyncMock(return_value=rows))
        ).get_recent_messages_with_stats(thread_id=IDENT)
