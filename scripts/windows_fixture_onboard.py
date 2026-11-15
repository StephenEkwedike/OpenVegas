"""Exact Codex 0.166.0 UI only. Never type a key, open a browser, or grant trust."""
import re
import time

from scripts import verify_windows_conpty as pty

API = r"(?:Provide your own API key|Use an OpenAI API key)"
KEY = "sk-openvegas-native-fixture"
MENU = re.compile(r"(?m)^\s*>?[^\S\n]*3\. " + API + r"[^\S\n]*$")
PREFILLED = re.compile(r"(?m)^\s*[\u2502|]\s*" + re.escape(KEY) + r"[^\S\n]*[\u2502|]\s*$")


def decision(stage, frame):
    low = frame.casefold()
    if any(s in low for s in ("trust and continue", "trust this folder", "continue in your browser",
                              "open this url", "api key login is disabled", "device code:", "failed to save api key")):
        return "stop"
    if stage == "menu":
        if (MENU.search(frame) and "1. Sign in with ChatGPT" in frame and "Pay for what you use" in frame
                and "press enter to continue" in low):
            return "3"
    if stage == "prefill":
        if ("Use your own OpenAI API key for usage-based billing" in frame and PREFILLED.search(frame)
                and "Detected OPENAI_API_KEY environment variable." in frame and "press enter to save" in low):
            return "\r"
    if stage == "saved":
        if "api key configured" in low and "usage-based billing with your api key" in low:
            return "done"
        # The next onboarding pane can replace the ephemeral "configured" frame.
        if MENU.search(frame) is None and "press enter to save" not in low and "sign in with chatgpt" not in low:
            if "? for shortcuts" in low and re.search(r"(?m)^\s*[\u203a\u276f>] ", frame):
                return "done"
    return None


def run(term, checkpoint):
    pty.require_hosted_lane()
    for stage in ("menu", "prefill", "saved"):
        checkpoint("codex-key-" + stage)
        limit, prior, since = time.monotonic() + 20, None, None
        while time.monotonic() < limit:
            term.read()
            f = term.frame()
            action = decision(stage, f)
            if action == "stop" or (issue := pty.screen_failure("claude-start", f)) not in (None, pty.Failure.LOGIN):
                raise pty.CheckpointMiss(pty.screen_failure("claude-start", f) or pty.Failure.SETUP)
            if action:
                if prior != action:
                    prior, since = action, time.monotonic()
                if action == "done" or time.monotonic() - since >= .2:
                    if action != "done":
                        term.process.write(action)
                    break
            else:
                prior, since = None, None
            time.sleep(.025)
        else:
            raise pty.CheckpointMiss(pty.Failure.UNKNOWN)
    # Clear only a consumed login observation, never a different fault.
    if term.finding is pty.Failure.LOGIN:
        term.finding = None
        term.observation = ""
    term.before_input = ""
