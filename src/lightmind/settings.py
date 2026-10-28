"""Settings as a page: how a settings file is changed, line by line, without touching anything else.

Both screens (the TUI `/settings`, the HTML page later) and the core itself (filling config.md, moving values out of
lightmind.md) change files only through this module.  A file is never dumped again as YAML: a changed value replaces
the text of its own line, a missing key is inserted at a fixed place, and comments, order, line endings (CRLF) and a BOM
stay as they are.

Every change is proved before it is written: the new text is parsed again and must equal "old plus the change", and
the text below the frontmatter must be identical, else nothing is written (`SettingsError`).  A save reads the file
again and compares it with what the screen loaded: a file that changed meanwhile is a `Conflict`, never overwritten.

May import: files, ultracode (pure data).
"""
from __future__ import annotations

import copy
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from .files import Home, _front, write_atomic as _write_atomic
from .ultracode import AGENT_EFFORTS      # efforts of a workflow agent: those of the chat without `ultracode`

BOM = chr(0xFEFF)
KeyPath = tuple[str, ...]                   # ("defaults", "model")
_KEY = re.compile(r"""^( *)(?:"([^"]+)"|'([^']+)'|([A-Za-z_][\w-]*))[ \t]*:(?=[ \t]|\r|$)""")
_CLOSE = re.compile(r"---\r?\n?")


class SettingsError(ValueError):
    """A change that cannot be made safely; nothing was written."""


class Conflict(SettingsError):
    """The file changed on disk since it was loaded; nothing was written."""


# ------------------------------------------------------------------ reading
def _lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def front_meta(text: str) -> dict[str, Any]:
    """The frontmatter as YAML, strict (no lenient flat reading).  SettingsError when it is none or not valid."""
    split = _front(text.lstrip(BOM).replace("\r\n", "\n"))
    if split is None:
        raise SettingsError("no frontmatter: the file must start with a --- block")
    try:
        meta = yaml.safe_load(split[0]) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise SettingsError((f"line {mark.line + 2}: " if mark else "") + str(getattr(e, "problem", "") or "not valid YAML")) from None
    if not isinstance(meta, dict):
        raise SettingsError("the frontmatter is not a mapping")
    return meta


def dig(meta: Any, path: KeyPath, default: Any = None) -> Any:
    for k in path:
        if not isinstance(meta, Mapping) or k not in meta:
            return default
        meta = meta[k]
    return meta


def dump_value(v: Any) -> str:
    """One YAML line for a value, as `yaml.safe_dump` writes it (quotes only where they are needed)."""
    s = yaml.safe_dump(v, default_flow_style=True, allow_unicode=True, width=10 ** 9)
    s = s.removesuffix("\n").removesuffix("\n...")
    if "\n" in s or "\r" in s:
        raise SettingsError(f"{v!r} does not fit on one line")
    return s


def _same(a: Any, b: Any) -> bool:
    """Equal, with equal types all the way down (True is not 1)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _expect(meta: dict[str, Any], changes: Mapping[KeyPath, Any]) -> dict[str, Any]:
    """`meta` with the changes made (None removes a key); what the file must parse to afterwards."""
    out = copy.deepcopy(meta)
    for path, value in changes.items():
        d = out
        for k in path[:-1]:
            if not isinstance(d.get(k), dict):
                if value is None:
                    d = None
                    break
                d[k] = {}
            d = d[k]
        if d is None:
            continue
        if value is None:
            d.pop(path[-1], None)
        else:
            d[path[-1]] = value
    return out


# ------------------------------------------------------------------ the line model
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _key_of(line: str) -> tuple[int, str] | None:
    m = _KEY.match(line)
    return (len(m.group(1)), m.group(2) or m.group(3) or m.group(4)) if m else None


def _span(lines: list[str]) -> tuple[int, int]:
    """(first line after the opening ---, index of the closing ---)."""
    if not lines or lines[0].lstrip(BOM).rstrip("\r\n") != "---":
        raise SettingsError("no frontmatter: the file must start with a --- block")
    for i in range(1, len(lines)):
        if _CLOSE.fullmatch(lines[i]):
            return 1, i
    raise SettingsError("the frontmatter is not closed")


def _find(lines: list[str], lo: int, hi: int, name: str, indent: int) -> int | None:
    """The last line of [lo, hi) that is the key `name` at exactly `indent` spaces."""
    hit = None
    for i in range(lo, hi):
        if _key_of(lines[i]) == (indent, name):
            hit = i
    return hit


def _block(lines: list[str], i: int, hi: int, indent: int) -> tuple[int, int]:
    """(end, last) of the key on line i: the block runs up to the next key of `indent` (or less), `last` is its last
    line with content (a comment or blank line after that belongs to what follows)."""
    j, last = i + 1, i
    while j < hi:
        if _content(lines[j]):
            if _indent(lines[j]) < indent or (_indent(lines[j]) == indent and _key_of(lines[j])):
                break
            last = j
        j += 1
    return j, last


def _children(lines: list[str], idx: int, end: int) -> list[int]:
    """The lines of the keys directly under the section on line idx (the indent of the first one counts)."""
    kids = [i for i in range(idx + 1, end) if _content(lines[i]) and _key_of(lines[i])]
    return [i for i in kids if _indent(lines[i]) == _indent(lines[kids[0]])] if kids else []


def _split_value(rest: str) -> tuple[str, str, str]:
    """The text after a colon, without the line end: `  sonnet    # note` -> ('sonnet', '    ', '# note').  A quoted
    value may hold ` #`."""
    s = rest.lstrip(" \t")
    if not s:
        return "", "", ""
    if s[0] == "#":                                            # no value, only a comment
        return "", rest[:len(rest) - len(s)], s
    if s[0] in "\"'":
        q, i = s[0], 1
        while i < len(s):
            if q == '"' and s[i] == chr(92):
                i += 2
                continue
            if s[i] == q:
                if q == "'" and s[i + 1:i + 2] == "'":
                    i += 2
                    continue
                i += 1
                break
            i += 1
        value, tail = s[:i], s[i:]
    else:
        m = re.search(r"[ \t]#", s)
        value, tail = (s[:m.start()], s[m.start():]) if m else (s.rstrip(), "")
    return value, tail[:len(tail) - len(tail.lstrip(" \t"))], tail.lstrip(" \t")


def _eol(line: str, fallback: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else fallback


def _insert(lines: list[str], lo: int, hi: int, a: int, b: int, indent: int, rest: KeyPath, value: Any,
            anchors: KeyPath, eol: str) -> int:
    """Add the missing keys `rest` (the last one with `value`) to the level whose keys sit in lines[a:b] at `indent`.
    A top-level key goes after the first of `anchors` that exists, else behind the last line of the frontmatter; a
    nested key goes behind the last line of its section.  Returns the number of lines added."""
    if indent == 0:
        anchor = next((x for x in (_find(lines, lo, hi, n, 0) for n in anchors) if x is not None), None)
        at = (_block(lines, anchor, hi, 0)[1] + 1 if anchor is not None
              else next((i + 1 for i in range(hi - 1, lo - 1, -1) if lines[i].strip()), lo))
    else:
        at = max((i for i in range(a, b) if _content(lines[i])), default=a - 1) + 1
    new = [" " * (indent + 2 * j) + f"{k}:" + (f" {dump_value(value)}" if j == len(rest) - 1 else "") + eol
           for j, k in enumerate(rest)]
    lines[at:at] = new
    return len(new)


def _set(lines: list[str], lo: int, hi: int, path: KeyPath, value: Any, anchors: KeyPath, eol: str) -> int:
    """Make `path` hold `value` (None removes the key) inside the frontmatter lines[lo:hi]; returns the new `hi`."""
    a, b, indent = lo, hi, 0
    for depth, name in enumerate(path):
        idx = _find(lines, a, b, name, indent)
        if idx is None:
            return hi if value is None else hi + _insert(lines, lo, hi, a, b, indent, path[depth:], value, anchors if depth == 0 else (), eol)
        line = lines[idx]
        head = line[:_KEY.match(line).end()]                      # type: ignore[union-attr]
        rest_text = line[len(head):].rstrip("\r\n")
        old, _, comment = _split_value(rest_text)
        end, last = _block(lines, idx, b, indent)
        label = ".".join(path[:depth + 1])
        if depth < len(path) - 1:                                  # a section: look inside it
            if old:
                raise SettingsError(f"{label} is not a section; edit the file by hand")
            kids = _children(lines, idx, end)
            child = _indent(lines[kids[0]]) if kids else indent + 2
            if child <= indent:
                raise SettingsError(f"{label}: cannot read its layout; edit the file by hand")
            a, b, indent = idx + 1, end, child
            continue
        if last > idx:
            raise SettingsError(f"{label} spans several lines; edit the file by hand")
        if value is None:
            del lines[idx]
            return hi - 1
        try:
            if old and _same(yaml.safe_load(old), value):          # already so: keep the line, quotes and all
                return hi
        except yaml.YAMLError:
            pass
        new_value = dump_value(value)
        col = len(head) + len(rest_text) - len(comment)           # keep the comment where it was, if the room allows
        gap = " " * max(1, col - (len(head) + 1 + len(new_value))) if comment else ""
        lines[idx] = f"{head} {new_value}{gap}{comment}" + _eol(line, eol)
        return hi
    return hi


def prove_text(old: str, new: str, expect: dict[str, Any]) -> None:
    """The counter-check: `new` must parse to exactly `expect` and keep the text below the frontmatter as it was in
    `old`; else SettingsError (and the caller writes nothing)."""
    def body(t: str) -> str | None:
        split = _front(t.lstrip(BOM).replace("\r\n", "\n"))
        return None if split is None else split[1]
    try:
        ok = _same(front_meta(new), expect) and body(new) == body(old)
    except SettingsError:
        ok = False
    if not ok:
        raise SettingsError("the change would not come out as asked (counter-check failed); "
                            "the file stays as it is, edit it by hand")


def edit_text(text: str, changes: Mapping[KeyPath, Any], after: Iterable[str] = ()) -> str:
    """`text` with the changes made, line by line.  `changes` maps a key path to its new value (None removes the
    key).  A missing key is inserted: a top-level key after the first of `after` that exists, else at the end of the
    frontmatter; a nested key at the end of its section; a missing section at the end of the frontmatter.  The result is
    parsed again and must equal the old frontmatter plus the changes with an unchanged body, else SettingsError and
    nothing is returned."""
    lines = _lines(text)
    lo, hi = _span(lines)
    old_meta = front_meta(text)
    eol = _eol(lines[hi - 1], "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n")
    for path, value in changes.items():
        hi = _set(lines, lo, hi, tuple(path), value, tuple(after), eol)
    new = "".join(lines)
    prove_text(text, new, _expect(old_meta, changes))
    return new


def drop_keys(text: str, paths: Iterable[KeyPath], note: str = "") -> str:
    """`text` without the lines of these two-level keys (their inline comments go with them).  A section left without
    a key is removed with the comment lines directly above it.  `note` (one comment line) takes the place of the first
    thing removed.  Proved like `edit_text`."""
    lines = _lines(text)
    lo, hi = _span(lines)
    old_meta = front_meta(text)
    eol = _eol(lines[hi - 1], "\n")
    gone: set[int] = set()
    changes: dict[KeyPath, Any] = {}
    emptied: list[str] = []
    for sec, key in (tuple(p) for p in paths):
        top = _find(lines, lo, hi, sec, 0)
        if top is None:
            continue
        end, last = _block(lines, top, hi, 0)
        kids = _children(lines, top, end)
        at = _find(lines, top + 1, end, key, _indent(lines[kids[0]])) if kids else None
        if at is None:
            continue
        gone.update(range(at, _block(lines, at, end, _indent(lines[at]))[1] + 1))
        changes[(sec, key)] = None
        if all(k in gone for k in kids):                           # nothing left in the section: it goes too
            first = top
            while first > lo and lines[first - 1].lstrip().startswith("#"):
                first -= 1
            gone.update(range(first, last + 1))
            emptied.append(sec)
    kept = [ln for i, ln in enumerate(lines) if i not in gone]
    if note and gone:
        kept.insert(min(gone), " " * _indent(lines[min(gone)]) + f"# {note}" + eol)
    new = "".join(kept)
    expect = _expect(old_meta, changes)
    for sec in emptied:
        if not expect.get(sec):
            expect.pop(sec, None)
    prove_text(text, new, expect)
    return new


# ------------------------------------------------------------------ files
def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsError("the file is not valid UTF-8") from None


def write_atomic(path: Path, data: bytes) -> None:
    """`files.write_atomic`, with a write error told as a SettingsError."""
    try:
        _write_atomic(path, data)
    except OSError as e:
        raise SettingsError(f"{path.name} cannot be written: {e}") from None


def backup(path: Path) -> Path | None:
    """A copy `<name>.bak-<date>` (`-2`, `-3` ... when that exists); None when there is nothing to copy."""
    if not path.is_file():
        return None
    base = path.with_name(f"{path.name}.bak-{date.today().isoformat()}")
    dst, n = base, 1
    while dst.exists():
        n += 1
        dst = base.with_name(f"{base.name}-{n}")
    shutil.copy2(path, dst)
    return dst


def save(home: Home, path: Path, changes: Mapping[KeyPath, Any], loaded: bytes | None = None,
         after: Iterable[str] = ()) -> bytes:
    """Make the changes in the file and return its new bytes.  The file is read again first: when `loaded` (what the
    screen showed) differs from it, nothing is written (`Conflict`).  Nothing changes on any error, the cache of
    `home` forgets the file after a write."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise SettingsError(f"{path.name} cannot be read: {e}") from None
    if loaded is not None and raw != loaded:
        raise Conflict(f"{path.name} changed on disk since it was loaded; load it again")
    new = edit_text(decode(raw), changes, after)
    data = new.encode("utf-8")
    if data != raw:
        write_atomic(path, data)
        home.forget(path)
    return data


# ====================================================================== the settings page: fields, values, reading, saving
# One description of what can be set, for the TUI screen `/settings` and the HTML page later: neither has a list of its own.
# GENERAL is the fixed key list of config.md (`files.CONFIG_KEYS`; defaults.server is per machine and lives in local.md, so
# it is not here).  AGENT is what an agent file lets a screen change.
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultracode")   # what the New chat card offers (claude_sdk may know fewer); ultracode right after max
EFFORT_TEXT = {"low": "fast and cheap, no extended thinking", "medium": "balanced", "high": "careful",
               "xhigh": "very careful", "max": "slowest, most thorough", "ultracode": "max + every substantial task as a workflow"}
CONTEXTS = ("100k", "200k", "500k", "800k", "1m")              # `ui.context_sizes` overrides
OPEN_WORDS = (("tab", "a new tab of this terminal"), ("window", "a new window"), ("replace", "this window, as before"))
BOOT_WORDS = ((True, "yes", "the checklist at the start"), ("brief", "brief", "only warnings and failures"),
              (False, "no", "no checklist"))
YES_NO = ((True, "yes", ""), (False, "no", ""))
NOTES_LAYOUTS = (("split", "split", "chat left, Notes & To-dos right (from 120 columns)"), ("switch", "switch", "Ctrl+N switches between chat and Notes & To-dos"))
PARALLEL = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32)
UPLOAD_MB = (10, 25, 50, 100, 200, 500, 1000, 2000)
MEMORY = (0, 1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768, 65536)
AGENTS = (10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000)       # workflow.max_agents
ITEMS = (16, 64, 256, 1024, 4096, 16384, 65536)                      # workflow.max_items
TOKENS = (10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000, 1_000_000_000)
MINUTES = (1, 5, 10, 15, 30, 60, 120, 240, 480, 1440)                # workflow.notify_after
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]+-]{0,79}")
AGENT_AFTER = ("mode", "effort", "model", "provider", "description", "name")      # a missing agent key goes behind the first that exists
Option = tuple[Any, str, str]                               # (value, label, note)


@dataclass(frozen=True)
class Field:
    """One setting.  `path` is the key in its file (`("defaults", "model")` in config.md, `("project",)` in an agent file),
    `kind` is `choice` (the values come from `options`) or `int` (`low` to `high`).  `unset` is the wording of "not set, the
    default applies" for a field that may be left empty (an agent's); empty: the field always has a value.  `danger` is the
    line a screen shows beside a value that changes the rights of every new chat."""
    path: KeyPath
    label: str
    kind: str
    help: str
    low: int = 0
    high: int = 0
    unset: str = ""
    danger: str = ""


def area_of(field: "Field") -> str:
    """The page of the settings screen a general field is on: `ultracode` for the `workflow:` section, else `general`."""
    return "ultracode" if field.path[0] == "workflow" else "general"


def tokens_label(n: int) -> str:
    """10000 -> `10k`, 2000000 -> `2M`: the number of tokens in the words the budget is written with."""
    return f"{n // 1_000_000}M" if n % 1_000_000 == 0 else f"{n // 1000}k" if n % 1000 == 0 else str(n)


WATCH_EVERY = ("5m", "10m", "15m", "30m", "1h", "2h")
WATCH_NOTIFY = (("both", "Telegram and ntfy", "everything that is set up"), ("telegram", "Telegram", "ntfy when Telegram is not set up"),
                ("ntfy", "ntfy", ""), ("off", "nowhere", "the chat is still woken"))
REMOTE_PUSH = (("webpush", "web push", "to the installed phone app"), ("ntfy", "ntfy", "the push service of watch.notify"), ("both", "both", ""),
               ("off", "nowhere", "the app shows it when opened"))
GENERAL = (
    Field(("defaults", "provider"), "Account", "choice", "Which account pays for a new chat (a file of providers/)."),
    Field(("defaults", "model"), "Model", "choice", "A model of that account, or a tier: strong, medium, cheap."),
    Field(("defaults", "effort"), "Effort", "choice", "How hard the model thinks. More is slower and dearer."),
    Field(("defaults", "context"), "Context", "choice", "How much a chat holds before it hands over to a fresh one."),
    Field(("defaults", "mode"), "Mode", "choice", "How free a new chat is: a mode (a file in modes/).",
          danger="Bypass asks no questions: every new chat then acts without asking."),
    Field(("sessions", "open"), "Open chats in", "choice", "Where a new or resumed chat opens: a new tab, a new window or this window."),
    Field(("sessions", "auto_title"), "Name chats", "choice", "Whether a chat gets a title and a short description by itself, from its newest work (one small model call every few turns)."),
    Field(("sessions", "title_model"), "Naming model", "choice", "The model that names chats. A cheap one is enough.", unset="haiku"),
    Field(("sessions", "title_every"), "Rename every", "int", "A chat is named after its first answer and then every this many answers (1 = after each), and when it ends.", low=1, high=50),
    Field(("ui", "boot"), "Start checklist", "choice", "Whether the checklist shows at the start, and how much of it."),
    Field(("ui", "boot_wait"), "Wait for Enter", "choice", "Yes: the finished checklist stays until Enter. No: it closes by itself."),
    Field(("ui", "notes_layout"), "Notes & To-dos", "choice", "split: beside the chat (from 120 columns). switch: Ctrl+N switches between chat and notes. Under 120 columns it is always switch."),
    Field(("run", "max_parallel"), "Parallel runs", "int", "How many runs started by `lm run` work at the same time.", low=1, high=32),
    Field(("run", "max_depth"), "Run depth", "int", "How deep runs may start runs: 0 is only the top level, 3 a run of a run of a run.", low=0, high=10),
    Field(("spawn", "model"), "Sub-agent model", "choice", "The model of a sub-agent the chat starts with `spawn`, unless the call or the agent names one. A cheaper one saves money. Default: the chat's model.",
          unset="same as the chat"),
    Field(("spawn", "auto_turns"), "Automatic turns", "int", "How many turns a chat may start by itself, one after another, when background sub-agents report back. Then results wait until you write. 0: never by itself.", low=0, high=20),
    Field(("workflow", "max_parallel"), "Workflow agents at once", "int", "How many agents of one workflow work at the same time. Never more than 16 or the CPUs minus 2.",
          low=1, high=32, unset="min(16, CPUs-2)"),
    Field(("workflow", "laptop_parallel"), "Workflow agents on a laptop", "int", "The same on a laptop (Windows, macOS or server `laptop`): it froze twice with more.", low=1, high=8),
    Field(("workflow", "agent_memory_mb"), "RAM per workflow agent (MB)", "int", "How much RAM one more agent may take. A new agent waits while less than this plus the free reserve is left.",
          low=256, high=65536),
    Field(("workflow", "free_memory_mb"), "RAM kept free (MB)", "int", "How much RAM must stay free when a workflow starts one more agent.", low=0, high=65536),
    Field(("workflow", "max_agents"), "Agents per workflow", "int", "The most agents one workflow run starts. Further calls give None (and a log line). A guard against endless loops in a script.", low=1, high=10000),
    Field(("workflow", "max_items"), "Items per parallel", "int", "The most elements one parallel() or pipeline() runs. The rest give None (and a log line).", low=1, high=65536),
    Field(("workflow", "budget"), "Token budget", "int", "Tokens one workflow may use in all (input, output, cache writes). Then running agents stop. A budget in the call or the script wins. Default: none.",
          low=10_000, high=1_000_000_000, unset="none"),
    Field(("workflow", "model"), "Workflow agent model", "choice", "The model of a workflow agent whose call and agent file name none. Default: the model of spawn, else the chat's.", unset="as spawn / the chat"),
    Field(("workflow", "effort"), "Workflow agent effort", "choice", "How hard a workflow agent thinks when its call and agent file name no effort."),
    Field(("workflow", "schema_retries"), "Schema retries", "int", "How many fresh tries an agent gets when its answer does not fit the schema of the call.", low=0, high=10),
    Field(("workflow", "skeptics"), "Sceptics per verify", "int", "How many sceptics an adversarial verify uses. Told to the model in ultracode.", low=1, high=9),
    Field(("workflow", "dry_rounds"), "Empty rounds", "int", "How many rounds without anything new end a loop-until-dry. Told to the model in ultracode.", low=1, high=10),
    Field(("workflow", "tidy_worktrees"), "Remove empty worktrees", "choice", "Yes: an isolated worktree without changes is removed with its branch at the end of the workflow. No: all stay."),
    Field(("workflow", "notify"), "Report long workflows", "choice", "Where the user is told that a long workflow has ended: Telegram, ntfy, both, or nowhere."),
    Field(("workflow", "notify_after"), "Report after (min)", "int", "A workflow that ran at least this many minutes is reported when it ends.", low=1, high=1440),
    Field(("workflow", "confirm"), "Ask before a workflow", "choice", "Yes: a workflow asks before it starts (never in bypass). No: it shows its plan and starts."),
    Field(("workflow", "keyword"), "Word ultracode", "choice", "Yes: the word ultracode in your message turns on effort ultracode for that chat. No: only /effort does."),
    Field(("watch", "every"), "Watch interval", "choice", "How often the watch looks at the sub-runs a chat started (`lm run --detach`) and wakes that chat with the result. A run that dies or stops is noticed within a minute either way."),
    Field(("watch", "max_restarts"), "Restarts per sub-run", "int", "How often the watch restarts one sub-run that died, hit its turn limit or a rate limit, or ran out of context. 0: never restart, only report.", low=0, high=10),
    Field(("watch", "notify"), "Tell the user", "choice", "Where the watch reports a dead, failed or off-track sub-run, a restart, and the end of all of them: Telegram, ntfy, both, or nowhere."),
    Field(("tools", "memory_mb"), "Tool memory (MB)", "int", "Most memory one tool or shell command (with all it starts) may use before it is stopped. 0 is no limit. Default: 40 % of the RAM, at least 2048.",
          low=0, high=262144, unset="40 % of the RAM"),
    Field(("vscode", "agent"), "VS Code agent", "choice", "The agent of a chat started from VS Code. Default: no agent.", unset="none"),
    Field(("vscode", "model"), "VS Code model", "choice", "The model of a chat started from VS Code. Default: defaults.model.", unset="default"),
    Field(("vscode", "effort"), "VS Code effort", "choice", "How hard the model thinks in VS Code. Default: defaults.effort.", unset="default"),
    Field(("vscode", "mode"), "VS Code mode", "choice", "The mode of a chat started from VS Code. Default: defaults.mode.", unset="default",
          danger="Bypass asks no questions: every chat started from VS Code then acts without asking."),
    Field(("quick", "model"), "Quick chat model", "choice", "The model of `lm q`. Default: opus.", unset="default"),
    Field(("quick", "effort"), "Quick chat effort", "choice", "How hard the model thinks in `lm q`. Default: high.", unset="default"),
    Field(("quick", "context"), "Quick chat context", "choice", "How much a quick chat holds before it hands over. Default: 500k.", unset="default"),
    Field(("quick", "mode"), "Quick chat mode", "choice", "The mode of `lm q`: a mode (a file in modes/). Default: bypass.", unset="default",
          danger="Bypass asks no questions: every quick chat then acts without asking."),
    Field(("remote", "enabled"), "Remote Control", "choice", "Whether this tree takes part in Remote Control (the phone app): /rc and the node only work when it is on. Default: no.", unset="no"),
    Field(("remote", "auto"), "Remote auto", "choice", "Whether every new chat turns Remote Control on by itself (like /rc). /rc off ends it for one chat. Default: yes (when Remote Control is on).", unset="yes"),
    Field(("remote", "gateway"), "Remote gateway", "choice", "The server the gateway of the phone app runs on. No default: set it before you switch Remote Control on.", unset="none"),
    Field(("remote", "default_node"), "Remote default node", "choice", "The machine a new session of the phone app starts on, unless the app picks another. No default.", unset="none"),
    Field(("remote", "agent"), "Remote agent", "choice", "The agent of a chat started from the phone. Default: no agent.", unset="none"),
    Field(("remote", "model"), "Remote model", "choice", "The model of a chat started from the phone. Default: defaults.model.", unset="default"),
    Field(("remote", "effort"), "Remote effort", "choice", "How hard the model thinks in a chat started from the phone. Default: defaults.effort.", unset="default"),
    Field(("remote", "mode"), "Remote mode", "choice", "The mode of a chat started from the phone. Default: defaults.mode.", unset="default",
          danger="Bypass asks no questions: every chat started from the phone then acts without asking."),
    Field(("remote", "push"), "Remote push", "choice", "How the phone is told that Remote Control is on: web push, ntfy, both or not at all. Default: web push.", unset="webpush"),
    Field(("remote", "push_questions"), "Push on questions", "choice", "Whether the phone gets a push when a chat asks a question and no device has that chat open. Default: yes.", unset="yes"),
    Field(("remote", "upload_mb"), "Upload limit (MB)", "int", "The largest file the phone app may upload to a chat. Default: 200.", low=1, high=2000, unset="200"),
)
AGENT = (
    Field(("provider",), "Account", "choice", "The account this agent uses. Default: defaults.provider.", unset="default"),
    Field(("model",), "Model", "choice", "A model of that account, or a tier. Default: defaults.model.", unset="default"),
    Field(("effort",), "Effort", "choice", "How hard it thinks. Default: defaults.effort.", unset="default"),
    Field(("mode",), "Mode", "choice", "A mode (a file in modes/). Default: defaults.mode.", unset="default",
          danger="Bypass asks no questions: this agent then acts without asking."),
    Field(("project",), "Project", "choice", "Its default project: memory and assets load with it, `-p` wins. Grants nothing.", unset="none"),
)


def _accounts(home: Home) -> dict[str, Any]:
    try:
        return dict(home.load_providers().accounts)
    except Exception:                                       # noqa: BLE001 - a half-edited provider file: no choices, not a crash
        return {}


def _modes(home: Home) -> dict[str, Any]:
    try:
        return home.modes()
    except ValueError:                                      # the reason travels with `modes_error`, the list is empty
        return {}


def modes_error(home: Home) -> str:
    """'' when the permissions file offers modes, else why not."""
    try:
        home.modes()
    except ValueError as e:
        return str(e)
    return ""


def _contexts(home: Home) -> tuple[str, ...]:
    ui = home.settings.get("ui")
    raw = ui.get("context_sizes") if isinstance(ui, dict) else None
    raw = raw.replace(",", " ").split() if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return tuple(dict.fromkeys(str(v).strip().lower() for v in raw if str(v).strip())) or CONTEXTS


def _is(o: Any, value: Any) -> bool:
    return type(o) is type(value) and o == value            # strict: 1 is not True


def options(home: Home, field: Field, values: Mapping[KeyPath, Any] | None = None) -> list[Option]:
    """The values `field` may take, as (value, label, note): what a screen offers and `check_value` accepts.  `values` says what
    the other fields are (or will be): the models depend on the account.  A field with `unset` starts with (None, wording).
    What the file holds now stays on the list, so a value the list does not know yet can still be kept."""
    key, vals = field.path[-1], values or {}
    out: list[Option]
    if key == "provider":
        out = [(n, n, a.type) for n, a in _accounts(home).items()]
    elif key in ("model", "title_model"):
        name = vals.get(field.path[:-1] + ("provider",)) or home.defaults.get("provider") or ""
        a = _accounts(home).get(str(name))
        out = []
        if a is not None:
            out = [(m, m, "default" if m == a.default_model else "") for m in a.models]
            if a.default_model and a.default_model not in a.models:
                out.insert(0, (a.default_model, a.default_model, "default"))
            out += [(t, t, f"tier -> {m}") for t, m in a.tiers.items()]
    elif field.path == ("workflow", "effort"):
        out = [(e, e, EFFORT_TEXT.get(e, "")) for e in AGENT_EFFORTS]          # `ultracode` is a level of the chat, not of an agent
    elif key == "effort":
        out = [(e, e, EFFORT_TEXT.get(e, "")) for e in EFFORTS]
    elif key == "context":
        out = [(c, c, "") for c in _contexts(home)]
    elif key == "mode":
        out = [(k, str(m.get("label") or k), " ".join(str(m.get("text") or "").split())[:60]) for k, m in _modes(home).items()]
    elif key == "open":
        out = [(w, w, t) for w, t in OPEN_WORDS]
    elif key == "boot":
        out = list(BOOT_WORDS)
    elif key in ("boot_wait", "auto_title", "enabled", "auto", "push_questions", "confirm", "keyword", "tidy_worktrees"):
        out = list(YES_NO)
    elif key == "notes_layout":
        out = list(NOTES_LAYOUTS)
    elif key == "every":
        out = [(e, e, "") for e in WATCH_EVERY]
    elif key == "notify":
        out = list(WATCH_NOTIFY)
    elif key == "push":
        out = list(REMOTE_PUSH)
    elif key in ("gateway", "default_node"):
        out = [(n, n, "") for n in home.list_servers()]
    elif key == "project":
        out = [(p, p, "") for p in home.list_projects()]
    elif key == "agent":
        out = [(a, a, "") for a in agent_names(home)]
    elif field.kind == "int":
        steps = {"max_parallel": PARALLEL, "memory_mb": MEMORY, "agent_memory_mb": MEMORY, "free_memory_mb": MEMORY, "upload_mb": UPLOAD_MB,
                 "max_agents": AGENTS, "max_items": ITEMS, "budget": TOKENS, "notify_after": MINUTES}.get(key) or range(field.low, field.high + 1)
        out = [(n, tokens_label(n) if key == "budget" else str(n), "only the top level" if key == "max_depth" and n == 0 else "no limit" if key == "memory_mb" and n == 0 else "")
               for n in steps if field.low <= n <= field.high]
    else:
        out = []
    cur = vals.get(field.path)
    if cur not in (None, "") and not any(_is(o[0], cur) for o in out) and (field.kind == "int" or key in ("model", "title_model", "context", "project", "provider", "mode", "agent", "gateway", "default_node")):
        out.append((cur, str(cur), "set in the file, not on the list"))
    return ([(None, field.unset, "")] if field.unset else []) + out


def check_value(home: Home, field: Field, value: Any, values: Mapping[KeyPath, Any] | None = None) -> str:
    """'' when `value` is allowed for `field`, else why not.  Types are strict (`1` is not `True`).  An empty value is allowed
    only for a field with `unset`.  A model the account file does not list is still a model when its name is plain."""
    if value is None or value == "":
        return "" if field.unset else f"{field.label}: a value is needed"
    if field.kind == "int":
        if type(value) is not int or not field.low <= value <= field.high:
            return f"{field.label}: a whole number from {field.low} to {field.high}"
        return ""
    if field.path[-1] in ("model", "title_model") and isinstance(value, str) and MODEL_NAME.fullmatch(value):
        return ""
    others = {k: v for k, v in (values or {}).items() if k != field.path}          # the candidate itself is no reason to allow it
    if any(_is(o[0], value) for o in options(home, field, others) if o[0] is not None):
        return ""
    return f"{field.label}: {value!r} is not one of the values offered"


@dataclass
class Row:
    """A field with what its file holds (`value`, None = not set there) and what applies now (`effective`)."""
    field: Field
    value: Any
    effective: Any


@dataclass
class Page:
    """The rows of one file, and the bytes they were read from: a save checks the file still has them (`Conflict`).
    `error` says why the file cannot be edited here (then `rows` is empty).  `state` is how the core reads config.md."""
    title: str
    path: Path
    loaded: bytes
    rows: list[Row]
    scope: str = "general"                                  # general | agent
    name: str = ""                                          # the agent
    error: str = ""
    state: str = "ok"                                       # ok | unguarded | error | missing | empty (general only)

    def values(self) -> dict[KeyPath, Any]:
        return {r.field.path: r.value for r in self.rows}


def _read_page(title: str, path: Path, fields: tuple[Field, ...], scope: str, name: str,
               effective: Callable[[Field, dict[str, Any]], Any]) -> Page:
    try:
        raw = path.read_bytes()
    except OSError as e:
        return Page(title, path, b"", [], scope, name, f"{path.name}: {e.strerror or e}")
    if not raw.strip():
        return Page(title, path, raw, [], scope, name, f"{path.name} is empty")
    try:
        meta = front_meta(decode(raw))
    except SettingsError as e:
        return Page(title, path, raw, [], scope, name, f"{path.name}: {e}")
    rows = []
    for f in fields:
        v = dig(meta, f.path)
        rows.append(Row(f, None if v in (None, "") else v, effective(f, meta)))
    return Page(title, path, raw, rows, scope, name)


def read_general(home: Home) -> Page:
    """config.md as a page (GENERAL).  `state` is how the core reads it: an `unguarded` or broken file shows its values all
    the same, so they can be mended, but they do not apply."""
    page = _read_page("General", home.config_file, GENERAL, "general", "", lambda f, meta: dig(home.settings, f.path))
    page.state = home.config.state
    if page.error.endswith("is empty"):
        page.error += ": run `lm update`, it fills the file from lightmind.md"
    return page


def agent_names(home: Home) -> list[str]:
    return sorted(home.list_agents())


def agent_file(home: Home, name: str) -> Path:
    return home.path("agents") / name / home.filename("agent")


def read_agent(home: Home, name: str) -> Page:
    """One agent file as a page (AGENT).  `effective` is what the agent really gets: its own value, else the tree's default
    (a project has none)."""
    def effective(f: Field, meta: dict[str, Any]) -> Any:
        own = dig(meta, f.path)
        if own not in (None, ""):
            return own
        return None if f.path == ("project",) else dig(home.settings, ("defaults",) + f.path)
    return _read_page(name, agent_file(home, name), AGENT, "agent", name, effective)


def save_page(home: Home, page: Page, changes: Mapping[KeyPath, Any]) -> Page:
    """Write only the changed fields of `page` (key path -> new value, None = not set) into its file, line by line, and read
    the page again.  Every value is checked first (`check_value`, against what the other fields will be); a file that changed
    since the page was read is a `Conflict`; the writer's counter-check applies.  Nothing is written on any error.  An agent
    field set to None keeps its key as an empty value (the comment next to it stays); a key the file does not have is left
    out."""
    if page.error:
        raise SettingsError(page.error)
    fields = {f.path: f for f in (GENERAL if page.scope == "general" else AGENT)}
    have = page.values()
    changes = {tuple(p): v for p, v in changes.items() if not (tuple(p) in have and _is(have[tuple(p)], v))}   # "already so" needs no check
    values = have | changes
    problems = [f"{'.'.join(p)}: no such setting" if (f := fields.get(tuple(p))) is None else check_value(home, f, v, values)
                for p, v in changes.items()]
    if any(problems):
        raise SettingsError("; ".join(p for p in problems if p))
    try:
        held = front_meta(decode(page.path.read_bytes()))
    except (OSError, SettingsError):
        held = {}
    todo: dict[KeyPath, Any] = {}
    for p, v in changes.items():
        p = tuple(p)
        if v is not None:
            todo[p] = v
        elif dig(held, p) is not None:
            todo[p] = ""
    if todo:
        save(home, page.path, todo, page.loaded, after=AGENT_AFTER if page.scope == "agent" else ())
    return read_general(home) if page.scope == "general" else read_agent(home, page.name)
