"""The status line of the chat as a widget you can reach: F2 focuses it, Left/Right choose a segment (model,
mode, account, effort, context), Enter opens the matching choice, a click does the same.  F2 focuses it (Tab does not
reach it: the input line keeps the key for its popup)."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static

from .theme import CYAN


class StatusBar(Static, can_focus=True):
    BINDINGS = [Binding("left", "move(-1)", "previous segment", show=False), Binding("right", "move(1)", "next segment", show=False),
                Binding("enter", "choose", "open the choice of this segment", show=False)]

    class Chosen(Message):
        def __init__(self, key: str):
            super().__init__()
            self.key = key

    def __init__(self, id: str | None = None):
        super().__init__(id=id)
        self.base = Text()
        self.spans: list[tuple[str, int, int]] = []          # (segment key, start, end) in the text
        self.sel = 0                                         # index into keys()

    def keys(self) -> list[str]:
        return list(dict.fromkeys(k for k, _, _ in self.spans))

    @property
    def key(self) -> str:
        ks = self.keys()
        return ks[min(self.sel, len(ks) - 1)] if ks else ""

    def show(self, text: Text, spans: list[tuple[str, int, int]]):
        self.base, self.spans = text, spans
        self.paint()

    def paint(self):
        t = self.base.copy()
        if self.has_focus and self.key:
            for k, a, b in self.spans:
                if k == self.key:
                    t.stylize(f"reverse bold {CYAN}", a, b)
        self.update(t)

    def on_focus(self):
        self.paint()

    def on_blur(self):
        self.paint()

    def action_move(self, d: int):
        ks = self.keys()
        if ks:
            self.sel = (self.sel + d) % len(ks)
            self.paint()

    def action_choose(self):
        if self.key:
            self.post_message(self.Chosen(self.key))

    def on_click(self, e: events.Click):
        o = e.get_content_offset(self)                       # (the border above the text is not the text)
        if o is None or o.y != 0:
            return
        for k, a, b in self.spans:
            if a <= o.x < b:
                self.sel = self.keys().index(k)
                self.focus()
                self.paint()
                self.post_message(self.Chosen(k))
                return
