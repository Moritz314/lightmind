"""The runtimes of a tree: where they are, which one is current, when a new one is pending, and how `lm update` gets one built.

A tree holds several builds of Lightmind side by side, so a new version can be installed while lm windows run: every
build is a folder of its own (`system/runtimes/<os>-<arch>/rN/`, a venv), the one-line file
`system/runtimes/<os>-<arch>.current` names the build that new starts use, and a window stays on the build it started
with until it ends.  The launchers (`system/lm`, `system/lm.cmd`) build, switch and clean up; this module reads the
same layout for the core (`lm check`, detached runs, `lm update`).

A new build is *pending* when the stamp the tree wants (`<state>/runtime-stamp.txt`, written by `request_rebuild`)
differs from the stamp the current build was made with (`lm-stamp.txt` inside it).  Nothing inside a runtime is ever
deleted to signal anything.  The runtime of earlier launchers (`system/runtime-<os>-<arch>/`) is the "legacy"
runtime: it is run as long as it is current and goes away once a new build exists and nobody uses it.

May import: files.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .files import Home, platform_tag

RUNTIMES = "runtimes"                      # inside `system`: one folder per machine type, a pointer file next to it
STAMP = "runtime-stamp.txt"                # inside `state`: what the tree wants, read by the launchers
USE_LOCK = "lm-use.lock"                   # inside a runtime (POSIX): held shared by every process that runs from it
LAUNCHERS = ("lm", "lm.cmd")                # the two engine launchers; lightmind, lmq and lmr only call them and never change
_ID = re.compile(r"r[0-9]+")
_hold_fd: int | None = None                # the lock this process holds on its own runtime (kept open on purpose)


def _tag() -> str:
    return "-".join(platform_tag())


def runtimes_dir(home: Home) -> Path:
    """The folder that holds the builds of this machine type, `system/runtimes/<os>-<arch>/`."""
    return home.path("system") / RUNTIMES / _tag()


def pointer_file(home: Home) -> Path:
    """The one-line file naming the build new starts use, next to `runtimes_dir`."""
    d = runtimes_dir(home)
    return d.with_name(d.name + ".current")


def legacy_dir(home: Home) -> Path:
    """The single runtime folder of earlier launchers, `system/runtime-<os>-<arch>/`."""
    return home.path("system") / f"runtime-{_tag()}"


def exe_of(rt: Path) -> Path:
    """The lm program of a runtime folder (what the launcher starts)."""
    return rt / "Scripts" / "lm.exe" if os.name == "nt" else rt / "bin" / "lm"


def python_of(rt: Path) -> Path:
    return rt / "Scripts" / "python.exe" if os.name == "nt" else rt / "bin" / "python"


def _read(f: Path) -> str:
    try:
        return f.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def pointed(home: Home) -> Path | None:
    """The build the pointer names, when the name is a plain `rN` and the folder has its lm program."""
    name = _read(pointer_file(home))
    if not _ID.fullmatch(name):
        return None
    rt = runtimes_dir(home) / name
    return rt if exe_of(rt).is_file() else None


def current(home: Home) -> Path | None:
    """The runtime new starts use: the pointed build, else the legacy runtime, else None."""
    return pointed(home) or (legacy_dir(home) if exe_of(legacy_dir(home)).is_file() else None)


def own() -> Path:
    """The runtime this process runs from."""
    return Path(sys.prefix)


def is_newest(home: Home) -> bool:
    """Does this process run from the runtime new starts use?  A process outside the runtime folders of the tree (a
    checkout, a test) counts as newest: it is the code that is being worked with."""
    cur = current(home)
    base = [runtimes_dir(home).resolve(), legacy_dir(home).resolve()]
    me = own().resolve()
    inside = any(me == b or b in me.parents for b in base)
    return not inside or (cur is not None and me == cur.resolve())


def python_for_new_process(home: Home) -> str:
    """The Python to start lm again in a new process: the newest runtime's, so a run started now has the new version;
    this process's own interpreter when the tree has no runtime."""
    cur = current(home)
    py = python_of(cur) if cur else None
    return str(py) if py and py.is_file() else sys.executable


# ----------------------------------------------------------------------------------------------- the pending signal
def stamp_file(home: Home) -> Path:
    return home.path("state") / STAMP


def wanted_stamp(home: Home) -> str:
    return _read(stamp_file(home))


def built_stamp(rt: Path) -> str:
    return _read(rt / "lm-stamp.txt")


def request_rebuild(home: Home) -> str:
    """Ask for a new build: write a fresh stamp (a unique token) to `<state>/runtime-stamp.txt`, through a temp file
    that is renamed over the old one.  The next launcher start, or `build_now`, builds a runtime whose own stamp
    differs.  Returns the token."""
    token = time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    f = stamp_file(home)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(token, encoding="ascii", newline="")
    os.replace(tmp, f)
    return token


def pending(home: Home) -> bool:
    """Is a new build wanted: there is no pointed build with the wanted stamp?  A legacy runtime counts as built with
    no stamp, so it is current only while nothing was asked for."""
    cur = current(home)
    return cur is None or built_stamp(cur) != wanted_stamp(home)


# ----------------------------------------------------------------------------------------------- what is there
def version_of(rt: Path) -> str:
    """The lightmind version installed in a runtime folder, read from its dist-info (nothing is imported); '' if unknown."""
    for d in sorted(rt.glob("*/site-packages/lightmind-*.dist-info")) + sorted(rt.glob("lib/python*/site-packages/lightmind-*.dist-info")):
        m = re.fullmatch(r"lightmind-(.+)\.dist-info", d.name)
        if m:
            return m.group(1)
    return ""


def all_runtimes(home: Home) -> list[Path]:
    """Every runtime folder of this machine type: the builds by number, then the legacy one."""
    d = runtimes_dir(home)
    builds = sorted((p for p in d.glob("*") if p.is_dir() and _ID.fullmatch(p.name)), key=lambda p: int(p.name[1:])) if d.is_dir() else []
    return builds + ([legacy_dir(home)] if legacy_dir(home).is_dir() else [])


def has_any_runtime(home: Home) -> bool:
    """Does the tree hold a runtime of ANY machine type?  The stamp is the tree's, not the machine's: a tree that
    several machines share must be built again on each of them."""
    system = home.path("system")
    folders = list(system.glob("runtime-*")) + list((system / RUNTIMES).glob("*/r*"))
    return any(p.is_dir() for p in folders)


def _processes_use(folder: Path) -> bool:
    """Does any process run from `folder`?  Where /proc is, a command line naming a file below it says so."""
    prefix = str(folder).encode() + os.sep.encode()
    try:
        for p in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if prefix in p.read_bytes():
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def in_use(rt: Path) -> bool:
    """Does a process run from this runtime?  POSIX: a build holds a shared flock on `lm-use.lock` for as long as a
    process runs from it, so an exclusive try says; a runtime without that file (the legacy one) is looked up in the
    process list.  Windows: a running program cannot be opened for writing, so opening lm.exe and python.exe tells.
    Nothing is changed."""
    if os.name == "nt":
        for exe in (exe_of(rt), python_of(rt)):
            try:
                os.close(os.open(exe, os.O_WRONLY))
            except PermissionError:
                return True
            except OSError:
                continue
        return False
    lock = rt / USE_LOCK
    if not lock.is_file():
        return _processes_use(rt)
    import fcntl
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _process_table() -> list[tuple[int, int, str]]:
    """(pid, parent pid, command line or program path) of every process.  POSIX: /proc; Windows: one PowerShell query
    (`wmic` is gone from current Windows).  Empty when the system does not tell."""
    rows: list[tuple[int, int, str]] = []
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process | ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.ParentProcessId, "
                  "($_.ExecutablePath + ' ' + $_.CommandLine) }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return rows
        for line in out.splitlines():
            pid, _, rest = line.partition("|")
            ppid, _, cmd = rest.partition("|")
            if pid.strip().isdigit() and ppid.strip().isdigit():
                rows.append((int(pid), int(ppid), cmd))
        return rows
    try:
        for p in Path("/proc").glob("[0-9]*"):
            try:
                cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                stat = (p / "stat").read_text(errors="replace")
                rows.append((int(p.name), int(stat.rsplit(")", 1)[1].split()[1]), cmd))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    return rows


def other_users(rt: Path) -> list[int]:
    """The pids of the processes that run from this runtime folder, not counting this process and the ones that started it
    (the launcher and the program stub it goes through).  Looks at command lines, so it also finds a process that holds no lock."""
    prefix = (str(rt.resolve()) + os.sep).lower()
    table = _process_table()
    parent = {pid: ppid for pid, ppid, _ in table}
    mine: set[int] = set()
    pid = os.getpid()
    while pid and pid not in mine:
        mine.add(pid)
        pid = parent.get(pid, 0)
    return sorted(pid for pid, _, cmd in table if pid not in mine and prefix in cmd.lower().replace("/", os.sep))


def launcher_is_old(home: Home) -> bool:
    """Is the launcher in the tree one of the earlier kind, whose rebuild replaces the single legacy runtime in place?  The
    new launchers keep every build side by side and name the `runtimes` folder."""
    try:
        return b"runtimes" not in launcher_of(home).read_bytes()
    except OSError:
        return False


def rebuild_blockers(home: Home) -> list[int]:
    """Pids of lm processes a rebuild would pull the runtime from under: those that run from the legacy runtime while the
    launcher on disk is an old one (it deletes that runtime).  [] when the rebuild is safe, which is always the case with the
    new launchers (they only remove builds nobody uses)."""
    legacy = legacy_dir(home)
    if not legacy.is_dir() or not launcher_is_old(home):
        return []
    return other_users(legacy)


def hold() -> None:
    """Mark the runtime this process runs from as in use: a shared flock on its `lm-use.lock`, kept for the life of
    the process.  The launcher holds the same lock for the process it starts, but a process that lm starts by itself
    (a detached run, a wake) does not inherit it; this is how they count.  Silent when there is no lock file
    (Windows, a checkout) or the lock cannot be had."""
    global _hold_fd
    if os.name == "nt" or _hold_fd is not None:
        return
    lock = own() / USE_LOCK
    try:
        import fcntl
        fd = os.open(lock, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except (OSError, ImportError):
        return
    _hold_fd = fd


@dataclass
class Summary:
    """What `lm check` says about the runtimes."""
    own_id: str                                           # folder name of the runtime of this process ('' outside the tree)
    newest_id: str                                        # folder name of the runtime new starts use
    newest_version: str
    old_in_use: list[str] = field(default_factory=list)   # folder names of runtimes in use that are not the newest


def summary(home: Home) -> Summary:
    cur = current(home)
    me = own().resolve()
    runtimes = all_runtimes(home)
    own_id = next((r.name for r in runtimes if r.resolve() == me), "")
    old = [r.name for r in runtimes if (cur is None or r.resolve() != cur.resolve()) and in_use(r)]
    return Summary(own_id=own_id, newest_id=cur.name if cur else "", newest_version=version_of(cur) if cur else "", old_in_use=old)


# ----------------------------------------------------------------------------------------------- lm update
def launcher_of(home: Home) -> Path:
    return home.path("system") / ("lm.cmd" if os.name == "nt" else "lm")


def refresh_launchers(home: Home) -> list[str]:
    """Bring the launchers in `system/` (`lm` and `lm.cmd`; the small aliases next to them only call these) to the
    shipped version.  A launcher that is the same is not touched.  A changed one is written to a temp file next to it and renamed over it: a bash
    that runs the old file keeps reading its old inode, and a cmd window that runs the old file is covered by the
    padding in lm.cmd.  Never written in place.  Returns the names replaced."""
    from .init import templates
    engine = home.rel("system") + "/"
    shipped = {rel[len(engine):]: node for rel, node in templates() if rel.startswith(engine) and rel[len(engine):] in LAUNCHERS}
    system = home.path("system")
    changed: list[str] = []
    for name, node in sorted(shipped.items()):
        data = node.read_bytes()
        dst = system / name
        if dst.is_file() and dst.read_bytes() == data:
            continue
        tmp = system / f".{name}.new"
        tmp.write_bytes(data)
        if data.startswith(b"#!"):
            tmp.chmod((tmp.stat().st_mode & 0o7777) | 0o111)
        for attempt in range(10):                           # Windows refuses the rename while cmd reads the file at that instant
            try:
                os.replace(tmp, dst)
                break
            except PermissionError:
                if attempt == 9:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.2)
        changed.append(name)
    return changed


@dataclass
class BuildResult:
    status: str                                           # built | skipped | failed
    detail: str = ""


def build_now(home: Home, *, timeout: float = 1800.0) -> BuildResult:
    """Have the launcher prepare the runtime now (`system/lm build`): it builds a new one when a new version is pending
    and starts nothing, so the next window opens at once.  The launcher's own messages go straight to the terminal.
    `skipped`: no launcher in the tree, or LM_NO_BUILD is set.  `failed`: the launcher said no (the next start tries
    again by itself)."""
    launcher = launcher_of(home)
    if os.environ.get("LM_NO_BUILD"):
        return BuildResult("skipped", "LM_NO_BUILD is set")
    if not launcher.is_file():
        return BuildResult("skipped", f"{launcher} is missing")
    try:
        r = subprocess.run([str(launcher), "build"], cwd=str(home.root), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return BuildResult("failed", str(e))
    return BuildResult("built" if r.returncode == 0 else "failed", f"exit code {r.returncode}")
