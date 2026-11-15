"""Require live, stable native editor and fresh input echo before submission."""
import re
import time

SPACE = r"[^\S\r\n]"
PROMPT = r"(?m)^" + SPACE + r"*(?:[\u2502\u2503]" + SPACE + r"*)?[\u203a\u276f>]" + SPACE + "*"
FOOTERS = {"codex": r"\? for shortcuts|shortcuts for help|[0-9]{1,3}% context left",
           "claude": r"\? for shortcuts|shortcuts for help|Type your message"}


def ready(frame, host):
    if re.search(r"(?im)^\s*[>\u203a\u276f]\s*\d+[.)]\s|press enter to|enter.{0,3}continue|trust this|folder access|select a|choose a", frame):
        return False
    if re.search(FOOTERS[host], frame, re.I) and re.search(PROMPT, frame):
        return True
    # The green public driver uses the native title then settles, not a mandatory
    # footer. Only accept a native chat glyph (never an ASCII menu selection).
    title = r"OpenAI\s+Codex" if host == "codex" else r"Claude\s+Code|Sonnet\s+4\.6"
    return bool(re.search(title, frame, re.I) and
                                    re.search(r"(?m)^\s*[\u203a\u276f](?!\s*\d+[.)])(?:\s|$)", frame))


def enter(term, text, checkpoint, purpose, *, host="codex", before_submit=None):
    from scripts import verify_windows_conpty as pty

    def guard():
        frame = term.frame()
        if issue := (term.finding or pty.screen_failure("claude-start", frame)):
            raise pty.CheckpointMiss(issue)
        return frame

    checkpoint(purpose + "-composer")
    end, since = time.monotonic() + 18, None
    chosen = False

    while time.monotonic() < end:
        term.read()
        if host == "codex" and purpose in ("named-rename", "turn-0") and not chosen:
            from scripts.windows_sandbox_choice import choice, select
            if choice(term.frame()) is not None:
                select(term, checkpoint)
                chosen = True
                checkpoint(purpose + "-restricted-composer")
                end, since = time.monotonic() + 30, None
                continue

        frame = guard()
        if ready(frame, host):
            if since is None:
                since = time.monotonic()
            if time.monotonic() - since >= .75:
                break
        else:
            since = None
        time.sleep(.025)
    else:
        if hasattr(pty, 'pane_failure'):
            raise pty.CheckpointMiss(pty.pane_failure(frame))
        raise pty.CheckpointMiss(pty.Failure.EDITOR if re.search(FOOTERS[host], frame, re.I)
                                 else pty.Failure.FOOTER)
    checkpoint(purpose + "-echo")
    term.before_input = frame
    term.process.write(text)
    try:
        term.expect(PROMPT + re.escape(text) + r"(?=[^\S\n]|[\u2588\u258f\u2502]|$)",
                    "fresh text in editor", timeout=8, diagnose="claude-start")
    except pty.CheckpointMiss as exc:
        if exc.reason is pty.Failure.UNKNOWN:
            raise pty.CheckpointMiss(pty.Failure.ECHO) from None
        raise
    time.sleep(.15)
    term.read()
    term.before_input = guard()
    if purpose == "named-rename":
        # Never submit a bare slash command or accept a suggestion in its place.
        if not (re.fullmatch(r"/rename ov-qa-[0-9a-f]{12}", text) and
                re.search(PROMPT + re.escape(text) + r"[\u2588\u258f]?[ \t]*(?:[\u2502\u2503][ \t]*)?$",
                          term.before_input)):
            raise pty.CheckpointMiss(pty.Failure.ECHO)
    checkpoint(purpose + "-response")
    if before_submit:
        before_submit()
    term.process.write("\r")
