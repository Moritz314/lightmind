"""Where a chat opens: in this window, or in a new tab or window of the terminal.

`plan_open(home, id)` reads `sessions.open` of config.md (`tab`, `window` or `replace`, default `tab`) and looks at the
terminal it runs in; `plan(...)` is the same as a pure function of its arguments, so every case can be tested without
starting anything.  The answer is a `Plan`: the command (an argument list, never a shell string) that starts
`<tree>/system/lm open <id>` in the new place, or `replace` when the chat stays here.  `spawn(plan, tree)` starts it.

A chat that goes to another place is created here (so the choices of the setup card are kept) and then handed over
(`Session.hand_over`): the new process resumes it by its id.

Which terminal: tmux first (`TMUX` is set), then Windows Terminal (`WT_SESSION`, unless `TERM_PROGRAM` says vscode, whose
terminal inherits the variable without being Windows Terminal), else none.  Over ssh on a server the tmux of the permanent
session is what answers.  Ids and names are `[A-Za-z0-9._-]`, and the first character is a letter or digit (it never
looks like an option).

May import: files.  Nothing else.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .files import Home

WANTS = ("tab", "window", "replace")
DEFAULT_WANT = "tab"
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")          # a session id, an agent or a project name
_CMD_SPECIAL = re.compile(r'[&|<>^%!()";]')               # characters cmd.exe (or wt.exe: `;`) would read as syntax


@dataclass(frozen=True)
class Plan:
    """`how`: replace (here) | tmux | wt (Windows Terminal) | cmd (a new console window).  `argv` starts the new place
    (empty for replace).  `what`: the word for the message ("tab", "window").  `note`: a line for the person when the
    wish (`sessions.open`) could not be met as asked; empty otherwise."""
    how: str
    argv: tuple[str, ...] = ()
    what: str = ""
    note: str = ""

    @property
    def separate(self) -> bool:
        return self.how != "replace"


def terminal(env: Mapping[str, str]) -> str:
    """`tmux`, `wt` or `none`: what can open a tab here.  tmux wins (a tmux inside Windows Terminal is tmux)."""
    if env.get("TMUX"):
        return "tmux"
    if env.get("WT_SESSION") and env.get("TERM_PROGRAM", "").strip().lower() != "vscode":
        return "wt"
    return "none"


def launcher(tree: Path, platform: str) -> Path:
    return tree / "system" / ("lm.cmd" if platform == "win32" else "lm")


def want(home: Home) -> str:
    """`sessions.open` of the settings; a word that is none of the three counts as the default (`lm check` says so)."""
    sec = home.settings.get("sessions")
    v = str(sec.get("open") or "") if isinstance(sec, dict) else ""
    v = v.strip().lower()
    return v if v in WANTS else DEFAULT_WANT


def plan(tree: Path, ref: str, wish: str, env: Mapping[str, str], platform: str,
         exists: Callable[[Path], bool] = Path.is_file) -> Plan:
    """The command that opens chat `ref` the way `wish` says in this terminal, or `replace` (with a note when that was
    not what was asked for).  `ref` that is not `[A-Za-z0-9._-]` is a ValueError: nothing a person types reaches a command
    line unchecked."""
    if not SAFE.fullmatch(ref or ""):
        raise ValueError(f"{ref!r} is not a chat id: only letters, digits, . _ - are allowed")
    wish = wish if wish in WANTS else DEFAULT_WANT
    if wish == "replace":
        return Plan("replace")
    lm, here = launcher(tree, platform), str(tree)
    if not exists(lm):
        return Plan("replace", note=f"{lm} is missing: the chat opened in this window")
    lm_s = str(lm)
    term = terminal(env)
    if term == "tmux":
        return Plan("tmux", ("tmux", "new-window", "-c", here, lm_s, "open", ref), "tmux window")
    if term == "wt" and ";" not in here + lm_s:                # `;` separates commands for wt.exe itself
        return Plan("wt", ("wt.exe", "-w", "0" if wish == "tab" else "new", "nt", "-d", here, lm_s, "open", ref), wish)
    if wish == "tab":                                         # PowerShell, cmd, VS Code: no tabs here, stay in this window
        return Plan("replace")
    if platform == "win32" and not _CMD_SPECIAL.search(lm_s):
        return Plan("cmd", ("cmd", "/c", "start", "", lm_s, "open", ref), "window")
    return Plan("replace", note="no tab or window to open (not in tmux or Windows Terminal): the chat opened in this window")


def plan_open(home: Home, ref: str, env: Mapping[str, str] | None = None, platform: str | None = None,
              exists: Callable[[Path], bool] = Path.is_file) -> Plan:
    return plan(home.root, ref, want(home), os.environ if env is None else env,
                sys.platform if platform is None else platform, exists)


def spawn(p: Plan, tree: Path, popen: Callable = subprocess.Popen, wait: float = 20.0) -> tuple[bool, str]:
    """Start the plan.  (True, '') when the command was accepted (it exited with 0), else (False, why); it never raises
    and never waits for the new place to end.  No pipe is handed to the command: the window it opens would keep the
    other end and this call would wait for a chat that is still going."""
    if not p.argv:
        return False, "nothing to start"
    try:
        proc = popen(list(p.argv), cwd=str(tree), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
        code = proc.wait(timeout=wait)
    except FileNotFoundError:
        return False, f"{p.argv[0]} was not found"
    except subprocess.TimeoutExpired:
        return False, f"{p.argv[0]} did not answer"
    except OSError as e:
        return False, f"{p.argv[0]}: {e}"
    return (True, "") if code == 0 else (False, f"{p.argv[0]} failed (exit {code})")
