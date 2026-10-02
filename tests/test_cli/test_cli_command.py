import shlex

from openvegas import cli_command as commands


def test_python_installation_and_spaces(monkeypatch):
    monkeypatch.setattr(commands.sys, "executable", "/my env/bin/python")
    monkeypatch.setattr(commands.sys, "frozen", False, raising=False)
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    assert shlex.split(commands.cli_command("emote", "setup")) == [
        "/my env/bin/python", "-m", "openvegas.cli", "emote", "setup"
    ]


def test_frozen_installation(monkeypatch):
    monkeypatch.setattr(commands.sys, "executable", "/app/openvegas")
    monkeypatch.setattr(commands.sys, "frozen", True, raising=False)
    assert commands.cli_argv("emote") == ["/app/openvegas", "emote"]


def test_powershell_quotes(monkeypatch):
    monkeypatch.setattr(commands.sys, "executable", "C:\\Jane's Apps\\openvegas.exe")
    monkeypatch.setattr(commands.sys, "platform", "win32")
    monkeypatch.setattr(commands.sys, "frozen", True, raising=False)
    assert commands.cli_command("emote") == "& 'C:\\Jane''s Apps\\openvegas.exe' 'emote'"
