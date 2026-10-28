"""What the screens show, read from the tree.  No widgets in here; every function takes the Home.

Reads are tolerant on purpose: lightmind.md can be half edited while the UI is open (a YAML error), and a
missing folder is just an empty list.  The screens keep showing what they had."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from ..files import Home, parse_size
from ..runs import SessionSummary, list_sessions      # not `loop`: that import costs 0.2 s the landing does not need
from ..core import (BUILTIN_SLASH, EFFORTS, STALE, Item, age, clean_user, duration, efforts, items, loadout,   # noqa: F401 - the moved reads, still reached as data.<name>
                    model_options, safe, settings, skill_commands, skills, slash_rows, state_of)
from .theme import colour

DEFAULT_MENU = [("New chat", "n", "new_chat"), ("Resume chat", "r", "resume"), ("Accounts", "a", "accounts"),
                ("Dashboard", "d", "dash"), ("Settings", "s", "settings"), ("Quit", "q", "quit")]
ACTIONS = ("new_chat", "resume", "archive", "accounts", "dash", "settings", "quit")     # what a menu or slash row can point to
DEFAULT_STATUS = "{server} · {model} · {account} · {mode} · ctx {ctx_bar} {ctx} · 5h {five_hour} · 7d {seven_day} · ${cost}"
EFFORT_TEXT = {"low": "fast and cheap, no extended thinking", "medium": "balanced", "high": "careful",
               "xhigh": "very careful", "max": "slowest, most thorough",
               "ultracode": "max + every substantial task as a workflow"}
CONTEXTS = ("100k", "200k", "500k", "800k", "1m")                # `ui.context_sizes` overrides; above WINDOW needs a 1m model
WINDOW = 200_000          # the plain model window; larger sizes are Lightmind's own handover limit on a 1m window


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(s) -> str:
    """Text for the screen without C0/C1 control characters (but newline and tab): whatever a model, a tool or a
    hook printed cannot send escape sequences to the terminal.  Every text the UI renders goes through here."""
    return _CONTROL.sub("", str(s))


def visible(s: str) -> str:
    """Characters that show nothing or change how text reads (format, separator, control, private-use and
    surrogate characters: bidi overrides, zero-width spaces, a tab) written out as a backslash-u escape, so that what the
    user sees is what would run."""
    return "".join(f"\\u{{{ord(c):04X}}}" if unicodedata.category(c) in ("Cf", "Zl", "Zp", "Cc", "Co", "Cs") else c for c in s)


# ------------------------------------------------------------------ settings
def ui(home: Home) -> dict:
    u = settings(home).get("ui")
    return u if isinstance(u, dict) else {}


def refresh_secs(home: Home) -> float:
    try:
        return max(1.0, float(ui(home).get("refresh", 5)))
    except (TypeError, ValueError):
        return 5.0


def layout(home: Home) -> str:
    return str(ui(home).get("layout") or "orbit").strip().lower()


def menu(home: Home) -> list[tuple[str, str, str]]:
    """`ui.start_menu` rows `label | key | action` -> (label, key, action); a missing key is the first
    letter of the label, a missing action the label as a name."""
    out = []
    for r in safe(lambda: home.ui_rows("start_menu"), []):
        if r and r[0]:
            out.append((r[0], (r[1] if len(r) > 1 and r[1] else r[0][:1]).lower(),
                        r[2] if len(r) > 2 and r[2] else r[0].lower().replace(" ", "_")))
    return out or DEFAULT_MENU


ACTION_TEXT = {"new_chat": "start a new chat", "resume": "resume an earlier chat", "accounts": "accounts and their limits",
               "dash": "dashboard: runs and cost", "settings": "change config.md and the agents' settings", "quit": "quit Orbit"}


def slash_commands(home: Home) -> list[tuple[str, str]]:
    """Every slash command with a one-line description: the built-ins, then the `ui.slash` rows (a row wins over a built-in
    of the same name and keeps its place), then one command per skill with its description."""
    out = dict(BUILTIN_SLASH)
    for name, action in slash_rows(home):
        out[name] = ACTION_TEXT.get(action, f"run {action}")
    for name, s in skill_commands(home, list(out)).items():
        out[name] = " ".join(s.description.split()) or "use this skill"
    return list(out.items())


def context_sizes(home: Home) -> tuple[str, ...]:
    """`ui.context_sizes` (a list, or "100k, 200k, 1m"); anything unparsable falls back to CONTEXTS."""
    raw = ui(home).get("context_sizes")
    raw = raw.replace(",", " ").split() if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out = []
    for v in raw:
        try:
            if parse_size(v) > 0:
                out.append(str(v).strip().lower())
        except (TypeError, ValueError):
            pass
    return tuple(dict.fromkeys(out)) or CONTEXTS


@dataclass
class Mode:
    key: str
    label: str
    color: str
    text: str
    asks: list[str]
    denies: list[str]
    order: int | None = None              # place in the Shift+Tab cycle; None: only through /mode
    never_asks: bool = False
    error: str = ""                       # why the mode file cannot be used ('' = usable)


def modes_error(home: Home) -> str:
    """'' when the permissions file offers modes, else why not (shown instead of an empty menu)."""
    try:
        home.modes()
    except ValueError as e:
        return str(e)
    return ""


def modes(home: Home) -> dict[str, Mode]:
    """The modes of the tree (mode files, or `modes:` of an older permissions file); empty when they cannot be read
    (`modes_error` says why).  A mode file shows what it asks and blocks as catalog numbers with their names."""
    out = {}
    for k, m in safe(home.modes, {}).items():
        locked = m.get("locked") if isinstance(m.get("locked"), dict) else {}
        summary = m.get("summary") or {}
        asks = summary.get("fragt") if "summary" in m else [str(x) for x in m.get("ask") or []]
        denies = summary.get("gesperrt") if "summary" in m else [str(x) for x in locked.get("deny") or []]
        order = m.get("order") if isinstance(m.get("order"), int) and not isinstance(m.get("order"), bool) else None
        out[k] = Mode(k, str(m.get("label") or k), colour(m.get("color")), str(m.get("text") or "").strip(),
                      list(asks or []), list(denies or []), order, bool(m.get("no_ask", k == "bypass")),
                      str(m.get("error") or ""))
    return out


# ------------------------------------------------------------ the landing
@dataclass
class Srv:
    name: str
    host: str = ""
    local: bool = True                        # False: another server, reached with ssh (see remote.py)


@dataclass
class Snap:
    servers: list[Srv]
    sessions: list[SessionSummary]            # newest first, closed ones included
    hub: str                                  # defaults.server: the node in the middle
    refresh: float = 5.0
    menu: list[tuple[str, str, str]] = field(default_factory=list)   # ui.start_menu, read together with the rest
    layout: str = "orbit"                     # ui.layout


def snapshot(home: Home, limit: int = 12) -> Snap:
    hub = str((settings(home).get("defaults") or {}).get("server") or "local")
    names = safe(home.list_servers, [])
    def srv(n: str) -> Srv:
        s = safe(lambda: home.load_server(n))
        return Srv(n, s.host if s else "", s.local if s else True)
    servers = [srv(n) for n in names]
    if hub not in names:                      # no servers/ folder entry: the default server is still there
        servers.insert(0, Srv(hub, "local"))
    return Snap(servers, safe(lambda: list_sessions(home, limit), []) if limit else [], hub, refresh_secs(home),
                menu(home), layout(home))


def view(home: Home) -> tuple[list[tuple[str, str, str]], str]:
    """(start menu, layout): what the start screen takes from lightmind.md / config.md.  The landing asks for it in a thread
    once a second, so an edit shows within a second and no frame ever reads a file."""
    return menu(home), layout(home)


def ago(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    days = (datetime.now().astimezone().date() - dt.date()).days
    return f"{dt:%H:%M}" if days == 0 else "yesterday" if days == 1 else f"{dt:%a}" if days < 7 else f"{dt:%b %d}"


def label_of(s: SessionSummary) -> str:
    return clean(s.agent or s.title or s.model or "chat")[:18]


def short_id(sid: str) -> str:
    return sid.rsplit("-", 1)[-1]


# -------------------------------------------------------------- new chat
def default_model(home: Home, account: str, wanted: str) -> str:
    names = [n for n, _ in model_options(home, account)]
    a = safe(lambda: home.load_providers().accounts.get(account))
    return wanted if wanted in names else (a.default_model if a and a.default_model else (names[0] if names else wanted))


def account_note(home: Home, name: str, a) -> str:
    """Type of an account plus what is missing: 'not logged in: lm login X' / 'key missing: lm secret set X'."""
    from ..accounts import hint
    from ..providers.base import ADAPTERS
    h = hint(home, name)
    return a.type + ("" if a.type in ADAPTERS else "  ·  no adapter yet") + (f"  ·  {h}" if h else "")
