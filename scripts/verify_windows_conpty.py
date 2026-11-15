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
        'GITHUB_EVENT_NAME': 'push', 'GITHUB_WORKFLOW': 'Windows stock managed critical QA (offline)',
        'GITHUB_REF': 'refs/heads/codex/qa-win-stock-critical',
        'OV_WINDOWS_CONPTY_ISOLATED': '1',
    }
    if sys.platform != 'win32' or any(os.environ.get(k) != v for k, v in required.items()):
        raise RuntimeError('This probe is restricted to the isolated Windows QA job.')


class Failure(Enum):
    BLANK = 'post-login-empty-frame'
    SANDBOX_TEXT = 'post-login-sandbox-text'
    SCREEN_TEXT = 'post-login-accessibility-text'
    MODEL_TEXT = 'post-login-model-selection'
    TRUST_TEXT = 'post-login-folder-access'
    TASK_TEXT = 'post-login-task-list'
    ERROR_TEXT = 'post-login-error-text'
    SELECT_TEXT = 'post-login-numbered-menu'
    CONFIRM_TEXT = 'post-login-enter-instruction'
    TITLE = 'native-title-without-editor'
    FG_ZERO = "foreground-exit-zero"
    FG_ONE = "foreground-exit-one"
    FG_TWO = "foreground-exit-two"
    FG_OTHER = "foreground-exit-other"
    SHELL_MENTION = "shell-setup-message"

    MISSING = "file-or-command-not-found"
    INVALID = "startup-argument-invalid"

    EDITOR = 'editor-not-stable'
    FOOTER = 'editor-footer-missing'
    ECHO = 'editor-echo-missing'
    READ = 'conpty-read-error'
    RENDER = 'terminal-decoder-error'
    KNOWNFOLDER = 'windows-known-folder-error'
    ACCESS = 'filesystem-access-denied'
    SANDBOX = 'sandbox-setup-dialog'
    READER = 'screen-reader-dialog'
    STARTUP = 'session-start-error'
    NOENV = 'environment-value-missing'
    PANIC = 'native-panic'
    SHELL = 'shell-discovery-error'
    LOCAL_GATE = 'managed-ownership-selection-routing-refused'
    MANAGED_START = 'managed-billing-unavailable'
    HOST_MISSING = 'pinned-host-entry-not-found'
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
    NOT_REACHED = 'supplier-not-reached'
    PROTOCOL = 'supplier-protocol'
    FORWARDED = 'control-as-prompt'
    DRAW = 'answer-not-rendered'
    UNSOLICITED = 'unsolicited-request'
    INPUT = 'prompt-shape'
    EXIT_TIMEOUT = 'exit-timeout'
    EXIT_CODE = 'exit-nonzero'
    BANK = 'bank-receipt'
    LAUNCH_EXCEPTION = 'launcher-cleanup'
    RECEIPT = 'receipt-write'
    BRIDGE = 'bridge-at-exit'
    SESSION_CLOSE = 'session-shutdown'
    SERVER_CLOSE = 'native-process-exit'
    CONSOLE = 'console-restore'
    GRANT_CLOSE = 'grant-close'


class CheckpointMiss(RuntimeError):
    def __init__(self, reason):
        self.reason = Failure(reason)
        super().__init__('Missing native checkpoint: ' + self.reason.value)


def pane_failure(frame):
    text = ' '.join(frame.casefold().split())
    if not text:
        return Failure.BLANK
    for reason, cues in (
        (Failure.TRUST_TEXT, ('folder access', 'trust and continue', 'open restricted', 'trust this')),
        (Failure.SCREEN_TEXT, ('screen reader', 'accessibility', 'reduced motion')),
        (Failure.MODEL_TEXT, ('try new model', 'use existing model', 'just got an upgrade')),
        (Failure.TASK_TEXT, ('agent command center', 'no tasks yet', 'new task')),
        (Failure.SANDBOX_TEXT, ('sandbox',)), (Failure.ERROR_TEXT, ('error:', 'failed to', 'cannot ', 'unable to')),
        (Failure.CONFIRM_TEXT, ('press enter', 'enter to', 'enter continue', 'enter confirm')),
        (Failure.SELECT_TEXT, ('1. ', '2. ')), (Failure.TITLE, ('openai codex',)),
    ):
        if any(cue in text for cue in cues):
            return reason
    return Failure.EDITOR


def screen_failure(stage, frame):
    text = ' '.join(frame.casefold().split())
    if stage in ('claude-start', 'claude-control'):
        for reason, literals in (
            (Failure.SHELL_MENTION, ("git-bash", "git bash", "claude_code_git_bash_path")),
            (Failure.MISSING, ("enoent", "the system cannot find", "is not recognized as", "no such file or directory")),
            (Failure.INVALID, ("einval", "invalid argument", "err_invalid_arg")),
            (Failure.FG_ZERO, ("ov-qa-fg-exit-zero",)), (Failure.FG_ONE, ("ov-qa-fg-exit-one",)),
            (Failure.FG_TWO, ("ov-qa-fg-exit-two",)), (Failure.FG_OTHER, ("ov-qa-fg-exit-other", "ov-qa-fg-exit-notfound", "ov-qa-fg-exit-dllmissing")),

            (Failure.KNOWNFOLDER, ('known folder', 'knownfolder', 'failed to get appdata', 'cannot find the appdata')),
            (Failure.NOENV, ('environment variable not found', 'missing environment variable')),
            (Failure.ACCESS, ('access is denied', 'permission denied', 'os error 5)')),
            (Failure.STARTUP, ('failed to initialize', 'error starting session', 'failed to start session',
                               'failed to start thread', 'error creating thread', 'failed to load configuration',
                               'error loading config', 'failed to spawn', 'failed to resolve codex')),
            (Failure.PANIC, ('panicked at', 'fatal error:', 'unhandled exception')),
            (Failure.READER, ('are you using a screen reader', 'enable screen reader mode?')),
            (Failure.SANDBOX, ('set up the windows sandbox', 'set up your sandbox', 'choose your sandbox')),
            (Failure.SHELL, ('could not find powershell', 'powershell was not found', 'no shell found')),
            (Failure.BASH, ('requires git-bash', 'git bash not found', 'could not find git bash')),
            (Failure.ARGS, ('unknown option', 'unrecognized option', 'invalid option')),
            (Failure.TRUST, ('yes, i trust', 'do you trust', 'trust this folder', 'trust this workspace')),
            (Failure.KEY, ('use this api key', 'use this key instead', 'detected a custom api key')),
            (Failure.LOGIN, ('select login method', 'choose a login method', 'sign in to continue',
                             'sign in with chatgpt', 'sign in with your chatgpt', 'enter your api key')),
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
    def __init__(self, process, *, exit_code=None):
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
        self.exit_code = exit_code
        self.screen = Screen(140, 42)
        self.vt = pyte.Stream(self.screen)
        self.before_input = ''
        self.query_tail = ''
        self.observation = ''
        self.finding = None

    def frame(self):
        return '\n'.join(line.rstrip() for line in self.screen.display)

    def read(self):
        try:
            chunk = self.process.read(16384, blocking=False)
        except (EOFError, OSError, RuntimeError):
            if self.exit_code and self.exit_code() is not None:
                raise CheckpointMiss(self.finding or Failure.EXITED) from None
            raise CheckpointMiss(self.finding or Failure.READ) from None
        if chunk:
            # Preserve only a closed diagnosis across rapid redraws and process exit.
            plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', chunk)
            sample = self.observation + plain
            self.finding = self.finding or screen_failure('claude-start', sample)
            self.observation = sample[-200:]
            output = self.query_tail + chunk
            for query, response in (('\x1b[?u', '\x1b[?0u\x1b[?1;2c'),
                                    ('\x1b]10;?\x07', '\x1b]10;rgb:ffff/ffff/ffff\x07'),
                                    ('\x1b]10;?\x1b\\', '\x1b]10;rgb:ffff/ffff/ffff\x1b\\'),
                                    ('\x1b]11;?\x07', '\x1b]11;rgb:0000/0000/0000\x07'),
                                    ('\x1b]11;?\x1b\\', '\x1b]11;rgb:0000/0000/0000\x1b\\')):
                if output.find(query, max(0, len(self.query_tail) - len(query) + 1)) != -1:
                    self.process.write(response)
            self.query_tail = output[-8:]
            try:
                self.vt.feed(chunk)
            except Exception:
                raise CheckpointMiss(self.finding or Failure.RENDER) from None
        return chunk

    def expect(self, pattern, stage, *, timeout=45, diagnose=None):
        deadline = time.monotonic() + timeout
        regex = re.compile(pattern, re.I)
        old = Counter(m.group() for m in regex.finditer(self.before_input))
        reason = None
        while time.monotonic() < deadline:
            frame = self.frame()
            if diagnose and self.finding:
                raise CheckpointMiss(self.finding)
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
                reason = self.finding or reason or Failure.EXITED
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
        if self.exit_code is None:
            raise CheckpointMiss(Failure.EXIT_CODE)
        while (status := self.exit_code()) is None:
            if time.monotonic() >= deadline:
                raise CheckpointMiss(Failure.EXIT_TIMEOUT)
            try:
                # Drain only: rendering here can send a terminal query response
                # to a console that's already closed. Neither EOF nor a closed
                # pipe proves that the supervisor exited successfully.
                self.process.read(16384, blocking=False)
            except (EOFError, OSError, RuntimeError):
                pass
            time.sleep(.05)
        if status != 0:
            raise CheckpointMiss({23: Failure.BANK, 26: Failure.LAUNCH_EXCEPTION, 31: Failure.RECEIPT,
                                  32: Failure.BRIDGE, 33: Failure.SESSION_CLOSE, 34: Failure.SERVER_CLOSE,
                                  35: Failure.CONSOLE, 36: Failure.GRANT_CLOSE}.get(status, Failure.EXIT_CODE))


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
    dll.WaitForSingleObject.argtypes = (W.HANDLE, W.DWORD)
    dll.WaitForSingleObject.restype = W.DWORD
    dll.GetExitCodeProcess.argtypes = (W.HANDLE, ctypes.POINTER(W.DWORD))
    dll.GetExitCodeProcess.restype = W.BOOL
    dll.GetCurrentProcess.argtypes = ()
    dll.GetCurrentProcess.restype = W.HANDLE
    dll.DuplicateHandle.argtypes = (W.HANDLE, W.HANDLE, W.HANDLE, ctypes.POINTER(W.HANDLE),
                                     W.DWORD, W.BOOL, W.DWORD)
    dll.DuplicateHandle.restype = W.BOOL
    job = dll.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = PTY(140, 42, backend=Backend.ConPTY)
    owned = False
    retained = W.HANDLE()
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
        here = dll.GetCurrentProcess()
        if not dll.DuplicateHandle(here, process.fd, here, ctypes.byref(retained),
                                   0x00101000, False, 0):  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            raise CheckpointMiss(Failure.EXIT_CODE)

        def exit_code():
            result = dll.WaitForSingleObject(retained, 0)
            if result == 258:  # WAIT_TIMEOUT: still running
                return None
            if result != 0:
                raise CheckpointMiss(Failure.EXIT_CODE)
            code = W.DWORD()
            if not dll.GetExitCodeProcess(retained, ctypes.byref(code)):
                raise CheckpointMiss(Failure.EXIT_CODE)
            return code.value

        terminal = Terminal(process, exit_code=exit_code)
        terminal.expect(r'OV-QA-READY', 'private console startup')
        # Child cannot start the host or its helpers until job ownership is established.
        terminal.type_line('OV-QA-START')
        yield terminal
    finally:
        try:
            if owned and not dll.TerminateJobObject(job, 1):
                raise RuntimeError('Cannot reap the contained test process tree.')
        finally:
            if retained:
                dll.CloseHandle(retained)
            dll.CloseHandle(job)
