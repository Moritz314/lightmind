"""The boot checklist: lines appear one by one like a Linux boot, each with a status at the end.

The checks themselves are `lightmind.checks` (the same list `lm check` prints).  They are started before Textual is
even imported, so most of them are done when this screen first paints; the screen only REVEALS them, in the fixed
order, one line per tick, with a spinner on the group that is still running.  With `ui.boot_wait: true` (the default)
the finished checklist stays on screen until Enter ("Enter: continue" at the bottom), and while it still checks only
Enter skips the rest; with `false` it closes by itself after a moment and any key skips it.
Plain mode (`NO_COLOR`, `LM_NO_ANIM=1`, no TTY: `app.calm`) waits for the results and shows them at once, without
colour or spinner.  `ui.boot: brief` lists only warnings and failures plus one summary line.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual import events
from textual.screen import Screen
from textual.widgets import Static

from .. import checks
from .. import title as release_title
from .orbit import MapView
from .theme import AMBER, CYAN, DIM, MAG, MINT, TXT, VIOLET

STEP = 0.07                  # seconds between two revealed lines
CAP = 5.5                    # seconds after which a group that has not answered is shown as skipped
HOLD = 0.7                   # how long the finished checklist stays before the landing (longer with a warning)
HOLD_BAD = 1.8
WAIT_HINT = "Enter: continue"                # the finished checklist waits for this key (`ui.boot_wait`)
SKIP_HINT = "Enter skips this"
SKIP_HINT_ANY = "any key skips this"
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TINT = {checks.OK: MINT, checks.WARN: AMBER, checks.FAIL: "#ff4d5e", checks.INFO: CYAN}


class BootScreen(Screen):
    """Reveals `pending` (a list of `checks.Pending`, already running) and then switches to the landing."""

    def __init__(self, pending: list[checks.Pending], brief: bool = False):
        super().__init__()
        self.pending, self.brief = pending, brief
        self.gi = 0                                  # the group being waited for
        self.queue: list[checks.Line] = []           # lines of a finished group, not yet shown
        self.rows: list[checks.Line] = []            # lines on screen
        self.all: list[checks.Line] = []             # every line of every group (the summary counts these)
        self.last = 0.0
        self.end = 0.0                               # when the finished list is left (0: still running)
        self.left = False

    def compose(self):
        yield Static(id="boot")

    def on_mount(self):
        self.calm = self.app.calm
        self.wait = checks.boot_wait(self.app.home)          # stays until Enter; else it closes by itself and any key skips
        self.t0 = time.monotonic()
        self.step = 0.0 if (self.calm or self.brief) else STEP
        self.timer = self.set_interval(0.04 if not self.calm else 0.1, self.tick)
        self.tick()

    # ------------------------------------------------------------------ the reveal
    def advance(self, now: float) -> None:
        """Move the reveal on by one tick (no widgets: the tests drive it with their own clock)."""
        if self.end:
            return
        while True:
            if self.queue:
                if self.step and now - self.last < self.step:
                    return
                ln = self.queue.pop(0)
                self.all.append(ln)
                if not (self.brief and ln.status in (checks.OK, checks.INFO)):
                    self.rows.append(ln)
                self.last = now
                continue
            if self.gi >= len(self.pending):
                bad = checks.worst(self.all) != checks.OK
                self.end = now + (HOLD_BAD if bad else HOLD) * (0.4 if self.calm else 1)
                return
            if self.calm and any(p.lines() is None for p in self.pending[self.gi:]) and now - self.t0 < CAP:
                return                                           # plain mode: all results first, then everything at once
            got = self.pending[self.gi].lines()
            if got is None and now - self.t0 >= CAP:
                got = [checks.Line(checks.WARN, self.pending[self.gi].name, "no answer yet, skipped")]
            if got is None:
                return
            self.queue, self.gi = list(got), self.gi + 1
            if not self.step:
                continue

    def render_text(self, now: float) -> Text:
        t = Text()
        name, _, rest = release_title().partition(" ")                   # `Lightmind` and `0.1.1 · orbit v1`
        t.append(name + " ", f"bold {CYAN}")
        t.append(rest, DIM)
        t.append("  ·  checking the tree\n\n", DIM)
        width = max([len(x.label) for x in self.all + self.queue] + [len(p.name) for p in self.pending] + [10])
        for ln in self.rows:
            tag = checks.TAGS[ln.status]
            t.append(tag, f"bold {TINT[ln.status]}" if not self.calm else None)
            t.append(f"  {ln.label:<{width}}  ", TXT if not self.calm else None)
            t.append((ln.detail if len(ln.detail) <= 78 else ln.detail[:77] + "…") + "\n", DIM if not self.calm else None)
        if self.end:
            bad = [x for x in self.all if x.status in (checks.WARN, checks.FAIL)]
            n_fail = sum(x.status == checks.FAIL for x in self.all)
            n_warn = len(bad) - n_fail
            worst = checks.FAIL if n_fail else checks.WARN if n_warn else checks.OK
            msg = (f"{len(self.all)} checks passed" if worst == checks.OK
                   else f"{n_fail} failed, {n_warn} warning{'s' * (n_warn != 1)} of {len(self.all)} checks")
            t.append("\n" + checks.TAGS[worst], f"bold {TINT[worst]}" if not self.calm else None)
            t.append(f"  {msg}  ·  {'ready' if self.wait else 'starting'}\n", TXT if not self.calm else None)
        elif self.gi < len(self.pending) and not self.queue:
            f = FRAMES[int(now * 12) % len(FRAMES)] if not self.calm else "."
            t.append(f"[ {f} ]", f"bold {VIOLET}" if not self.calm else None)
            t.append(f"  {self.pending[self.gi].name} …\n", DIM if not self.calm else None)
        hint = (WAIT_HINT if self.wait else "") if self.end else SKIP_HINT if self.wait else SKIP_HINT_ANY
        t.append(("\n" + hint) if hint else "", DIM if not self.calm else None)
        return t

    def tick(self) -> None:
        if self.left:
            return
        now = time.monotonic()
        self.advance(now)
        self.query_one("#boot", Static).update(self.render_text(now))
        if self.end and now >= self.end and not self.wait:
            self.leave()
        elif self.end and self.wait:
            self.timer.stop()                             # finished and waiting for Enter: nothing on screen changes any more

    def note(self) -> str:
        """One line for the landing's ticker when the checklist found something."""
        n_fail = sum(x.status == checks.FAIL for x in self.all)
        n_warn = sum(x.status == checks.WARN for x in self.all)
        if not (n_fail or n_warn):
            return ""
        first = next(x for x in self.all if x.status == checks.FAIL) if n_fail else next(x for x in self.all if x.status == checks.WARN)
        return f"boot: {n_fail} failed, {n_warn} warning{'s' * (n_warn != 1)}  ·  {first.label}: {first.detail}"[:110]

    def leave(self) -> None:
        if self.left:
            return
        self.left = True
        self.timer.stop()
        from .screens import MapScreen
        self.app.boot_note = self.note()
        self.app.switch_screen(MapScreen())

    def on_key(self, e: events.Key) -> None:
        if self.wait and e.key != "enter":            # only Enter skips or closes; any other key is left alone
            return
        e.stop()
        if not self.end:                              # unfinished groups are dropped; what was seen still counts for the note
            self.all += [ln for p in self.pending[self.gi:] if (ls := p.lines()) for ln in ls]
        self.leave()
