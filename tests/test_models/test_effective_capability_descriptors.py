"""Customer model metadata observes account rollout and runtime kill switches."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server.routes import models


def descriptor():
    return {
        "provider": "openrouter", "model_id": "fixture/model", "enabled": True,
        "available": True,
        "capabilities": {
            **{key: True for key in ("file_upload", "image_input", "web_search", "stream_events", "tools", "reasoning_controls")},
            "reasoning_efforts": ["low", "high"], "reviewed": True,
        },
    }


@pytest.mark.asyncio
async def test_customer_descriptor_never_enables_disabled_feature_or_mutates_catalog(monkeypatch):
    monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")
    original = descriptor()
    before = copy.deepcopy(original)
    service = SimpleNamespace(list_descriptors=AsyncMock(return_value=[original]))
    monkeypatch.setattr(models, "get_catalog", lambda: service)
    checks = []

    def allowed(provider, model, feature, *, user_id):
        checks.append((provider, model, feature, user_id))
        return feature not in {"web_search", "reasoning_controls"}

    monkeypatch.setattr(models, "resolve_capability", allowed)
    response = await models.list_models("openrouter", {"user_id": "account-fixture"})
    caps = response["models"][0]["capabilities"]
    assert caps["file_upload"] and not caps["web_search"]
    assert caps["reasoning_efforts"] == []
    assert original == before
    assert checks and all(check[-1] == "account-fixture" for check in checks)


@pytest.mark.asyncio
async def test_required_disabled_feature_is_not_validated(monkeypatch):
    monkeypatch.setattr(models, "get_catalog", lambda: SimpleNamespace(
        validate_selection=AsyncMock(return_value=descriptor()),
    ))
    monkeypatch.setattr(models, "model_switch_enabled", lambda: True)
    monkeypatch.setattr(models, "resolve_capability", lambda *args, **kwargs: False)
    with pytest.raises(HTTPException) as exc:
        await models.validate_model_selection(models.ModelSelectionRequest(
            provider="openrouter", model="fixture/model", required_capabilities=["web_search"],
        ), {"user_id": "account-fixture"})
    assert exc.value.status_code == 422


def test_global_switch_cannot_advertise_tools(monkeypatch):
    monkeypatch.setattr(models, "resolve_capability", lambda *args, **kwargs: False)
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "0")
    caps = models._effective_descriptor(descriptor(), "account-fixture")["capabilities"]
    assert not caps["tools"] and not caps["stream_events"]
    assert caps["reasoning_efforts"] == []


@pytest.fixture
def real_file_gate_routes(monkeypatch):
    import test_openrouter_attachment_routes as attachments
    import test_openrouter_route_preflight as preflight

    from server.routes import files
    from server.services.dependencies import current_flags

    ctx = attachments.setup.__wrapped__(monkeypatch)
    monkeypatch.setattr(models, "get_catalog", preflight.routes.get_catalog)
    monkeypatch.setenv("OPENVEGAS_MODEL_SWITCH_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "1")
    monkeypatch.setenv("OPENVEGAS_ENABLE_VISION", "1")
    for feature in ("FILE_UPLOAD", "IMAGE_INPUT"):
        monkeypatch.delenv(f"OPENVEGAS_ENABLE_{feature}", raising=False)
        monkeypatch.setenv(f"OPENVEGAS_ROLLOUT_{feature}_PCT", "100")
    service = SimpleNamespace(upload_init=AsyncMock(return_value={"upload_id": "synthetic-owned-upload"}))
    monkeypatch.setattr(files, "get_file_upload_service", lambda: service)
    ctx.app.include_router(models.router)
    ctx.app.include_router(files.router)
    current_flags.cache_clear()
    try:
        yield ctx, service
    finally:
        current_flags.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration, allowed", [
    ("unset", False), ("explicit_off", False), ("direct_override", False),
    ("enabled", True), ("global_off", False), ("rollout_off", False),
    ("cached_off", False),
])
async def test_real_file_gate_matches_descriptor_and_required_validation(real_file_gate_routes, monkeypatch, configuration, allowed):
    import httpx
    import test_openrouter_attachments as attachments

    from server.services.dependencies import current_flags

    ctx, service = real_file_gate_routes
    if configuration == "unset":
        monkeypatch.delenv("OPENVEGAS_ENABLE_FILES", raising=False)
    else:
        monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "0" if configuration in {"explicit_off", "direct_override", "cached_off"} else "1")
    if configuration == "direct_override":
        monkeypatch.setenv("OPENVEGAS_ENABLE_FILE_UPLOAD", "1")
    elif configuration == "global_off":
        monkeypatch.setenv("OPENVEGAS_FEATURES_ENABLED", "0")
    elif configuration == "rollout_off":
        monkeypatch.setenv("OPENVEGAS_ROLLOUT_FILE_UPLOAD_PCT", "0")
        monkeypatch.setenv("OPENVEGAS_ROLLOUT_IMAGE_INPUT_PCT", "0")
    elif configuration == "cached_off":
        assert not current_flags().files_enabled
        monkeypatch.setenv("OPENVEGAS_ENABLE_FILES", "1")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.app), base_url="http://fixture") as client:
        selection = {"provider": "openrouter", "model": attachments.MODEL}
        result = await client.post("/models/validate", json=selection)
        assert result.status_code == 200, result.text
        caps = result.json()["model"]["capabilities"]
        assert caps["file_upload"] is allowed and caps["image_input"] is allowed
        for required in ("file_upload", "image_input"):
            result = await client.post("/models/validate", json={**selection, "required_capabilities": [required]})
            assert result.status_code == (200 if allowed else 422), result.text
        # The upload endpoint is model-independent; account/model gates may be
        # stricter, but no descriptor may approve its disabled transport.
        upload = await client.post("/files/upload/init", json={
            "filename": "fixture.txt", "size_bytes": 3, "mime_type": "text/plain", "sha256": "a" * 64,
        })
    if not current_flags().files_enabled:
        assert upload.status_code == 503 and upload.json()["error"] == "feature_disabled"
        service.upload_init.assert_not_awaited()
    else:
        assert upload.status_code == 200
        service.upload_init.assert_awaited_once()


@pytest.fixture(autouse=True)
def reset_descriptor_feature_flag_cache():
    from server.services.dependencies import current_flags

    current_flags.cache_clear()
    yield
    current_flags.cache_clear()
