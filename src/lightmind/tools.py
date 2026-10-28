"""Tool dispatcher: built-ins, drop-in external tools, tool hashes, rule check, secret
injection, output masking.

Drop-in: a tool is a folder in the `tools` location with scripts and a README.md; discovered
at chat start, no registration.  Scripts declare dependencies with PEP 723 inline metadata
and the AI runs them with the bash tool, as the skill says.  Only with `tools.as_functions:
true` in lightmind.md is a tool that has a `tool.md` with a `run:` command also offered as a
function of its own (`run:` e.g. `uv run pdf.py`).  Skills accept the Claude Code format
(folder with SKILL.md) unchanged.

Parameter kinds: a param may declare `kind: read-path` or `kind: write-path` in tool.md;
the dispatcher then adds a read()/write() check for that argument, so a tool cannot reach
files the rules forbid.

May import: model, files, rules, secrets, runs.  The rule/secret functions are reached
through `api` (default: the real modules) so tests can inject a fake checker.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from . import interject as _ij
from . import spawn as _spawn
from .files import Home
from .images import is_image, load_image
from .providers.base import Interjected
from .util import setting as _setting, to_int as _to_int
from .model import Agent, Check, RuleContext, SessionState, Tool, ToolCall, ToolResult
from .memlimit import Job, exceeded, limit_mb, posix_preexec
from .runs import RunDir

if TYPE_CHECKING:
    from .rules import Asker

PATH_KINDS = {"read-path": "read", "write-path": "write"}
_ENV_KEEP = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "HOME", "USERPROFILE",
             "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "USER",
             "USERNAME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT",
             "PLAYWRIGHT_BROWSERS_PATH")
NEW_GROUP: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}   # POSIX: own process group
_SKIP_DIRS = {"__pycache__", ".venv", ".git"}


def default_api() -> SimpleNamespace:
    from . import rules, secrets   # late: tests inject a fake api instead
    return SimpleNamespace(
        build_layers=rules.build_layers, checks_for_call=rules.checks_for_call,
        enforce=rules.enforce, describe_call=rules.describe_call, decide=rules.decide, audit=rules.audit, Ledger=rules.Ledger,
        build_tool_env=secrets.build_tool_env, Masker=secrets.Masker,
        secrets_tool=secrets.secrets_tool)


EDIT_MAX_BYTES = 20 * 2**20       # `edit` holds the whole file in memory twice


def _read_window(p: Path, offset: int, limit: int) -> tuple[list[str], int]:
    """`(lines offset..offset+limit, total line count)` read line by line: memory stays at one window."""
    window: list[str] = []
    total = 0
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if offset <= total < offset + limit:
                window.append(line.rstrip("\r\n"))
            total += 1
    return window, total


_CREDENTIAL_VAR = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|PRIVATE_?KEY", re.I)


_SECRET_CACHE: dict[str, tuple[tuple, list[tuple[str, str, str]]]] = {}


def _secret_entries(home: Home, _secrets: Any) -> list[tuple[str, str, str]]:
    """`(ref, var, value)` of every `.env` file of the tree.  Parsed once; the files' mtime and size are the key, so
    a changed, added or removed file is picked up on the next call (the stat per file is all that repeats)."""
    try:
        refs = _secrets.list_secrets(home)
    except OSError:
        refs = []
    root = home.path("secrets")
    files, sig = [], []
    for ref in refs:
        name, _, acct = ref.partition("/")
        f = root / name / (acct + _secrets.ENV_SUFFIX)
        try:
            st = f.stat()
        except OSError:
            continue
        files.append((ref, f))
        sig.append((ref, st.st_mtime_ns, st.st_size))
    key, hit = str(root), _SECRET_CACHE.get(str(root))
    if hit and hit[0] == tuple(sig):
        return hit[1]
    entries: list[tuple[str, str, str]] = []
    complete = True
    for ref, f in files:
        try:
            values = _secrets.parse_env(f)
        except OSError:                                         # locked by a scanner or sync: read again next time
            complete = False
            continue
        entries += [(ref, var, value) for var, value in values.items()]
    if complete:
        _SECRET_CACHE[key] = (tuple(sig), entries)
    return entries


def secret_masker(home: Home, masker: Any = None) -> Any:
    """A Masker that knows EVERY secret value of the tree (all `secrets/<name>/<account>.env`
    files) and the credential-like variables of the environment, not only the values a tool
    received so far.  A session (also a resumed one) and the hooks start from this, so a secret
    written to a file earlier cannot reach the model raw later.  `masker.add()` stays for later injections."""
    from . import secrets as _secrets          # late, like rules: keeps the import graph small
    m = masker if masker is not None else _secrets.Masker()
    for ref, var, value in _secret_entries(home, _secrets):
        m.add(ref, var, value)
    for var, value in os.environ.items():       # a tool or hook inherits parts of the environment: mask its credentials
        if _CREDENTIAL_VAR.search(var):
            m.add("env", var, value)
    return m


# ------------------------------------------------------------------ hashes
def tool_hash(tool: Tool) -> str:
    """sha256 over tool.md + every file in the tool folder (sorted)."""
    h, root = hashlib.sha256(), Path(tool.path)
    for f in sorted(p for p in root.rglob("*") if p.is_file()
                    and not _SKIP_DIRS & set(p.relative_to(root).parts)):
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _store(home: Home) -> Path:
    return home.path("state") / "tools-approved.json"


def hash_approved(home: Home, tool: Tool) -> bool:
    """Is the current hash in the approval store (location: paths.state)?"""
    try:
        return json.loads(_store(home).read_text("utf-8")).get(tool.name) == tool_hash(tool)
    except (OSError, ValueError):
        return False


def approve_hash(home: Home, tool: Tool) -> None:
    p = _store(home)
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    data[tool.name] = tool_hash(tool)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, sort_keys=True), "utf-8")


# ----------------------------------------------------------------- schemas
def _schema(name: str, desc: str, props: dict[str, dict], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}


_S = {"type": "string"}
_BUILTINS: dict[str, dict[str, Any]] = {s["name"]: s for s in (
    _schema("bash", "Run a command line in the tree root with the configured shell (bash); returns output. "
                    "`description` is required: one short sentence in plain words, in the language of the user, saying what "
                    "the command does for them (\"Show the changed files\"), never the command itself; the user reads it "
                    "when asked for permission.",
            {"command": _S, "description": _S, "timeout": {"type": "integer"}}, ["command", "description"]),
    _schema("read", "Read a text file (numbered lines); a PNG/JPEG/GIF/WebP file is shown as a picture. A line "
                    "`[Anhang: <path>]` in a message of the user is a file they attached: open that path with this tool "
                    "(PDF and Office files: see the media skill).",
            {"path": _S, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
    _schema("write", "Write a text file (creates folders).", {"path": _S, "content": _S},
            ["path", "content"]),
    _schema("edit", "Replace one exact occurrence of old with new in a file.",
            {"path": _S, "old": _S, "new": _S}, ["path", "old", "new"]),
    _schema("skill_load", "Return the full text of a skill or of a tool's tool.md.",
            {"name": _S}, ["name"]),
    _schema("ask", "Ask the user a question in the terminal and return the answer.",
            {"question": _S}, ["question"]),
    _schema("todo", "The to-dos of this chat as a small file tree (the user sees it beside the chat): loose tasks and notes "
                    "in the root, folders (no deeper) holding tasks and notes. Actions: list [folder], add_task title [info] "
                    "[folder], add_note title [text] [folder], add_folder title, update id [status open|doing|done] [info] "
                    "[title] [text], comments [id] (the user's remarks on tasks), delete id, rename title (the name of this chat, shown at the top "
                    "of the list), describe text (its description; empty = automatic again). `folder` is a name or id, none = "
                    "root. Put what you are working on in `doing`, write what you found into `info`; answer the user's remarks.",
            {"action": _S, "folder": _S, "id": _S, "title": _S, "status": _S, "info": _S, "text": _S}, ["action"]),
    _schema("spawn", _spawn.DESCRIPTION, {"agent": _S, "task": _S, "model": _S, "effort": _S, "background": {"type": "boolean"}}, ["task"]),
    _schema("workflow", "Run a workflow: a Python script that orchestrates many sub-agents (skill `ultracode`: read it before "
                        "your first script). `script` is the code inline, or `path` a .py file in the tree; it holds `meta = {...}` "
                        "(a pure literal: name, description, phases) and `async def main(args)` using agent(), parallel(), pipeline(), "
                        "phase(), log(), workflow(), budget. `args`: JSON text, the script's input. `resume_run_id`: the id of an "
                        "earlier workflow of this chat: the unchanged start comes from its journal, the rest runs live. `budget`: a "
                        "token limit such as `+500k` or `2m` (also read from a `+500k` in the script's meta). background (default "
                        "true): the call returns at once with the workflow id and the result comes later as a message like a "
                        "sub-agent's; with false the call waits. The script's return value is the result (data, not words of the user).",
            {"script": _S, "path": _S, "args": _S, "resume_run_id": _S, "budget": _S, "background": {"type": "boolean"}}, []),
    _schema("job", "A long `bash` or `spawn` (background false) call is detached when the user writes a message while it runs: "
                   "it keeps running as a job (`job-1`...) and you get the user's message at once. Actions: status (all jobs; the "
                   "output of a finished one comes once), wait id [timeout seconds, default 60] (returns when the job is "
                   "done, or at once when another message of the user arrives), cancel id. Jobs end with your turn: wait for "
                   "the ones you need before you finish.",
            {"action": _S, "id": _S, "timeout": {"type": "integer"}}, ["action"]),
    _schema("relay", "Pass a message on to a sub-agent that is still working (`to`: its id or name, or `all`). It reads it "
                     "at its next step and adapts its task. Use it when a message of the user changes what a sub-agent "
                     "should do; do not forward what does not concern it.",
            {"to": _S, "text": _S}, ["to", "text"]),
    _schema("timer", "Set a timer that wakes THIS chat with a message of your own at a later time (also when you have ended your turn "
                     "and nobody is typing). Actions: set when message [until], list, cancel id. `when`: `in 20m` once, `every 15m` or "
                     "`every 2h` repeating (at least 1 minute apart; at most 20 timers per chat). `until`: a time (2026-10-23T18:00), "
                     "`in 3h` or `children-done` (the sub-runs you started are over); a repeating timer without `until` ends after a "
                     "day. The message comes back to you as a note marked [Timer]; it is a reminder from yourself, not a message of "
                     "the user. You cannot set timers for other chats. Sub-runs you start with `lm run --detach` are watched "
                     "automatically; you do not need a timer for that.",
            {"action": _S, "when": _S, "message": _S, "until": _S, "id": _S}, ["action"]),
    _schema("secrets", "Manage secrets (new|set|rotate|describe); returns placeholders only.",
            {"action": _S, "ref": _S, "var": _S, "from_ref": _S, "text": _S,
             "generate": {"type": "boolean"}, "length": {"type": "integer"}}, ["action", "ref"]),
)}
ALWAYS_ON = ("skill_load", "ask", "todo", "spawn", "workflow", "job", "relay", "timer")
DETACHABLE = ("bash", "spawn", "workflow")      # tool calls that can run long; a message of the user detaches them (see `call_watched`)


def builtin_schemas(home: Home) -> list[dict[str, Any]]:
    """JSON schemas of the built-ins, sorted by name.  web and ssh follow with servers (phase 3)."""
    return [_BUILTINS[n] for n in sorted(_BUILTINS)]


def _kind(spec: Any) -> str:
    return spec if isinstance(spec, str) else (spec or {}).get("kind", "")


def external_schemas(tools: list[Tool]) -> list[dict[str, Any]]:
    """Schema per external tool from tool.md params; sorted by name."""
    out = []
    for t in sorted(tools, key=lambda t: t.name):
        props, req = {}, []
        for p, spec in t.params.items():
            spec = {} if isinstance(spec, str) else dict(spec or {})
            spec.pop("kind", None)
            if spec.pop("required", False):
                req.append(p)
            props[p] = {"type": "string", **spec}
        out.append(_schema(t.name, t.description, props, req))
    return out


# ----------------------------------------------------------------- helpers
def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not a number")


def _spent(text: str, reserved: float) -> float:
    """USD a money tool reports in its output (`spent_usd`), clamped to [0, reserved]; anything
    unreadable, non-numeric, NaN or infinite counts as the full reservation."""
    try:
        v = json.loads(text, parse_constant=_reject_constant).get("spent_usd", reserved)
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return reserved
        v = float(v)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return reserved
    return min(max(v, 0.0), reserved) if math.isfinite(v) and math.isfinite(reserved) else reserved


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(p))


def _within(path: str, root: str) -> bool:
    """Is the normalised `path` the folder `root` or inside it?"""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                      # another drive
        return False


def resolve_shell(name: str = "bash", root: str | Path | None = None) -> str:
    """Absolute path of the shell the `shell` setting names.  Windows: Git Bash under Program Files,
    then the Git install of the `git` on PATH, then PATH; never WSL's System32 launcher.  A relative
    PATH entry, the current directory and every PATH entry that lies inside `root` (the tree root, links
    resolved) are not searched: Windows would look in the current directory first, and a file an agent
    left in the tree, even in a folder on PATH, must not become the shell.  An absolute `name` is
    taken as given.  Not found -> an error, no fallback to cmd.exe or another shell."""
    exe = name + ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    if os.path.isabs(name):
        if os.path.isfile(name):
            return name
    else:
        here, inside = _norm(os.getcwd()), _norm(root) if root else None
        path = [d for d in (x.strip('"') for x in os.environ.get("PATH", "").split(os.pathsep))
                if os.path.isabs(d) and _norm(d) != here and not (inside and _within(_norm(d), inside))]
        dirs = path
        if os.name == "nt":
            git = next((d for d in path if os.path.isfile(os.path.join(d, "git.exe"))), None)   # e.g. ...\Git\cmd
            dirs = [os.path.join(p, "Git", "bin") for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
                    if (p := os.environ.get(v))] + ([os.path.join(git, "..", "bin")] if git else []) + path
        for d in dirs:
            c = os.path.normpath(os.path.join(d, exe))
            if os.path.isfile(c) and "system32" not in c.lower():
                return c
    raise RuntimeError(f"shell {name!r} (setting `shell`) not found; install it (Git for Windows "
                       "provides bash) or set `shell:` to its full path")


def kill_tree(pid: int) -> None:
    """Kill a process and everything it started: `taskkill /T` on Windows, the process group on POSIX
    (the process must have been started with NEW_GROUP).  Best effort, never raises."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def _safe_path(home: Home, raw: Any) -> Path | None:
    """The absolute place `raw` names, or None when it needs interpretation: not a plain
    non-empty string, a Windows trap (alternate data stream, 8.3 short name, trailing dot or
    space, device / UNC prefix), or a form that differs once links are resolved.  What the rule
    checker judged and what the tool opens are then the same place."""
    from .rules import unsafe_path
    if not isinstance(raw, str) or not raw.strip() or unsafe_path(raw) or any(ord(c) < 32 for c in raw):
        return None                                   # control characters: realpath raises on Linux
    root = os.path.normpath(home.root)
    lex = os.path.normpath(home.abs(raw))
    nc = os.path.normcase
    if nc(lex) == nc(root) or nc(lex).startswith(nc(root) + os.sep):
        expected = os.path.join(os.path.realpath(root), os.path.relpath(lex, root))
    else:
        expected = lex
    real = os.path.realpath(lex)
    if nc(os.path.normpath(real)) != nc(os.path.normpath(expected)):
        return None
    return Path(real)


# -------------------------------------------------------------- dispatcher
# `tools/<name>` as a path in a command line; group 1 = what stands before it (up to and including the slash)
_TOOL_REF = re.compile(r"""(?<![\w.-])((?:[^\s"'`;|&<>()=]*[/\\])?)tools[/\\]([\w.-]+)(?=[/\\\s"'`;|&<>()]|$)""")


class Dispatcher:
    """One per chat/run.  call() = checks -> enforce -> inject -> run -> mask -> log."""

    def __init__(self, home: Home, *, agent: Agent | None, mode: str, session: SessionState,
                 rule_ctx: RuleContext, run: RunDir, asker: "Asker | None" = None,
                 api: Any = None, questioner: Any = None, workdir: str = "",
                 spawn_ctx: "_spawn.SpawnCtx | None" = None, background: "_spawn.Background | None" = None) -> None:
        self.home, self.agent, self.mode, self.session = home, agent, mode, session
        self.ctx, self.run, self.asker = rule_ctx, run, asker
        self.names_set: Any = None        # (field, text) -> line: set by the Session; `todo` rename/describe end in titles.set_manual
        self.todo_told: Any = None       # (before, after) digests: set by the Session so own `todo` changes are not echoed back
        self.questioner = questioner     # async (question) -> answer text for the `ask` tool; None = input() on the terminal
        self.workdir = workdir           # absolute folder the user works in: `$LM_WORKDIR` in the shell (the cwd stays the tree)
        self.api = api or default_api()
        self.masker = secret_masker(home, self.api.Masker())
        self.n = 0
        # sub-agents (spawn.py): the queue their events travel on to the chat's loop, the calls it has seen
        self.spawn = spawn_ctx or _spawn.new_root()
        self.background = background                    # the chat's hub for sub-agents that work while it is free (a Session's)
        self.events: asyncio.Queue = asyncio.Queue()    # the loop swaps in a fresh one per turn
        self.cur: SimpleNamespace | None = None         # model, effort, context, budget of the running turn (set by stream_loop)
        self._pending: list[ToolCall] = []
        self.inbox: _ij.Inbox | None = None             # open interjections of this agent loop (the Session's, or a sub-agent's)
        self.jobs = _ij.Jobs()                          # long calls detached by an interjection; ended with the turn
        self._spawned: set[asyncio.Task] = set()
        # tools of this dispatcher only, set by the core (orchestrate.py: StructuredOutput): {name: (schema, async handler(args))}.
        # They only hand data back to the core, so no rule is checked for them; the model can never add one.
        self.local_tools: dict[str, tuple[dict[str, Any], Any]] = {}
        # discovered fresh at chat start; agent None or without tools = everything found
        want = {r.name for r in (agent.tools if agent else []) + session.tools_added}
        want -= set(session.tools_removed)
        everything = agent is None or not agent.tools
        self.builtins = {n for n in _BUILTINS if n in ALWAYS_ON or everything or n in want}
        # `tools.as_functions`: false = only the built-ins are functions (the AI runs a tool script with
        # bash, as its skill says); true = tools with a manifest are functions too, never one without `run`
        as_fn = bool(_setting(home, "tools.as_functions"))
        self.ext = {t.name: t for t in home.list_tools() if t.run and (everything or t.name in want)
                    and t.name not in session.tools_removed} if as_fn else {}
        if "spawn" in self.builtins and not _spawn.available(self):
            self.builtins.discard("spawn")
        if "spawn" not in self.builtins or self.spawn.level:      # workflows only from the chat itself: one level of them
            self.builtins.discard("workflow")
        if "spawn" not in self.builtins:
            self.builtins.discard("relay")

    # ---- sub-agents
    def emit(self, ev: Any) -> None:
        """An event of a sub-agent for the chat's event stream (the loop of this dispatcher yields it)."""
        self.events.put_nowait(ev)

    def note_call(self, c: ToolCall) -> None:
        """The loop saw a `spawn` call (its id): the tool itself does not get the id, it takes it from here."""
        self._pending.append(c)

    def take_call_id(self, agent: str, task: str) -> str:
        for i, c in enumerate(self._pending):
            if str(c.args.get("agent") or "").strip() == agent and str(c.args.get("task") or "").strip() == task:
                return self._pending.pop(i).id
        return f"spawn-{self.n}"

    def cancel_spawned(self) -> None:
        """The chat was stopped: end every sub-agent that is still working."""
        me = asyncio.current_task()
        for t in list(self._spawned):
            if t is not me and not t.done():
                t.cancel()

    def schemas(self) -> list[dict[str, Any]]:
        """Built-in + external tools of the current loadout, sorted by name."""
        s = ([_BUILTINS[n] for n in self.builtins] + external_schemas(list(self.ext.values()))
             + [schema for schema, _ in self.local_tools.values()])
        return sorted(s, key=lambda x: x["name"])

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Never raises for denied/failed calls; returns ToolResult(ok=False, ...)."""
        self.n += 1
        args = args or {}
        try:
            res = await self._call(name, args)
        except Exception as e:  # noqa: BLE001 - a tool must never crash the loop
            res = ToolResult(False, f"error: {type(e).__name__}: {e}")
        res.text = _ij.neutralize(res.text)                # a tool result cannot pass for an interjection
        d = res.denied_by
        self.run.event("tool", {
            "n": self.n, "name": name, "ok": res.ok, "file": res.file,
            "args": self.masker.mask(json.dumps(args, default=str)[:500]),
            "denied_by": (d.rule.text() if d.rule else d.reason) if d else ""})
        return res

    # ---- interjections
    def deliver(self, emit: bool = True) -> tuple[str, list[Interjected]]:
        """Take the open interjections of this loop: (the framed text for the model, the events for the UI).  With
        `emit` the events also go on the event queue of the running turn (the backend that owns the loop cannot
        yield them itself).  ("", []) when nothing waits."""
        items = self.inbox.take() if self.inbox is not None else []
        if not items:
            return "", []
        evs = [Interjected(i.id, i.text) for i in items]
        if emit:
            for ev in evs:
                self.emit(ev)
        running = [lbl for lbl, _, _ in self._children()] if "relay" in self.builtins else []
        return _ij.frame(items, self.inbox.source if self.inbox else _ij.USER, running), evs

    def _children(self) -> list[tuple[str, "_ij.Inbox", Any]]:
        """The sub-agents this dispatcher started that are working now: (label, inbox, owner).  Not its grandchildren and not
        its siblings: a message passed on by `relay` must come from the agent that started the receiver."""
        return [(lbl, ib, own) for lbl, ib, own in (self.spawn.shared or {}).get("inboxes", {}).values() if own is self]

    def _detachable(self, name: str) -> bool:
        return name in DETACHABLE or name in self.ext

    async def call_watched(self, name: str, args: dict[str, Any]) -> ToolResult:
        """`call`, but a long tool call does not hide a message of the user: once one is open (or arrives while the
        call runs) the call gets `interject.GRACE` seconds to end by itself, then it is detached into a job and the
        result says so.  The call is not stopped.  Only `bash`, `spawn` and external tools can be detached."""
        ib = self.inbox
        if ib is None or not self._detachable(name):
            return await self.call(name, args)
        task = asyncio.ensure_future(self.call(name, args))
        try:
            while not task.done():
                if ib.pending:
                    await asyncio.wait({task}, timeout=_ij.GRACE)
                    break
                waiter = asyncio.ensure_future(ib.wait())
                try:
                    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
            if task.done():
                return task.result()
        except BaseException:                               # the turn was stopped: the call ends with it
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        job = self.jobs.add(name, _ij.describe(name, args), task)
        self._spawned.discard(task)                         # the job list owns it now: the end of a model call must not cancel it
        return ToolResult(True, f"[{name} is still running as {job.id} ({job.what}); it was not stopped. A message of the user "
                                f"arrived meanwhile (below). Follow the job with the `job` tool (status, wait, cancel); "
                                f"it ends with your turn.]")

    # ---- pipeline
    async def _call(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name in self.local_tools:
            return await self.local_tools[name][1](args if isinstance(args, dict) else {})
        tool = self.ext.get(name)
        if tool is None and name not in self.builtins:
            return ToolResult(False, f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return ToolResult(False, "arguments must be an object")
        declared = set(tool.params) if tool else set(_BUILTINS[name]["input_schema"]["properties"])
        args = {k: v for k, v in args.items() if k in declared}   # undeclared arguments never pass
        approved = True
        if tool:
            miss = [p for p, s in tool.params.items()
                    if isinstance(s, dict) and s.get("required") and p not in args]
            if miss:
                return ToolResult(False, f"missing parameter(s): {', '.join(miss)}")
            bad = [p for p, s in tool.params.items()
                   if _kind(s) in PATH_KINDS and p in args and _safe_path(self.home, args[p]) is None]
            if bad:       # a path parameter must be a plain string that names exactly one place
                return ToolResult(False, f"refused: path parameter(s) {', '.join(bad)} must be a "
                                         "plain string without links, 8.3 names or stream suffixes")
            approved = hash_approved(self.home, tool)
        checks = self.api.checks_for_call(name, args, tool_meta=tool, hash_approved=approved)
        if tool:
            for p, spec in tool.params.items():
                if _kind(spec) in PATH_KINDS and p in args:
                    checks.append(Check(PATH_KINDS[_kind(spec)], args[p]))
        layers = self.api.build_layers(self.home, self.agent, self.mode, self.session, None)
        describe = getattr(self.api, "describe_call", None)      # (a test api may bring none: the rules make the sentence)
        extra = (self.masker.mask(describe(name, args, self.ctx.home, meta=tool)),) if describe else ()
        dec = await self.api.enforce(layers, checks, self.ctx, self.asker, self.session, *extra)
        if dec.effect != "allow":
            why = dec.rule.text() if dec.rule else dec.reason
            return ToolResult(False, f"denied by rule {why} ({dec.layer})", denied_by=dec)
        if tool and not approved and not self._never_asks():   # run unasked in bypass: not approved for the other modes
            approve_hash(self.home, tool)
        if tool:       # the tool opens exactly the place that was checked, not a path relative to its own folder
            args = {**args, **{p: str(_safe_path(self.home, args[p])) for p, s in tool.params.items()
                               if _kind(s) in PATH_KINDS and p in args}}
            if tool.match in args:
                from .rules import match_values        # hand the tool the values the rules judged
                args[tool.match] = match_values(args[tool.match])
            return await self._external(tool, args, layers)
        return await getattr(self, f"_b_{name}")(**args)

    def _out(self, name: str, text: str, ok: bool = True, inline: bool = False) -> ToolResult:
        """Mask; above the inline limit the full text goes to tool-out (unless `inline`)."""
        text = self.masker.mask(text)
        mode = self.ext[name].output if name in self.ext else "auto"
        limit = int(self.home.key_doc("context").meta.get("tool_output_inline", 2000))
        if not inline and (mode == "file" or (mode != "inline" and len(text) > limit)):
            f = self.run.tool_out(self.n, name, text)
            head = "\n".join(text.splitlines()[:40])
            return ToolResult(ok, f"output in {f} ({len(text)} chars); first 40 lines:\n{head}",
                              file=str(f))
        return ToolResult(ok, text)

    def _parent_sig(self) -> str:
        """Proof for `lm run --detach` that the child really is started by this run (wake.sign_parent)."""
        from . import wake
        try:
            return wake.sign_parent(self.home, self.run.run.id)
        except OSError:
            return ""

    def _env(self) -> dict[str, str]:
        """What a tool process gets: `home.runtime_env()` (uv, Python and browsers inside the tree) cut down to
        the names in _ENV_KEEP, so no credential of the parent process reaches a tool."""
        env = {k: v for k, v in self.home.runtime_env().items() if k.upper() in _ENV_KEEP}
        if self.workdir:
            env["LM_WORKDIR"] = self.workdir
        env.update(LM_HOME=str(self.home.root), LM_RUN=str(self.run.path), LM_RESULTS=str(self.run.results_path()),
                   LM_DEPTH=str(self.run.run.depth + 1), LM_PARENT=self.run.run.id,
                   LM_PARENT_SIG=self._parent_sig())   # a run started from here is one deeper
        return env

    def memory_limit(self, tool_mb: Any = None) -> int:
        """MB one process tree may use (`tools.memory_mb` in config.md, or a tool's own `memory_mb:`); 0 = no limit."""
        return limit_mb(_setting(self.home, "tools.memory_mb"), tool_mb)

    async def _exec(self, cmd: str | list[str], cwd: Path, env: dict, timeout: int,
                    stdin: str = "", merge: bool = False, label: str = "bash",
                    memory_mb: Any = None) -> tuple[int, str]:
        mb = self.memory_limit(memory_mb)
        kw: dict[str, Any] = dict(
            cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge else asyncio.subprocess.PIPE, **NEW_GROUP)
        if mb and os.name != "nt":
            kw["preexec_fn"] = posix_preexec(mb)       # Windows: the job below
        if isinstance(cmd, str):       # a command line runs in the configured shell, not the platform default
            cmd = [resolve_shell(str(_setting(self.home, "shell", "bash")), self.home.root), "-c", cmd]
        p = await asyncio.create_subprocess_exec(*cmd, **kw)
        job = Job.start(mb, p.pid)
        try:
            try:
                out, err = await asyncio.wait_for(p.communicate(stdin.encode()), timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                kill_tree(p.pid)               # the shell and everything it started, not only the shell
                with contextlib.suppress(OSError):
                    p.kill()
                await p.wait()
                if isinstance(e, asyncio.CancelledError):
                    raise                      # a stopped chat or run stops its tools with it
                return 124, f"timeout after {timeout}s"
            rc = p.returncode or 0
            text = out.decode("utf-8", "replace")
            if err and rc:
                text += ("\n" if text else "") + err.decode("utf-8", "replace")
            if exceeded(rc, text, mb, job):
                return 137, f"stopped: {label} used more than {mb} MB (tools.memory_mb in config.md)"
            return rc, text
        finally:
            if job:
                job.close()                    # ends whatever is left of the tree

    async def _external(self, tool: Tool, args: dict[str, Any], layers) -> ToolResult:
        def allowed(ref: str) -> bool:
            return self.api.decide(layers, Check("secret", ref), self.ctx).effect == "allow"
        inj = self.api.build_tool_env(self.home, list(tool.secrets), allowed, self._env())
        if [s for s in tool.required_secrets if s not in {u["secret"] for u in inj.uses}]:
            return ToolResult(False, "required secret not available (denied by rules)")
        for u in inj.uses:
            for var in u.get("vars", []):
                if inj.env.get(var):
                    self.masker.add(u["secret"], var, inj.env[var])
            self.api.audit(self.home, "secret_use", {"run": self.run.run.id, "tool": tool.name, **u})
        money = tool.category == "money"
        if money:
            ledger, spend = self.api.Ledger(self.home), tool.max_usd_per_call
            if not ledger.reserve(spend):
                return ToolResult(False, "denied: money limit reached")
        if tool.run.startswith("builtin:"):
            return await getattr(self, "_b_" + tool.run[8:])(**args)
        rc, text = await self._exec(shlex.split(tool.run), Path(tool.path), inj.env,
                                    tool.timeout, json.dumps(args), label=tool.name,
                                    memory_mb=tool.memory_mb)
        if money:
            ledger.settle(spend, _spent(text, spend))
        if tool.category != "none":
            self.api.audit(self.home, tool.category,
                           {"run": self.run.run.id, "tool": tool.name, "ok": rc == 0})
        return self._out(tool.name, text, rc == 0, inline=rc == 137)

    def _bash_tools(self, command: str) -> list[Tool]:
        """Tools of this tree whose folder the command line names (`tools/<name>/...`, relative or absolute)."""
        found: dict[str, Tool] = {}
        for m in _TOOL_REF.finditer(command):
            prefix, name = m.group(1), m.group(2)
            if name in found or name.startswith("."):
                continue
            base = Path(prefix[:-1]) if prefix else Path(".")       # what stands before `tools/`
            try:
                if (base if base.is_absolute() else self.home.root / base).resolve() != self.home.root.resolve():
                    continue                    # another tree's or a stray `tools` folder: no secrets for it
                found[name] = self.home.load_tool(name)
            except (OSError, ValueError):       # not a tool of this tree
                continue
        return list(found.values())

    def _bash_env(self, command: str) -> dict[str, str]:
        """Tool environment plus the granted secrets of every tool the command runs, so
        `uv run tools/x/x.py` gets what its tool.md declares; same rule check and audit as a tool call."""
        env = self._env()
        tools = self._bash_tools(command)
        if not tools:
            return env
        layers = self.api.build_layers(self.home, self.agent, self.mode, self.session, None)

        def allowed(ref: str) -> bool:
            return self.api.decide(layers, Check("secret", ref), self.ctx).effect == "allow"
        for tool in tools:
            inj = self.api.build_tool_env(self.home, list(tool.secrets), allowed, env)
            env = inj.env
            for u in inj.uses:
                for var in u.get("vars", []):
                    if inj.env.get(var):
                        self.masker.add(u["secret"], var, inj.env[var])
                self.api.audit(self.home, "secret_use",
                               {"run": self.run.run.id, "tool": tool.name, "via": "bash", **u})
        return env

    # ---- built-ins (paths resolve against the tree root)
    async def _b_bash(self, command: str, description: str = "", timeout: int = 120) -> ToolResult:
        rc, text = await self._exec(command, self.home.root, self._bash_env(command), timeout, merge=True)
        if rc == 137 and text.startswith("stopped:"):
            return self._out("bash", text, False, inline=True)
        return self._out("bash", text + ("" if rc == 0 else f"\n[exit {rc}]"), rc == 0)

    def _path_or_refusal(self, path: Any) -> Path | ToolResult:
        p = _safe_path(self.home, path)
        return p if p is not None else ToolResult(
            False, f"refused: {path!r} is ambiguous or resolves to a different place")

    async def _b_read(self, path: str, offset: int = 0, limit: int = 200) -> ToolResult:
        p = self._path_or_refusal(path)
        if isinstance(p, ToolResult):
            return p
        if is_image(p):
            return self._read_image(p)
        offset, limit = max(0, offset), max(0, limit)
        shown, total = await asyncio.to_thread(_read_window, p, offset, limit)     # a big log must not stall the loop
        text = "\n".join(f"{offset + i + 1}\t{l}" for i, l in enumerate(shown))
        if offset + limit < total:
            text += f"\n[{total} lines total; continue with offset={offset + limit}]"
        return self._out("read", text, inline=True)   # already paged; never moved to a file

    def _read_image(self, p: Path) -> ToolResult:
        """A picture goes to the model as an image block; the log and the text only say its size."""
        try:
            image, w, h = load_image(p)
        except (ValueError, ImportError) as e:
            return ToolResult(False, f"cannot read image: {e}")
        return ToolResult(True, f"image {w}x{h}", image=image)

    async def _b_write(self, path: str, content: str) -> ToolResult:
        p = self._path_or_refusal(path)
        if isinstance(p, ToolResult):
            return p
        def put() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, "utf-8", newline="")
        put()                                                   # not in a thread: a cancel must not report a write that still lands
        return ToolResult(True, f"wrote {len(content)} chars to {path}")

    async def _b_edit(self, path: str, old: str, new: str) -> ToolResult:
        p = self._path_or_refusal(path)
        if isinstance(p, ToolResult):
            return p
        def swap() -> ToolResult:
            if p.stat().st_size > EDIT_MAX_BYTES:
                return ToolResult(False, f"refused: file is larger than {EDIT_MAX_BYTES // 2**20} MB, edit it with a script")
            text = p.read_text("utf-8")
            if text.count(old) != 1:
                return ToolResult(False, f"old text found {text.count(old)} times, need exactly 1")
            p.write_text(text.replace(old, new), "utf-8", newline="")
            return ToolResult(True, f"edited {path}")
        return swap()                                           # not in a thread, same reason as write

    async def _b_skill_load(self, name: str) -> ToolResult:
        if (not isinstance(name, str) or not name.strip() or ".." in name
                or any(c in name for c in "/\\:\0")):
            return ToolResult(False, f"refused: {name!r} is not a plain skill or tool name")

        def inside(p: Path, root: Path) -> bool:
            try:
                p.resolve().relative_to(root.resolve())
                return True
            except (ValueError, OSError):
                return False
        skills, tools = self.home.path("skills"), self.home.path("tools")
        for s in self.home.list_skills():
            if s.name == name:
                if not inside(Path(s.path), skills):
                    return ToolResult(False, f"refused: skill {name!r} lies outside the skills location")
                return ToolResult(True, f"{s.body}\n\n[skill folder: {Path(s.path).parent}]")
        if (tools / name).is_dir():
            if not inside(tools / name, tools):
                return ToolResult(False, f"refused: tool {name!r} lies outside the tools location")
            t = self.home.load_tool(name)
            return ToolResult(True, (Path(t.path) / self.home.filename("tool")).read_text("utf-8"))
        return ToolResult(False, f"no skill or tool named {name!r}")

    def _never_asks(self) -> bool:
        from .rules import mode_never_asks             # late, as above: a test api may bring no rules module
        home = getattr(self, "home", None)
        return mode_never_asks(home, self.mode) if home is not None else self.mode == "bypass"

    async def _b_ask(self, question: str) -> ToolResult:
        if self._never_asks():
            return ToolResult(True, f"(no answer: the mode {self.mode} asks nothing; decide and act)")
        if self.questioner is not None:
            return ToolResult(True, str(await self.questioner(question)))
        def prompt() -> str:
            try:
                return input(f"\n[agent asks] {question}\n> ")
            except EOFError:
                return "(no answer: non-interactive)"
        return ToolResult(True, await asyncio.to_thread(prompt))

    async def _b_todo(self, action: str, **kw) -> ToolResult:
        from . import todos
        if str(action).strip().lower() in ("rename", "describe"):
            field, text = ("title", kw.get("title", "")) if str(action).strip().lower() == "rename" else ("description", kw.get("text", ""))
            if self.names_set is None:
                return ToolResult(False, "this chat cannot be renamed here")
            return ToolResult(True, self.masker.mask(self.names_set(field, str(text or ""))))
        store = todos.Store(self.run.path)
        before = store.digest()
        ok, text = todos.run_tool(store, action, **kw)
        if self.todo_told is not None:
            self.todo_told(before, store.digest())
        return ToolResult(ok, self.masker.mask(text))

    async def _b_spawn(self, task: str = "", agent: str = "", model: str = "", effort: str = "",
                       background: Any = True) -> ToolResult:
        bg = background if isinstance(background, bool) else str(background).strip().lower() not in ("false", "0", "no")
        return await _spawn.run(self, agent, task, model, effort, bg)

    async def _b_workflow(self, script: str = "", path: str = "", args: Any = "", resume_run_id: str = "",
                          budget: str = "", background: Any = True) -> ToolResult:
        from . import orchestrate       # late: the runtime is only loaded when a workflow runs
        bg = background if isinstance(background, bool) else str(background).strip().lower() not in ("false", "0", "no")
        return await orchestrate.run_tool(self, script=script, path=path, args=args, resume_run_id=resume_run_id,
                                          budget=budget, background=bg)

    async def _b_job(self, action: str = "status", id: str = "", timeout: Any = 60) -> ToolResult:
        action, jobs = str(action).strip().lower(), self.jobs
        if action == "status":
            if not jobs.items:
                return ToolResult(True, "no jobs")
            lines = []
            for j in jobs.items.values():
                if j.task.done() and not j.reported:
                    lines.append(f"{j.id} {j.tool} finished: {self._job_text(j)}")
                else:
                    lines.append(f"{j.id} {j.tool} {'reported' if j.task.done() else f'running {j.age}s'}: {j.what}")
            return ToolResult(True, "\n".join(lines))
        job = jobs.get(str(id))
        if job is None:
            return ToolResult(False, f"no such job {id!r}; `job status` lists them")
        if action == "cancel":
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            jobs.items.pop(job.id, None)
            return ToolResult(True, f"{job.id} cancelled")
        if action != "wait":
            return ToolResult(False, "action is status, wait or cancel")
        ib = self.inbox
        if ib is not None and ib.pending:
            return ToolResult(True, f"not waiting: a message of the user is open and comes with this result; {job.id} is still running")
        waiter = asyncio.ensure_future(ib.wait()) if ib is not None else None
        try:
            await asyncio.wait({job.task} | ({waiter} if waiter else set()), return_when=asyncio.FIRST_COMPLETED,
                               timeout=min(max(_to_int(timeout, 60), 1), _ij.WAIT_MAX))
        finally:
            if waiter:
                waiter.cancel()
        if job.task.done():
            return ToolResult(True, f"{job.id} finished: {self._job_text(job)}")
        why = "a new message of the user arrived" if ib is not None and ib.pending else "timeout"
        return ToolResult(True, f"{job.id} still running after {job.age}s ({why})")

    @staticmethod
    def _job_text(j: "_ij.Job") -> str:
        j.reported = True
        if j.task.cancelled():
            return "cancelled"
        exc = j.task.exception()
        return f"error: {exc}" if exc else ("" if j.task.result().ok else "failed\n") + j.task.result().text

    async def _b_relay(self, to: str = "", text: str = "") -> ToolResult:
        reg: dict = {sid: v for sid, v in (self.spawn.shared or {}).get("inboxes", {}).items() if v[2] is self}
        want = str(to).strip().lower()
        hits = [(sid, lbl, ib) for sid, (lbl, ib, _) in reg.items() if want == "all" or want in (sid.lower(), lbl.lower())]
        if not str(text).strip():
            return ToolResult(False, "relay needs a text")
        if not hits:
            return ToolResult(False, f"no sub-agent {to!r} is working now" + (f" (working: {', '.join(v[0] for v in reg.values())})" if reg else ""))
        done, full = [], []
        for sid, lbl, ib in hits:
            (done if ib.put(str(text)) else full).append(lbl)
        msg = (f"relayed to {', '.join(done)}: it reads this at its next step" if done else "")
        if full:
            msg += ("; " if msg else "") + f"{', '.join(full)} already has {_ij.MAX_OPEN} open messages, try again later"
        return ToolResult(bool(done), msg)

    async def _b_timer(self, action: str = "", when: str = "", message: str = "", until: str = "", id: str = "") -> ToolResult:
        """Timers of the caller's own session only: the target is never an argument, so there is nothing to aim elsewhere."""
        from . import timers
        sid = self.run.run.id
        try:
            if action == "set":
                it = timers.set_timer(self.home, sid, when, message, until)
                return ToolResult(True, f"timer {it.name}: {it.when}" + (f", until {it.until}" if it.until else "")
                                  + ". It wakes this chat with your message, marked [Timer].")
            if action == "list":
                rows = timers.list_timers(self.home, sid)
                return ToolResult(True, "\n".join(f"{r['id']}  {r['when']}  next {r['next']}"
                                                    + (f"  until {r['until']}" if r["until"] else "") + f"  {r['message']}" for r in rows) or "no timers")
            if action == "cancel":
                return (ToolResult(True, f"timer {id} cancelled") if timers.cancel_timer(self.home, sid, str(id).strip())
                        else ToolResult(False, f"no timer {id!r} of this chat"))
        except ValueError as e:
            return ToolResult(False, str(e))
        return ToolResult(False, "timer: action is set, list or cancel")

    async def _b_secrets(self, action: str, ref: str, **kw) -> ToolResult:
        return ToolResult(True, self.api.secrets_tool(self.home, action, ref, **kw))
