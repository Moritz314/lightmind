"""Health checks of a tree: the boot checklist of Orbit and `lm check` are one and the same list.

Every check is real (a file read, a process asked, a folder counted) and gives one line `status label detail`.  The
checks run concurrently, one thread per group (a server, the vault, ...), and a caller shows the groups in their fixed
order as they finish: the boot screen polls `Pending.lines()`, `lm check` waits (`run_all`).  No model call, no secret
is read (a login counts when its credentials file EXISTS, see accounts.py), nothing is written.

Anything that leaves the process (git, ssh) goes through `_run`, so tests replace that one function.

May import: files, model, accounts, remote, tools (late).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, codename
from .files import Home

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
TAGS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}
NET_TIMEOUT = 4.0                    # seconds for one ssh or git fetch; a server that does not answer is a warning


_CONTROL = re.compile("[" + chr(92) + "x00-" + chr(92) + "x08" + chr(92) + "x0b-" + chr(92) + "x1f" + chr(92) + "x7f-" + chr(92) + "x9f]")      # the same set as tui.data.clean (not imported: that pulls in Textual)


def clean(s) -> str:
    """Text without C0/C1 control characters (newline and tab stay): whatever git, ssh or a file name printed cannot send
    escape sequences to the terminal, through the boot screen, its ticker note or `lm check`."""
    return _CONTROL.sub("", str(s))


@dataclass
class Line:
    status: str
    label: str
    detail: str = ""

    def __post_init__(self):
        self.label, self.detail = clean(self.label), clean(self.detail).replace(chr(10), " ").replace(chr(9), " ")


def _run(cmd: list[str], timeout: float = NET_TIMEOUT, cwd: str | None = None) -> tuple[int, str]:
    """(exit code, stdout + stderr).  -1: timeout, -2: the program is missing.  Never raises, never prompts."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=cwd, env=env, stdin=subprocess.DEVNULL, creationflags=flags)
        return r.returncode, clean(r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, f"{cmd[0]} not found"
    except OSError as e:
        return -2, str(e)


def _git(folder: Path, *args: str, timeout: float = NET_TIMEOUT) -> tuple[int, str]:
    return _run(["git", "-C", str(folder), *args], timeout)


# ------------------------------------------------------------------------------------------------ the checks
def check_runtime(home: Home) -> list[Line]:
    """The version this window runs, and the state of the runtimes: which one new windows use and how many older ones
    are still in use (normal after an update: those windows end by themselves)."""
    from . import runtime
    parts = [f"lightmind {__version__}", codename(), f"python {platform.python_version()}",
             f"{platform.system().lower()} {platform.machine().lower()}"]
    try:
        sm = runtime.summary(home)
    except OSError:
        sm = None
    if sm and sm.newest_id:
        parts.append(f"runtime {sm.own_id or 'outside the tree'}")
        if sm.own_id != sm.newest_id:
            parts.append(f"new windows use {sm.newest_id}" + (f" ({sm.newest_version})" if sm.newest_version else ""))
        n = len(sm.old_in_use)
        parts.append(f"{n} older runtime{'s' if n != 1 else ''} still in use" if n else "no older runtime in use")
    return [Line(OK, "runtime", " · ".join(x for x in parts if x))]


def check_config(home: Home) -> list[Line]:
    out = []
    for label, load in (("lightmind.md", lambda: dict(home.settings)),
                        ("permissions.md", lambda: dict(home.key_doc("permissions").meta))):
        try:
            meta = load()
            extra = ""
            if label == "permissions.md":
                n = len(home.modes())                           # raises with the reason when there are none
                extra = f" · {n} mode{'s' * (n != 1)}"
            out.append(Line(OK, "config", f"{label} parsed{extra}"))
        except Exception as e:                                  # noqa: BLE001 - any parse problem is the finding
            out.append(Line(FAIL, "config", f"{label}: {str(e).splitlines()[0][:90] if str(e) else e.__class__.__name__}"))
    return out + _mode_files(home) + _config_page(home)


def _mode_files(home: Home) -> list[Line]:
    """The mode files and the catalog of rights (`modes.findings`): a mode that cannot be used is a failure."""
    from . import modes
    try:
        found = modes.findings(home)
    except Exception as e:                                      # noqa: BLE001 - any problem is the finding
        return [Line(FAIL, "config", f"modes: {str(e)[:90] or e.__class__.__name__}")]
    level = {"fail": FAIL, "warn": WARN, "info": INFO}
    return [Line(level[lv], "config", text[:160]) for lv, text in found]


def _config_page(home: Home) -> list[Line]:
    """config.md: broken is a failure (the values of lightmind.md apply meanwhile), a key the core does not read is a
    warning, a key in both files an info.  A permissions.md that lets an agent write config.md without asking is a
    failure as well: the file is not used at all then (it names the mode of every new chat), `lm update` repairs it."""
    try:
        cfg = home.config
        why = home.unguarded_reason()
    except Exception:                                           # noqa: BLE001 - lightmind.md itself is reported above
        return []
    name = cfg.path.name
    out: list[Line] = []
    if why:
        used = "its settings are not used, those of lightmind.md apply" if cfg.state == "unguarded" else "it would not be used"
        out.append(Line(FAIL, "config", f"{name} is not guarded ({why}); {used}. Run `lm update` to add `write({{config}})` "
                                        f"to grant_files of permissions.md"))
        if cfg.state == "unguarded":
            return out
    if cfg.state == "error":
        out.append(Line(FAIL, "config", f"{name}: {cfg.error}; the settings of lightmind.md apply"))
    elif cfg.state in ("missing", "empty"):
        out.append(Line(INFO, "config", f"{name} is {cfg.state}" + ("" if cfg.state == "missing" else " (`lm update` fills it from lightmind.md)")))
    else:
        n = sum(len(v) for v in cfg.values.values())
        out.append(Line(OK, "config", f"{name} parsed · {n} setting{'s' * (n != 1)}"))
        if cfg.ignored:
            out.append(Line(WARN, "config", f"{name} is read only for its settings keys, ignored: {', '.join(cfg.ignored[:6])}"))
        if cfg.both:
            out.append(Line(INFO, "config", f"set in lightmind.md and {name}, the latter wins: {', '.join(cfg.both[:6])}"))
    return out


def check_agents(home: Home) -> list[Line]:
    """`lm lint`: every agent file reads, and the project an agent names (`project:`) exists."""
    out: list[Line] = []
    names = home.list_agents()
    for name in names:
        try:
            a = home.load_agent(name)
        except Exception as e:                                  # noqa: BLE001 - any parse problem is the finding
            out.append(Line(FAIL, "agents", f"{name}: {str(e).splitlines()[0][:90] if str(e) else e.__class__.__name__}"))
            continue
        if a.project and not home.has_project(a.project):
            out.append(Line(FAIL, "agents", f"{name}: project {a.project!r} does not exist (projects/{a.project})"))
    if not out:
        out.append(Line(OK if names else INFO, "agents", f"{len(names)} found" if names else "none yet"))
    return out


def lint_all(home: Home) -> list[Line]:
    """What `lm lint` checks (so far: the agent files)."""
    return check_agents(home)


def check_providers(home: Home) -> list[Line]:
    from .accounts import list_accounts
    try:
        rows = list_accounts(home)
    except Exception as e:                                      # noqa: BLE001
        return [Line(FAIL, "providers", f"providers/*.md unreadable: {str(e)[:90]}")]
    if not rows:
        return [Line(FAIL, "providers", "no account defined in providers/")]
    out, ready = [], 0
    for r in rows:
        name, state = r["name"], r["state"]
        what = "logged in" if r.get("logged_in") else "key present" if r.get("key_present") else "no key needed"
        if state in ("ready", "no-key-needed"):
            ready += 1
            out.append(Line(OK, f"account {name}", f"{r['type']} · {what} · {r.get('default_model') or '-'}"))
        elif state == "config-error":
            out.append(Line(FAIL, f"account {name}", r.get("hint") or "configuration error"))
        else:
            out.append(Line(WARN, f"account {name}", r.get("hint") or state))
    if not ready:
        out.append(Line(FAIL, "providers", "no account is usable"))
    return out


def _skill_files(home: Home) -> list[tuple[str, Path]]:
    fn = home.filename("skill")
    found = []
    for p in home._children("skills"):
        f = p if not p.is_dir() else next((p / n for n in (fn, "SKILL.md", "skill.md") if (p / n).is_file()), p / fn)
        if f.is_file() and f.suffix == ".md":
            found.append((p.name, f))
    return found


def check_skills(home: Home) -> list[Line]:
    good, broken = 0, []
    for name, f in _skill_files(home):
        try:
            home.doc(f)
            good += 1
        except Exception:                                       # noqa: BLE001
            broken.append(name)
    if broken:
        return [Line(WARN, "skills", f"{good} found, {len(broken)} broken: {', '.join(broken[:4])}")]
    return [Line(OK if good else INFO, "skills", f"{good} found" if good else "none installed")]


def _uv(home: Home) -> str | None:
    exe = "uv.exe" if os.name == "nt" else "uv"
    try:
        mine = home.path("bin") / exe
    except Exception:                                           # noqa: BLE001
        mine = None
    return str(mine) if mine and mine.is_file() else shutil.which("uv")


def check_tools(home: Home) -> list[Line]:
    from .tools import resolve_shell
    good, broken = 0, []
    for p in home._children("tools"):
        if not p.is_dir():
            continue
        try:
            home.load_tool(p.name)
            good += 1
        except Exception:                                       # noqa: BLE001
            broken.append(p.name)
    out = [Line(WARN if broken else OK if good else INFO, "tools",
                f"{good} found" + (f", {len(broken)} broken: {', '.join(broken[:4])}" if broken else "") if good or broken else "none installed")]
    try:
        want = home.settings.get("shell") or "bash"
        shell = resolve_shell(str(want.get("bash") or "bash") if isinstance(want, dict) else str(want), home.root)
        out.append(Line(OK, "shell", shell))
    except Exception as e:                                      # noqa: BLE001
        out.append(Line(FAIL, "shell", f"bash not found: {str(e)[:80]}"))
    uv = _uv(home)
    out.append(Line(OK, "uv", uv) if uv else Line(WARN, "uv", "not found; tool scripts with dependencies cannot start"))
    return out


def _count_notes(folder: Path) -> int:
    n = 0
    for _, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in (".git", ".obsidian")]
        n += sum(1 for f in files if f.lower().endswith(".md"))
    return n


def _remote_state(folder: Path) -> tuple[str, str | None]:
    """(detail, problem): branch, clean or changed, behind / ahead of the upstream after a quick fetch."""
    rc, out = _git(folder, "branch", "--show-current")
    branch = out if rc == 0 and out else "detached"
    rc, dirty = _git(folder, "status", "--porcelain")
    parts = [branch, "changed" if rc == 0 and dirty else "clean"]
    if _git(folder, "rev-parse", "--abbrev-ref", "@{upstream}")[0] != 0:
        return " · ".join(parts + ["no remote"]), None
    rc, msg = _git(folder, "fetch", "--quiet")
    if rc != 0:
        return " · ".join(parts + ["fetch failed"]), f"fetch {'timed out' if rc == -1 else 'failed'} (offline?)"
    rc, counts = _git(folder, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    try:
        ahead, behind = (int(x) for x in counts.split())
    except ValueError:
        return " · ".join(parts), None
    if behind:
        parts.append(f"{behind} behind (pulled at chat start)")
    if ahead:
        parts.append(f"{ahead} ahead")
    if not behind and not ahead:
        parts.append("up to date")
    return " · ".join(parts), ("behind" if behind else None)


def check_vault(home: Home) -> list[Line]:
    try:
        vault = home.path("vault")
    except Exception:                                           # noqa: BLE001
        return [Line(INFO, "vault", "no vault configured")]
    if not vault.is_dir():
        return [Line(WARN, "vault", f"folder missing: {home.rel('vault')}")]
    notes = _count_notes(vault)
    rc, top = _git(vault, "rev-parse", "--show-toplevel")
    if rc == -2:
        return [Line(WARN, "vault", f"{notes} notes · git is not installed")]
    if rc != 0 or os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(vault)):
        return [Line(INFO, "vault", f"{notes} notes · no own git repo")]
    detail, problem = _remote_state(vault)
    return [Line(WARN if problem and problem != "behind" else OK, "vault", f"{notes} notes · {detail}")]


def check_server(home: Home, name: str) -> list[Line]:
    from . import remote
    try:
        s = home.load_server(name)
    except Exception as e:                                      # noqa: BLE001
        return [Line(FAIL, f"server {name}", f"info.md unreadable: {str(e)[:80]}")]
    if s.local:
        return [Line(OK, f"server {name}", "this machine")]
    try:
        dests = remote.targets(s)
    except remote.RemoteError as e:
        return [Line(FAIL, f"server {name}", str(e)[:90])]
    label = f"server {name}"
    for dest in dests:                                          # an `ssh` list: the first that answers is the one used
        rc, msg = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", *dest, "true"], NET_TIMEOUT)
        if rc == 0:
            return [Line(OK, label, f"ssh {' '.join(dest)} reachable")]
    why = "timed out" if rc == -1 else "ssh not installed" if rc == -2 else (msg.splitlines() or ["refused"])[-1][:70]
    return [Line(WARN, label, f"ssh {' / '.join(' '.join(d) for d in dests)} not reachable: {why}")]


PULL = True                          # False while `lm check` runs: it reads the last sync result and pulls nothing


def check_sync(home: Home, pull: bool | None = None) -> list[Line]:
    """A sync repo (lm sync): the start pull's result, conflicts, config findings.  Otherwise only a plain git repo is described.
    `pull`: the boot checklist awaits the background pull of the session start; `lm check` only reads the last result."""
    from . import sync
    if sync.enabled(home):
        rows = sync.check_lines(home, PULL if pull is None else pull) + [("warn", w) for w in sync.config_warnings(home)]
        return [Line({"ok": OK, "warn": WARN}.get(s, INFO), "sync", d) for s, d in rows]
    root = home.root
    rc, top = _git(root, "rev-parse", "--show-toplevel", timeout=2)
    if rc != 0 or os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(root)):
        return [Line(INFO, "sync", "not set up (the tree is no git repo)")]
    detail, problem = _remote_state(root)
    return [Line(WARN if problem and problem != "behind" else OK, "sync", detail)]


def check_limits(home: Home) -> list[Line]:
    from .accounts import list_accounts
    out = []
    try:
        rows = list_accounts(home)
    except Exception:                                           # noqa: BLE001
        return [Line(INFO, "limits", "unknown (accounts unreadable)")]
    for r in rows:
        five, seven = r.get("five_hour"), r.get("seven_day")
        if five is None and seven is None:
            out.append(Line(INFO, f"limits {r['name']}", "none seen yet"))
            continue
        pct = lambda v: "-" if v is None else f"{v:.0f}%"        # noqa: E731
        hot = max(v for v in (five, seven) if v is not None) >= 90
        out.append(Line(WARN if hot else OK, f"limits {r['name']}",
                        f"5h {pct(five)} · 7d {pct(seven)}" + (f" (seen {r['limits_seen'][:16].replace('T', ' ')})" if r.get("limits_seen") else "")))
    return out or [Line(INFO, "limits", "no accounts")]


# ------------------------------------------------------------------------------------------------ the runner
@dataclass
class Pending:
    """One group of checks, started.  `lines()` is None while it runs."""
    name: str
    future: Future

    def lines(self) -> list[Line] | None:
        if not self.future.done():
            return None
        try:
            return self.future.result()
        except Exception as e:                                  # noqa: BLE001 - a crashed check is a finding, not a crash
            return [Line(FAIL, self.name, f"check crashed: {e.__class__.__name__}: {str(e)[:70]}")]


def groups(home: Home) -> list[tuple[str, Callable[[], list[Line]]]]:
    """The fixed order of the checklist.  One group per server, so that the ssh calls run side by side."""
    try:
        servers = home.list_servers()
    except Exception:                                           # noqa: BLE001
        servers = []
    g: list[tuple[str, Callable[[], list[Line]]]] = [
        ("runtime", lambda: check_runtime(home)), ("config", lambda: check_config(home)),
        ("providers", lambda: check_providers(home)), ("skills", lambda: check_skills(home)),
        ("tools", lambda: check_tools(home)), ("vault", lambda: check_vault(home))]
    g += [(f"server {n}", (lambda n=n: check_server(home, n))) for n in servers]
    g += [("sync", lambda: check_sync(home)), ("limits", lambda: check_limits(home))]
    return g


def start(home: Home) -> list[Pending]:
    """Start every group at once (one thread each, daemon-like: the pool is not waited for) and return them in order."""
    gs = groups(home)
    pool = ThreadPoolExecutor(max_workers=max(1, len(gs)), thread_name_prefix="lm-check")
    pending = [Pending(name, pool.submit(fn)) for name, fn in gs]
    pool.shutdown(wait=False)
    return pending


def run_all(home: Home) -> list[Line]:
    """Wait for everything: the lines in checklist order."""
    global PULL
    out: list[Line] = []
    PULL = False
    try:
        for p in start(home):
            while (ls := p.lines()) is None:
                p.future.exception()                            # blocks until done
            out += ls
    finally:
        PULL = True
    return out


def worst(lines: list[Line]) -> str:
    return FAIL if any(x.status == FAIL for x in lines) else WARN if any(x.status == WARN for x in lines) else OK


def format_plain(lines: list[Line], color: bool = False) -> str:
    """The checklist as text, one line each: `[ OK ]  label  detail`."""
    width = max((len(x.label) for x in lines), default=0)
    tint = {OK: "32", WARN: "33", FAIL: "31", INFO: "36"}
    rows = []
    for x in lines:
        tag = TAGS[x.status]
        if color:
            tag = f"\x1b[{tint[x.status]}m{tag}\x1b[0m"
        rows.append(f"{tag}  {x.label:<{width}}  {x.detail}".rstrip())
    return "\n".join(rows)


def boot_wait(home: Home) -> bool:
    """`ui.boot_wait`: true (default) = the finished checklist stays until Enter closes it, and only Enter skips it while it
    checks; false = the old way: it closes by itself after a moment, and any key skips it."""
    try:
        v = (home.settings.get("ui") or {}).get("boot_wait", True)
    except Exception:                                           # noqa: BLE001
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "off", "0")
    return not (v is False or v == 0)


def boot_mode(home: Home) -> str:
    """`ui.boot`: true | false | brief (default true).  brief = the checklist stays short: only warnings and failures are listed."""
    try:
        v = (home.settings.get("ui") or {}).get("boot", True)
    except Exception:                                           # noqa: BLE001
        return "true"
    if isinstance(v, str) and v.strip().lower() in ("brief", "false", "true", "no", "off", "yes", "on"):
        v = v.strip().lower()
        return "brief" if v == "brief" else "false" if v in ("false", "no", "off") else "true"
    return "false" if v is False else "brief" if v == "brief" else "true"
