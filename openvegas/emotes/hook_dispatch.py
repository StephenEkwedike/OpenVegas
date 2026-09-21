"""Early, passive packaged hook dispatch. Never import the application CLI.

Frozen entry points must route --openvegas-emote-hook here before their normal
imports. The installer probes that exact path before writing any settings.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from pathlib import Path

PROBE = "openvegas-hook-dispatch-v2\n"


def main(args: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    if args == ["probe"]:
        try:
            with (open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet),
                  contextlib.redirect_stderr(quiet)):
                from .hooks import handle_input as _handler

                if not callable(_handler):
                    return 1
        except (Exception, KeyboardInterrupt, SystemExit):  # noqa: BLE001 - bounded probe fails closed
            return 1
        sys.stdout.write(PROBE)
        return 0
    # No argparse/Click diagnostics or arbitrary application subcommands.
    if (len(args) != 5 or args[0] != "handle" or args[1] != "--installation"
            or args[3] != "--owner"):
        return 0
    old_handler = None
    previous_timer = None
    try:
        # Below the native host's two-second timeout, including stdin/IPC stalls.
        # Exit successfully on timeout: never feed rejection feedback to an agent.
        if os.name == "posix" and hasattr(signal, "setitimer"):
            old_handler = signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
            previous_timer = signal.setitimer(signal.ITIMER_REAL, 1.5)
        with (open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet),
              contextlib.redirect_stderr(quiet)):
            from .hooks import handle_input, read_input

            handle_input(read_input(), installation=Path(args[2]), owner=args[4])
    except (Exception, KeyboardInterrupt, SystemExit):  # noqa: BLE001 - never alter agent control flow
        return 0
    finally:
        if previous_timer is not None:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, old_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
