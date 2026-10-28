"""Result folders: `<runs>/ergebnisse/<YYYY-MM-DD> <title>/` holds what a run produces for a person, and the run folder
keeps `out` as a junction (Windows) or symlink (POSIX) to it, so `$LM_RESULTS` = `<run>/out` stays stable while the
folder is renamed after the title.  Once it holds a file its name is fixed: links to it may already be in a chat.  Pure file helpers (names, links, claiming a free folder); `runs.RunDir` decides when.
May import: nothing of the core.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("lightmind.results")

FOLDER = "ergebnisse"                  # below the `runs` location; holds no meta.json, so run listings skip it
TITLE_MAX = 50
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
COUNTER_RE = re.compile(r" \((\d+)\)$")
NOTE_FILE = "ergebnis.txt"             # fallback when no link can be made: the path of the result folder


def clean_name(text: str, limit: int = TITLE_MAX, fallback: str = "ohne Titel") -> str:
    """A file or folder name that Windows accepts: no `<>:"/\\|?*`, no control characters, no trailing dot or space,
    no reserved device name; at most `limit` characters (umlauts stay).  `fallback` when nothing is left."""
    text = " ".join(BAD_CHARS.sub(" ", str(text or "")).split())
    text = text[:limit].rstrip(" .")
    if not text.strip(" ."):
        return fallback
    if text.split(".")[0].strip().upper() in RESERVED:
        text = "_" + text
    return text


def base_name(start: str, title: str, agent: str, run_id: str) -> str:
    """`<date> <title>`; without a title `<date> <agent> <short id>` (the 4 hex of the run id).  `start` is the ISO start
    of the run (its date is the day the run began)."""
    day = start[:10] if re.fullmatch(r"\d{4}-\d\d-\d\d", start[:10]) else _today()
    if title.strip():
        return f"{day} {clean_name(title)}"
    return f"{day} {clean_name(agent or 'chat', 20, 'chat')} {clean_name(run_id.rsplit('-', 1)[-1], 8, 'run')}"


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def strip_counter(name: str) -> str:
    """`Foo (2)` -> `Foo`: the name without the counter that makes it unique."""
    return COUNTER_RE.sub("", name)


def candidates(base: str):
    """`base`, `base (2)`, `base (3)` ..."""
    yield base
    n = 2
    while True:
        yield f"{base} ({n})"
        n += 1


def claim_folder(root: Path, base: str) -> Path:
    """Create `root/<base>` or the first `root/<base> (n)` that is free and return it.  `mkdir` without `exist_ok` is
    the claim: two processes never get the same folder."""
    root.mkdir(parents=True, exist_ok=True)
    for name in candidates(base):
        p = root / name
        try:
            p.mkdir()
            return p
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


def free_name(root: Path, base: str, own: str = "") -> str:
    """The first name from `base`, `base (2)` ... that does not exist in `root`; `own` (the folder being renamed)
    counts as free, so a folder that already has the right name keeps it."""
    for name in candidates(base):
        if name == own or not (root / name).exists():
            return name
    raise AssertionError("unreachable")


def unique_file(folder: Path, stem: str, suffix: str = ".md") -> Path:
    """`folder/<stem><suffix>` or the first `<stem> (n)<suffix>` that does not exist yet; the file is created empty (the
    claim).  `stem` is cleaned first."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = clean_name(stem, 40, "ergebnis")
    for name in candidates(stem):
        p = folder / f"{name}{suffix}"
        try:
            with p.open("x"):
                return p
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


def short_task(task: str, label: str, sid: str) -> str:
    """Short name for a sub-agent's answer: the first words of its brief, else the agent's name, else its id."""
    words = " ".join(str(task or "").split()[:6])
    return clean_name(words or label or sid, 40, "ergebnis")


def has_content(folder: Path) -> bool:
    """True when the folder holds anything; then its name is kept, because file links to it may already be out."""
    try:
        return any(folder.iterdir())
    except OSError:
        return True                                      # unreadable: rather keep the name


def is_link(p: Path) -> bool:
    """True for a symlink and for a Windows junction (`os.path.islink` does not see junctions)."""
    return os.path.islink(p) or bool(getattr(os.path, "isjunction", lambda _p: False)(p))


def drop_link(p: Path) -> None:
    """Remove a link without touching its target: `unlink` for a symlink, `rmdir` for a junction."""
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)


def make_link(link: Path, target: Path) -> bool:
    """`link` -> `target`: `_winapi.CreateJunction` on Windows (no admin right needed), a symlink elsewhere.  False when it
    cannot be made (the caller then writes `ergebnis.txt`)."""
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, ImportError, AttributeError) as e:
        log.warning("result link %s -> %s not made: %s", link, target, e)
        return False


def link_points_to(link: Path, target: Path) -> bool:
    """True when `link` is a link that leads to `target`."""
    if not is_link(link):
        return False
    try:
        return os.path.samefile(link, target)
    except OSError:
        return False


def write_note(run_dir: Path, target: Path) -> None:
    """Fallback for a missing link: `<run>/ergebnis.txt` holds the absolute path of the result folder."""
    try:
        (run_dir / NOTE_FILE).write_text(str(target) + "\n", "utf-8")
    except OSError as e:
        log.warning("%s not written: %s", NOTE_FILE, e)
