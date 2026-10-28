"""Timers and triggers that fire on a clock: `lm triggers tick`, the `timer` tool, the OS beat.

A trigger is one `.md` file (format: templates/triggers/README.md).  Two places are read:
 * `triggers/*.md`  written by people (a write there asks the user);
 * `<state>/triggers/*.md`  written by the core only (the `timer` tool and the watch): an agent cannot write there.
`tick` runs once a minute from the OS (systemd user timer, crontab, or a scheduled task).  It handles the kinds the OS
scheduler cannot: `when: every <N>m|h` and `when: in <N>m` (cron triggers stay with tools/triggers/apply.py).  A trigger
with a `session` (or `owner`) wakes that existing session (wake.py); one with an agent and a task starts a new detached run.
`until` ends a trigger: a time, or `children-done` (the owner has no open child run).  The clock state is in
`<state>/ticks.json`: a beat that was missed is made up at most once, and a beat is never fired twice.

May import: files, runs, runtime, wake, model (watch late: `watch` imports this module).
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from . import runs, runtime, wake
from .files import Home, parse_frontmatter, write_atomic

MIN_SECONDS = 60                # smallest distance of a timer
MAX_PER_SESSION = 20            # timers one session may have
MAX_HORIZON = 30 * 86400        # furthest a timer may reach
DEFAULT_UNTIL = 24 * 3600       # a repeating timer without `until` ends after a day
MAX_MESSAGE = 1000
WHEN_RE = re.compile(r"^\s*(every|in)\s+(\d+)\s*(m|min|h)\s*$", re.I)
CHILDREN_DONE = "children-done"
KINDS = ("timer", "watch")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_when(when: str) -> tuple[str, int] | None:
    """`every 15m` / `in 90m` / `every 2h` -> ("every", 900) / ("in", 5400) / ("every", 7200); None for anything else (cron ...)."""
    m = WHEN_RE.match(str(when or ""))
    if not m:
        return None
    n = int(m.group(2)) * (3600 if m.group(3).lower() == "h" else 60)
    return m.group(1).lower(), n


def parse_until(text: Any, now: float) -> float | str | None:
    """A time (ISO, local), `in 3h`, `3h`, or `children-done`; None = none."""
    s = str(text or "").strip()
    if not s:
        return None
    if s.lower() == CHILDREN_DONE:
        return CHILDREN_DONE
    m = re.match(r"^(?:in\s+)?(\d+)\s*(m|min|h|d)$", s, re.I)
    if m:
        n = int(m.group(1))
        return now + n * {"m": 60, "min": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).timestamp()
    except ValueError:
        raise ValueError(f"until: {s!r} is no time (use 2026-10-23T18:00, `in 3h` or `{CHILDREN_DONE}`)") from None


@dataclass
class Item:
    name: str
    when: str = ""
    kind: str = ""                  # timer | watch | "" (written by a person)
    session: str = ""               # an existing session to wake (a run id or a session name)
    owner: str = ""                 # the session that made it (a run id)
    until: str = ""
    task: str = ""
    agent: str = ""
    workflow: str = ""
    machine: str = ""
    enabled: bool = True
    created: float = 0.0            # epoch; 0 = first seen by the tick
    path: str = ""
    local: bool = False             # from the state folder: made by the core
    error: str = ""
    extra: dict = field(default_factory=dict)


def state_dir(home: Home) -> Path:
    return home.path("state") / "triggers"


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _load_one(p: Path, local: bool) -> Item:
    it = Item(name=p.stem, path=str(p), local=local)
    try:
        meta, body = parse_frontmatter(p.read_text("utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as e:
        it.error = f"unreadable: {e}"
        return it
    it.when, it.kind = str(meta.get("when") or "").strip(), str(meta.get("kind") or "").strip()
    it.session, it.owner = str(meta.get("session") or "").strip(), str(meta.get("owner") or "").strip()
    it.until, it.task = str(meta.get("until") or "").strip(), str(meta.get("task") or "")
    it.agent, it.workflow = str(meta.get("agent") or "").strip(), str(meta.get("workflow") or "").strip()
    it.machine, it.enabled = str(meta.get("machine") or "").strip(), meta.get("enabled", True) is not False
    it.created = _num(meta.get("created"))
    if local and it.kind not in KINDS:
        it.error = "a trigger of the state folder needs kind: timer or watch"
    return it


def load_items(home: Home) -> list[Item]:
    """Every trigger file of both places (a broken one comes back with `error` set)."""
    out: list[Item] = []
    for d, local in ((home.path("triggers"), False), (state_dir(home), True)):
        try:
            files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix == ".md" and not p.name.startswith(("_", ".")) and p.name != "README.md")
        except OSError:
            continue
        out += [_load_one(p, local) for p in files]
    return out


def _always_on(home: Home) -> bool:
    try:
        meta, _ = parse_frontmatter((home.path("state") / "local.md").read_text("utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return bool(meta.get("always_on") or (meta.get("run") or {}).get("always_on"))


def mine(home: Home, it: Item) -> bool:
    """Does this instance run the trigger?  State-folder ones are local by nature; the others follow `machine` (empty: only
    an `always_on` instance, or one that has no name because nothing is synced)."""
    if it.local:
        return True
    name = home.machine
    if it.machine:
        return bool(name) and it.machine.casefold() == name.casefold()
    return not name or _always_on(home)


def write_item(home: Home, it: Item) -> None:
    meta: dict[str, Any] = {"name": it.name, "when": it.when, "kind": it.kind, "session": it.session, "owner": it.owner,
                            "until": it.until, "task": it.task, "created": it.created, "enabled": it.enabled}
    meta = {k: v for k, v in meta.items() if v not in ("", None)}
    write_atomic(state_dir(home) / f"{it.name}.md", "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n")


def remove_item(home: Home, name: str) -> bool:
    if not NAME_RE.match(name):
        return False
    p = state_dir(home) / f"{name}.md"
    try:
        p.unlink()
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ clock state
def _ticks_file(home: Home) -> Path:
    return home.path("state") / "ticks.json"


def _load_ticks(home: Home) -> dict[str, dict]:
    try:
        d = json.loads(_ticks_file(home).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_ticks(home: Home, d: dict[str, dict]) -> None:
    write_atomic(_ticks_file(home), json.dumps(d, indent=1))


class _TickLock:
    """One tick at a time: a tick that takes longer than a minute (a slow check) must not be overlapped by the next one."""

    def __init__(self, home: Home) -> None:
        self.f = home.path("state") / "tick.lock"
        self.held = False

    def __enter__(self) -> "_TickLock":
        self.f.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {runs.process_stamp(os.getpid())}".encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    parts = self.f.read_text("utf-8").split()
                    pid, stamp = int(parts[0]), (parts[1] if len(parts) > 1 else "")
                except (OSError, ValueError, IndexError):
                    pid, stamp = 0, ""
                old = time.time() - self.f.stat().st_mtime > 6 * 3600 if self.f.exists() else True
                if old or not runs._live(pid, stamp):
                    self.f.unlink(missing_ok=True)
                    continue
                return self
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.held:
            self.f.unlink(missing_ok=True)


# ------------------------------------------------------------------ the tick
@dataclass
class TickReport:
    fired: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False           # another tick was running


def _open_children(home: Home, owner: str) -> int:
    from . import watch
    return len(watch.open_children(home, owner)) if owner else 0


def _target(home: Home, it: Item) -> str | None:
    """The run id of the session a trigger wakes, or None (no such session)."""
    ref = it.session or it.owner
    if not ref:
        return None
    try:
        rid = runs.resolve_session(home, ref)
    except ValueError:
        return None
    return rid if rid and (home.path("runs") / rid / "meta.json").is_file() else None


def _start_run(home: Home, it: Item) -> None:
    """A trigger without a session: a new detached run of its agent (the way tools/triggers/apply.py does it)."""
    argv = [runtime.python_for_new_process(home), "-m", "lightmind.cli", "--home", str(home.root), "--json", "run", "--detach", "-a", it.agent, "-"]
    subprocess.run(argv, input=it.task.encode("utf-8"), cwd=str(home.root), timeout=120, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fire(home: Home, it: Item) -> str:
    """Do what the trigger says.  Returns a short word for the report; raises on a failure."""
    if it.kind == "watch":
        from . import watch
        return watch.cycle(home, it.owner or it.session)
    if it.session or it.owner:
        rid = _target(home, it)
        if rid is None:
            if it.session and it.agent and it.task and not it.owner:        # `session: <name>` + agent + task: made by the first call
                from .proc import popen_detached
                task_file = state_dir(home) / f".{it.name}.task"
                write_atomic(task_file, it.task)                # the tick does not wait for a turn that may last an hour
                with open(task_file, "rb") as fin:
                    popen_detached([runtime.python_for_new_process(home), "-m", "lightmind.cli", "--home", str(home.root), "run", "--continue", it.session,
                                    "--create", "-a", it.agent, "-"], cwd=str(home.root), stdin=fin)
                return "session-run"
            raise LookupError(f"no session {it.session or it.owner!r}")
        text = it.task.strip() or f"Timer {it.name} fired."
        return wake.deliver(home, rid, text, "Timer", key=f"timer:{it.name}")
    if it.agent and it.task:
        _start_run(home, it)
        return "run"
    raise ValueError("no session, owner or agent: nothing to do")


def _expired(home: Home, it: Item, now: float, created: float = 0.0) -> bool:
    u = parse_until(it.until, created or now) if it.until else None      # `in 3h` counts from the creation, not from every tick
    if u == CHILDREN_DONE:
        return it.kind != "watch" and _open_children(home, it.owner or it.session) == 0
    return isinstance(u, float) and now >= u


def tick(home: Home, now: float | None = None) -> TickReport:
    """Evaluate every trigger once: fire what is due, end what is over, then sweep the inboxes."""
    now = time.time() if now is None else now
    rep = TickReport()
    with _TickLock(home) as lk:
        if not lk.held:
            rep.skipped = True
            return rep
        ticks = _load_ticks(home)
        seen: set[str] = set()
        for it in load_items(home):
            seen.add(it.name)
            if it.error or not it.enabled or not mine(home, it):
                continue
            w = parse_when(it.when)
            if w is None:
                continue                                    # cron and the other kinds are not for the tick
            kind, secs = w
            st = ticks.setdefault(it.name, {"created": it.created or now, "last": 0.0, "n": 0})
            if st.get("done"):
                continue
            if it.until and _bad_until(it, now):
                rep.errors.append(f"{it.name}: bad until {it.until!r}")
                st["done"] = True
                continue
            if _expired(home, it, now, st["created"]):
                st["done"] = True
                rep.retired.append(it.name)
                if it.local:
                    remove_item(home, it.name)
                continue
            beat = (st["last"] or st["created"]) + secs <= now
            if it.kind == "watch":
                owner = it.owner or it.session
                try:
                    from . import watch
                    if not (beat or watch.poll(home, owner)):      # a cheap look every beat; a child that changed state is acted on at once
                        continue
                    st["last"], st["n"] = now, int(st.get("n", 0)) + 1
                    rep.fired.append(f"{it.name}:{watch.cycle(home, owner)}")
                    if watch.retire(home, owner):                  # decided under the book lock: a child registered just now keeps its watch
                        st["done"] = True
                        rep.retired.append(it.name)
                except Exception as e:                      # noqa: BLE001 - a failing watch must not stop the other triggers
                    rep.errors.append(f"{it.name}: {type(e).__name__}: {e}")
            elif beat:
                st["last"], st["n"] = now, int(st.get("n", 0)) + 1      # before the call: a crash in it never makes a double fire
                if kind == "in":
                    st["done"] = True
                    rep.retired.append(it.name)
                _save_ticks(home, ticks)
                try:
                    rep.fired.append(f"{it.name}:{fire(home, it)}")
                except Exception as e:                      # noqa: BLE001
                    rep.errors.append(f"{it.name}: {type(e).__name__}: {e}")
                    if isinstance(e, LookupError):
                        st["done"] = True
            if st.get("done") and it.local:
                remove_item(home, it.name)
                if it.kind == "watch":
                    ticks.pop(it.name, None)                    # a later child starts a fresh watch trigger of the same name
        ticks = {k: v for k, v in ticks.items() if k in seen}      # state of a deleted trigger goes
        _save_ticks(home, ticks)
        try:
            sweep(home, now)
        except Exception as e:                              # noqa: BLE001
            rep.errors.append(f"sweep: {type(e).__name__}: {e}")
    return rep


def _bad_until(it: Item, now: float) -> bool:
    try:
        parse_until(it.until, now)
        return False
    except ValueError:
        return True


def sweep(home: Home, now: float) -> None:
    """Inboxes that nobody reads: no process holds the session -> continue it headless; a process holds it but has not read
    the entry for `wake.STALE` seconds (it is no chat with a screen) -> tell the user once."""
    try:
        boxes = list(home.path("runs").glob(f"*/{wake.INBOX}"))
    except OSError:
        return
    for f in boxes:
        rid = f.parent.name
        if not wake.pending(home, rid):
            f.unlink(missing_ok=True)
            continue
        if not runs.lock_holder(home, rid):
            wake.start_headless(home, rid)
        elif wake.oldest_age(home, rid) > wake.STALE:
            from . import watch
            watch.notify(home, f"Session {rid} is busy and has not read a message from a timer or the watch for "
                               f"{int(wake.STALE // 60)} minutes.", key=f"stale:{rid}")


# ------------------------------------------------------------------ the `timer` tool
def _mine_timers(home: Home, session: str) -> list[Item]:
    return [i for i in load_items(home) if i.local and i.kind == "timer" and i.owner == session]


def set_timer(home: Home, session: str, when: str, message: str, until: str = "", now: float | None = None) -> Item:
    """Make a timer for session `session` (always the caller's own).  Raises ValueError with the reason when refused."""
    now = time.time() if now is None else now
    w = parse_when(when if re.match(r"^\s*(every|in)\b", str(when), re.I) else f"in {when}")
    if w is None:
        raise ValueError("when: use `in 20m`, `every 15m` or `every 2h`")
    kind, secs = w
    if secs < MIN_SECONDS:
        raise ValueError(f"the smallest distance is {MIN_SECONDS // 60} minute")
    if secs > MAX_HORIZON:
        raise ValueError("a timer reaches 30 days at most")
    message = wake.plain(message, MAX_MESSAGE).strip()
    if not message:
        raise ValueError("a timer needs a message")
    if len(_mine_timers(home, session)) >= MAX_PER_SESSION:
        raise ValueError(f"this session has {MAX_PER_SESSION} timers already: cancel one first")
    u = parse_until(until, now)
    if isinstance(u, float):
        if u <= now:
            raise ValueError("until is in the past")
        if u - now > MAX_HORIZON:
            raise ValueError("until reaches 30 days at most")
        until_s = datetime.fromtimestamp(u).strftime("%Y-%m-%dT%H:%M:%S")
    elif u == CHILDREN_DONE:
        until_s = CHILDREN_DONE
    else:
        until_s = datetime.fromtimestamp(now + DEFAULT_UNTIL).strftime("%Y-%m-%dT%H:%M:%S") if kind == "every" else ""
    it = Item(name=f"timer-{_secrets.token_hex(4)}", when=f"{kind} {secs // 60}m", kind="timer", session=session, owner=session,
              until=until_s, task=message, created=now, local=True)
    write_item(home, it)
    return it


def list_timers(home: Home, session: str, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    ticks = _load_ticks(home)
    out = []
    for it in _mine_timers(home, session):
        w = parse_when(it.when) or ("in", 0)
        st = ticks.get(it.name, {})
        nxt = (st.get("last") or it.created or now) + w[1]
        out.append({"id": it.name, "when": it.when, "next": datetime.fromtimestamp(nxt).strftime("%H:%M"),
                    "until": it.until, "message": it.task[:80]})
    return out


def cancel_timer(home: Home, session: str, name: str) -> bool:
    """Only the session's own timers: a name of another session's timer (or of a watch) is refused."""
    mine_ = {i.name for i in _mine_timers(home, session)}
    return name in mine_ and remove_item(home, name)


# ------------------------------------------------------------------ the OS beat
UNIT = "lightmind-tick"
CRON_BEGIN, CRON_END = "# lightmind tick begin", "# lightmind tick end"


def _exe(home: Home) -> list[str]:
    win = os.name == "nt"
    launcher = home.root / "system" / ("lm.cmd" if win else "lm")
    return [str(launcher)] if launcher.is_file() else [sys.executable, "-m", "lightmind.cli", "--home", str(home.root)]


def _systemd_user() -> bool:
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def unit_name(home: Home) -> str:
    """One beat per tree: two trees of one user do not overwrite each other's unit."""
    import hashlib
    return f"{UNIT}-{hashlib.sha1(str(home.root).encode('utf-8')).hexdigest()[:8]}"


def _unit_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_units(home: Home) -> tuple[str, str]:
    cmd = " ".join(_unit_quote(a) for a in _exe(home) + ["triggers", "tick"])
    name = unit_name(home)
    service = (f"[Unit]\nDescription=Lightmind tick (timers and the watch of {home.root.name})\n\n[Service]\nType=oneshot\n"
               f"Environment={_unit_quote('LIGHTMIND_HOME=' + str(home.root))}\nExecStart={cmd}\n")
    timer = (f"[Unit]\nDescription=Lightmind tick every minute\n\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\n"
             f"AccuracySec=5s\nUnit={name}.service\n\n[Install]\nWantedBy=timers.target\n")
    return service, timer


def installed(home: Home) -> str:
    """Which beat runs the tick here: 'systemd', 'cron', 'schtasks' or ''."""
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Query", "/TN", "Lightmind tick"], capture_output=True)
        return "schtasks" if r.returncode == 0 else ""
    if _systemd_user() and (_unit_dir() / f"{unit_name(home)}.timer").is_file():
        return "systemd"
    if shutil.which("crontab"):
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode == 0 and CRON_BEGIN in r.stdout:
            return "cron"
    return ""


def install(home: Home) -> str:
    """Set up the beat that calls `lm triggers tick` every minute.  Returns the way used."""
    import shlex
    if os.name == "nt":
        cmd = " ".join(f'"{a}"' for a in _exe(home) + ["triggers", "tick"])
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "1", "/TN", "Lightmind tick", "/TR", cmd],
                       check=True, capture_output=True)
        return "schtasks"
    if _systemd_user():
        d = _unit_dir()
        d.mkdir(parents=True, exist_ok=True)
        service, timer = render_units(home)
        write_atomic(d / f"{unit_name(home)}.service", service)
        write_atomic(d / f"{unit_name(home)}.timer", timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{unit_name(home)}.timer"], check=True, capture_output=True)
        return "systemd"
    if shutil.which("crontab"):
        cmd = " ".join(shlex.quote(a) for a in _exe(home) + ["triggers", "tick"])
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        base = _strip_cron(cur.stdout if cur.returncode == 0 else "")
        block = f"{CRON_BEGIN}\n* * * * * LIGHTMIND_HOME={shlex.quote(str(home.root))} {cmd} >/dev/null 2>&1\n{CRON_END}\n"
        subprocess.run(["crontab", "-"], input=base + block, text=True, check=True)
        return "cron"
    raise RuntimeError("no systemd user manager, no crontab: run `lm triggers tick` every minute by hand")


def _strip_cron(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if line.strip() == CRON_BEGIN:
            skip = True
        elif line.strip() == CRON_END:
            skip = False
        elif not skip:
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def uninstall(home: Home) -> str:
    way = installed(home)
    if way == "schtasks":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "Lightmind tick"], capture_output=True)
    elif way == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{unit_name(home)}.timer"], capture_output=True)
        for n in (f"{unit_name(home)}.timer", f"{unit_name(home)}.service"):
            (_unit_dir() / n).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    elif way == "cron":
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        subprocess.run(["crontab", "-"], input=_strip_cron(cur.stdout), text=True, check=True)
    return way
