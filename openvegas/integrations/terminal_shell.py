"""Session-local shell compatibility; never modify host binaries or user rc files."""
from __future__ import annotations

import os
import shlex
from pathlib import Path

CODEX_FUNCTION = r'''
# A shared Codex daemon cannot inherit this terminal's private game connection.
# Delegate to the installed binary in-process; account/config/arguments stay intact.
if (( ! $+aliases[codex] && ! $+functions[codex] )); then
  function codex {
    local ov_help ov_arg
    local -a ov_flags
    # Explicit host transport choices take precedence over terminal defaults.
    for ov_arg in "$@"; do
      if [[ $ov_arg == --remote || $ov_arg == --remote=* ]]; then
        command codex "$@"
        return $?
      fi
    done
    if [[ -n ${OPENVEGAS_TERMINAL_SOCKET:-} && -n ${OPENVEGAS_TERMINAL_TOKEN:-} ]]; then
      ov_help=$(command codex --help 2>/dev/null)
      if [[ $ov_help == *--no-daemon* ]] && (( ! ${@[(Ie)--no-daemon]} )); then
        ov_flags+=(--no-daemon)
      fi
      # Keep scrollback in the primary buffer so the game can borrow the
      # alternate buffer and restore the coding screen without redrawing it.
      if [[ $ov_help == *--no-alt-screen* ]] && (( ! ${@[(Ie)--no-alt-screen]} )); then
        ov_flags+=(--no-alt-screen)
      fi
      __OPENVEGAS_CODEX_SUPERVISOR__ "${ov_flags[@]}" "$@"
      return $?
    fi
    command codex "$@"
  }
else
  print -u2 'OpenVegas: preserving your custom codex alias/function; use supported --no-daemon and --no-alt-screen options for same-terminal games.'
fi
'''

CLAUDE_FUNCTION = r'''
if (( ! $+aliases[claude] && ! $+functions[claude] )); then
  function claude {
    if [[ -n ${OPENVEGAS_TERMINAL_SOCKET:-} && -n ${OPENVEGAS_TERMINAL_TOKEN:-} ]]; then
      __OPENVEGAS_CLAUDE_SUPERVISOR__ "$@"
    else
      command claude "$@"
    fi
  }
fi
'''


def prepare(shell: list[str], env: dict[str, str], directory: Path) -> dict[str, str]:
    """Use zsh's startup directory only until its normal login startup completes.

    Original startup files still run in order, with their original ZDOTDIR. No
    capabilities are written here; the shell and its children inherit them.
    Non-zsh/custom commands are left alone (including the relay's PTY fixtures).
    """
    if Path(shell[0]).name != "zsh":
        return env
    directory.mkdir(mode=0o700)
    restore = '''
if (( _OPENVEGAS_ZDOTDIR_SET )); then
  ZDOTDIR=$_OPENVEGAS_ZDOTDIR_VALUE
else
  unset ZDOTDIR
fi
'''
    remember = '''
typeset -g _OPENVEGAS_ZDOTDIR_SET=${+ZDOTDIR}
typeset -g _OPENVEGAS_ZDOTDIR_VALUE=${ZDOTDIR-}
'''
    initial = ("ZDOTDIR=" + shlex.quote(env["ZDOTDIR"]) + "\n"
               if "ZDOTDIR" in env else "unset ZDOTDIR\n")
    for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
        script = initial if name == ".zshenv" else restore
        script += f'[[ ! -r "${{ZDOTDIR:-$HOME}}/{name}" ]] || source "${{ZDOTDIR:-$HOME}}/{name}"\n'
        if name == ".zshrc":
            from .installer import host_command
            script += CODEX_FUNCTION.replace('__OPENVEGAS_CODEX_SUPERVISOR__', shlex.join(host_command('codex', env=env)))
            script += CLAUDE_FUNCTION.replace('__OPENVEGAS_CLAUDE_SUPERVISOR__', shlex.join(host_command('claude', env=env)))
        # Leave the user's ZDOTDIR intact after startup, including for logout.
        if name != ".zlogin":
            redirect = remember + "ZDOTDIR=" + shlex.quote(str(directory)) + "\n"
            script += ("if [[ -o login ]]; then\n" + redirect + "fi\n"
                       if name == ".zshrc" else redirect)
        else:
            script += "unset _OPENVEGAS_ZDOTDIR_SET _OPENVEGAS_ZDOTDIR_VALUE\n"
        target = directory / name
        target.write_text(script)
        os.chmod(target, 0o600)
    return {**env, "ZDOTDIR": str(directory)}
