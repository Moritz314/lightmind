"""The title of the terminal's tab/window, kept at the chat title.

The SDK's claude.exe renames the console it shares with us (the tab reads "claude"), so on Windows the title is set with
SetConsoleTitleW and can be read back (`current()`) to see whether it was overwritten; elsewhere it is an OSC 0 escape
sequence, which tmux and ssh pass on.  Imports nothing of Lightmind."""
from __future__ import annotations

import sys
from typing import Callable

APP = "Lightmind"
KEEP_EVERY = 2.0            # seconds between looks at the title (the TUI)


def tab_text(title: str = "", name: str = "") -> str:
    """"{title} · Lightmind"; no title yet: the session name given with -n; neither: "Lightmind"."""
    t = "".join(c for c in (title or name or "") if c.isprintable()).strip()
    return f"{t} · {APP}" if t else APP


def _kernel32():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32                       # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None


def current() -> str | None:
    """The console title now (Windows with a console), None where it cannot be read."""
    k = _kernel32()
    if k is None:
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        return buf.value if k.GetConsoleTitleW(buf, len(buf)) else None
    except (OSError, AttributeError, ValueError):
        return None


def set_title(text: str, write: Callable[[str], object] | None = None) -> None:
    """Set the tab title.  `write` takes the OSC 0 sequence (the TUI's driver, so the screen is not disturbed); without
    it the sequence goes to stdout, and only when that is a terminal.  Never raises: a title is a nicety."""
    text = "".join(c for c in text if c.isprintable())
    k = _kernel32()
    if k is not None:
        try:
            k.SetConsoleTitleW(text)
        except (OSError, AttributeError, ValueError):
            pass
    try:
        if write is not None:
            write(f"\033]0;{text}\007")
        elif sys.stdout.isatty():
            sys.stdout.write(f"\033]0;{text}\007")
            sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass


def keep(text: str, write: Callable[[str], object] | None = None) -> None:
    """Set the title again, but only when something else changed it (only where it can be read)."""
    now = current()
    if now is not None and now != text:
        set_title(text, write)
