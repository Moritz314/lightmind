"""Palette and colour helpers of the Orbit UI (taken over from mockup C)."""
from __future__ import annotations

from functools import lru_cache

from rich.text import Text
from textual.color import Color

from .. import ultracode
from ..util import setting

BG, PANEL, TXT, DIM = "#080a14", "#0f1224", "#e8ecff", "#5a6288"
CYAN, VIOLET, MAG, AMBER, MINT = "#35e0ff", "#9b6bff", "#ff4fa3", "#ffc247", "#47f0b0"
WORD = f"bold {MAG}"                                # the word `ultracode`: it switches the effort on, so it stands out from the speakers' colours
NEUTRAL = "#2a2f4d"                                # the lines around the chat: one colour in every mode
OFF = "#3a4060"                                    # things that are listed but not available
NAMED = {"green": "#3ddc84", "red": "#ff4d5e", "blue": "#4da3ff", "yellow": "#ffd24d",
         "orange": "#ff9a3c", "purple": "#a06bff", "magenta": MAG, "cyan": CYAN}


@lru_cache(maxsize=8192)
def _blend(a: str, b: str, f: float) -> str:
    return Color.parse(a).blend(Color.parse(b), f).hex


def blend(a: str, b: str, f: float) -> str:
    return _blend(a, b, round(max(0.0, min(1.0, f)), 2))


def grad(x: float) -> str:
    stops = [CYAN, VIOLET, MAG, VIOLET, CYAN]
    x = (x % 1.0) * (len(stops) - 1)
    i = int(x)
    return blend(stops[i], stops[i + 1] if i + 1 < len(stops) else stops[-1], x - i)


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def sty(col, a=1.0, bold=False):
    return ("bold " if bold else "") + (col if a >= 0.999 else blend(BG, col, clamp(a)))


def bar(p: float, w=24, col=CYAN) -> Text:
    n = max(0, min(w, round(p / 100 * w)))
    t = Text()
    t.append("█" * n, col)
    t.append("░" * (w - n), "#2a2f4d")
    return t


def colour(value, default="#9aa0c0") -> str:
    """The `color:` of a mode: a name from NAMED, or any CSS colour; anything else is the default."""
    v = str(value or "").strip().lower()
    try:
        return Color.parse(NAMED.get(v, v)).hex
    except Exception:                              # noqa: BLE001 - a typo in permissions.md must not crash the UI
        return default


def keyword_on(widget) -> bool:
    """Does the word `ultracode` switch anything on?  False when `workflow.keyword` is off in the settings of the tree
    (the loop reads the same key); read per call, as the settings are cached by file time, so a change in /config shows at once.
    `widget`: any widget; without a running app (a block built in a test) the default, on, counts."""
    try:
        home = getattr(widget.app, "home", None)
    except Exception:                              # noqa: BLE001 - no active app: nothing to read, keep the default
        home = None
    return setting(home, "workflow.keyword", True) is not False


def word_spans(widget, text: str) -> list[tuple[int, int]]:
    """Where `text` holds the coloured word (offsets), none when the keyword is off.  The settings are read only when the word is there at all."""
    hits = ultracode.spans(text)
    return hits if hits and keyword_on(widget) else []
