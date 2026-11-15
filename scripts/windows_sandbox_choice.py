"""Only the optional stock restricted-token sandbox in the disposable Windows QA."""
import re
import time

ADMIN="Set up default sandbox (requires Administrator permissions)"
LIMITED="Use non-admin sandbox (higher risk if prompt injected)"


def choice(frame):
    words=" ".join(frame.casefold().split())
    if "set up the codex agent sandbox" not in words or "protect your files and control network access" not in words:
        return None
    if any(c in words for c in ("organization requires","full access","disable sandbox","trust and continue","try setting up admin")):
        return None
    if not ("enter select" in words and "esc back" in words):
        return None
    opts=re.findall(r"(?m)^\s*([\u203a\u276f>]?)[^\S\n]*(\d+)\. ([^\n]+?)\s*$",frame)
    labels=[(index,label.strip()) for _,index,label in opts]
    if labels not in ([("1",ADMIN),("2",LIMITED),("3","Quit")],[("1",LIMITED),("2","Quit")]):
        return None
    selected=[label for glyph,_,label in opts if glyph]
    if selected==[ADMIN]: return "down"
    if selected==[LIMITED]: return "confirm"
    return None


def select(term, checkpoint):
    from scripts import verify_windows_conpty as pty
    pty.require_hosted_lane()
    previous=None
    for _ in range(2):
        action=choice(term.frame())
        if action is None or action==previous:
            raise pty.CheckpointMiss(pty.Failure.SANDBOX)
        time.sleep(.2)
        term.read()
        if choice(term.frame())!=action:
            raise pty.CheckpointMiss(pty.Failure.SANDBOX)
        if action=="down":
            checkpoint("optional-sandbox-select")
            term.process.write("\x1b[B")
        else:
            checkpoint("optional-sandbox-confirm")
            term.process.write("\r")
        end=time.monotonic()+10
        while time.monotonic()<end:
            term.read()
            if choice(term.frame())!=action: break
            time.sleep(.025)
        else: raise pty.CheckpointMiss(pty.Failure.SANDBOX)
        if action=="confirm": return
        previous=action
    raise pty.CheckpointMiss(pty.Failure.SANDBOX)
