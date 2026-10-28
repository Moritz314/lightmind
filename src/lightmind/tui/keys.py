"""The overview of all keys (F1, or Ctrl+/): made from the bindings that are live right now, so it cannot go stale."""
from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Static, TextArea

from .chatinput import ChatInput, NEWLINE
from .theme import AMBER, CYAN, DIM, TXT


def show(key: str) -> str:
    """"ctrl+k" -> "Ctrl+K", "escape" -> "Esc": the way a person names a key."""
    names = {"escape": "Esc", "pageup": "PageUp", "pagedown": "PageDown", "ctrl+underscore": "Ctrl+/", "ctrl+slash": "Ctrl+/", "ctrl+@": "Ctrl+Space"}
    return names.get(key) or "+".join(p.upper() if len(p) == 1 else p.title() for p in key.split("+"))


def collect(app) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(heading, [(keys, what it does)])] for the screen that is open: its own keys, the focused widget's, the app's."""
    screen = app.screen
    where = type(screen).__name__.replace("Screen", "").lower() or "screen"
    got: dict[str, dict[str, list[str]]] = {}                 # heading -> description -> keys
    for key, ab in screen.active_bindings.items():
        b: Binding = ab.binding
        if not b.description or isinstance(ab.node, TextArea):         # (a text field's editing keys are the ones everybody knows)
            continue
        heading = "everywhere" if ab.node is app else f"this screen ({where})" if isinstance(ab.node, Screen) else "focused field"
        keys = got.setdefault(heading, {}).setdefault(b.description, [])
        shown = show(key)
        if shown not in keys:
            keys.append(shown)
    out = [(h, [(" / ".join(k), d) for d, k in rows.items()]) for h, rows in got.items()]
    if any(isinstance(ab.node, ChatInput) for ab in screen.active_bindings.values()) or isinstance(app.focused, ChatInput):
        out.append(("input line", [("Enter", "send the message"),
                                   (" / ".join(show(k) for k in NEWLINE), "new line")]))
    return out


class KeysScreen(ModalScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "close", priority=True)]

    def __init__(self, sections):
        super().__init__()
        self.sections = sections

    def compose(self):
        t = Text("KEYS\n\n", f"bold {CYAN}")
        for heading, rows in self.sections:
            t.append(heading + "\n", f"bold {AMBER}")
            w = max((len(k) for k, _ in rows), default=0)
            for k, d in rows:
                t.append(f"  {k:<{w}}  ", f"bold {TXT}")
                t.append(d + "\n", DIM)
            t.append("\n")
        t.append("Esc or F1 closes", DIM)
        with VerticalScroll(id="keys"):
            yield Static(t, id="keytext")
