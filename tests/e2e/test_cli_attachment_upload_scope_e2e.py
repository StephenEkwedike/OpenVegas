from __future__ import annotations

from click.testing import CliRunner

from openvegas.cli import cli
from openvegas.client import APIError


class _FakeAttachmentClient:
    instances: list["_FakeAttachmentClient"] = []

    def __init__(self):
        self.thread_id = "thread-attach"
        self.run_id = "run-attach"
        self.run_version = 0
        self.signature = "sha256:" + ("b" * 64)
        self.upload_init_calls = 0
        self.upload_complete_calls = 0
        _FakeAttachmentClient.instances.append(self)

    async def get_mode(self):
        return {"conversation_mode": "persistent"}

    async def list_models(self, _provider: str | None = None):
        return {"models": [{"model_id": "gpt-5", "enabled": True}]}

    async def agent_run_create(self, **_kwargs):
        return {
            "run_id": self.run_id,
            "run_version": self.run_version,
            "valid_actions_signature": self.signature,
        }

    async def agent_register_workspace(self, **_kwargs):
        return {"ok": True}

    async def ide_get_context(self, **_kwargs):
        raise RuntimeError("no ide bridge")

    async def upload_init(self, **_kwargs):
        self.upload_init_calls += 1
        return {"upload_id": f"up_{self.upload_init_calls}"}

    async def upload_complete(self, **_kwargs):
        self.upload_complete_calls += 1
        return {"file_id": f"file_{self.upload_complete_calls}", "status": "uploaded"}

    async def ask(self, prompt, _provider, _model, **_kwargs):
        _ = prompt
        return {"thread_id": self.thread_id, "text": "Processed.", "v_cost": "0.01"}

    async def get_balance(self):
        return {"balance": "1000.000000", "balance_v": "1000.000000"}

    async def suggest_topup(self, suggested_topup_usd=None):
        _ = suggested_topup_usd
        return {"low_balance": False}


class _FakeAttachmentClientUploadFail(_FakeAttachmentClient):
    def __init__(self):
        super().__init__()
        self.ask_calls = 0

    async def upload_init(self, **_kwargs):
        raise APIError(500, "Internal Server Error")

    async def ask(self, prompt, _provider, _model, **_kwargs):
        _ = prompt
        self.ask_calls += 1
        return {"thread_id": self.thread_id, "text": "Should not run.", "v_cost": "0.01"}


class _FakeAttachmentClientTrack(_FakeAttachmentClient):
    def __init__(self):
        super().__init__()
        self.ask_calls = 0

    async def ask(self, prompt, _provider, _model, **_kwargs):
        _ = prompt
        self.ask_calls += 1
        return {"thread_id": self.thread_id, "text": "Processed.", "v_cost": "0.01"}


def test_cli_attachment_upload_does_not_raise_client_name_error(monkeypatch, tmp_path):
    target = tmp_path / "Color pallette.pdf"
    target.write_bytes(b"%PDF-1.4\n% fake pdf content")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _FakeAttachmentClient)
    monkeypatch.setattr("openvegas.cli.load_config", lambda: {})
    items = iter(
        [
            "Can you see what's in Color pallette.pdf?",
            "/exit",
        ]
    )
    monkeypatch.setattr("openvegas.cli.Prompt.ask", lambda *_args, **_kwargs: next(items))
    runner = CliRunner()
    result = runner.invoke(cli, ["chat"])
    assert result.exit_code == 0, result.output
    assert "name 'client' is not defined" not in result.output
    assert "Uploading {Color pallette.pdf}" in result.output
    inst = _FakeAttachmentClient.instances[-1]
    assert inst.upload_init_calls >= 1
    assert inst.upload_complete_calls >= 1


def test_cli_skips_model_request_when_attachment_upload_fails(monkeypatch, tmp_path):
    target = tmp_path / "Color pallette.pdf"
    target.write_bytes(b"%PDF-1.4\n% fake pdf content")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _FakeAttachmentClientUploadFail)
    monkeypatch.setattr("openvegas.cli.load_config", lambda: {})
    items = iter(
        [
            "Can you see what's in Color pallette.pdf?",
            "/exit",
        ]
    )
    monkeypatch.setattr("openvegas.cli.Prompt.ask", lambda *_args, **_kwargs: next(items))
    runner = CliRunner()
    result = runner.invoke(cli, ["chat"])
    assert result.exit_code == 0, result.output
    assert "Skipped model request for this turn to avoid extra cost" in result.output
    inst = _FakeAttachmentClient.instances[-1]
    assert getattr(inst, "ask_calls", 0) == 0


def test_cli_skips_model_request_when_attachment_type_is_unsupported(monkeypatch, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENVEGAS_CHAT_ALLOWED_MIME", "application/pdf")
    monkeypatch.setattr("openvegas.client.OpenVegasClient", _FakeAttachmentClientTrack)
    monkeypatch.setattr("openvegas.cli.load_config", lambda: {})
    items = iter(
        [
            "/attach notes.txt",
            "Can you analyze this notes.txt?",
            "/exit",
        ]
    )
    monkeypatch.setattr("openvegas.cli.Prompt.ask", lambda *_args, **_kwargs: next(items))
    runner = CliRunner()
    result = runner.invoke(cli, ["chat"])
    assert result.exit_code == 0, result.output
    assert "Unsupported file type for chat attachments" in result.output
    inst = _FakeAttachmentClient.instances[-1]
    assert getattr(inst, "ask_calls", 0) == 0
