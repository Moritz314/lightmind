"""The part of the surfaces that is not a screen: what the Orbit TUI and `lm serve` (the VS Code backend) share.

No Textual, no rich.  Three things live here:

* reads from the tree that both surfaces show (settings, slash catalog, tools and skills of a chat, models, session state),
  moved out of `tui/data.py`, which re-exports them;
* what a chat does around the turn: the `on_start` hooks with the wait for them (`start_hooks`), the sync pull and tick
  (`sync_*`), and the slash words of a message (`prepare`, `command`);
* nothing that draws.

May import: files, model, context, slash, hooks, runs, sync.
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from contextlib import suppress
from pathlib import Path

from . import slash as sl
from . import sync
from .context import skill_commands as _skill_commands
from .context import skill_message
from .files import Home
from .hooks import run_on_start
from .model import ToolRef
from .runs import SessionSummary

EFFORTS = ("low", "medium", "high", "xhigh", "max")              # claude_sdk.EFFORTS, see efforts()
STALE = 6 * 3600          # seconds: a chat that still says "running" but was silent this long counts as paused

def safe(fn, default=None):
    try:
        return fn()
    except Exception:                                        # noqa: BLE001 - see the module docstring
        return default


def efforts() -> tuple[str, ...]:
    """The values the provider accepts: claude_sdk's own list once that module is loaded (importing
    the SDK takes seconds, so the UI never imports it just for this list)."""
    known = tuple(getattr(sys.modules.get("lightmind.providers.claude_sdk"), "EFFORTS", EFFORTS))
    return known + (("ultracode",) if "ultracode" not in known else ())      # no provider knows it: max plus workflows (ultracode.py)


def settings(home: Home) -> dict:
    return safe(lambda: dict(home.settings), {})


def slash_rows(home: Home) -> list[tuple[str, str]]:
    """`ui.slash` rows `/name → action` -> (name, action)."""
    return [(r[0].lstrip("/").lower(), r[1]) for r in safe(lambda: home.ui_rows("slash"), []) if len(r) > 1 and r[0]]


BUILTIN_SLASH = (("mode", "change the permission mode"), ("model", "change the model"), ("account", "switch the account"),
                 ("effort", "how hard the model thinks"),
                 ("ultracode", "ultracode on | off | status: effort max + every substantial task as a workflow"),
                 ("tools", "switch tools on or off (skills are always on)"),
                 ("skill", "list the skills; /skill <name> <request> uses one"),
                 ("rename", "set the chat title by hand; alone: automatic again"),
                 ("describe", "set the chat description by hand; alone: automatic again"),
                 ("server", "where this chat runs"), ("accounts", "accounts and their limits"), ("clear", "close this chat, start a new one"),
                 ("login", "log in a Claude subscription account (opens the browser)"), ("key", "store the API key of an account"),
                 ("settings", "change config.md and the agents' settings"), ("config", "same as /settings"),
                 ("new", "same as /clear"), ("resume", "pick an earlier chat"), ("quit", "leave Lightmind"), ("exit", "same as /quit"),
                 ("reload", "read settings, agent, skills and accounts again"),
                 ("upload", "pick files and attach them to the message"), ("datei", "same as /upload"),
                 ("rc", "Remote Control: use this chat from your phone (/rc <name>, /rc off)"),
                 ("remote-control", "same as /rc"), ("archive", "archive this chat and leave it (Ctrl+Q does it and quits); /archive list, /archive restore <chat>"))

# what a device on the Remote Control link may not type: it needs a screen of the terminal or leaves the chat (/archive is
# allowed, and so is the link's own `session.archive`)
RC_DENY = ("upload", "datei", "settings", "config", "login", "key", "accounts", "resume", "server", "clear", "new", "quit",
           "exit", "rc", "remote-control")


def signature(home: Home) -> dict[str, int]:
    """file -> mtime of what `reload` watches: skills, agents, tools, the active project entries and the three settings files and the account (provider) files."""
    sig: dict[str, int] = {}
    for key in ("skills", "agents", "tools", "projects", "providers"):
        try:
            root = home.path(key)
        except Exception:                                    # noqa: BLE001 - a tree without that location
            continue
        n = 0
        for p in root.rglob("*") if root.is_dir() else ():
            if n > 3000:
                break
            n += 1
            with suppress(OSError):
                if p.is_file():
                    sig[p.relative_to(home.root).as_posix()] = p.stat().st_mtime_ns
    for f in ("lightmind.md", "config.md", "permissions.md"):
        with suppress(OSError):
            sig[f] = (home.root / f).stat().st_mtime_ns
    return sig


def label(rel: str) -> str:
    parts = rel.split("/")
    kind = {"skills": "skill", "agents": "agent", "tools": "tool", "projects": "project", "providers": "provider"}.get(parts[0])
    return f"{kind} {parts[1]}" if kind and len(parts) > 1 else rel


def installed_version() -> str:
    """The version of Lightmind installed now (its metadata is read anew); "" when it cannot be told."""
    from importlib import metadata
    with suppress(Exception):
        return metadata.version("lightmind")
    return ""


def reload(home: Home, s, before: dict[str, int]) -> tuple[Result, dict[str, int]]:
    """`/reload`: read what the chat is built from again (lightmind.md, config.md, permissions.md, the agent, the skills,
    tools, projects and accounts), build the context anew at the next turn and go on with the same history.  `before` is
    `signature` of the last load; returns the line to show and the new signature.  The caller runs it between turns."""
    from . import __version__
    now = signature(home)
    changed = sorted({label(p) for p in set(now) | set(before) if now.get(p) != before.get(p)})
    try:
        problems = s.reload(providers=any(c.startswith("provider ") for c in changed))
    except Exception as e:                                   # noqa: BLE001 - a chat that is only read says so
        return Result(False, f"reload: {e}"), before
    lines = ["reloaded: " + (", ".join(changed[:12]) + (f" and {len(changed) - 12} more" if len(changed) > 12 else "")
                            if changed else "nothing changed on disk") + "  ·  the history stays, the next message uses it"]
    lines += [f"reload: {p}" for p in problems]
    if (running := s.bg.running()):
        lines.append(f"{len(running)} sub-agent(s) still working keep the settings they started with: {', '.join(running)}")
    if s.agent is not None and f"agent {s.agent.name}" in changed:
        lines.append("mode, effort and model set by the agent file apply from a new chat")
    new = installed_version()
    if new and new != __version__:
        lines.append(f"Lightmind itself was updated ({__version__} -> {new}): /reload cannot load new code, restart the chat for that")
    return Result(not problems, "\n".join(lines)), now


def skill_commands(home: Home, reserved=()):
    """command name -> Skill; a skill named like a built-in or `ui.slash` command is `skill:<name>`."""
    return safe(lambda: _skill_commands(home, [*(n for n, _ in BUILTIN_SLASH), *(n for n, _ in slash_rows(home)), *reserved]), {})


def duration(text, default: float) -> float:
    """'30m' | '90s' | '2h' | 45 -> seconds (the `run.ask_timeout` setting)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([smhd]?)\s*", str(text or "").lower())
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]] if m else default


def age(iso: str) -> float:
    """Seconds since an ISO timestamp (a huge number when it does not parse)."""
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(iso).astimezone()).total_seconds()
    except ValueError:
        return 1e12


def state_of(s: SessionSummary) -> str:
    """running | paused | done, from the run status and how long ago the chat was last active."""
    if s.status in ("done", "failed"):
        return "done"
    return "running" if s.status == "running" and age(s.last_active) < STALE else "paused"


@dataclass
class Item:
    name: str
    kind: str                                 # skill | tool
    desc: str
    usable: bool                              # can be passed as tools_on: the core wants skills and tools with a tool.md


def items(home: Home) -> list[Item]:
    """Everything a chat can switch on and off: the tools of the tree (skills are always on, see `skills`)."""
    out = []
    for t in safe(home.list_tools, []):
        out.append(Item(t.name, "tool", " ".join(t.description.split()),
                        bool(t.path) and (Path(t.path) / home.filename("tool")).is_file()))
    return out


def skills(home: Home) -> list[Item]:
    """The skills of the tree, for display only: they cannot be switched off."""
    return [Item(s.name, "skill", " ".join(s.description.split()), True) for s in safe(home.list_skills, [])]


def loadout(all_: list[Item], off: set[str], before=()) -> tuple[list[ToolRef], list[str]]:
    """(tools_on, tools_off) for the session API.  `before` = what the chat had switched off until now.  The
    on-list goes along when anything is or was off: a name must be listed again to come back.  It carries the
    kind (a bare name that a skill shares with a tool, like `pdf`, would be taken for the skill)."""
    gone = [i.name for i in all_ if i.name in off]
    return ([ToolRef(i.kind, i.name) for i in all_ if i.name not in off and i.usable] if gone or before else []), gone


def model_options(home: Home, account: str) -> list[tuple[str, str]]:
    """(name, note) of what an account offers: its models, then its tiers; accepted by open_session / set."""
    a = safe(lambda: home.load_providers().accounts.get(account))
    if a is None:
        return []
    out = [(m, "default" if m == a.default_model else "") for m in a.models]
    if a.default_model and a.default_model not in a.models:
        out.insert(0, (a.default_model, "default"))
    return out + [(t, f"tier → {m}") for t, m in a.tiers.items()]


def clean_user(text: str) -> str:
    """A logged user message without the on_prompt context the core appended to it."""
    return re.sub(r'\n*<context source="on_prompt">.*?</context>', "", text, flags=re.S).strip()


# ------------------------------------------------------------------ around the turn
async def start_hooks(home: Home, s) -> tuple[list[str], str]:
    """The `on_start` hooks of a session, in a thread (they are blocking shell commands).  -> (the lines they showed,
    a warning when running them failed).  The first message of a chat waits for this; a surface sets its own flag."""
    try:
        return list(await asyncio.to_thread(run_on_start, home, s)), ""
    except Exception as e:                                   # noqa: BLE001 - the chat starts all the same
        return [], f"on_start failed: {e}"


TICK = sync.TICK


def sync_start(home: Home) -> None:
    """A sync repo: pull in the background.  Silent without one."""
    sync.start_pull(home)


async def sync_tick(home: Home) -> None:
    """The sync clock (every `TICK` seconds): it looks at the settings and starts a thread, so not on the UI thread."""
    await asyncio.to_thread(sync.tick, home)


async def sync_end(home: Home, timeout: float = 15) -> None:
    """Commit + push at the end, never longer than `timeout`."""
    await asyncio.to_thread(sync.session_end, home, timeout)


# ------------------------------------------------------------------ slash commands
@dataclass
class Result:
    ok: bool
    text: str = ""


def mode_labels(home: Home) -> dict[str, tuple[str, str]]:
    """key -> (label, text) of the `modes:` presets of the permissions file.  Raises ValueError with the reason when
    there are none."""
    return {k: (str(m.get("label") or k), str(m.get("text") or "").strip()) for k, m in home.modes().items()}


def _change(s, label: str, **kw) -> str:
    """'' when `s.set(**kw)` worked, else the core's own text."""
    try:
        s.set(**kw)
    except Exception as e:                                   # noqa: BLE001 - the core's own text says what is wrong
        return f"{label}: {e}"
    return ""


def command(home: Home, s, name: str, arg: str) -> Result:
    """`/mode`, `/model`, `/account`, `/effort` with their word, and `/tools`: change the session.  The words are as the
    TUI shows them; a surface that has no picker asks for them by name."""
    if name == "mode":
        try:
            ms = mode_labels(home)
        except ValueError as e:
            return Result(False, f"mode: {e}")
        key = next((k for k, (label, _) in ms.items() if arg.lower() in (k.lower(), label.lower())), arg)
        if err := _change(s, "mode", mode=key):
            return Result(False, err)
        return Result(True, f"mode: {ms[key][0] if key in ms else key}")
    if name == "model":
        opts = model_options(home, s.account)
        if err := _change(s, "model", model=arg):
            return Result(False, err)
        return Result(True, f"model: {s.model}" + ("" if arg in dict(opts) else f"  (not listed for {s.account}, sent as written)"))
    if name == "account":
        if err := _change(s, "account", account=arg):
            return Result(False, err)
        return Result(True, f"account: {s.account}  ·  model {s.model}  ·  the history is replayed once")
    if name == "effort":
        known = efforts()
        if arg not in known:
            return Result(False, f"effort: unknown {arg!r}; use {', '.join(known)}")
        if arg == "ultracode":
            return ultracode_command(home, s, "on")          # the same words and the same way back as /ultracode
        if err := _change(s, "effort", effort=arg):
            return Result(False, err)
        return Result(True, f"effort: {arg}")
    if name == "ultracode":
        return ultracode_command(home, s, arg)
    if name == "tools":
        return tools_change(home, s, arg)
    if name in ("rename", "describe"):
        try:
            return Result(True, (s.rename if name == "rename" else s.describe)(arg))
        except Exception as e:                               # noqa: BLE001 - a chat that is only read says so
            return Result(False, f"{name}: {e}")
    return Result(False, f"unknown command /{name}")


def ultracode_status(home: Home, s) -> str:
    """The state of ultracode and the settings that apply, as a short list (`/ultracode status`)."""
    from . import ultracode as uc
    from .util import setting

    def runtime() -> tuple[int, str]:
        from . import orchestrate                           # late: it pulls in the whole sub-agent runtime
        return orchestrate.max_parallel(home), orchestrate.memory_limits(home)
    parallel, ram = safe(runtime, (0, ""))
    extra = {"ask before a workflow": "yes" if setting(home, "workflow.confirm") is True else "no",
             "the word ultracode switches it on": "no" if setting(home, "workflow.keyword") is False else "yes"}
    if ram:
        extra["RAM per agent / kept free"] = ram
    return uc.status(s.effort, s.effort_before, uc.read(home), parallel, extra)


def ultracode_command(home: Home, s, arg: str) -> Result:
    """`/ultracode [on|off|status]`: switch the level on for this chat (the same as `/effort ultracode`), back to the effort
    it had before (`max` when unknown), or list the state and the settings that apply."""
    from . import ultracode as uc
    word = (arg or "on").strip().lower()
    if word == "status":
        return Result(True, ultracode_status(home, s))
    if word not in ("on", "off"):
        return Result(False, f"ultracode: unknown {arg!r}; use {', '.join(sl.ULTRACODE_WORDS)}")
    if word == "on":
        if uc.is_on(s.effort):
            return Result(True, "ultracode is on already")
        if err := _change(s, "ultracode", effort=uc.ULTRACODE):
            return Result(False, err)
        return Result(True, "ultracode is on: effort max, every substantial task as a workflow (/ultracode off goes back to "
                            f"{s.effort_before or uc.FALLBACK}; /ultracode status shows the settings)")
    if not uc.is_on(s.effort):
        return Result(True, f"ultracode is off already (effort {s.effort})")
    back = s.effort_before if s.effort_before and not uc.is_on(s.effort_before) else uc.FALLBACK
    if err := _change(s, "ultracode", effort=back):
        return Result(False, err)
    return Result(True, f"ultracode is off: effort {back}")


def tools_change(home: Home, s, arg: str) -> Result:
    """`/tools +name -name name`: switch tools on, off or over."""
    its = items(home)
    names = {i.name for i in its}
    on = names - set(s.state.tools_removed)
    for tok in arg.split():
        sign, name = (tok[0], tok[1:]) if tok[0] in "+-" else ("~", tok)
        if name not in names:
            if name in {i.name for i in skills(home)}:
                return Result(False, f"tools: {name!r} is a skill; skills are always on and cannot be switched off")
            return Result(False, f"tools: no tool named {name!r}")
        on = on | {name} if sign == "+" else on - {name} if sign == "-" else on ^ {name}
    return apply_tools(home, s, on)


def apply_tools(home: Home, s, on) -> Result:
    its, before = items(home), set(s.state.tools_removed)
    ons, off = loadout(its, {i.name for i in its} - set(on), before)
    if s.agent is not None:                                  # an agent's own loadout is not widened to the whole tree
        ons = [r for r in ons if r.name in before]
    if err := _change(s, "tools", tools_on=ons or None, tools_off=off or None):
        return Result(False, err)
    now = {i.name for i in its} - set(s.state.tools_removed)
    return Result(True, "tools: " + (", ".join(sorted(now)) or "none"))


# the built-ins a surface without screens runs itself (the rest of the TUI's list opens screens or leaves the chat)
SERVE_SLASH = ("mode", "model", "account", "effort", "ultracode", "tools", "skill", "rename", "describe")


def catalog(home: Home, s, builtin=SERVE_SLASH) -> sl.Catalog:
    """The slash words of a message to session `s`."""
    return sl.Catalog(builtin, skill_commands(home, builtin), [],
                      {"effort": sl.Take(efforts), "ultracode": sl.Take(lambda: sl.ULTRACODE_WORDS),
                       "model": sl.Take(lambda: sl.models(home, s.account), sl.model_ok)})


@dataclass
class Prepared:
    """What a message with slash words comes to: `error` (nothing runs or is sent), the `notes` of the commands that ran
    (level, text), and the `message` to send (None: only commands)."""
    error: str = ""
    notes: list[tuple[str, str]] = field(default_factory=list)
    message: str | None = None


def prepare(home: Home, s, text: str) -> Prepared:
    """Parse the slash words of `text` (slash.py), run the commands in order, and say what is to be sent.  A command that
    leaves the chat is not available here and says so."""
    p = sl.parse(text, catalog(home, s)) if sl.has_slash(text) else sl.Parsed(text=text.strip())
    if p.error:
        return Prepared(error=p.error)
    out = Prepared()
    skills_ = skill_commands(home, SERVE_SLASH)
    for a in p.acts:
        if a.name == "skill":
            name, _, request = a.arg.partition(" ")
            if not name:
                out.notes.append(("info", "skills (always on; /<name> <request> uses one):\n" + "\n".join(
                    f"/{c}  {' '.join(sk.description.split())[:90]}" for c, sk in skills_.items())))
                return out
            sk = skills_.get(name.lower()) or skills_.get("skill:" + name.lower())
            if sk is None:
                return Prepared(error=f"skill: no skill named {name!r}", notes=out.notes)
            out.message = skill_message(home, sk, request)
            return out
        r = command(home, s, a.name, a.arg)
        if not r.ok:
            return Prepared(error=r.text, notes=out.notes)
        out.notes.append(("info", r.text))
    if p.then:
        out.notes.append(("warn", f"/{p.then.name} is not available here"))
    if p.text or p.skills:
        out.message = sl.message(home, p, skills_)
    return out
