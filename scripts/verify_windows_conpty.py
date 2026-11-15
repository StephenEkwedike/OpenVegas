"""Test-only driver for a private Windows pseudoconsole. Never attaches to an existing terminal."""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from enum import Enum
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


class Failure(Enum):
    EXITED = 'child-exited'
    UNKNOWN = 'no-reply'
    TRUST = 'trust-dialog'
    LOGIN = 'login-dialog'
    KEY = 'api-key-dialog'
    SETUP = 'startup-dialog'
    BASH = 'git-bash-error'
    ARGS = 'native-option-error'
    BUSY = 'switch-busy'
    HELD = 'switch-held'
    REFUSED = 'switch-refused'
    ROUTE = 'native-route'
    INVENTORY = 'native-inventory'
    ROOT = 'native-root'
    MODEL = 'native-model'
    WORKSPACE = 'native-workspace'
    FENCE = 'native-fence'
    LEASE = 'session-lease'
    COMMAND = 'command-unavailable'
    WALLET_PREPARE = 'wallet-prepare'
    WALLET_OUTCOME = 'wallet-outcome'
    REQUEST = 'request-boundary'


class CheckpointMiss(RuntimeError):
    def __init__(self, reason):
        self.reason = Failure(reason)
        super().__init__('Missing native checkpoint: ' + self.reason.value)


def screen_failure(stage, frame):
    text = ' '.join(frame.casefold().split())
    if stage in ('claude-start', 'claude-control'):
        for reason, literals in (
            (Failure.BASH, ('requires git-bash', 'git bash not found', 'could not find git bash')),
            (Failure.ARGS, ('unknown option', 'unrecognized option', 'invalid option')),
            (Failure.TRUST, ('yes, i trust', 'do you trust', 'trust this folder', 'trust this workspace')),
            (Failure.KEY, ('use this api key', 'use this key instead', 'detected a custom api key')),
            (Failure.LOGIN, ('select login method', 'choose a login method', 'sign in to continue')),
            (Failure.SETUP, ('choose the text style', 'choose a theme', 'select a theme')),
        ):
            if any(s in text for s in literals):
                return reason
        if stage == 'claude-control' and 'ov-billing' in text and any(
                s in text for s in ('unknown command', 'unknown slash command', 'unknown skill',
                                    'command not found', 'unrecognized command')):
            return Failure.COMMAND
    if stage in ('switch', 'claude-control'):
        for reason in (Failure.ROUTE, Failure.INVENTORY, Failure.ROOT, Failure.MODEL,
                       Failure.WORKSPACE, Failure.FENCE, Failure.LEASE, Failure.WALLET_PREPARE,
                       Failure.WALLET_OUTCOME, Failure.REQUEST):
            if 'control check: ' + reason.value + '.' in text:
                return reason
        if 'local control busy' in text or 'billing control busy' in text:
            return Failure.BUSY
        if 'inference held' in text:
            return Failure.HELD
        if 'switch refused' in text or 'local control unconfirmed' in text:
            return Failure.REFUSED
    return None


class Terminal:
    def __init__(self, process):
        import pyte

        class Screen(pyte.Screen):
            def write_process_input(self, data):
                process.write(data.replace('\x9b', '\x1b['))

            def report_device_status(self, mode=0, **kwargs):
                if not kwargs.get('private'):
                    super().report_device_status(mode)

            def set_mode(self, *modes, **kwargs):
                if kwargs.get('private') and set(modes) & {47, 1047, 1049}:
                    self.erase_in_display(2)
                super().set_mode(*modes, **kwargs)

            def reset_mode(self, *modes, **kwargs):
                if kwargs.get('private') and set(modes) & {47, 1047, 1049}:
                    self.erase_in_display(2)
                super().reset_mode(*modes, **kwargs)

        self.process = process
        self.screen = Screen(140, 42)
        self.vt = pyte.Stream(self.screen)
        self.before_input = ''
        self.query_tail = ''

    def frame(self):
        return '\n'.join(line.rstrip() for line in self.screen.display)

    def read(self):
        chunk = self.process.read(16384, blocking=False)
        if chunk:
            output = self.query_tail + chunk
            for query, response in (('\x1b[?u', '\x1b[?0u'),
                                    ('\x1b]11;?\x07', '\x1b]11;rgb:0000/0000/0000\x07'),
                                    ('\x1b]11;?\x1b\\', '\x1b]11;rgb:0000/0000/0000\x1b\\')):
                if output.find(query, max(0, len(self.query_tail) - len(query) + 1)) != -1:
                    self.process.write(response)
            self.query_tail = output[-8:]
            self.vt.feed(chunk)
        return chunk

    def expect(self, pattern, stage, *, timeout=45, diagnose=None):
        deadline = time.monotonic() + timeout
        regex = re.compile(pattern, re.I)
        old = Counter(m.group() for m in regex.finditer(self.before_input))
        reason = None
        while time.monotonic() < deadline:
            frame = self.frame()
            observed = screen_failure(diagnose, frame) if diagnose else None
            if observed and diagnose in ('claude-start', 'claude-control') and observed in (
                    Failure.TRUST, Failure.LOGIN, Failure.KEY, Failure.SETUP, Failure.ARGS, Failure.BASH):
                # Never send keystrokes to a native trust or authentication dialog.
                raise CheckpointMiss(observed)
            if observed and (reason is None or observed not in (Failure.REFUSED, Failure.HELD)):
                reason = observed
            visible = Counter(m.group() for m in regex.finditer(frame))
            fresh = visible - old
            if fresh:
                return next(iter(fresh))
            old &= visible
            try:
                if self.read():
                    continue
            except (EOFError, OSError):
                if self.process.isalive():
                    raise
            if not self.process.isalive():
                reason = reason or Failure.EXITED
                break
            time.sleep(.025)
        # Do not put transcripts, paths, credentials, or prompts in pytest/JUnit.
        if diagnose:
            raise CheckpointMiss(reason or Failure.UNKNOWN)
        raise RuntimeError('Missing terminal checkpoint: ' + stage)

    def type_line(self, line):
        if not line.isascii() or not line.isprintable():
            raise ValueError('Expected a printable QA input line.')
        self.before_input = self.frame()
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
