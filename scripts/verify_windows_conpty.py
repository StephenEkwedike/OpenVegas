"""Test-only driver for a private Windows pseudoconsole. Never attaches to an existing terminal."""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def require_hosted_lane():
    required = {
        'CI': 'true', 'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
        'GITHUB_EVENT_NAME': 'push', 'GITHUB_WORKFLOW': 'Windows native billing QA (offline)',
        'GITHUB_REF': 'refs/heads/codex/qa-windows-native-billing-20261114',
        'OV_WINDOWS_CONPTY_ISOLATED': '1',
    }
    if sys.platform != 'win32' or any(os.environ.get(k) != v for k, v in required.items()):
        raise RuntimeError('This probe is restricted to the isolated Windows QA job.')


class Terminal:
    def __init__(self, process):
        self.process = process

    def expect(self, pattern, stage, *, timeout=45):
        deadline = time.monotonic() + timeout
        tail = plain = ''
        while time.monotonic() < deadline:
            chunk = self.process.read(16384, blocking=False)
            if chunk:
                tail = (tail + chunk)[-65536:]
                # Only recognize visible text, not a title or a control sequence.
                plain = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\x1b\[[0-?]*[ -/]*[@-~]', '', tail)
                failure = re.search(r'OV-QA-FAIL ([a-z_0-9:,]{0,600}) END', plain)
                if failure:
                    raise RuntimeError('QA frames: ' + failure[1])
                match = re.search(pattern, plain, re.I)
                if match:
                    return match.group()
                for label, marker in (
                    ('busy', 'Local control busy'), ('refused', 'Switch refused; payer unchanged'),
                    ('held', 'Billing outcome unconfirmed'), ('unconfirmed', 'Local control unconfirmed'),
                    ('trust', 'trust this folder'), ('signin', 'Sign in with ChatGPT'),
                ):
                    if marker.casefold() in plain.casefold():
                        raise RuntimeError('QA screen: ' + label)
            elif not self.process.isalive():
                break
            else:
                time.sleep(.025)
        # Do not put transcripts, paths, credentials, or prompts in pytest/JUnit.
        for label, marker in (('command-missing', 'unknown command'), ('setup', 'choose the text style'),
                              ('api-key-dialog', 'external api key'), ('plugin-trust', 'trust the plugin'),
                              ('git-bash', 'requires git-bash'), ('claude-login', 'select login method')):
            if marker in plain.casefold():
                raise RuntimeError('QA screen: ' + label)
        raise RuntimeError('Missing terminal checkpoint: ' + stage)

    def type_line(self, line):
        if not line.isascii() or not line.isprintable():
            raise ValueError('Expected a printable QA input line.')
        self.process.write(line)
        # Native composers can distinguish a paste from a later Enter key.
        time.sleep(.15)
        self.process.write('\r')

    def require_success(self):
        deadline = time.monotonic() + 15
        while self.process.isalive() and time.monotonic() < deadline:
            self.process.read(16384, blocking=False)
            time.sleep(.05)
        if self.process.get_exitstatus() != 0:
            raise RuntimeError('Managed launcher or synthetic grant audit failed.')


@contextmanager
def isolated_console(script, args, env, cwd):
    """Start only our fixed Python test entry, in an owned, bounded process tree."""
    from ctypes import wintypes as W
    from winpty import Backend, PTY

    require_hosted_lane()
    if not env or Path(env['USERPROFILE']) != Path(cwd).parent:
        raise ValueError('A disposable profile and workspace are required.')
    dll = ctypes.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    dll.CreateJobObjectW.argtypes = (ctypes.c_void_p, W.LPCWSTR)
    dll.CreateJobObjectW.restype = W.HANDLE
    dll.AssignProcessToJobObject.argtypes = (W.HANDLE, W.HANDLE)
    dll.TerminateJobObject.argtypes = (W.HANDLE, W.UINT)
    dll.TerminateProcess.argtypes = (W.HANDLE, W.UINT)
    dll.GetProcessId.argtypes = dll.CloseHandle.argtypes = (W.HANDLE,)
    job = dll.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = PTY(140, 42, backend=Backend.ConPTY)
    owned = False
    try:
        block = '\0'.join(f'{k}={v}' for k, v in sorted(env.items(), key=lambda pair: pair[0].upper())) + '\0'
        cmd = ' ' + subprocess.list2cmdline(['-I', '-B', str(script), *args])
        if not process.spawn(sys.executable, cwd=str(cwd), env=block, cmdline=cmd):
            raise RuntimeError('ConPTY child did not start.')
        if not process.fd or dll.GetProcessId(process.fd) != process.pid:
            raise RuntimeError('ConPTY process handle not verified.')
        if not dll.AssignProcessToJobObject(job, process.fd):
            dll.TerminateProcess(process.fd, 1)
            raise RuntimeError('Cannot contain the test process tree.')
        owned = True
        terminal = Terminal(process)
        terminal.expect(r'OV-QA-READY', 'private console startup')
        # Child cannot start the host or its helpers until job ownership is established.
        terminal.type_line('OV-QA-START')
        yield terminal
    finally:
        try:
            if owned and not dll.TerminateJobObject(job, 1):
                raise RuntimeError('Cannot reap the contained test process tree.')
        finally:
            dll.CloseHandle(job)
