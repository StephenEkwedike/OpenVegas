"""Transient setup feedback; never changes stdin or the child's TTY."""
import os
import select
import sys
from contextlib import contextmanager

import click
from rich.console import Console


def launcher_ready():
    # The npm launcher owns this private pipe. Acknowledge the cleared spinner
    # before Click prints a prompt, so startup feedback cannot erase user input.
    fd = os.environ.pop("OPENVEGAS_LAUNCH_NOTIFY_FD", "")
    if fd != "3" or os.name != "posix":
        return
    try:
        os.write(3, b"ready\n")
        if select.select([3], [], [], 2)[0]:
            os.read(3, 1)
    except OSError:
        pass
    finally:
        try:
            os.close(3)
        except OSError:
            pass


@contextmanager
def setup_progress(label, *, verbose=False):
    console = Console(stderr=True)
    if verbose:
        click.echo(label + "...")
    if not verbose and sys.stderr.isatty() and not os.environ.get("CI"):
        with console.status(label, spinner="dots", spinner_style="blue"):
            yield lambda text: console.print(text, markup=False, highlight=False)
    else:
        yield click.echo
