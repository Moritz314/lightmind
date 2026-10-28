"""Fetch skills from a skills repository into the tree (`lm skill add`, `lm skill list`).

A source is a folder with `skills/<name>/SKILL.md` (a local directory is read directly) or anything git can clone
(https, ssh, `file://`, a bare repo).  A repository may carry an `index.json` at its root
(`{"format": 1, "skills": [{"name", "description", "path": "skills/<name>", "files": [...]}]}`); without one the skills
are found by reading `skills/*/SKILL.md`.  The content of a repository is not trusted: names are checked, symlinks and
oversized folders are refused, and the copy is made beside the target and renamed into place, so a failure leaves nothing.
Where a skill came from is kept in `<state>/skills.json`; the core itself finds skills by listing the skills folder.

May import: files, init.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .files import Home
from .init import GIT_PROTOCOLS

DEFAULT_SOURCE = "https://github.com/Moritz314/lightmind-skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000
RECORD = "skills.json"                 # inside the state folder: {name: {source, commit, installed}}
BACKUPS = "skill-backups"              # inside the state folder, not in skills/ where a backup would show up as a skill


class SkillRepoError(Exception):
    """Something the user can fix or must know; the message is one clear line."""


@dataclass
class Offer:
    name: str
    description: str


@dataclass
class Fetched:
    root: Path          # the folder that holds `skills/`
    source: str
    commit: str = ""    # git only


def default_source(home: Home, given: str = "") -> str:
    """`--from` wins over the setting `skills.source` of config.md; both empty: the default repository."""
    if given.strip():
        return given.strip()
    section = home.settings.get("skills")
    configured = section.get("source") if isinstance(section, dict) else None
    return str(configured).strip() if configured not in (None, "") else DEFAULT_SOURCE


def _git_url_ok(source: str) -> bool:
    return bool(source) and not source.startswith("-") and not any(c in source for c in "\0\r\n")


def _clone(source: str, dest: Path) -> str:
    """Shallow clone with the protocol allow-list of the vault clone (never `ext::`); returns the commit hash."""
    if not _git_url_ok(source):
        raise SkillRepoError(f"skills source {source!r} is not a folder, git URL or repository path")
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": GIT_PROTOCOLS, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run(["git", "-c", "protocol.ext.allow=never", "clone", "--quiet", "--depth", "1", "--", source, str(dest)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=env)
        if p.returncode != 0:
            last = ((p.stderr or p.stdout).strip().splitlines() or [f"exit {p.returncode}"])[-1]
            raise SkillRepoError(f"git clone of {source} failed: {last}")
        h = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SkillRepoError(f"git clone of {source} failed: {e}") from None
    return h.stdout.strip() if h.returncode == 0 else ""


@contextlib.contextmanager
def fetch(source: str) -> Iterator[Fetched]:
    """The source as a folder on disk for the length of the `with`: a local directory with `skills/` as it is, else a clone."""
    local = Path(source).expanduser()
    if local.is_dir() and (local / "skills").is_dir():
        yield Fetched(local, source)
        return
    tmp = Path(tempfile.mkdtemp(prefix="lm-skills-"))
    try:
        commit = _clone(source, tmp / "repo")
        yield Fetched(tmp / "repo", source, commit)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ what a source offers
def _frontmatter(path: Path) -> dict[str, str]:
    """`name` and `description` of a SKILL.md header, read plainly (one line each): the file is not trusted, so no YAML loader."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        key, sep, val = ln.partition(":")
        if sep and key in ("name", "description"):
            out[key] = val.strip().strip("'\"")
    return out


def _from_index(root: Path) -> list[Offer] | None:
    """The valid entries of `index.json`; None when there is no usable index.  An entry whose name breaks the rule or whose
    path is not exactly `skills/<name>` is dropped: the index must not point outside the skills folder."""
    f = root / "index.json"
    if not f.is_file() or f.is_symlink():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        entries = data["skills"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    out: list[Offer] = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and NAME_RE.fullmatch(e["name"]) and e.get("path") == f"skills/{e['name']}":
            out.append(Offer(e["name"], str(e.get("description") or "")))
    return out


def _from_scan(root: Path) -> list[Offer]:
    out: list[Offer] = []
    base = root / "skills"
    for p in sorted(base.iterdir()) if base.is_dir() else []:
        md = p / "SKILL.md"
        if NAME_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink() and md.is_file() and not md.is_symlink():
            out.append(Offer(p.name, _frontmatter(md).get("description", "")))
    return out


def offered(root: Path) -> list[Offer]:
    idx = _from_index(root)
    return idx if idx is not None else _from_scan(root)


# ------------------------------------------------------------------ safe copy
def _check_and_list(src: Path, name: str) -> list[Path]:
    """Every file below `src`, after the checks: no symlink, only plain files and folders, within the size and count caps."""
    files: list[Path] = []
    total = 0
    for dirpath, dirs, names in os.walk(src, followlinks=False):
        for n in dirs + names:
            p = Path(dirpath) / n
            if p.is_symlink():
                raise SkillRepoError(f"skill {name!r} contains a symlink ({p.relative_to(src)}); refused")
        for n in names:
            p = Path(dirpath) / n
            if not p.is_file():
                raise SkillRepoError(f"skill {name!r} contains {p.relative_to(src)}, which is not a plain file; refused")
            total += p.stat().st_size
            files.append(p)
            if len(files) > MAX_FILES or total > MAX_BYTES:
                raise SkillRepoError(f"skill {name!r} is too big (limit {MAX_FILES} files, {MAX_BYTES // (1024 * 1024)} MB); refused")
    return files


def _copy_files(src: Path, files: list[Path], dest: Path) -> None:
    for f in files:
        target = dest / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
        shutil.copymode(f, target)


def _state(home: Home) -> Path:
    d = home.path("state")
    d.mkdir(parents=True, exist_ok=True)
    return d


def records(home: Home) -> dict[str, dict]:
    try:
        data = json.loads((_state(home) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(home: Home, name: str, entry: dict) -> None:
    data = records(home)
    data[name] = entry
    (_state(home) / RECORD).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


@dataclass
class Installed:
    name: str
    path: Path
    source: str
    commit: str
    backup: str = ""


def add(home: Home, name: str, source: str, force: bool = False) -> Installed:
    if not NAME_RE.fullmatch(name):
        raise SkillRepoError(f"{name!r} is not a skill name (lowercase letters, digits and '-', up to 64 characters)")
    skills_dir = home.path("skills")
    target = skills_dir / name
    if (target.exists() or target.is_symlink()) and not force:
        raise SkillRepoError(f"skills/{name} exists already; use --force to replace it (the old one is kept in the state folder)")
    with fetch(source) as got:
        if (got.root / "skills").is_symlink():          # a repo could point it at any folder of this machine
            raise SkillRepoError(f"skills in {source} is a symlink; refused")
        src = got.root / "skills" / name
        base = (got.root / "skills").resolve()
        if src.is_symlink() or not src.is_dir() or src.resolve().parent != base:
            have = ", ".join(o.name for o in offered(got.root)) or "none"
            raise SkillRepoError(f"{source} has no skill {name!r}; it offers: {have}")
        if not (src / "SKILL.md").is_file():
            raise SkillRepoError(f"skill {name!r} in {source} has no SKILL.md; refused")
        files = _check_and_list(src, name)
        skills_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".skill-add-{name}-", dir=skills_dir.parent))
        try:
            _copy_files(src, files, tmp)
            backup = _put_in_place(home, tmp, target, name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _record(home, name, {"source": source, "commit": got.commit, "installed": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return Installed(name, target, source, got.commit, backup)


def _put_in_place(home: Home, tmp: Path, target: Path, name: str) -> str:
    """Rename the finished copy to `skills/<name>`; an old one goes to `<state>/skill-backups/` first (and back if the rename fails)."""
    backup = ""
    if target.exists() or target.is_symlink():
        dest = _state(home) / BACKUPS / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        backup = str(dest)
    try:
        os.rename(tmp, target)
    except OSError as e:
        if backup:
            shutil.move(backup, str(target))
        raise SkillRepoError(f"could not put skills/{name} in place: {e}") from None
    return backup


def tree_rows(home: Home) -> list[dict]:
    """The skills of the tree with where each came from (empty source: made by hand or shipped)."""
    rec = records(home)
    rows = []
    for s in home.list_skills():
        folder = Path(s.path).parent.name
        info = rec.get(folder) if isinstance(rec.get(folder), dict) else {}
        rows.append({"name": s.name, "description": s.description, "source": str(info.get("source", "")),
                     "commit": str(info.get("commit", ""))})
    return rows


def available_rows(source: str) -> list[dict]:
    with fetch(source) as got:
        return [{"name": o.name, "description": o.description} for o in offered(got.root)]
