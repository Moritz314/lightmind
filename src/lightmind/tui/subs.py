"""Sub-agents in the chat: the line under the input (running / done, clickable; the agents of a workflow grouped by its
phases) and the read-only screen with one sub-agent's own chat: the task of the main chat on top, its steps, its answer
at the bottom."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from .. import model as m
from ..providers.base import ToolDone
from ..subagents import Flow, Sub
from . import data
from .theme import AMBER, CYAN, DIM, MAG, MINT

CARD_TASK = 120                                              # the task as far as the card in the main transcript shows it
GROUP_ITEMS = 3                                              # agents of one phase shown by name; more are shown as counts


def one_line(text: str, n: int = CARD_TASK) -> str:
    t = " ".join(data.clean(text).split())
    return t if len(t) <= n else t[:n - 1] + "…"


def bar_text(subs: list[Sub], flows: list[Flow] | None = None) -> tuple[Text, list[tuple[int, int]]]:
    """'◆ reviewer running · ◆ explore done' and the (start, end) cells of each item, for the click."""
    t, spans, _ = bar_items(subs, flows)
    return t, spans


def _state(s: Sub) -> str:
    return "running" if not s.done else ("done" if s.ok else "failed")


def _item(t: Text, s: Sub) -> tuple[int, int]:
    a = len(t.plain)
    t.append("◆ ", CYAN)
    t.append(s.name, AMBER)
    t.append(" " + _state(s), DIM if not s.done else (MINT if s.ok else MAG))
    return a, len(t.plain)


def bar_items(subs: list[Sub], flows: list[Flow] | None = None) -> tuple[Text, list[tuple[int, int]], list[Sub]]:
    """The line, the clickable cells and the sub-agent each cell opens.  Sub-agents of `spawn` one by one; those of a
    workflow under '▸ <name>' and per phase '<phase>:', by name up to GROUP_ITEMS, else as counts (a click opens the first
    one still running)."""
    t, spans, targets = Text(), [], []

    def sep() -> None:
        if t.plain:
            t.append(" · ", DIM)
    for s in (x for x in subs if not x.workflow):
        sep()
        spans.append(_item(t, s))
        targets.append(s)
    for f in flows or []:
        mine = [x for x in subs if x.workflow == f.id]
        sep()
        t.append("▸ ", CYAN)
        t.append(f.name, AMBER)
        if f.done:
            t.append(" done" if f.ok else " failed", MINT if f.ok else MAG)
        groups: dict[str, list[Sub]] = {}
        for x in mine:
            groups.setdefault(x.phase, []).append(x)
        for phase, group in groups.items():
            t.append("  " + (f"{phase}: " if phase else ""), DIM)
            if len(group) <= GROUP_ITEMS:
                for i, x in enumerate(group):
                    if i:
                        t.append(", ", DIM)
                    spans.append(_item(t, x))
                    targets.append(x)
                continue
            a = len(t.plain)
            counts = {k: sum(1 for x in group if _state(x) == k) for k in ("running", "done", "failed")}
            t.append(" · ".join(f"{n} {k}" for k, n in counts.items() if n), DIM)
            spans.append((a, len(t.plain)))
            targets.append(next((x for x in group if not x.done), group[-1]))
    return t, spans, targets


class SubBar(Static):
    """The line under the input; a click on an item opens that sub-agent."""

    def __init__(self, **kw):
        super().__init__("", **kw)
        self.spans: list[tuple[int, int]] = []
        self.subs: list[Sub] = []
        self.targets: list[Sub] = []             # the sub-agent each span opens

    def show(self, subs: list[Sub], flows: list[Flow] | None = None) -> None:
        self.subs = subs
        t, self.spans, self.targets = bar_items(subs, flows)
        self.update(t)
        self.display = bool(subs or flows)

    def on_click(self, e: events.Click) -> None:
        x = e.x - self.styles.padding.left
        for (a, b), s in zip(self.spans, self.targets):
            if a <= x < b:
                e.stop()
                self.screen.open_sub(s.id)
                return


class SubScreen(Screen):
    """Read only: no input here."""
    BINDINGS = [("escape", "back", "back"), ("x", "stop", "stop it"), ("pageup", "scroll(-1)", "up"), ("pagedown", "scroll(1)", "down")]

    def action_stop(self):
        chat = getattr(self.app, "chat", None)
        if chat is not None and chat.s.bg.cancel(self.sid):
            self.notify("sub-agent stopped")

    def __init__(self, log, sid: str, main: str):
        super().__init__()
        self.log_, self.sid, self.main = log, sid, main
        self.sig: tuple = ()

    @property
    def sub(self) -> Sub:
        return self.log_.subs[self.sid]

    def compose(self):
        yield Vertical(Static(id="head"), VerticalScroll(id="smsgs"), Static(id="shint"), id="box")

    async def on_mount(self):
        self.set_interval(0.4, self.sync)
        await self.sync()

    async def sync(self):
        s = self.sub
        sig = (len(s.steps), sum(len(x.text) for x in s.steps if isinstance(x, m.Text)), s.done, s.summary)
        if sig == self.sig:
            return
        self.sig = sig
        from .chat import Msg, short_args, speaker_name       # late: chat imports this module
        who, me = speaker_name(s.name), speaker_name(self.main)
        head = Text()
        head.append(" ◆ ", f"bold {CYAN}")
        head.append(s.name, f"bold {AMBER}")
        head.append("  ·  " + ("running" if not s.done else "done" if s.ok else "failed"), DIM)
        self.query_one("#head", Static).update(head)
        self.query_one("#shint", Static).update(Text(" read only  ·  Esc back", DIM))
        col = max(len(who), len(me), 3)
        rows: list[Msg] = [Msg("agent", me, data.clean(s.task) or "(no task text)", "", col)]
        results = {x.id: x for x in s.steps if isinstance(x, ToolDone)}
        for x in s.steps:
            if isinstance(x, m.Text) and x.text.strip():
                rows.append(Msg("agent", who, data.clean(x.text), "", col))
            elif isinstance(x, m.ToolCall):
                w = Msg("tool", who, x.name, data.clean(short_args(x.args)), col)
                r = results.get(x.id)
                if r is not None:
                    w.result = (r.ok, next((ln.strip() for ln in data.clean(r.text).splitlines() if ln.strip()), ""))
                    w.draw()
                rows.append(w)
        if s.done:
            rows.append(Msg("note", "", f"answer to {self.main}", "", col))
            rows.append(Msg("agent", who, data.clean(s.summary) or "(empty)", "", col))
        else:
            rows.append(Msg("note", "", "still running", "", col))
        box = self.query_one("#smsgs", VerticalScroll)
        await box.remove_children()
        await box.mount(*rows)
        box.scroll_end(animate=False)

    def action_scroll(self, d: int):
        box = self.query_one("#smsgs", VerticalScroll)
        box.scroll_page_down() if d > 0 else box.scroll_page_up()

    def action_back(self):
        self.app.pop_screen()
