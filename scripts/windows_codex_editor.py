"""Wait for actual ConPTY editor state; first paint alone is not evidence of editability."""
import re
import time

PROMPT = r"(?m)^[ \t]*(?:[\u2502\u2503][ \t]*)?[\u203a\u276f>][ \t]*"


FOOTER = r"\? for shortcuts|shortcuts for help|[0-9]{1,3}% context left"


def enter(term, text, checkpoint, purpose):
    from scripts import verify_windows_conpty as pty

    checkpoint(purpose + "-composer")
    if not re.search(FOOTER, term.frame(), re.I):
        term.before_input = ""
        term.expect(FOOTER, "Codex composer footer", timeout=45, diagnose="claude-start")
    # Drain transitional frames. Do not type on a title, a trust modal or a signin prompt.
    until = time.monotonic() + .75
    while time.monotonic() < until:
        term.read()
        if issue := pty.screen_failure("claude-start", term.frame()):
            raise pty.CheckpointMiss(issue)
        time.sleep(.025)
    frame = term.frame()
    if not re.search(FOOTER, frame, re.I) or not re.search(PROMPT, frame):
        raise RuntimeError("Codex editor not established")
    checkpoint(purpose + "-echo")
    term.before_input = frame
    term.process.write(text)
    term.expect(PROMPT + re.escape(text) + r"(?=[ \t\u2588\u258f\u2502]|$)",
                "fresh text in the native editor", timeout=7, diagnose="claude-start")
    if issue := pty.screen_failure("claude-start", term.frame()):
        raise pty.CheckpointMiss(issue)
    checkpoint(purpose + "-response")
    term.before_input = term.frame()
    term.process.write("\r")
