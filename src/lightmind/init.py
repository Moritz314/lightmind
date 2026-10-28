"""Create and update a user tree from the shipped templates (`lm init`, `lm update`).

`init_tree` copies `templates/` (a file named `dot-x` becomes `.x`; files starting with `#!` get the
executable bit) and records every shipped path in `<state>/templates.json`.  `update_tree` adds only
paths that were never shipped before: a file the user changed is never touched, a file the user
deleted is never recreated.  The one exception is the engine folder `system/`: if the record of
shipped files is gone, `update_tree` rebuilds `system/` from the templates.

Next to that record `init_tree` writes `<state>/install-source.txt`: where the running lightmind was
installed from.  The launchers (`system/lm`, `system/lm.cmd`) read it, one line of plain text, to build
the runtime of the tree on the first start.

`config.md` needs more than a copy: in a tree made before it existed, `update_tree` (and the start of the TUI) guards it
in permissions.md and fills an empty one with the template plus the values lightmind.md holds today, which are moved,
not copied (`migrate_config`).

May import: files, model, settings, runtime.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse
from urllib.request import url2pathname

from . import __version__, runtime, settings
from .files import CONFIG, CONFIG_KEYS, ENTRY, LOCAL, MODES, RIGHTS, Home, PathsError, read_config
from .settings import SettingsError

TRACK = "templates.json"          # inside the `state` location of lightmind.md
INSTALL_SOURCE = "install-source.txt"     # inside `state` too: one line, read by the launchers (see install_source)
EXTRAS = "tui"                    # installed next to lightmind by the launchers (system/lm and lm.cmd name it too)
GIT_PROTOCOLS = "https:ssh:git:file"      # what a vault repo may be cloned over; never a remote helper such as ext::
_SKIP_DIRS = {"__pycache__"}


@dataclass
class InitResult:
    root: Path
    created: list[str] = field(default_factory=list)     # tree-relative paths written by this call
    vault: str = ""                                       # paths.vault of the new tree
    warnings: list[str] = field(default_factory=list)


def _walk(node, prefix: str = "") -> Iterator[tuple[str, object]]:
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        if child.name in _SKIP_DIRS or child.name.endswith(".pyc"):
            continue
        if child.is_dir():
            yield from _walk(child, prefix + child.name + "/")
        else:
            name = "." + child.name[4:] if child.name.startswith("dot-") else child.name
            yield prefix + name, child


def templates() -> Iterator[tuple[str, object]]:
    """(tree-relative path, template file) for everything the package ships."""
    return _walk(resources.files("lightmind") / "templates")


def shipped_paths() -> list[str]:
    return sorted(rel for rel, _ in templates())


def _write(root: Path, rel: str, node) -> None:
    dst = root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = node.read_bytes()
    dst.write_bytes(data)
    if data.startswith(b"#!"):
        dst.chmod((dst.stat().st_mode & 0o7777) | 0o111)


def _track_file(home: Home) -> Path:
    return home.path("state") / TRACK


def _load_track(home: Home) -> dict:
    try:
        rec = json.loads(_track_file(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _save_track(home: Home, shipped: set[str]) -> None:
    f = _track_file(home)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": __version__, "shipped": sorted(shipped)}, indent=2) + "\n",
                 encoding="utf-8", newline="\n")


def _direct_url() -> dict:
    """The PEP 610 record (`direct_url.json`) of the installed lightmind; {} when there is none."""
    try:
        raw = metadata.distribution("lightmind").read_text("direct_url.json")
        data = json.loads(raw or "{}")
    except (metadata.PackageNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def install_source() -> str:
    """Where the running lightmind was installed from, as ONE line the launchers can read without
    Python: `folder <path>` (a source checkout, also an editable install), `wheel <path>` (a wheel
    file) or `requirement <PEP 508>` (the package index, a URL, git; the launcher passes it to uv as it
    is, so it names the extras).  Taken from `direct_url.json` of the installed distribution; no such
    record means the package index, at this version."""
    data = _direct_url()
    url = str(data.get("url") or "")
    path = Path(url2pathname(urlparse(url).path)) if url.startswith("file:") else None
    if path is not None and "dir_info" in data and path.is_dir():
        line = f"folder {path}"
    elif path is not None and "archive_info" in data and path.suffix == ".whl" and path.is_file():
        line = f"wheel {path}"
    elif url and "vcs_info" in data:
        vcs = data["vcs_info"] if isinstance(data["vcs_info"], dict) else {}
        rev = vcs.get("commit_id") or vcs.get("requested_revision") or ""
        sub = data.get("subdirectory")
        line = (f"requirement lightmind[{EXTRAS}] @ {vcs.get('vcs') or 'git'}+{url}"
                + (f"@{rev}" if rev else "") + (f"#subdirectory={sub}" if sub else ""))
    elif url.startswith(("http://", "https://")) and "archive_info" in data:
        line = f"requirement lightmind[{EXTRAS}] @ {url}"
    else:
        line = f"requirement lightmind[{EXTRAS}]=={__version__}"
    return line if not any(c in line for c in "\r\n\0") else f"requirement lightmind[{EXTRAS}]=={__version__}"


def _save_install_source(home: Home, *, only_if_missing: bool = False) -> None:
    f = home.path("state") / INSTALL_SOURCE
    if only_if_missing and f.exists():
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(install_source() + "\n", encoding="utf-8", newline="\n")


def repo_name(url: str) -> str:
    """'https://example.com/me/notes(.git)' / 'git@host:o/notes.git' / a local path
    -> 'notes'."""
    tail = re.split(r"[/\\:]", url.strip().rstrip("/\\"))[-1]
    tail = tail[:-4] if tail.endswith(".git") else tail
    return re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip(".-") or "vault-repo"


def _set_vault_path(text: str, value: str) -> str:
    """Set `vault:` inside the `paths:` block of lightmind.md, keeping comments and layout."""
    lines, inside = text.split("\n"), False
    for i, ln in enumerate(lines):
        if re.match(r"^paths:\s*(#.*)?$", ln):
            inside = True
        elif inside and ln.strip() and not ln.startswith((" ", "\t", "#")):
            break
        elif inside and re.match(r"^\s+vault:\s*\S", ln):
            lines[i] = re.sub(r"^(\s+vault:\s*)\S+", lambda m: m.group(1) + value, ln, count=1)
            break
    return "\n".join(lines)


def check_repo(repo: str) -> str:
    """The repo value as git gets it.  A value that starts with `-` would be read as a git option
    (`--upload-pack=...` runs a command), and a control character has no place in a URL or path:
    ValueError for both."""
    repo = repo.strip()
    if not repo or repo.startswith("-") or any(c in repo for c in "\0\r\n"):
        raise ValueError(f"vault repo {repo!r} is not a git URL or path")
    return repo


def _clone_vault(home: Home, repo: str, git: str, res: InitResult) -> None:
    parent = home.path("vault")
    dest = parent / repo_name(repo)
    if dest.exists() and any(dest.iterdir()):
        res.warnings.append(f"vault: {dest} exists and is not empty; not cloned")
        return
    parent.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run([git, "-c", "protocol.ext.allow=never", "clone", "--quiet", "--", repo, str(dest)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                           env={**os.environ, "GIT_ALLOW_PROTOCOL": GIT_PROTOCOLS})
    except (OSError, subprocess.TimeoutExpired) as e:
        res.warnings.append(f"vault: git clone failed: {e}")
        return
    if p.returncode != 0:
        last = ((p.stderr or p.stdout).strip().splitlines() or [f"exit {p.returncode}"])[-1]
        res.warnings.append(f"vault: git clone failed: {last}")
        return
    rel = f"{home.rel('vault')}/{dest.name}/"
    entry = home.root / ENTRY
    entry.write_text(_set_vault_path(entry.read_text(encoding="utf-8"), rel), encoding="utf-8", newline="\n")
    ignore = home.root / ".gitignore"            # a nested repo must not be added to the tree's own repo
    if ignore.is_file() and rel not in ignore.read_text(encoding="utf-8").split("\n"):
        ignore.write_text(ignore.read_text(encoding="utf-8").rstrip("\n") + f"\n{rel}\n", encoding="utf-8", newline="\n")


# ----------------------------------------------------------------- config.md in an older tree
_ANCHORS = ("write({entry})", "write({permissions})")          # the line `write({config})` goes behind


def _template_text(rel: str) -> str:
    return (resources.files("lightmind") / "templates" / rel).read_bytes().decode("utf-8")


def config_guarded(home: Home) -> bool:
    """Does permissions.md make an agent ask before it writes or edits config.md, in every mode?  The rule checker
    itself is asked (`Home.unguarded_reason`); what it cannot judge is not guarded."""
    return not home.unguarded_reason()


def protect_config(home: Home) -> list[str]:
    """Make an agent ask before it writes config.md: add `write({config})` to the locked-ask list of permissions.md,
    right behind `write({entry})`.  config.md picks the mode of every new chat, and `bypass` asks nothing; without this
    line its values are not used at all (`Home.config` is then `unguarded`).  Does nothing when the file is guarded
    already, only ever adds a line, and keeps the old file as `permissions.md.bak-<date>`.  Returns notes (what was
    done, or what is left to do by hand)."""
    why = home.unguarded_reason()
    if not why:
        return []
    had = read_config(home.config_file)                        # what config.md held while nothing guarded it
    try:
        f = home.path("permissions")
    except PathsError:
        return [f"permissions.md: config.md is not guarded ({why})"]
    if not f.is_file():
        return [f"permissions.md: config.md is not guarded ({why})"]
    try:
        text = settings.decode(f.read_bytes())
        meta = settings.front_meta(text)
        items = meta.get("grant_files")
        if isinstance(items, list) and "write({config})" in items:
            return [f"permissions.md: config.md is not guarded although grant_files lists `write({{config}})`: {why}"]
        anchor = next((a for a in _ANCHORS if isinstance(items, list) and a in items), None)
        line = next((m for a in _ANCHORS if anchor == a
                     for m in re.finditer(r"^([ \t]*)- " + re.escape(a) + r"[^\r\n]*(\r?\n)", text, re.M)), None)
        if anchor is None or line is None:
            raise SettingsError("no `write({entry})` line in grant_files to put it behind")
        new = text[:line.end()] + f"{line.group(1)}- write({{config}}){line.group(2)}" + text[line.end():]
        expect = copy.deepcopy(meta)                           # an alias of the list (`*grant_files`) follows the list itself
        expect["grant_files"].insert(expect["grant_files"].index(anchor) + 1, "write({config})")
        settings.prove_text(text, new, expect)
        kept = settings.backup(f)
        settings.write_atomic(f, new.encode("utf-8"))
        home.forget(f)
    except (OSError, SettingsError) as e:
        return [f"permissions.md: add `write({{config}})` to grant_files by hand, config.md is not guarded yet ({e})"]
    if (still := home.unguarded_reason()):
        return [f"permissions.md: `write({{config}})` added, but config.md is still not guarded: {still} "
                f"(a mode file must list number 14 of rechte.md under `fragt:` or block it; in a `modes:` block of "
                f"permissions.md a mode needs `locked: ask: *grant_files`, bypass `locked: deny: *grant_files`)"]
    notes = [f"permissions.md: config.md is guarded now (`write({{config}})` added; the old file is {kept.name if kept else 'gone'})"]
    if had.state == "ok" and had.values:                       # it was not in effect until now: say what starts to apply
        shown = ", ".join(f"{s}.{k}={str(v)[:40]}" for s, d in had.values.items() for k, v in d.items())
        notes.append(f"config.md already holds settings, they apply from now on: {shown} "
                     f"(look them over: nothing guarded the file before)")
    return notes


def fill_config(home: Home, create: bool = False) -> list[str]:
    """Fill an empty config.md (a missing one as well, with `create`) with the template and the values lightmind.md holds
    today.  The values are MOVED: written to config.md and removed from lightmind.md, which stays as
    `lightmind.md.bak-<date>`; `defaults.server` goes to <state>/local.md.  Everything is built and proved in memory
    first; when anything fails nothing is written.  The core does this, never an agent.  Returns notes."""
    cfg = home.config
    if cfg.state != "empty" and not (create and cfg.state == "missing"):
        return []
    entry = home.entry.meta
    moved = {(s, k): entry[s][k] for s, keys in CONFIG_KEYS.items() for k in keys
             if isinstance(entry.get(s), dict) and entry[s].get(k) is not None}
    server = settings.dig(entry, ("defaults", "server"))
    notes: list[str] = []
    try:
        config_text = settings.edit_text(_template_text(CONFIG), moved)
        local_file, local_text = home.path("state") / LOCAL, None
        if server is not None:
            try:
                old = settings.decode(local_file.read_bytes()) if local_file.is_file() else ""
                if settings.dig(settings.front_meta(old) if old.strip() else {}, ("defaults", "server")) is None:
                    local_text = settings.edit_text(old if old.strip() else "---\n---\n", {("defaults", "server"): server})
            except SettingsError as e:
                notes.append(f"defaults.server stays in lightmind.md: local.md cannot take it ({e})")
                server = None
        drop = list(moved) + ([("defaults", "server")] if server is not None else [])
        entry_file = home.root / ENTRY
        entry_text = settings.decode(entry_file.read_bytes())
        entry_new = settings.drop_keys(entry_text, drop, "moved to config.md: " + ", ".join(".".join(p) for p in moved)
                                       + ("; defaults.server is in system/state/local.md (this machine only)" if server is not None else "")
                                       ) if drop else None
        if home.config.state not in ("empty", "missing"):       # filled by another process meanwhile
            return []
        if local_text is not None:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            settings.backup(local_file)
            settings.write_atomic(local_file, local_text.encode("utf-8"))
            home.forget(local_file)
        home.config_file.parent.mkdir(parents=True, exist_ok=True)
        settings.write_atomic(home.config_file, config_text.encode("utf-8"))
        home.forget(home.config_file)
        if entry_new is not None and entry_new != entry_text:
            kept = settings.backup(entry_file)
            settings.write_atomic(entry_file, entry_new.encode("utf-8"))
            home.forget(entry_file)
            notes.append(f"lightmind.md: {len(drop)} values moved out of it (the old file is {kept.name if kept else 'gone'})")
        notes.insert(0, f"config.md: filled from the template with {len(moved)} values of lightmind.md")
        if local_text is not None:
            notes.append(f"defaults.server ({server}) is in system/state/local.md now")
    except (OSError, SettingsError) as e:
        return [f"config.md: not filled, nothing was changed ({e})"]
    return notes


def migrate_config(home: Home, create: bool = False, repair: bool = False) -> list[str]:
    """What an older tree needs for config.md, in this order: the guard in permissions.md, then the fill.  `repair`
    (`lm update`) puts the guard back whenever it is missing, because config.md is not used without it.  At the start
    of the TUI only a config.md that is about to be filled gets its guard: a line the user took out is not put back
    behind the user's back (the checklist says FAIL and `lm update` repairs it).  Nothing is filled while the guard is
    missing: a page an agent could write without asking is not given the values of lightmind.md.  Returns notes."""
    notes: list[str] = []
    filling = home.config.state == "empty" or (create and home.config.state == "missing")
    if repair or filling:
        notes += protect_config(home)
    if filling and config_guarded(home):
        notes += fill_config(home, create)
    return notes


def init_tree(target: str | Path | None = None, vault_repo: str | None = None, *,
              git: str = "git") -> InitResult:
    """Create a tree at `target` (default: $LIGHTMIND_HOME, else ~/lightmind).  Never overwrites a
    file.  With `vault_repo` (a git URL or path) the repo is cloned into `<vault>/<repo-name>/` and
    `paths.vault` of the new lightmind.md points there; a failed clone leaves the default vault and
    is reported in `warnings`.  The repo is cloned over https, ssh, git or file only, and a value that
    git would take for an option raises ValueError before anything is created.  Raises FileExistsError
    when `target` already holds a tree."""
    if vault_repo:
        vault_repo = check_repo(vault_repo)
    root = Home.resolve_root(str(target) if target is not None else None)
    if (root / ENTRY).exists():
        raise FileExistsError(f"{root} is already a Lightmind tree; use update")
    res = InitResult(root=root)
    for rel, node in templates():
        if not (root / rel).exists():
            _write(root, rel, node)
            res.created.append(rel)
    home = Home(root)
    if vault_repo:
        _clone_vault(home, vault_repo, git, res)
    res.vault = home.rel("vault")
    _save_track(home, set(res.created))
    _save_install_source(home)
    return res


class Created(list):
    """The paths `update_tree` created (a plain list); `notes` says what else it did to the tree."""

    def __init__(self, items=()):
        super().__init__(items)
        self.notes: list[str] = []
        self.launchers: list[str] = []


def update_tree(target: str | Path | None = None, *, schedule_rebuild: bool = True) -> Created:
    """Add template files that earlier versions did not ship; returns the paths created.  Files
    already shipped are left alone, whether changed, unchanged or deleted by the user.  config.md is handled by
    `migrate_config` (guard in permissions.md, an empty file filled from lightmind.md), see there; what it did is
    in `.notes` of the result.

    A tree without its record of shipped files (the `system/` folder was deleted, or the tree is a git
    clone: git ignores `system/`) gets its engine folder rebuilt: the template files of `system/` are
    created again and every other template counts as shipped, because what is missing there was
    deleted on purpose.  Raises FileNotFoundError when `target` is no tree, or when the record is
    missing and `lightmind.md` has no `paths.system` to tell which templates belong to the engine.

    The launchers in `system/` are brought to the shipped version (`runtime.refresh_launchers`, names in `.launchers`),
    but only by the newest runtime: an older window must not put its older launchers over newer ones.  After that, and
    with `schedule_rebuild`, a new runtime is asked for (`_schedule_runtime_rebuild`)."""
    root = Home.resolve_root(str(target) if target is not None else None)
    home = Home(root)
    f = _track_file(home)
    if f.is_file():
        shipped = set(_load_track(home).get("shipped", []))
    elif "system" in home.paths:
        engine = home.rel("system") + "/"
        shipped = {rel for rel, _ in templates() if not rel.startswith(engine)}
    else:
        raise FileNotFoundError(f"{f} not found: {root} was not created by init, so what it holds is unknown")
    created = Created()
    first_config = CONFIG not in shipped                         # never shipped to this tree before
    keep_modes = _modes_in_permissions(home)
    skipped_modes = False
    for rel, node in templates():
        if rel in shipped:
            continue
        if rel == CONFIG:
            continue                                             # not a plain copy: it must carry the values of lightmind.md
        if keep_modes and (rel.startswith(MODES + "/") or rel == RIGHTS):
            skipped_modes = True                                 # the shipped mode files would replace the tree's own modes
            continue
        if not (root / rel).exists():
            _write(root, rel, node)
            created.append(rel)
        shipped.add(rel)
    existed = home.config.state != "missing"
    created.notes = migrate_config(home, create=first_config, repair=True)
    if skipped_modes:
        created.notes.append(f"{MODES}/ and {RIGHTS}: not created, the modes of this tree stay in permissions.md (`modes:`); "
                             f"to switch to numbered rights, write {RIGHTS} and {MODES}/<mode>.md as in the shipped templates")
    if first_config and not existed and home.config.state != "missing":
        created.append(CONFIG)
    shipped.add(CONFIG)
    _save_track(home, shipped)
    _save_install_source(home, only_if_missing=True)      # never refreshed: it may name a wheel kept inside the tree
    if runtime.is_newest(home):
        created.launchers = runtime.refresh_launchers(home)        # before the stamp: a build that follows must find the new launcher
    if schedule_rebuild:
        _schedule_runtime_rebuild(home)
    return created


def _modes_in_permissions(home: Home) -> bool:
    """Does the tree keep its modes in permissions.md (a `modes:` block, no mode file)?  Then `update_tree` must not
    add the shipped mode files: the core would read them instead of the tree's own modes."""
    from .modes import mode_files
    try:
        return not mode_files(home) and bool(home.key_doc("permissions").meta.get("modes"))
    except Exception:                                            # noqa: BLE001 - no readable permissions.md: nothing to keep
        return False


def install_from_folder(source: str | Path, target: str | Path | None = None) -> tuple[Path, str]:
    """Dev convenience (`lm update --from FOLDER`): build a wheel from what is COMMITTED in a lightmind-core checkout
    (`git archive HEAD`, so uncommitted edits never ship), keep it as the only wheel in `system/wheels`, record it in
    `state/install-source.txt` and ask for a new runtime (`runtime.request_rebuild`), so that the next start, or the build
    that `lm update` starts at once, makes it from the wheel.
    No runtime is touched: a running lm keeps its build, the new one is made next to it.  Returns (wheel, stamp)."""
    import shutil
    import tarfile
    import tempfile
    src = Path(source).expanduser().resolve()
    if not (src / "pyproject.toml").is_file():
        raise FileNotFoundError(f"{src} is no lightmind-core folder (no pyproject.toml)")
    home = Home(Home.resolve_root(str(target) if target is not None else None))
    system = home.path("system")
    exe = "uv.exe" if os.name == "nt" else "uv"
    uv = str(system / "bin" / exe) if (system / "bin" / exe).is_file() else shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found (system/bin or PATH)")
    r = subprocess.run(["git", "-C", str(src), "archive", "--format=tar", "HEAD"], capture_output=True)
    if r.returncode != 0:
        raise ValueError(f"git archive HEAD failed in {src}: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    with tempfile.TemporaryDirectory(prefix="lm-from-") as tmp:
        code, out = Path(tmp) / "src", Path(tmp) / "out"
        code.mkdir()
        import io
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            tar.extractall(code, filter="data")
        b = subprocess.run([uv, "build", "--wheel", "--out-dir", str(out), str(code)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        built = sorted(out.glob("lightmind-*.whl"))
        if b.returncode != 0 or not built:
            raise RuntimeError(f"uv build failed: {(b.stderr or b.stdout).strip()[-300:]}")
        wheels = system / "wheels"
        wheels.mkdir(parents=True, exist_ok=True)
        for old in wheels.glob("lightmind-*.whl"):
            old.unlink()
        kept = wheels / built[-1].name
        shutil.copy2(built[-1], kept)
    f = home.path("state") / INSTALL_SOURCE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"wheel {kept}" + chr(10), encoding="utf-8", newline=chr(10))
    return kept, runtime.request_rebuild(home)


def _schedule_runtime_rebuild(home: Home) -> None:
    """`lm update` must also bring the engine up to date: the launcher prefers a kept wheel in `system/wheels`, so a
    runtime would stay at the version it was built with.  A tree that has a runtime therefore loses the kept wheel and
    asks for a new runtime (`runtime.request_rebuild`): the next build is made from `state/install-source.txt`.  No
    runtime folder is touched, so running windows go on.  The wheel stays when it is the only source (the record names
    a wheel inside `system/wheels`, or there is no readable record); then there is nothing new to build."""
    system = home.path("system")
    if not runtime.has_any_runtime(home):
        return
    rec = home.path("state") / INSTALL_SOURCE
    parts = rec.read_text(encoding="utf-8", errors="replace").split(None, 1) if rec.is_file() else []
    wheels = system / "wheels"
    kept_is_source = len(parts) < 2 or (parts[0] == "wheel" and Path(parts[1].strip()).parent.resolve() == wheels.resolve())
    if kept_is_source:
        return
    for w in wheels.glob("lightmind-*.whl"):
        w.unlink(missing_ok=True)
    runtime.request_rebuild(home)
