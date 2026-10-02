"""Copyable commands pinned to the running CLI installation."""

import shlex
import sys


def cli_argv(*args: str) -> list[str]:
    prefix = [sys.executable]
    if not getattr(sys, "frozen", False):
        prefix.extend(["-m", "openvegas.cli"])
    return [*prefix, *args]


def cli_command(*args: str) -> str:
    argv = cli_argv(*args)
    if sys.platform == "win32":
        # PowerShell requires its invocation operator for quoted executable paths.
        return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv)
    return shlex.join(argv)
