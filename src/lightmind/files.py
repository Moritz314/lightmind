"""Markdown files: frontmatter + row-list parser, Home (the only place that knows
where things live), tree loaders, mtime cache.

The core knows ONE entry file: `lightmind.md` at the tree root.  Its frontmatter holds
every setting: `paths:` (locations), `files:` (fixed file names), `defaults:`, `shell:`,
`tools:`, `on_start:`, `on_prompt:`, `on_end:`, `context:`, `run:`, `ui:`.  Its body is the master
prompt.  Nothing else in the code may contain a folder name.  The hook lists are read and run by
`loop.py`; here they are plain settings.

Two more files lie over it, later wins: `config.md` (the settings the user changes, only the keys of
`CONFIG_KEYS`) and `<state>/local.md` (this machine only).
"""
from __future__ import annotations

import os
import platform
import re
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import accountpick

from .model import (Account, Agent, Doc, Project, ProvidersConfig, Server, Skill, Tool, ToolRef,
                    Trigger)

ENTRY = "lightmind.md"          # the single hard-coded name
ENV_HOME = "LIGHTMIND_HOME"
_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_README = "README.md"           # used only when lightmind.md has no `files.readme`
LOCAL = "local.md"              # <state>/local.md: settings of this instance only, laid over lightmind.md (never synced)
CONFIG = "config.md"            # the settings page at the tree root (`paths.config` may name another place)
MODES = "modes"                 # one file per mode (`paths.modes` may name another place)
RIGHTS = "rechte.md"            # the numbered catalog the mode files name (`paths.rights` may name another place)
CONFIG_KEYS: dict[str, tuple[str, ...]] = {      # the ONLY keys the core reads from config.md; anything else is ignored
    "defaults": ("provider", "model", "effort", "context", "mode"),     # not `server`: that is per machine, in local.md
    "sessions": ("open", "auto_title", "title_model", "title_every"),
    "ui": ("boot", "boot_wait", "notes_layout"),
    "run": ("max_parallel", "max_depth"),
    "spawn": ("model", "auto_turns"),
    "workflow": ("max_parallel", "laptop_parallel", "agent_memory_mb", "free_memory_mb", "max_agents", "max_items", "budget",
                 "model", "effort", "schema_retries", "skeptics", "dry_rounds", "tidy_worktrees", "notify", "notify_after",
                 "confirm", "keyword"),   # the workflow runtime (orchestrate.py, ultracode.py) and the word `ultracode`
    "tools": ("memory_mb",),                                            # MB one tool or shell command tree may use
    "vscode": ("agent", "model", "effort", "mode"),                     # defaults of a chat started from the VS Code extension
    "watch": ("every", "max_restarts", "notify"),                       # the automatic watch over child runs (watch.py): beat, restarts per child, who is told
    "quick": ("model", "effort", "context", "mode", "permission_mode"),  # defaults of `lm q`, the quick chat (quick.py); a missing key = the built-in value; `permission_mode` = alias of `mode`
    "skills": ("source",),                                              # where `lm skill add` / `lm skill list --available` fetch skills from (a git URL or folder); empty = the default repository
    "remote": ("enabled", "auto", "gateway", "default_node", "agent", "model", "effort", "mode", "push", "push_questions", "upload_mb"),   # Remote Control (the phone app): on/off, where the gateway runs, defaults of a chat started from the phone, push, upload limit
}


CONFIG_FREE_TEXT = {("skills", "source")}      # keys of CONFIG_KEYS that hold free text (a URL): edited in the file, no choice field on the settings page
CONFIG_ALIASES = {("quick", "permission_mode")}     # keys of CONFIG_KEYS that only spell another key (quick.mode); no settings field of their own


class PathsError(KeyError):
    """A key is missing from lightmind.md `paths:` / `files:`."""


# ----------------------------------------------------------------- parsing
def _front(text: str) -> tuple[str, str] | None:
    """(YAML source, body) of a text that starts with a `---` block (BOM gone, LF only), else None."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    after = text[end + 4:]
    if after and not after.startswith("\n"):
        return None
    return text[4:end], after[1:] if after.startswith("\n") else after


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split `---` YAML frontmatter from the body.  No frontmatter -> ({}, text)."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    split = _front(text)
    if split is None:
        return {}, text
    src, body = split
    try:
        meta = yaml.safe_load(src) or {}
    except yaml.YAMLError:
        meta = _flat_frontmatter(src)           # Claude Code reads `description: a: b` unquoted; so do we
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, body


def _flat_frontmatter(src: str) -> dict[str, Any]:
    """Lenient reading of frontmatter that is not valid YAML, only when it is flat: every line is
    `key: value` (value taken verbatim) or blank / a comment.  Anything nested raises as before."""
    meta: dict[str, Any] = {}
    for ln in src.split("\n"):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"([A-Za-z_][\w-]*):(?:\s+(.*))?$", ln)
        if not m:
            raise ValueError(f"frontmatter is not valid YAML near: {ln[:60]!r}")
        v = (m.group(2) or "").strip()
        meta[m.group(1)] = v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'" else v
    return meta


def _strip_comment(s: str) -> str:
    m = re.search(r"(^|\s)#", s)
    return s[:m.start()] if m else s


def parse_rows(body: str) -> dict[str, list[list[str]]]:
    """`# Section` headings (H1) with `- a | b | c` items -> {section: [[cells]]}.

    Cells are trimmed; ` #...` (hash after whitespace) starts a comment; fenced code is
    skipped; items before the first heading go under ''.  Arrow rows (`- /x -> y`) come
    back as one cell; use split_arrow().
    """
    out: dict[str, list[list[str]]] = {"": []}
    cur, fenced = "", False
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if re.match(r"^# ", line):
            cur = _strip_comment(line[2:]).strip()
            out.setdefault(cur, [])
        elif re.match(r"^\s*[-*] ", line):
            item = _strip_comment(re.sub(r"^\s*[-*] ", "", line)).strip()
            if item:
                out.setdefault(cur, []).append([c.strip() for c in item.split("|")])
    if not out[""]:
        del out[""]
    return out


def split_arrow(cell: str) -> tuple[str, str]:
    """'/landing → lm workflow x' (also '->') -> ('/landing', 'lm workflow x')."""
    a, sep, b = cell.partition("→")
    if not sep:
        a, _, b = cell.partition("->")
    return a.strip(), b.strip()


def write_atomic(path: Path | str, data: bytes | str, mode: int | None = None) -> None:
    """Write `path` through a temporary file beside it and replace it: a reader sees the old or the new file, never half of
    one.  Text is written as UTF-8 without newline translation.  With `mode` (keys, .env files) the temporary file is created
    with it at once, so a secret is never readable by others, not even for a moment.  On Windows a virus scanner may hold the
    target for a moment: the replace is retried a few times.  The temporary file is removed when anything fails."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    else:
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)                        # a leftover would keep its old, maybe wider, mode
    try:
        if mode is None:
            tmp.write_bytes(data)
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_doc(path: str | Path) -> Doc:
    p = Path(path)
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    return Doc(path=str(p), meta=meta, body=body)


def parse_size(v: Any) -> int:
    """200000 | '200k' | '1m' -> int."""
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"\s*([\d.]+)\s*([km]?)\s*", str(v).lower())
    if not m:
        raise ValueError(f"bad size {v!r}")
    return int(float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)])


def platform_tag() -> tuple[str, str]:
    """(os, arch) as used in runtime folder names: windows|linux|macos, x86_64|arm64.

    The launchers (`lm`, `lm.cmd`) normalise the same way, so one tree can hold runtimes per machine
    type (`system/runtimes/windows-x86_64/`, `system/runtimes/linux-arm64/`, ...; see runtime.py)."""
    s, m = sys.platform, platform.machine().lower()
    os_ = "windows" if s.startswith("win") else "macos" if s == "darwin" else "linux" if s.startswith("linux") else s
    arch = "arm64" if m in ("arm64", "aarch64") else "x86_64" if m in ("amd64", "x86_64", "x64") else m
    return os_, arch


def _deep_merge(base: dict, top: dict) -> dict:
    out = dict(base)
    for k, v in top.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _plain_name(s: str) -> bool:
    """A folder-like name: letters, digits, `.`, `_`, `-`; no globs, slashes or `..`."""
    return bool(_ACCOUNT.fullmatch(s)) and ".." not in s


def _secret_ref(name: str, detail: str) -> str | None:
    """`service/account` from `service/account` or `service: account`, a bare `service` (all its
    accounts), or None when any part is not a plain name."""
    parts = [name, detail] if detail else name.split("/")
    return "/".join(parts) if len(parts) <= 2 and all(_plain_name(p) for p in parts) else None


def _ssh_value(v: Any) -> str | list[str]:
    """`ssh:` of a server card: one alias, or a list of aliases for remote.py to try in order."""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x or "").strip()]
    return str(v or "")


def _as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    return [str(x) for x in ([v] if isinstance(v, str) else v)]


def _first_line(body: str) -> str:
    """First non-empty line that is not a heading: the one-line description of a README."""
    for ln in body.replace("\r\n", "\n").split("\n"):
        s = ln.strip()
        if s and not s.startswith("#"):
            return s
    return ""


# ------------------------------------------------------------------ config.md
@dataclass
class ConfigFile:
    """What `Home.config` found in config.md.  `state`: missing | empty (blank file) | ok | error | unguarded.  `values`
    is the part the core reads (the keys of CONFIG_KEYS; an empty value counts as not set), `ignored` the dotted keys it
    does not read, `both` the ones lightmind.md sets as well (config.md wins).  An `error` (the file is broken) and
    `unguarded` (permissions.md does not make an agent ask before it writes config.md, see `Home.unguarded_reason`)
    leave `values` empty: the settings of lightmind.md apply and the file is reported, never silently skipped."""
    path: Path
    state: str
    error: str = ""
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    both: list[str] = field(default_factory=list)


def read_config(path: Path, entry_meta: Mapping[str, Any] | None = None) -> ConfigFile:
    """Read config.md strictly: the frontmatter must be YAML (no lenient flat reading, a typo is an error)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ConfigFile(path, "missing")
    text = raw.decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n")
    if not text.strip():
        return ConfigFile(path, "empty")
    split = _front(text)
    if split is None:
        return ConfigFile(path, "error", "no frontmatter: the file must start with a --- block")
    try:
        meta = yaml.safe_load(split[0]) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        why = str(getattr(e, "problem", "") or "not valid YAML")
        return ConfigFile(path, "error", (f"line {mark.line + 2}: " if mark else "") + why)
    if not isinstance(meta, dict):
        return ConfigFile(path, "error", "the frontmatter is not a mapping")
    cfg = ConfigFile(path, "ok")
    for sec, val in meta.items():
        keys = CONFIG_KEYS.get(str(sec))
        if keys is None:
            cfg.ignored.append(str(sec))
        elif isinstance(val, dict):
            for k, v in val.items():
                if str(k) not in keys:
                    cfg.ignored.append(f"{sec}.{k}")
                elif v is not None:
                    cfg.values.setdefault(str(sec), {})[str(k)] = v
        elif val is not None:
            cfg.ignored.append(str(sec))
    for sec, d in cfg.values.items():
        have = (entry_meta or {}).get(sec)
        cfg.both += [f"{sec}.{k}" for k in d if isinstance(have, dict) and have.get(k) is not None]
    return cfg


# -------------------------------------------------------------------- Home
class Home:
    """Tree root + the settings of lightmind.md.  mtime-cached document access: an edit of
    lightmind.md (or any other file) is seen by the next call."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self._cache: dict[str, tuple[int, int, Doc]] = {}
        self._settings: dict[str, Any] | None = None      # see `settings`
        self._overlay: dict[bool, tuple[Any, Any, Any, dict[str, Any]]] = {}      # see `_stack`
        self._config: tuple[Any, Any, Any, ConfigFile] | None = None
        self._guard: tuple[Any, str] | None = None         # see `unguarded_reason`
        self._judging = threading.local()                  # `.on` while `unguarded_reason` runs in this thread: config.md counts as absent
        if not (self.root / ENTRY).is_file():
            raise FileNotFoundError(f"{self.root / ENTRY} not found (is {self.root} a Lightmind tree?)")

    @staticmethod
    def resolve_root(arg: str | None = None) -> Path:
        """--home arg > $LIGHTMIND_HOME > ~/lightmind."""
        return Path(arg or os.environ.get(ENV_HOME) or Path.home() / "lightmind").expanduser()

    @classmethod
    def open(cls, arg: str | None = None) -> "Home":
        return cls(cls.resolve_root(arg))

    # -- lightmind.md: settings (frontmatter, never sent to the AI) and master prompt (body)
    @property
    def entry(self) -> Doc:
        return self.doc(ENTRY)

    @property
    def settings(self) -> dict[str, Any]:
        """The whole frontmatter of lightmind.md: what other modules read (shell, tools, hooks,
        defaults ...).  Assigning replaces it for them (a test double); Home's own lookups never
        go through it."""
        return self._meta if self._settings is None else self._settings

    @settings.setter
    def settings(self, value: dict[str, Any] | None) -> None:
        self._settings = value

    @property
    def _meta(self) -> dict[str, Any]:
        """Same dict, for Home's own lookups: sections never go through the public name, so replacing `settings`
        (a test double, a wrapper) cannot break the tree itself.  Two files are laid over lightmind.md, section by
        section, later wins: `config.md` (only the keys of CONFIG_KEYS, and only while it is guarded, see `config`) and
        `<state>/local.md` of this instance (never synced)."""
        return self._stack(True)

    def _layers(self) -> tuple[dict[str, Any], Any, Path]:
        """The settings of lightmind.md, the size and time of this instance's local.md (None: there is none) and
        where it is.  One stat for the file, so that a lookup costs as few calls to the file system as possible."""
        base = self.entry.meta
        state = str((base.get("paths") or {}).get("state") or "system/state/").rstrip("/\\")
        lp = self.abs(state) / LOCAL
        return base, self._stat_key(lp), lp

    def _stack(self, with_config: bool) -> dict[str, Any]:
        """lightmind.md, config.md (when asked for) and local.md laid over each other.  `paths` and `files` are read
        without config.md (`with_config=False`): it can never move a location, and the guard check that decides
        whether config.md counts reads locations itself."""
        base, lkey, lp = self._layers()
        return self._merged(base, lkey, lp, self._config_for(base, lkey, lp) if with_config else None)

    def _merged(self, base: dict[str, Any], lkey: Any, lp: Path, cfg: "ConfigFile | None") -> dict[str, Any]:
        use = cfg.values if cfg is not None else {}
        if lkey is None and not use:
            return base
        slot = cfg is not None
        hit = self._overlay.get(slot)
        if hit and hit[0] is base and hit[1] is cfg and hit[2] == lkey:
            return hit[3]
        merged = _deep_merge(base, use) if use else base
        if lkey is not None:
            try:
                top = self.doc(lp).meta
            except (OSError, ValueError):
                top = {}
            merged = _deep_merge(merged, top)
        self._overlay[slot] = (base, cfg, lkey, merged)
        return merged

    def _config_path(self, base: Mapping[str, Any]) -> Path:
        v = str((base.get("paths") or {}).get("config") or CONFIG)
        p = Path(v.rstrip("/\\")).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def config_file(self) -> Path:
        """config.md: `paths.config` of lightmind.md, else `config.md` at the tree root."""
        return self._config_path(self.entry.meta)

    def _perm_path(self, meta: Mapping[str, Any]) -> Path | None:
        """Where permissions.md is, read from settings without config.md (None: lightmind.md names no such file)."""
        v = (meta.get("paths") or {}).get("permissions")
        if v is None:
            return None
        os_, arch = platform_tag()
        p = Path(str(v).replace("{os}", os_).replace("{arch}", arch).rstrip("/\\")).expanduser()
        return p if p.is_absolute() else self.root / p

    @staticmethod
    def _stat_key(p: Path) -> Any:
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    @property
    def config(self) -> ConfigFile:
        """config.md as the core sees it, cached by size and time (`forget` after the core wrote it).  FAIL CLOSED: a
        file with content counts only while `unguarded_reason` is empty; else its state is `unguarded` and its values are
        not used, because the mode it names (`bypass` asks nothing) would otherwise be one an agent could set."""
        base, lkey, lp = self._layers()
        return self._config_for(base, lkey, lp)

    def _config_for(self, base: dict[str, Any], lkey: Any, lp: Path) -> ConfigFile:
        p = self._config_path(base)
        if getattr(self._judging, "on", False):             # inside the guard check itself: config.md counts as absent
            return ConfigFile(p, "missing")
        skey = self._stat_key(p)
        pkey = None
        if skey is not None:                                # no file, nothing to judge: permissions.md is not looked at
            perm = self._perm_path(self._merged(base, lkey, lp, None))
            pkey = (self._stat_key(perm) if perm else None, self._modes_key())
        hit = self._config
        if hit and hit[0] == (skey, pkey) and hit[1] is base and hit[2] == lkey and hit[3].path == p:
            return hit[3]
        cfg = read_config(p, base)
        if cfg.state == "ok" and (why := self.unguarded_reason()):
            cfg = ConfigFile(p, "unguarded", why, ignored=cfg.ignored)
        self._config = ((skey, pkey), base, lkey, cfg)
        return cfg

    def _guard_key(self) -> tuple[Any, ...]:
        """What the guard verdict depends on: permissions.md (size and time), the settings that name the locations
        (the dict itself, compared by identity) and where config.md is."""
        base, lkey, lp = self._layers()
        nocfg = self._merged(base, lkey, lp, None)
        perm = self._perm_path(nocfg)
        return (self._stat_key(perm) if perm else None, nocfg, self._config_path(base), self._modes_key())

    def _modes_key(self) -> tuple[Any, ...]:
        """Size and time of the mode files and the catalog: the guard verdict depends on them too."""
        from .modes import state_key
        try:
            return state_key(self)
        except Exception:                                   # noqa: BLE001 - no usable locations: nothing to key on
            return ()

    @staticmethod
    def _same_key(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool:
        return a[0] == b[0] and a[1] is b[1] and a[2] == b[2] and a[3] == b[3]

    def unguarded_reason(self) -> str:
        """'' when an agent cannot write or edit config.md without being asked (or is denied) in EVERY mode of
        permissions.md, else why not.  The rule checker itself is asked; whatever it cannot judge counts as unguarded.
        config.md picks the mode of every new chat, and `bypass` removes every question, so it is guarded like lightmind.md
        (the line `write({config})` of `grant_files`, which `lm update` adds to an older tree)."""
        key = self._guard_key()
        if self._guard and self._same_key(self._guard[0], key):
            return self._guard[1]
        why = self._judge_guard()
        self._guard = (key, why)
        return why

    def _judge_guard(self) -> str:
        from . import rules                                 # late: rules reads locations through this Home, never config.md
        from .model import RuleContext
        self._judging.on = True
        try:
            modes = list(self.modes())
            try:
                rel = self.config_file.resolve().relative_to(self.root).as_posix()
            except ValueError:
                rel = str(self.config_file)
            ctx = RuleContext(home=str(self.root))             # no run, no project: `$RUN` never matches, and no realpath for it
            with rules.real_memo():
                for mode in modes:
                    layers = rules.build_layers(self, None, mode, None)
                    for tool, args in (("write", {"path": rel}), ("edit", {"path": rel, "old": "a", "new": "b"})):
                        if rules.decide_all(layers, rules.checks_for_call(tool, args), ctx).effect == "allow":
                            return f"an agent may {tool} {rel} without asking in mode {mode}"
            return ""
        except Exception as e:                              # noqa: BLE001 - what cannot be judged is not guarded
            return f"the guard in permissions.md cannot be checked ({e.__class__.__name__}: {str(e)[:80]})"
        finally:
            self._judging.on = False

    def forget(self, rel: str | Path) -> None:
        """Drop what is cached of a file the core has just written.  The cache goes by size and time, and an
        edit of the same size within one clock tick of the file system would not be seen."""
        self._cache.pop(str(self.abs(rel)), None)
        self._config, self._guard, self._overlay = None, None, {}

    @property
    def machine(self) -> str:
        """Name of this instance: $LM_MACHINE, else the file `<state>/machine` (written by `lm sync init|join`); '' when unset."""
        name = os.environ.get("LM_MACHINE", "").strip()
        if not name:
            try:
                name = (self.path("state") / "machine").read_text(encoding="utf-8").strip()
            except (OSError, PathsError):
                name = ""
        return name

    @property
    def paths(self) -> dict[str, str]:
        return {k: str(v) for k, v in (self._stack(False).get("paths") or {}).items()}

    @property
    def files(self) -> dict[str, str]:
        return {k: str(v) for k, v in (self._stack(False).get("files") or {}).items()}

    @property
    def defaults(self) -> dict[str, Any]:
        """`defaults:` (server, provider, model, effort, context, mode)."""
        return dict(self._meta.get("defaults") or {})

    def fill(self, text: str, extra: Mapping[str, str] | None = None, *, absolute: bool = False) -> str:
        """Replace `{key}` by the location of paths.<key> (as written, no trailing slash; with
        `absolute` the full path), `{file_<key>}` by files.<key>, `{home}` by the absolute tree root,
        `{os}` / `{arch}`.  Unknown placeholders stay as they are.  Same input -> same bytes."""
        vals = {k: self.path(k).as_posix() if absolute else self.rel(k) for k in self.paths}
        vals.setdefault("home", self.root.as_posix())
        vals.update({"file_" + k: v for k, v in self.files.items()})
        vals.setdefault("os", platform_tag()[0])
        vals.setdefault("arch", platform_tag()[1])
        if extra:
            vals.update(extra)
        return _PLACEHOLDER.sub(lambda m: vals.get(m.group(1), m.group(0)), text)

    def master_prompt(self) -> str:
        """Body of lightmind.md with placeholders substituted: the always-sent, cache-stable start
        of every context."""
        return self.fill(self.entry.body).strip()

    # -- locations
    def rel(self, key: str) -> str:
        """Location of `key` as written in lightmind.md, without trailing slash; `{os}` and
        `{arch}` are expanded."""
        try:
            v = self.paths[key]
        except KeyError:
            raise PathsError(f"lightmind.md has no paths.{key}") from None
        os_, arch = platform_tag()
        return v.replace("{os}", os_).replace("{arch}", arch).rstrip("/\\")

    def path(self, key: str) -> Path:
        p = Path(self.rel(key)).expanduser()
        return p if p.is_absolute() else self.root / p

    def filename(self, key: str) -> str:
        try:
            return self.files[key]
        except KeyError:
            raise PathsError(f"lightmind.md has no files.{key}") from None

    def abs(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def runtime_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment that keeps uv, Python and browsers inside the tree: UV_CACHE_DIR,
        UV_PYTHON_INSTALL_DIR, PLAYWRIGHT_BROWSERS_PATH from the `cache`, `python`, `browsers`
        locations, and the `bin` location at the END of PATH: it serves when the system has no uv of its
        own, and a file left in it cannot shadow a system program.  Keys absent from `paths:` are skipped."""
        env = dict(os.environ if base is None else base)
        for var, key in (("UV_CACHE_DIR", "cache"), ("UV_PYTHON_INSTALL_DIR", "python"),
                         ("PLAYWRIGHT_BROWSERS_PATH", "browsers")):
            if key in self.paths:
                env[var] = str(self.path(key))
        if "bin" in self.paths:
            b = str(self.path("bin"))
            rest = [d for d in env.get("PATH", "").split(os.pathsep)
                    if d and os.path.normcase(os.path.normpath(d)) != os.path.normcase(os.path.normpath(b))]
            env["PATH"] = os.pathsep.join([*rest, b])
        return env

    # -- cached documents (re-read when mtime/size changes)
    def doc(self, rel: str | Path) -> Doc:
        p = self.abs(rel)
        st = p.stat()
        key = str(p)
        hit = self._cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
        d = load_doc(p)
        self._cache[key] = (st.st_mtime_ns, st.st_size, d)
        return d

    def key_doc(self, key: str) -> Doc:
        """Document behind a paths key that points to a file (e.g. permissions).  A name that has
        no `paths:` entry but is a mapping in the lightmind.md frontmatter (`context`, `ui`, ...)
        gives that section as `Doc.meta` (empty body)."""
        if key not in self.paths and isinstance(self._meta.get(key), dict):
            return Doc(path=str(self.root / ENTRY), meta=self._meta[key], body="")
        return self.doc(self.path(key))

    def modes(self) -> dict[str, dict]:
        """The modes of the tree, key -> preset (label, color, text ...).  The mode files (`modes/*.md`, see
        `modes.presets`) when the tree has any, else the `modes:` block of the permissions file (an older tree).
        Raises ValueError with the reason when there is no mode at all, so no surface shows an empty menu without
        saying why.  A mode file that cannot be used is still listed, with `error` set."""
        from . import modes as mode_files             # late: modes reads the rule syntax of rules, which reads Home
        found = mode_files.presets(self)
        if found is not None:
            return found
        try:
            name = self.path("permissions").name
            meta = self.key_doc("permissions").meta
        except Exception as e:                              # noqa: BLE001 - missing file, bad YAML, no read right: the reason goes to the user
            raise ValueError(f"permissions file cannot be read: {type(e).__name__}: {e}") from e
        raw = meta.get("modes")
        if not isinstance(raw, dict) or not raw:
            raise ValueError(f"{name} has no `modes:` mapping and {mode_files.modes_dir(self).name}/ holds no mode file, "
                             "so there is no mode to pick; check the files, or restore them from the template (`lm update`)")
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}

    def rows(self, key: str) -> dict[str, list[list[str]]]:
        return parse_rows(self.key_doc(key).body)

    # -- ui rows
    def ui_rows(self, name: str) -> list[list[str]]:
        """`ui.<name>` list items as cells: `New chat | n | new_chat` -> [New chat, n, new_chat];
        `/clear → new_chat` -> [/clear, new_chat]."""
        out = []
        for item in (self._meta.get("ui") or {}).get(name) or []:
            cells = [c.strip() for c in str(item).split("|")]
            if len(cells) == 1:
                cells = [c for c in split_arrow(cells[0])]
            out.append(cells)
        return out

    # -- tree loaders (README.md and anything starting with `_` or `.` is never an item)
    def _readme(self) -> str:
        return self.files.get("readme", _README)

    def _children(self, key: str) -> list[Path]:
        d = self.path(key)
        if not d.is_dir():
            return []
        rd = self._readme().lower()
        return sorted((p for p in d.iterdir()
                       if not p.name.startswith(("_", ".")) and p.name.lower() != rd),
                      key=lambda p: p.name)

    def _loadout(self, folder: Path, key: str) -> tuple[list[ToolRef], list[str], list[str]]:
        """tools.md: `# Skills`, `# Tools`, `# Secrets`, `# Servers` sections, first cell = name.
        A secret or server entry grants it, so only plain names count: a row that is not
        `service/account` (or a bare `service` for all its accounts), or a server that is not a plain
        name, is skipped with a warning and never widened into a broader grant."""
        f = folder / self.filename(key)
        if not f.is_file():
            return [], [], []
        refs: list[ToolRef] = []
        secrets: list[str] = []
        servers: list[str] = []
        for section, rows in parse_rows(self.doc(f).body).items():
            m = re.match(r"\s*([a-z]+)", section.lower())
            word = m.group(1).removesuffix("s") if m else ""
            for r in rows:
                cell = r[0].strip() if r else ""
                # `- name`, `- bash, files` (a comma list) or `- name: detail` (`mail: info`
                # is the account `info` of a secret; `buildbox: read, execute` names a server)
                for item in ([cell] if ":" in cell else cell.split(",")):
                    name, _, detail = (x.strip() for x in item.partition(":"))
                    if not name:
                        continue
                    if word in ("skill", "tool", "connector"):
                        if all((x.kind, x.name) != (word, name) for x in refs):
                            refs.append(ToolRef(word, name))     # type: ignore[arg-type]
                    elif word == "secret":
                        ref = _secret_ref(name, detail)
                        if ref is None:
                            warnings.warn(f"{f}: secret entry {item.strip()!r} is not a plain name or "
                                          f"name/account; skipped, nothing is granted", stacklevel=2)
                        elif ref not in secrets:
                            secrets.append(ref)
                    elif word == "server":
                        if not _plain_name(name):
                            warnings.warn(f"{f}: server entry {item.strip()!r} is not a plain name; "
                                          f"skipped, nothing is granted", stacklevel=2)
                        elif name not in servers:
                            servers.append(name)
        return refs, secrets, servers

    @staticmethod
    def _grants(perm: dict[str, Any], secrets: list[str], servers: list[str]) -> dict[str, Any]:
        """Listing a secret or server in tools.md grants it: merge into the `permissions` block."""
        out = dict(perm)
        for k, extra in (("secrets", secrets), ("servers", servers)):
            if extra:
                cur = _as_list(out.get(k))
                out[k] = cur + [x for x in extra if x not in cur]
        return out

    def _agent_in(self, folder: Path, name: str) -> Agent:
        p = folder / self.filename("agent")
        d = self.doc(p)
        m = dict(d.meta)
        refs, secrets, servers = self._loadout(folder, "agent_tools")
        a = Agent(name=str(m.pop("name", name)), body=d.body, path=str(p), tools=refs,
                  secrets=secrets, servers=servers)
        for k in ("description", "provider", "model", "effort", "mode", "server", "lifetime", "project"):
            if k in m:
                v = m.pop(k)
                setattr(a, k, accountpick.as_text(v) if k == "provider" else "" if v is None else str(v).strip())
        for k in ("context_limit", "budget_tokens"):
            if k in m:
                setattr(a, k, parse_size(m.pop(k)))
        if "budget_usd" in m:
            a.budget_usd = float(m.pop("budget_usd"))
        if "budget" in m:                    # budget: 400k  |  budget: {tokens: 400k, usd: 2}
            b = m.pop("budget")
            if isinstance(b, dict):
                if b.get("tokens") is not None:
                    a.budget_tokens = parse_size(b["tokens"])
                if b.get("usd") is not None:
                    a.budget_usd = float(b["usd"])
            elif b is not None:
                a.budget_tokens = parse_size(b)
        ms = m.pop("may_spawn", None)
        a.may_spawn = None if ms is None else list(ms)
        a.data = list(m.pop("data", None) or [])
        a.permissions = self._grants(dict(m.pop("permissions", None) or {}), secrets, servers)
        a.extra = m
        return a

    # agents
    def list_agents(self) -> list[str]:
        fn = self.filename("agent")
        return [p.name for p in self._children("agents") if (p / fn).is_file()]

    def load_agent(self, name: str) -> Agent:
        if not _NAME_RE.match(name):
            raise ValueError(f"bad agent name {name!r}")
        return self._agent_in(self.path("agents") / name, name)

    # skills
    def list_skills(self) -> list[Skill]:
        fn = self.filename("skill")
        out: list[Skill] = []
        for p in self._children("skills"):
            if p.is_dir():   # drop-in folder; Claude Code format (SKILL.md) accepted unchanged
                f = next((p / n for n in (fn, "SKILL.md", "skill.md") if (p / n).is_file()), p / fn)
            else:
                f = p
            if f.is_file() and f.suffix == ".md":
                try:
                    dd = self.doc(f)
                except (ValueError, OSError) as e:   # one broken skill must not hide all the others
                    warnings.warn(f"skill {f.name} in {p.name} skipped: {e}", UserWarning, stacklevel=2)
                    continue
                name = str(dd.meta.get("name") or (p.name if p.is_dir() else p.stem))
                out.append(Skill(name=name, description=str(dd.meta.get("description", "")),
                                 preload_when=str(dd.meta.get("preload_when", "")),
                                 requires=list(dd.meta.get("requires") or []),
                                 secret=str(dd.meta.get("secret", "")), body=dd.body, path=str(f)))
        return out

    # tools: a folder with a script and a README.md; tool.md (frontmatter manifest) is optional
    def list_tools(self) -> list[Tool]:
        return [self.load_tool(p.name) for p in self._children("tools") if p.is_dir()]

    def load_tool(self, name: str) -> Tool:
        folder = self.path("tools") / name
        if not folder.is_dir():
            raise FileNotFoundError(f"tool {name!r}")
        manifest = next((folder / n for n in (self.filename("tool"), self._readme())
                         if (folder / n).is_file()), None)
        d = self.doc(manifest) if manifest else Doc(path="")
        m = d.meta
        return Tool(name=str(m.get("name", name)),
                    description=str(m.get("description") or _first_line(d.body)),
                    run=str(m.get("run", "")), params=dict(m.get("params") or {}),
                    category=str(m.get("category", "none")), match=str(m.get("match", "")),
                    secrets=list(m.get("secrets") or []),
                    required_secrets=list(m.get("required_secrets") or []),
                    timeout=int(m.get("timeout", 60)), memory_mb=int(m["memory_mb"]) if m.get("memory_mb") not in (None, "") else None,
                    output=str(m.get("output", "auto")),
                    server=str(m.get("server", "any")),
                    max_usd_per_call=float(m.get("max_usd_per_call", 0)),
                    body=d.body, path=str(folder))

    # triggers: one .md file each
    def list_triggers(self) -> list[str]:
        return [p.stem for p in self._children("triggers") if p.is_file() and p.suffix == ".md"]

    def load_trigger(self, name: str) -> Trigger:
        if not _NAME_RE.match(name):
            raise ValueError(f"bad trigger name {name!r}")
        p = self.path("triggers") / f"{name}.md"
        d = self.doc(p)
        m = dict(d.meta)
        t = Trigger(name=name, when=str(m.pop("when", "")), task=str(m.pop("task", "")),
                    agent=str(m.pop("agent", "")), workflow=str(m.pop("workflow", "")),
                    tools=_as_list(m.pop("tools", None)), data=_as_list(m.pop("data", None)),
                    server=str(m.pop("server", "")), machine=str(m.pop("machine", "") or ""),
                    session=str(m.pop("session", "") or "").strip(), until=str(m.pop("until", "") or "").strip(),
                    owner=str(m.pop("owner", "") or "").strip(), enabled=bool(m.pop("enabled", True)), body=d.body, path=str(p))
        t.extra = m
        return t

    # servers: servers/<name>/info.md
    def list_servers(self) -> list[str]:
        fn = self.filename("info")
        return [p.name for p in self._children("servers") if (p / fn).is_file()]

    def load_server(self, name: str) -> Server:
        if not _NAME_RE.match(name):
            raise ValueError(f"bad server name {name!r}")
        p = self.path("servers") / name / self.filename("info")
        d = self.doc(p)
        m = dict(d.meta)
        host = str(m.pop("host", ""))
        machine = str(m.pop("machine", "") or "").strip()
        host_local = host.lower() in ("local", "localhost", "")
        mine = self.machine
        s = Server(name=name, host=host, user=str(m.pop("user", "") or ""),
                   port=int(m.pop("port", 22) or 22), key=str(m.pop("key", "") or ""),
                   role=_as_list(m.pop("role", None)), lm_home=str(m.pop("lm_home", "") or ""), ssh=_ssh_value(m.pop("ssh", None)),
                   local=machine.casefold() == mine.casefold() if machine and mine else host_local,
                   machine=machine, legacy_local=host_local and not machine, body=d.body, path=str(p))
        s.extra = m
        return s

    # projects: projects/<name>/ (projectMemory.md, projectAssets.md, tools.md)
    def list_projects(self) -> list[str]:
        return [p.name for p in self._children("projects") if p.is_dir()]

    def has_project(self, name: str) -> bool:
        """A project under `projects/`: a valid name (`a-z`, digits, `-`: nothing that leaves the folder) and the folder is
        there.  A `_` folder is an example, not a project."""
        try:
            return bool(_NAME_RE.match(name or "")) and (self.path("projects") / name).is_dir()
        except (PathsError, OSError):
            return False

    def load_project(self, name: str) -> Project:
        if not _NAME_RE.match(name):
            raise ValueError(f"bad project name {name!r}")
        folder = self.path("projects") / name
        if not folder.is_dir():
            raise FileNotFoundError(f"project {name!r}")
        mem, assets = folder / self.filename("project_memory"), folder / self.filename("project_assets")
        d = self.doc(mem) if mem.is_file() else Doc(path="")
        m = dict(d.meta)
        refs, secrets, servers = self._loadout(folder, "project_tools")
        pr = Project(name=name, provider=accountpick.as_text(m.pop("provider", "")), model=str(m.pop("model", "") or ""),
                     repo=str(m.pop("repo", "") or ""), workdir=str(m.pop("path", "") or ""),
                     home_server=str(m.pop("home_server", "") or ""), tools=refs, secrets=secrets,
                     servers=servers, memory=d.body, path=str(folder))
        # Grants come from tools.md only: projectMemory.md is not a protected file, so an agent could
        # write a permissions block there.  Such a block is dropped, with a warning.
        perm = m.pop("permissions", None)
        if perm and (not isinstance(perm, dict) or any(perm.values())):
            warnings.warn(f"{mem}: `permissions:` is ignored; a project gets its grants only from "
                          f"{self.filename('project_tools')}", stacklevel=2)
        pr.permissions = self._grants({}, secrets, servers)
        if assets.is_file():
            pr.assets = [r for rows in parse_rows(self.doc(assets).body).values() for r in rows]
        pr.extra = m
        return pr

    # providers: one account per file, providers/<name>.md
    def load_providers(self) -> ProvidersConfig:
        return self._read_providers()

    def _read_providers(self) -> ProvidersConfig:
        accounts: dict[str, Account] = {}
        for p in self._children("providers"):
            if p.is_file() and p.suffix == ".md":
                d = self.doc(p)
                m = d.meta
                name = str(m.get("name") or p.stem)
                accounts[name] = Account(
                    name=name, type=str(m.get("type", "")), secret=str(m.get("secret") or ""),
                    admin_secret=str(m.get("admin_secret") or ""), use=str(m.get("use") or ""),
                    base_url=str(m.get("base_url") or ""), server=str(m.get("server") or ""),
                    config_dir=str(m.get("config_dir") or ""), default_model=str(m.get("default_model") or ""),
                    models={str(k): dict(v or {}) for k, v in (m.get("models") or {}).items()},
                    tiers={str(k): str(v) for k, v in (m.get("tiers") or {}).items()},
                    fallback=_as_list(m.get("fallback")),
                    aliases=_as_list(m.get("alias") or m.get("aliases")),
                    switch_at=float(m["switch_at"]) if m.get("switch_at") is not None else None,
                    path=str(p), body=d.body)
        default = accountpick.as_text(self.defaults.get("provider", ""))
        aliases = {al: n for n, a in accounts.items() for al in a.aliases}
        fallback = {n: list(a.fallback) for n, a in accounts.items() if a.fallback}
        tiers: dict[str, dict[str, str]] = {}
        for n, a in accounts.items():
            for tier, model in a.tiers.items():
                tiers.setdefault(tier, {})[n] = model
        sw = accounts[default].switch_at if default in accounts else None
        return ProvidersConfig(default=default, accounts=accounts, aliases=aliases, fallback=fallback,
                               tiers=tiers, switch_at=sw if sw is not None else 0.9)

    def prices(self, account: str | None = None) -> dict[str, tuple[float, float, float, float]]:
        """model -> (input, output, cache_write, cache_read), USD per million tokens, from the
        `models:` of the provider files.  One account when given; else all, the default account first
        (the first file naming a model wins).  A missing cache price counts as the input price."""
        cfg = self._read_providers()          # the files themselves, whatever load_providers answers
        order = [account] if account else (
            [cfg.default] * (cfg.default in cfg.accounts) + sorted(n for n in cfg.accounts if n != cfg.default))
        out: dict[str, tuple[float, float, float, float]] = {}
        for n in order:
            for model, info in (cfg.accounts[n].models if n in cfg.accounts else {}).items():
                if model in out or "input" not in info or "output" not in info:
                    continue
                i = float(info["input"])
                out[model] = (i, float(info["output"]), float(info.get("cache_write", i)),
                              float(info.get("cache_read", i)))
        return out
