import os
import socket
import threading

import pytest

from openvegas.integrations import setup_progress as progress


def test_no_launcher_is_noop(monkeypatch):
    monkeypatch.delenv('OPENVEGAS_LAUNCH_NOTIFY_FD', raising=False)
    monkeypatch.setattr(os, 'write', lambda *a: pytest.fail('unexpected descriptor access'))
    progress.launcher_ready()


def test_loading_ack_precedes_prompt(monkeypatch):
    parent, child = socket.socketpair()
    original_read, original_write, original_close = os.read, os.write, os.close
    monkeypatch.setenv('OPENVEGAS_LAUNCH_NOTIFY_FD', '3')
    monkeypatch.setattr(os, 'write', lambda fd, b: original_write(child.fileno(), b))
    monkeypatch.setattr(os, 'read', lambda fd, n: original_read(child.fileno(), n))
    monkeypatch.setattr(os, 'close', lambda fd: None)
    real_select = progress.select.select
    monkeypatch.setattr(progress.select, 'select', lambda *a: real_select([child], [], [], 2))
    events = []
    def launcher():
        assert parent.recv(64) == b'ready\n'
        events.append('spinner cleared')
        parent.send(b'1')
    thread = threading.Thread(target=launcher)
    thread.start()
    progress.launcher_ready()
    events.append('prompt')
    thread.join(timeout=2)
    parent.close()
    child.close()
    assert events == ['spinner cleared', 'prompt']
    assert 'OPENVEGAS_LAUNCH_NOTIFY_FD' not in os.environ


def test_quiet_non_tty_and_verbose_diagnostics(capsys):
    with progress.setup_progress('Connecting') as emit:
        emit('Approve matching code')
    assert capsys.readouterr().out == 'Approve matching code\n'
    with progress.setup_progress('Connecting', verbose=True):
        pass
    assert capsys.readouterr().out == 'Connecting...\n'
