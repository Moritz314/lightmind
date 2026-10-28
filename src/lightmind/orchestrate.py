"""Workflow runtime: runs a Python workflow script that orchestrates many sub-agents through the spawn loop (asyncio).

A script is plain Python text with `meta = {...}` (a pure literal: name, description, phases) and `async def main(args)`.
It gets `agent`, `parallel`, `pipeline`, `phase`, `log`, `workflow`, `args` and `budget` as globals and nothing else
of the core: it is checked by its syntax tree before it runs (no clock, no chance, no imports beyond a few pure helpers,
no names or attributes with `_`, no classes) and executed with a small set of builtins, so it reaches files and
programs only through its agents, which have the chat's rights (spawn.py).  The check is a guard against mistakes and
against simple tricks of an injected prompt, not a hard security boundary: the boundary are the agents' rule checks.

Every agent() call is written to `runs/<run>/workflow/<wf-id>/journal.jsonl`; a later run with `resume_run_id` takes
the unchanged start from there (see `Journal`).  May import: spawn, schema, memlimit, util, model, providers.base
(+ tools lazily for the rule check of a named agent type).
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import hashlib
import inspect
import itertools
import json
import math
import os
import re
import string
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import CodeType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable

from . import schema as jsonschema
from . import spawn, ultracode
from .memlimit import available_ram_mb
from .model import Check, ToolResult
from .providers.base import Notice, WorkflowEvent
from .util import setting as _setting, to_int as _int

if TYPE_CHECKING:
    from .tools import Dispatcher

MAX_AGENTS = 1000            # agents per run (nested workflows included): a guard against endless loops in a script; `workflow.max_agents`
MAX_ITEMS = 4096             # elements per parallel() / pipeline(); the rest is logged and returns None; `workflow.max_items`
MAX_PARALLEL = 16            # agents at once, at most; also never more than CPUs - 2
LAPTOP_PARALLEL = 3          # agents at once on a laptop (`workflow.laptop_parallel`): it froze twice on 2026-10-23
AGENT_MEMORY_MB = 1024       # RAM one more agent may take (`workflow.agent_memory_mb`): a Claude CLI process and its tools
FREE_MEMORY_MB = 2048        # RAM that must stay free after it (`workflow.free_memory_mb`)
SCHEMA_RETRIES = 3           # further tries when an answer does not fit its schema; `workflow.schema_retries`
STEP_LIMIT = 1_000_000       # loop steps of the script itself: it runs on the event loop of the chat, a busy loop would freeze it
RAM_POLL = 2.0               # seconds between two looks at the free RAM while an agent waits for it
RESULT_LIMIT = 6000          # characters of the result value shown in the tool result; the whole value is in result.json
STRUCTURED = "StructuredOutput"

_ID = re.compile(r"wf-\d{1,6}")
parse_budget = ultracode.parse_budget       # the parser lives with the other settings (ultracode.py); kept here for the scripts' tool


class WorkflowError(Exception):
    """The script cannot run (a check failed) or broke a rule of the runtime; the message says what to change."""


# ------------------------------------------------------------------ the script
NONDETERMINISTIC = {"time", "datetime", "random", "uuid", "secrets", "calendar", "zoneinfo"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "globals", "locals", "vars", "getattr", "setattr", "delattr",
                   "input", "breakpoint", "type", "object", "super", "memoryview", "help", "exit", "quit", "dir",
                   "classmethod", "staticmethod", "property", "hash", "id"}
# attributes that lead from plain values back to frames, code, the event loop or str.format's attribute lookup
BLOCKED_ATTRS = {"format", "format_map", "mro", "with_traceback", "throw", "get_loop", "get_coro", "get_stack",
                 "print_stack", "subprocess_shell", "subprocess_exec", "run_in_executor", "add_done_callback",
                 "set_result", "set_exception", "call_soon", "call_later", "create_task", "send", "asend", "athrow"}
_FRAME_ATTR = re.compile(r"^(f|tb|gi|cr|ag|co|func)_")
_SAFE_BUILTINS = ("abs", "all", "any", "ascii", "bin", "bool", "callable", "chr", "dict", "divmod", "enumerate", "filter",
                  "float", "frozenset", "hex", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
                  "min", "next", "oct", "ord", "pow", "range", "repr", "reversed", "round", "set", "slice", "sorted",
                  "str", "sum", "tuple", "zip", "Exception", "ValueError", "KeyError", "IndexError", "TypeError",
                  "RuntimeError", "StopIteration", "ZeroDivisionError", "AssertionError", "ArithmeticError",
                  "LookupError", "NotImplementedError")


def _modules() -> dict[str, Any]:
    """What `import x` gives a script: the pure helpers only, as namespaces of chosen functions.  Not the real modules
    of pure Python: their attributes lead to the modules they import (`json.codecs.open`)."""
    import collections
    import functools
    import textwrap
    return {
        "json": SimpleNamespace(loads=json.loads, dumps=json.dumps),
        "re": SimpleNamespace(**{n: getattr(re, n) for n in ("search", "match", "fullmatch", "findall", "finditer", "sub",
                                                              "subn", "split", "compile", "escape", "I", "IGNORECASE",
                                                              "M", "MULTILINE", "S", "DOTALL", "X", "VERBOSE")}),
        "math": math,                    # a C module: functions and constants only
        "itertools": itertools,          # a C module; its endless iterators are bounded by STEP_LIMIT
        "collections": SimpleNamespace(Counter=collections.Counter, defaultdict=collections.defaultdict,
                                       OrderedDict=collections.OrderedDict, deque=collections.deque,
                                       namedtuple=collections.namedtuple),
        "functools": SimpleNamespace(reduce=functools.reduce, partial=functools.partial),
        "textwrap": SimpleNamespace(dedent=textwrap.dedent, indent=textwrap.indent, shorten=textwrap.shorten,
                                    wrap=textwrap.wrap, fill=textwrap.fill),
        "string": SimpleNamespace(ascii_letters=string.ascii_letters, ascii_lowercase=string.ascii_lowercase,
                                  ascii_uppercase=string.ascii_uppercase, digits=string.digits, punctuation=string.punctuation),
    }


ALLOWED_MODULES = ("json", "re", "math", "itertools", "collections", "functools", "textwrap", "string")


class _Guard(ast.NodeVisitor):
    """Collects every place of a script that may not run, with its line, so one message names all of them."""

    def __init__(self, bound: set[str]) -> None:
        self.errors: list[str] = []
        self.bound = bound          # names the script binds itself: `id = 3` is its own `id`, not the builtin

    def bad(self, node: ast.AST, why: str) -> None:
        self.errors.append(f"line {getattr(node, 'lineno', '?')}: {why}")

    def _module(self, node: ast.AST, name: str) -> None:
        root = name.split(".")[0]
        if root in NONDETERMINISTIC:
            self.bad(node, f"`{name}` is not allowed: a workflow script must not read the clock or chance "
                           "(resume replays it and must make the same agent calls)")
        elif root not in ALLOWED_MODULES or "." in name:
            self.bad(node, f"import of `{name}` is not allowed: a script reaches files and programs only through "
                           f"agent(); allowed: {', '.join(ALLOWED_MODULES)}")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._module(node, a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.bad(node, "relative imports are not allowed")
        self._module(node, node.module or "")
        for a in node.names:
            if a.name.startswith("_") or a.name == "*":
                self.bad(node, f"`from {node.module} import {a.name}` is not allowed")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            self.bad(node, f"names starting with `_` are not allowed (`{node.id}`)")
        elif node.id in FORBIDDEN_NAMES and node.id not in self.bound:
            self.bad(node, f"`{node.id}` is not allowed in a workflow script")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr in BLOCKED_ATTRS or _FRAME_ATTR.match(node.attr):
            hint = " (use an f-string)" if node.attr in ("format", "format_map") else ""
            self.bad(node, f"the attribute `.{node.attr}` is not allowed in a workflow script{hint}")
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bad(node, f"setting an attribute (`.{node.attr} = ...`) is not allowed in a workflow script; use dicts")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bad(node, "classes are not allowed in a workflow script; use dicts")

    def _args(self, node: Any) -> None:
        for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                  *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            if a.arg.startswith("_"):
                self.bad(node, f"names starting with `_` are not allowed (`{a.arg}`)")

    def visit_FunctionDef(self, node: Any) -> None:
        if node.name.startswith("_"):
            self.bad(node, f"names starting with `_` are not allowed (`{node.name}`)")
        self._args(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._args(node)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name and node.name.startswith("_"):
            self.bad(node, f"names starting with `_` are not allowed (`{node.name}`)")
        self.generic_visit(node)


class _Steps(ast.NodeTransformer):
    """Counts the script's own loop steps: every `for`, `while` and comprehension ticks once per round.  The script runs
    on the chat's event loop, so a loop without an await in it would freeze the chat; past STEP_LIMIT it stops."""

    def _wrap(self, it: ast.expr, name: str) -> ast.expr:
        return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=[it], keywords=[])

    def visit_For(self, node: ast.For) -> ast.For:
        self.generic_visit(node)
        node.iter = self._wrap(node.iter, "_wf_iter")
        return node

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AsyncFor:
        self.generic_visit(node)
        node.iter = self._wrap(node.iter, "_wf_aiter")
        return node

    def visit_While(self, node: ast.While) -> ast.While:
        self.generic_visit(node)
        tick = ast.Expr(value=ast.Call(func=ast.Name(id="_wf_tick", ctx=ast.Load()), args=[], keywords=[]))
        node.body.insert(0, tick)
        return node

    def visit_comprehension(self, node: ast.comprehension) -> ast.comprehension:
        self.generic_visit(node)
        node.iter = self._wrap(node.iter, "_wf_aiter" if node.is_async else "_wf_iter")
        return node


@dataclass
class Script:
    """A checked workflow script, ready to run."""
    source: str
    meta: dict[str, Any]
    code: CodeType
    path: str = ""

    @property
    def name(self) -> str:
        return str(self.meta.get("name") or Path(self.path).stem or "workflow")

    @property
    def phases(self) -> list[str]:
        return [str(p.get("title") if isinstance(p, dict) else p) for p in self.meta.get("phases") or []]


def _bound(tree: ast.AST) -> set[str]:
    """Every name the script binds: assignments, loop and with targets, functions, parameters, imports, except names."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out


def _meta(tree: ast.Module) -> dict[str, Any]:
    """The literal of `meta = {...}` at the top level of the script."""
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "meta"):
            try:
                value = ast.literal_eval(node.value)
            except ValueError as e:
                raise WorkflowError(f"line {node.lineno}: `meta` must be a pure literal (no names, no calls): {e}") from None
            if not isinstance(value, dict):
                raise WorkflowError(f"line {node.lineno}: `meta` must be a dict")
            return value
    raise WorkflowError("the script has no `meta = {...}` at its top level (name, description, phases)")


def _check_meta(meta: dict[str, Any]) -> None:
    name, phases = meta.get("name"), meta.get("phases", [])
    if not isinstance(name, str) or not name.strip():
        raise WorkflowError("meta needs a `name` (text)")
    if not isinstance(meta.get("description", ""), str):
        raise WorkflowError("meta `description` must be text")
    if not isinstance(phases, list) or not all(isinstance(p, str) or (isinstance(p, dict) and isinstance(p.get("title"), str))
                                               for p in phases):
        raise WorkflowError("meta `phases` must be a list of {title, detail}")


def load(source: str, path: str = "") -> Script:
    """Check a script and compile it.  WorkflowError names every problem with its line.  `source`: the script's text;
    `path`: where it came from (for messages and the default name)."""
    try:
        tree = ast.parse(source, filename=path or "<workflow>")
    except SyntaxError as e:
        raise WorkflowError(f"syntax error in line {e.lineno}: {e.msg}") from None
    guard = _Guard(_bound(tree))
    guard.visit(tree)
    if guard.errors:
        raise WorkflowError("the script cannot run:\n" + "\n".join(guard.errors))
    meta = _meta(tree)
    _check_meta(meta)
    if not any(isinstance(n, ast.AsyncFunctionDef) and n.name == "main" for n in tree.body):
        raise WorkflowError("the script needs `async def main(args):` at its top level")
    tree = ast.fix_missing_locations(_Steps().visit(tree))
    return Script(source, meta, compile(tree, path or "<workflow>", "exec"), path)


def config(home: Any) -> ultracode.Config:
    """The `workflow:` settings with this module's constants as the defaults of the numeric ones."""
    return ultracode.read(home, max_agents=MAX_AGENTS, max_items=MAX_ITEMS, schema_retries=SCHEMA_RETRIES)


# ------------------------------------------------------------------ limits
def is_laptop(home: Any) -> bool:
    """A machine that must be spared: Windows or macOS, or a tree whose server is named `laptop`.  Servers run Linux."""
    return os.name == "nt" or sys.platform == "darwin" or str(_setting(home, "defaults.server") or "") == "laptop"


def max_parallel(home: Any) -> int:
    """Agents at once: `workflow.max_parallel` (default and ceiling min(16, CPUs-2)), on a laptop at most
    `workflow.laptop_parallel` (3).  Never below 1."""
    cpus = max((os.cpu_count() or 4) - 2, 1)
    n = min(_int(_setting(home, "workflow.max_parallel"), 0) or MAX_PARALLEL, MAX_PARALLEL, cpus)
    if is_laptop(home):
        n = min(n, max(_int(_setting(home, "workflow.laptop_parallel"), LAPTOP_PARALLEL), 1))
    return max(n, 1)


def memory_limits(home: Any) -> str:
    """The two RAM settings in one phrase (`/ultracode status`): what one agent may take / what stays free."""
    return (f"{_int(_setting(home, 'workflow.agent_memory_mb'), AGENT_MEMORY_MB)} / "
            f"{_int(_setting(home, 'workflow.free_memory_mb'), FREE_MEMORY_MB)} MB")


def memory_ok(home: Any) -> tuple[bool, int]:
    """(may one more agent start now, free MB).  Unknown free RAM (0) never blocks."""
    free = available_ram_mb()
    need = _int(_setting(home, "workflow.agent_memory_mb"), AGENT_MEMORY_MB) + _int(_setting(home, "workflow.free_memory_mb"), FREE_MEMORY_MB)
    return (free == 0 or free >= need), free


# ------------------------------------------------------------------ the journal
def _key(prompt: str, opts: dict[str, Any]) -> str:
    """The hash of what decides an agent's result: its prompt and options (label and phase only change the display)."""
    wanted = {k: v for k, v in opts.items() if k not in ("label", "phase")}
    raw = json.dumps({"prompt": prompt, **wanted}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class Journal:
    """`journal.jsonl` of one workflow run: one line per agent() call ({type: agent, seq, key, ok, value, tokens, ...}),
    plus start, phase, log and end.  Resume: the agent lines of an earlier run that ended well are the cache.  An agent
    call is served from it while no call has missed yet and its key (hash of prompt and options, plus how often that
    hash came before) is there; the first call that misses (a changed prompt, a new call, or one that failed last time)
    switches the rest of the run to live, also calls that would match."""

    def __init__(self, folder: Path, cache: dict[str, Any] | None = None) -> None:
        self.folder = folder
        self.path = folder / "journal.jsonl"
        self.cache = cache or {}
        self.live = not self.cache
        self.seen: dict[str, int] = {}
        self.hits = 0
        folder.mkdir(parents=True, exist_ok=True)

    def write(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def key(self, prompt: str, opts: dict[str, Any]) -> str:
        h = _key(prompt, opts)
        n = self.seen.get(h, 0)
        self.seen[h] = n + 1
        return f"{h}#{n}"

    def lookup(self, key: str) -> tuple[bool, Any]:
        """(hit, value).  The first miss turns the journal live for good."""
        if not self.live and key in self.cache:
            self.hits += 1
            return True, self.cache[key]
        self.live = True
        return False, None


def read_cache(folder: Path) -> dict[str, Any]:
    """key -> value of every agent that ended well in the journal of `folder`."""
    out: dict[str, Any] = {}
    try:
        lines = (folder / "journal.jsonl").read_text("utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "agent" and e.get("ok") and "key" in e:
            out[str(e["key"])] = e.get("value")
    return out


# ------------------------------------------------------------------ the runtime
class Budget:
    """`budget` of a script: `total` (tokens, None = no limit), `spent()`, `remaining()` (inf without a limit).  Tokens are
    counted like an agent's own budget: input + output + cache writes, at every usage report.  A hard limit: once spent
    reaches total, the other running agents are stopped, the reporting one ends after its turn, and every further agent()
    returns None."""

    def __init__(self, total: int | None) -> None:
        self.total = total
        self.used = 0

    def spent(self) -> int:
        return self.used

    def remaining(self) -> float:
        return math.inf if self.total is None else max(self.total - self.used, 0)

    def over(self) -> bool:
        return self.total is not None and self.used >= self.total


@dataclass
class Shared:
    """What a workflow run and its nested workflow share: limits, budget, journal, counters, the running agents."""
    wid: str
    folder: Path
    journal: Journal
    budget: Budget
    sem: asyncio.Semaphore
    agents: int = 0
    failed: int = 0
    skipped: int = 0
    seq: int = 0
    running: set = field(default_factory=set)
    stop: str = ""                           # why running agents were stopped ("budget")
    told: set = field(default_factory=set)   # log lines given once (limits, memory)
    parallel: int = 1
    worktrees: list = field(default_factory=list)
    waiting_ram: bool = False
    cfg: ultracode.Config | None = None      # the settings this run started with (a change in config.md applies to the next run)


class Runtime:
    """The functions a script calls, bound to one workflow run.  `emit` takes the events (WorkflowEvent, the sub-agents'
    events); `level` 0 is the workflow the tool started, 1 a nested one (it cannot nest further)."""

    def __init__(self, d: "Dispatcher", script: Script, shared: Shared, emit: Callable[[Any], None], level: int = 0,
                 prefix: str = "") -> None:
        self.d, self.script, self.sh, self.emit, self.level, self.prefix = d, script, shared, emit, level, prefix
        self.current_phase = ""
        self.steps = 0

    # ---- what the script sees
    def globals(self, args: Any) -> dict[str, Any]:
        mods = _modules()

        def safe_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
            if level or name not in mods:
                raise ImportError(f"import of {name!r} is not allowed in a workflow script")
            return mods[name]
        allowed = {n: getattr(builtins, n) for n in _SAFE_BUILTINS}
        allowed.update(__import__=safe_import, print=lambda *a, **_k: self.log(" ".join(map(str, a))))
        return {"__builtins__": allowed, "__name__": "workflow", "_wf_iter": self._iter, "_wf_aiter": self._aiter,
                "_wf_tick": self._tick, "agent": self.agent, "parallel": self.parallel, "pipeline": self.pipeline,
                "phase": self.phase, "log": self.log, "workflow": self.workflow, "args": args, "budget": self.sh.budget}

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > STEP_LIMIT:
            raise WorkflowError(f"the script made more than {STEP_LIMIT} loop steps of its own; a workflow script only "
                                "plans agents, it must not compute much (an endless loop?)")

    def _iter(self, it: Any) -> Any:
        for x in it:
            self._tick()
            yield x

    async def _aiter(self, it: Any) -> Any:
        async for x in it:
            self._tick()
            yield x

    def phase(self, title: Any) -> None:
        """Group the following agents under `title` in the progress display."""
        self.current_phase = self.prefix + str(title)
        self.sh.journal.write({"type": "phase", "title": self.current_phase})
        self.emit(WorkflowEvent(self.sh.wid, "phase", self.script.name, self.current_phase))

    def log(self, text: Any) -> None:
        """One line above the phases (and in the journal)."""
        line = self.prefix + " ".join(str(text).split())[:500]
        self.sh.journal.write({"type": "log", "text": line})
        self.emit(WorkflowEvent(self.sh.wid, "log", self.script.name, line))

    def _once(self, key: str, text: str) -> None:
        if key not in self.sh.told:
            self.sh.told.add(key)
            self.log(text)

    async def parallel(self, thunks: Any) -> list[Any]:
        """Run every thunk (a function without arguments that returns an awaitable, or an awaitable) at the same time and
        wait for all: a barrier.  A thunk that fails gives None (and a log line).  Above MAX_ITEMS the rest gives None."""
        items = list(thunks)
        cut = self._cap(items, "parallel")

        async def one(i: int, t: Any) -> Any:
            if i >= cut:
                if inspect.iscoroutine(t):
                    t.close()                        # never started: no "never awaited" warning
                return None
            try:
                aw = t() if callable(t) else t
                return await aw if inspect.isawaitable(aw) else aw
            except (WorkflowError, asyncio.CancelledError):
                raise
            except Exception as e:                   # noqa: BLE001 - a failed branch is None, the others go on
                self.log(f"parallel branch {i + 1} failed: {type(e).__name__}: {e}")
                return None
        return list(await asyncio.gather(*(one(i, t) for i, t in enumerate(items))))

    async def pipeline(self, items: Any, *stages: Any) -> list[Any]:
        """Send every item through all stages on its own, without a barrier between stages: an item goes on as soon as its
        last stage is done.  A stage gets (value) or (value, item) or (value, item, index), as many as it takes; the first
        stage's value is the item.  A stage that gives None or fails ends that item's chain with None."""
        seq = list(items)
        cut = self._cap(seq, "pipeline")

        async def chain(i: int, item: Any) -> Any:
            if i >= cut:
                return None
            value = item
            for n, stage in enumerate(stages):
                try:
                    out = stage(*(value, item, i)[:_arity(stage)])
                    value = await out if inspect.isawaitable(out) else out
                except (WorkflowError, asyncio.CancelledError):
                    raise
                except Exception as e:               # noqa: BLE001
                    self.log(f"pipeline item {i + 1}, stage {n + 1} failed: {type(e).__name__}: {e}")
                    return None
                if value is None:
                    return None
            return value
        return list(await asyncio.gather(*(chain(i, x) for i, x in enumerate(seq))))

    def _cap(self, items: list[Any], what: str) -> int:
        cap = self.sh.cfg.max_items
        if len(items) > cap:
            self.log(f"{what}: {len(items)} items, only the first {cap} run; items {cap + 1}-{len(items)} give None")
        return min(len(items), cap)

    async def workflow(self, name_or_path: Any, args: Any = None) -> Any:
        """Run another workflow script inside this one (one level deep) and return what its main returns.  `name_or_path`:
        a .py path (relative to the tree root or absolute) or a name of `skills/ultracode/workflows/<name>.py`."""
        if self.level:
            raise WorkflowError("workflows nest one level deep: a nested workflow cannot start another")
        path = await _resolve(self.d, str(name_or_path))
        child = load(path.read_text("utf-8"), str(path))
        self.log(f"nested workflow {child.name} starts")
        rt = Runtime(self.d, child, self.sh, self.emit, level=1, prefix=f"{child.name}: ")
        value = await rt.run(args)
        self.log(f"nested workflow {child.name} ended")
        return value

    async def run(self, args: Any) -> Any:
        glob = self.globals(args)
        exec(self.script.code, glob)                  # noqa: S102 - the checked script (see load); it only defines meta and main
        main = glob.get("main")
        if not inspect.iscoroutinefunction(main):
            raise WorkflowError("`main` must be `async def main(args)`")
        return await main(args)

    # ---- agent()
    async def agent(self, prompt: Any, label: str = "", phase: str = "", schema: Any = None, model: str = "",
                    effort: str = "", isolation: Any = None, agent_type: str = "", **unknown: Any) -> Any:
        """Start one sub-agent with `prompt`; its final text is the value (with `schema`: the checked object it gave
        through StructuredOutput).  None when it failed, was skipped (limits, budget, refused) or was stopped.  With
        `isolation="worktree"` it works in a git worktree of its own and the value is {"value", "worktree", "branch"}."""
        if unknown:
            raise WorkflowError(f"agent(): unknown option(s) {', '.join(sorted(unknown))}; known: label, phase, schema, "
                                "model, effort, isolation, agent_type")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError("agent() needs a prompt (text)")
        if schema is not None and not isinstance(schema, dict):
            raise WorkflowError("agent(schema=...) must be a JSON schema (a dict)")
        if isolation not in (None, "", "worktree"):
            raise WorkflowError('agent(isolation=...) is None or "worktree"')
        opts = {"label": str(label or ""), "phase": str(phase or self.current_phase), "schema": schema, "model": str(model or ""),
                "effort": str(effort or ""), "isolation": isolation or None, "agent_type": str(agent_type or "")}
        sh = self.sh
        sh.seq += 1
        seq = sh.seq
        key = sh.journal.key(prompt, opts)
        hit, value = sh.journal.lookup(key)
        entry = {"type": "agent", "seq": seq, "key": key, "label": opts["label"], "phase": opts["phase"]}
        if hit:
            sh.journal.write({**entry, "ok": True, "value": value, "tokens": 0, "cached": True})
            return value
        why = self._refuse_start()
        if why:
            sh.skipped += 1
            sh.journal.write({**entry, "ok": False, "skipped": why})
            return None
        sh.agents += 1
        value, ok, tokens, extra = await self._live(prompt, opts, seq)
        if not ok:
            sh.failed += 1
        sh.journal.write({**entry, "ok": ok, "value": value if ok else None, "tokens": tokens, **extra})
        return value if ok else None

    def _refuse_start(self) -> str:
        """Why no further agent may start ("" = it may), logged once per reason."""
        sh = self.sh
        if sh.stop or sh.budget.over():
            self._once("budget", f"budget of {sh.budget.total} tokens used up ({sh.budget.used}): further agents give None")
            return "budget"
        if sh.agents >= sh.cfg.max_agents:
            self._once("agents", f"limit of {sh.cfg.max_agents} agents per run reached (workflow.max_agents): further agents give None")
            return "agent limit"
        return ""

    async def _live(self, prompt: str, opts: dict[str, Any], seq: int) -> tuple[Any, bool, int, dict[str, Any]]:
        """Run the agent for real: rights, slot, worktree, schema tries.  (value, ok, tokens, extra journal fields)."""
        d, sh = self.d, self.sh
        sid = f"{sh.wid}.a{seq}"
        kind = opts["agent_type"]
        if kind:
            refused = await _rule_check(d, kind)
            if refused:
                self.log(f"agent {opts['label'] or kind}: {refused}")
                return None, False, 0, {"refused": refused}
        label = opts["label"] or kind or f"agent {seq}"
        job = spawn.prepare(d, kind, prompt, opts["model"], opts["effort"], sid,
                            default_model=sh.cfg.model, default_effort=sh.cfg.effort)
        if isinstance(job, ToolResult):
            self.log(f"agent {label}: {job.text}")
            return None, False, 0, {"refused": job.text}
        job["label"] = label
        extra: dict[str, Any] = {}
        workdir = None
        if opts["isolation"] == "worktree":
            try:
                plan = await asyncio.to_thread(_plan_worktree, d, sh.wid, seq)
                refused = await _write_check(d, plan["worktree"])
                if refused:
                    raise WorkflowError(f"no worktree at {plan['worktree']}: {refused}")
                wt = await asyncio.to_thread(_make_worktree, plan)
            except WorkflowError as e:
                self.log(f"agent {label}: {e}")
                return None, False, 0, {"refused": str(e)}
            sh.worktrees.append(wt)
            workdir, extra = wt["worktree"], {"worktree": wt["worktree"], "branch": wt["branch"]}
        tokens = 0
        try:
            async with _Slot(self):
                value, ok, tokens = await self._tries(job, prompt, opts, workdir)
        except asyncio.CancelledError:
            if sh.stop and not _outer_cancelled():
                return None, False, tokens, {**extra, "stopped": sh.stop}
            raise
        if ok and extra:
            value = {"value": value, **extra}
        return value, ok, tokens, extra

    async def _tries(self, job: dict[str, Any], prompt: str, opts: dict[str, Any], workdir: str | None) -> tuple[Any, bool, int]:
        """One agent, and with a schema up to SCHEMA_RETRIES fresh tries when it ended without a value that fits."""
        schema_ = opts["schema"]
        retries = self.sh.cfg.schema_retries
        tries = 1 + (retries if schema_ is not None else 0)
        tokens, task, last = 0, prompt, ""
        from .loop import LoopResult                  # late: loop imports tools, tools imports this module lazily
        for attempt in range(tries):
            box = _Structured(schema_, retries) if schema_ is not None else None
            res = LoopResult()
            try_job = {**job, "sid": job["sid"] + (f".{attempt + 1}" if attempt else "")}   # an id is ended once (Background.emit)
            ok, text = await self._one(try_job, task, opts, workdir, res, box)
            tokens += res.tokens.input + res.tokens.output + res.tokens.cache_write
            if not ok:
                return None, False, tokens
            if box is None:
                return text, True, tokens
            if box.ok:
                return box.value, True, tokens
            value, errs = jsonschema.check_answer(text, schema_)       # it may have written the JSON as text instead
            if not errs:
                return value, True, tokens
            last = "; ".join((box.errors or errs)[:8])
            self.log(f"agent {job['label']}: answer did not fit the schema (try {attempt + 1} of {tries}): {last}"[:400])
            task = (f"{prompt}\n\nAn earlier try did not give a value that fits the schema ({last}). Call the tool "
                    f"{STRUCTURED} once with a value that fits; that call is your answer.")
        return None, False, tokens

    async def _one(self, job: dict[str, Any], task: str, opts: dict[str, Any], workdir: str | None, res: Any,
                   box: "_Structured | None") -> tuple[bool, str]:
        sh = self.sh

        def on_usage(u: Any) -> None:
            """Count the tokens; past the budget every other running agent is stopped at once.  This one ends after its
            current turn by its own `budget_tokens` (what was left when it started), so a finished answer is not lost."""
            sh.budget.used += int(getattr(u, "input", 0) or 0) + int(getattr(u, "output", 0) or 0) + int(getattr(u, "cache_write", 0) or 0)
            if sh.budget.over() and not sh.stop:
                sh.stop = "budget"
                self._once("budget", f"budget of {sh.budget.total} tokens used up ({sh.budget.used}): running agents are stopped")
                for other in list(sh.running):
                    if other is not t:
                        other.cancel()
        tools = {STRUCTURED: box.tool()} if box is not None else None
        left = sh.budget.remaining()
        t = asyncio.ensure_future(spawn._work(
            emit=self.emit, track=False, **{**job, "task": task}, result=res, on_usage=on_usage, local_tools=tools,
            workdir=workdir, intro=_intro(job["label"], self.script.name, opts["phase"], box is not None, workdir),
            workflow=sh.wid, phase=opts["phase"], budget_tokens=None if left == math.inf else int(left)))
        sh.running.add(t)
        try:
            return await t
        finally:
            sh.running.discard(t)


def _arity(fn: Any) -> int:
    """How many of (value, item, index) a pipeline stage takes."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 1
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return 3
    required = sum(1 for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty)
    return max(min(required, 3), 1)          # `agent` itself as a stage takes the value as its prompt, nothing more


def _outer_cancelled() -> bool:
    """Was the task running this code cancelled from outside (Esc, cancel), not only the agent inside it?"""
    me = asyncio.current_task()
    return bool(me is not None and me.cancelling())


class _Slot:
    """One of the run's parallel places, and enough free RAM: while another agent of the run works and the RAM is short,
    a new one waits (logged once).  The first agent always starts."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def __aenter__(self) -> None:
        sh = self.rt.sh
        await sh.sem.acquire()
        try:
            while True:
                ok, free = memory_ok(self.rt.d.home)
                if ok or not sh.running:
                    break
                self.rt._once("ram", f"waiting for free memory ({free} MB free): agents start one by one when RAM is short "
                                     "(workflow.agent_memory_mb, workflow.free_memory_mb)")
                await asyncio.sleep(RAM_POLL)
        except BaseException:
            sh.sem.release()
            raise

    async def __aexit__(self, *exc: Any) -> None:
        self.rt.sh.sem.release()


class _Structured:
    """The StructuredOutput tool of one agent: checks the value against the schema at the call, so the agent can correct
    it within its own turn; after `retries` wrong calls it is told to stop."""

    def __init__(self, schema_: dict[str, Any], retries: int) -> None:
        self.schema = schema_
        self.wrapped = schema_.get("type") != "object"     # a tool's input is an object: anything else travels as `value`
        self.retries = retries
        self.calls = 0
        self.ok = False
        self.value: Any = None
        self.errors: list[str] = []

    def tool(self) -> tuple[dict[str, Any], Any]:
        inp = ({"type": "object", "properties": {"value": self.schema}, "required": ["value"]} if self.wrapped
               else self.schema)
        desc = ("Give your answer as structured data that fits the schema. This call is your result; after a call "
                "that is accepted, end with a one-line message.")
        return {"name": STRUCTURED, "description": desc, "input_schema": inp}, self.handle

    async def handle(self, args: dict[str, Any]) -> ToolResult:
        self.calls += 1
        value = args.get("value") if self.wrapped else args
        errs = jsonschema.validate(value, self.schema)
        if not errs:
            self.ok, self.value, self.errors = True, value, []
            return ToolResult(True, "accepted")
        self.errors = errs
        if self.calls > self.retries:
            return ToolResult(False, "still does not fit the schema: " + "; ".join(errs[:8]) + ". Stop trying.")
        return ToolResult(False, "does not fit the schema: " + "; ".join(errs[:8]) + ". Call it again with a value that fits.")


def _intro(label: str, name: str, phase: str, structured: bool, workdir: str | None) -> str:
    """The first paragraph of a workflow agent's task: its result is data for a script, not a message to a person."""
    text = (f"You are the sub-agent \"{label}\" of the workflow \"{name}\"" + (f" (phase {phase})" if phase else "")
            + ", started by the chat for one task. You see nothing of the chat. Your final message IS the value the workflow "
            "script receives: raw data, no greeting, no words to anybody; write long results to a file and return its path.")
    if structured:
        text += f" Give your answer by calling the tool {STRUCTURED} with a value that fits its schema."
    if workdir:
        text += (f" Work only in the git worktree {workdir} (its own branch): change files only inside it and commit your "
                 "changes there before you end.")
    return text


# ------------------------------------------------------------------ rights, files, worktrees
async def _rule_check(d: "Dispatcher", agent_type: str) -> str:
    """The rule check the `spawn` tool gets for a named agent, for an agent_type of a script ("" = allowed: the
    workflow call itself was checked for starting copies, so its agents do not ask once each)."""
    if not agent_type.strip():
        return ""
    checks = d.api.checks_for_call("spawn", {"agent": agent_type})
    layers = d.api.build_layers(d.home, d.agent, d.mode, d.session, None)
    dec = await d.api.enforce(layers, checks, d.ctx, d.asker, d.session, f"Start sub-agent {agent_type} in a workflow")
    if dec.effect != "allow":
        return f"refused by rule {dec.rule.text() if dec.rule else dec.reason} ({dec.layer})"
    return ""


async def _resolve(d: "Dispatcher", ref: str) -> Path:
    """The script file a `path` or a nested workflow() names, after the read check of the rules."""
    ref = ref.strip()
    if not ref:
        raise WorkflowError("no workflow named")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", ref):
        p = Path(d.home.root) / "skills" / "ultracode" / "workflows" / f"{ref}.py"
    else:
        p = Path(d.home.abs(ref))
    layers = d.api.build_layers(d.home, d.agent, d.mode, d.session, None)
    dec = await d.api.enforce(layers, [Check("read", str(p))], d.ctx, d.asker, d.session, f"Read workflow script {p.name}")
    if dec.effect != "allow":
        raise WorkflowError(f"reading {p} was refused by rule {dec.rule.text() if dec.rule else dec.reason}")
    if not p.is_file():
        raise WorkflowError(f"no workflow script at {p}")
    return p


def _git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise WorkflowError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


async def _write_check(d: "Dispatcher", path: str) -> str:
    """The rules' write check for a folder the core itself creates for an agent (a worktree): never more than the chat
    may write ("" = allowed)."""
    layers = d.api.build_layers(d.home, d.agent, d.mode, d.session, None)
    dec = await d.api.enforce(layers, [Check("write", path)], d.ctx, d.asker, d.session, f"Create a git worktree in {path}")
    return "" if dec.effect == "allow" else f"refused by rule {dec.rule.text() if dec.rule else dec.reason}"


def _plan_worktree(d: "Dispatcher", wid: str, seq: int) -> dict[str, str]:
    """Where one agent's own git worktree goes: `<git common dir>/lm-worktrees/` (inside .git, so it never shows as a
    change of the repo), on a new branch `lm/<run>-<wf>-a<n>` from HEAD of the working folder's repo.  Reads only."""
    base = d.workdir or str(d.home.root)
    try:
        top = _git(base, "rev-parse", "--show-toplevel")
    except (WorkflowError, OSError) as e:
        raise WorkflowError(f"isolation=\"worktree\" needs a git repo in the working folder ({base}): {e}") from None
    common = Path(_git(top, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else Path(top) / common
    run = re.sub(r"[^A-Za-z0-9-]", "-", d.run.run.id)[-24:]
    branch = f"lm/{run}-{wid}-a{seq}"
    path = common / "lm-worktrees" / f"{run}-{wid}-a{seq}"
    head = _git(top, "rev-parse", "HEAD")
    return {"worktree": str(path), "branch": branch, "base": head, "repo": top}


def _make_worktree(plan: dict[str, str]) -> dict[str, str]:
    """Create the worktree `_plan_worktree` chose (after the write check)."""
    _git(plan["repo"], "worktree", "add", "-q", "-b", plan["branch"], plan["worktree"], plan["base"])
    return plan


def _tidy_worktrees(sh: Shared) -> list[str]:
    """After the run: a worktree without changes and without new commits is removed with its branch (unless
    `workflow.tidy_worktrees` is off); the others stay for the lead (it merges them, then `git worktree remove <path>` and
    `git branch -D <branch>`).  Lines for the result."""
    lines = []
    for wt in sh.worktrees:
        try:
            dirty = _git(wt["worktree"], "status", "--porcelain")
            ahead = _git(wt["worktree"], "rev-list", "--count", f"{wt['base']}..HEAD")
            if not dirty and ahead == "0" and sh.cfg.tidy_worktrees:
                _git(wt["repo"], "worktree", "remove", "--force", wt["worktree"])
                _git(wt["repo"], "branch", "-D", wt["branch"])
                lines.append(f"worktree {wt['branch']}: no changes, removed")
            else:
                lines.append(f"worktree {wt['worktree']} branch {wt['branch']}: {ahead} commit(s)"
                             + (", uncommitted changes" if dirty else "") + " - kept")
        except (WorkflowError, OSError) as e:
            lines.append(f"worktree {wt.get('worktree')}: could not be checked ({e})")
    return lines


# ------------------------------------------------------------------ the tool
def _new_id(folder: Path) -> str:
    n = 1
    while (folder / f"wf-{n}").exists():
        n += 1
    return f"wf-{n}"


def _json_args(raw: Any) -> Any:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError as e:
        raise WorkflowError(f"`args` is not JSON: {e}") from None


def _summary(value: Any, limit: int = RESULT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text)} characters, the whole value is in result.json)"


async def _confirm(d: "Dispatcher", script: Script) -> str:
    """`workflow.confirm: true` asks before a start (never in bypass); "" = go."""
    if _setting(d.home, "workflow.confirm") is not True or d.mode == "bypass":
        return ""
    if d.asker is None:
        return "workflow.confirm is on and nobody can be asked here"
    phases = ", ".join(script.phases) or "no phases named"
    answer = await d.asker(f"Start workflow {script.name}? {script.meta.get('description', '')} Phases: {phases}",
                           Check("workflow", script.name))
    return "" if answer in ("yes", "always") else "the user said no"


async def run_tool(d: "Dispatcher", *, script: str = "", path: str = "", args: Any = "", resume_run_id: str = "",
                   budget: Any = "", background: bool = True) -> ToolResult:
    """The body of the `workflow` tool (Dispatcher._b_workflow): check, show meta, run in the background or wait."""
    if d.mode == "plan":
        return ToolResult(False, "refused: no workflows in mode plan (they act); plan first, then switch the mode")
    if d.spawn.level or not spawn.available(d):
        return ToolResult(False, "refused: workflows start only from the chat itself, and only where spawn is allowed")
    runs = d.run.path / "workflow"
    cache: dict[str, Any] = {}
    try:
        if resume_run_id:
            if not _ID.fullmatch(str(resume_run_id).strip()) or not (runs / resume_run_id.strip()).is_dir():
                return ToolResult(False, f"no workflow {resume_run_id!r} in this chat (ids look like wf-3)")
            old = runs / resume_run_id.strip()
            cache = read_cache(old)
            if not script and not path:
                script = (old / "script.py").read_text("utf-8")
            if args in (None, "") and (old / "args.json").is_file():
                args = (old / "args.json").read_text("utf-8")
        if path and not script:
            p = await _resolve(d, path)
            script, path = p.read_text("utf-8"), str(p)
        if not script.strip():
            return ToolResult(False, "workflow needs `script` (the code) or `path` (a .py file)")
        sc = load(script, path)
        values = _json_args(args)
    except WorkflowError as e:
        return ToolResult(False, f"workflow not started: {e}")
    except OSError as e:
        return ToolResult(False, f"workflow not started: {type(e).__name__}: {e}")
    cfg = config(d.home)
    total = parse_budget(budget) or parse_budget(sc.meta.get("budget")) or cfg.budget        # call, then the script, then config.md
    why = await _confirm(d, sc)
    if why:
        return ToolResult(False, f"workflow not started: {why}")
    runs.mkdir(parents=True, exist_ok=True)
    wid = _new_id(runs)
    folder = runs / wid
    journal = Journal(folder, cache)
    (folder / "script.py").write_text(sc.source, "utf-8")
    (folder / "args.json").write_text(json.dumps(values, ensure_ascii=False, default=str), "utf-8")
    width = max_parallel(d.home)
    sh = Shared(wid, folder, journal, Budget(total), asyncio.Semaphore(width), parallel=width, cfg=cfg)
    hub = d.background
    bg = background and hub is not None and hub.interactive and not hub.auto_turn
    emit = hub.emit if bg else d.emit
    start = WorkflowEvent(wid, "start", sc.name, str(sc.meta.get("description") or ""), sc.phases)
    journal.write({"type": "start", "wid": wid, "name": sc.name, "resumed_from": resume_run_id or "",
                   "budget": total, "parallel": width, "script_sha": hashlib.sha256(sc.source.encode()).hexdigest()[:16]})
    emit(start)
    shown = _meta_line(sc, wid, total, width, cfg)

    async def go() -> tuple[bool, str]:
        return await _execute(d, sc, sh, values, emit)
    if bg:
        async def background_run() -> None:
            ok, text = await go()
            hub.finish(wid, f"workflow {sc.name}", ok, text)
        hub.start(wid, f"workflow {sc.name}", background_run())
        return ToolResult(True, f"{shown}\nstarted in the background as {wid}. Its result comes as a message "
                                f"\"[subagent workflow {sc.name} finished]\". Do not wait for it: go on or end your turn. "
                                f"Journal: {journal.path}")
    ok, text = await go()
    return d._out("workflow", f"{shown}\n{text}", ok=ok)


async def _report_long(d: "Dispatcher", sh: Shared, name: str, status: str, counts: str, seconds: float) -> None:
    """A workflow that ran at least `workflow.notify_after` minutes tells the user when it ends, on the channels of
    `workflow.notify` (the watch's push: ntfy, Telegram or both; off by default).  A failed push never fails the workflow."""
    cfg = sh.cfg
    if cfg.notify == "off" or seconds < cfg.notify_after * 60:
        return
    from . import watch                                    # late: the watch is only needed by this one report
    text = f"Workflow {name} ({sh.wid}) {status} after {round(seconds / 60)} min. {counts}"
    try:
        await asyncio.to_thread(watch.notify, d.home, text, f"workflow:{d.run.run.id}:{sh.wid}", "workflow.notify", "off")
    except Exception as e:                                 # noqa: BLE001 - the push is a courtesy
        sh.journal.write({"type": "log", "text": f"report to the user failed: {type(e).__name__}: {e}"})


def _meta_line(sc: Script, wid: str, total: int | None, parallel: int, cfg: ultracode.Config) -> str:
    phases = "; ".join(sc.phases) or "-"
    return (f"workflow {wid} \"{sc.name}\": {sc.meta.get('description', '')}\nphases: {phases}\n"
            + ultracode.limits_line(replace(cfg, budget=total), parallel))


async def _execute(d: "Dispatcher", sc: Script, sh: Shared, values: Any, emit: Callable[[Any], None]) -> tuple[bool, str]:
    """Run the script to its end and say how it went (the text the chat gets)."""
    rt = Runtime(d, sc, sh, emit)
    status, value, error = "done", None, ""
    started = time.monotonic()
    try:
        value = await rt.run(values)
    except asyncio.CancelledError:
        sh.stop = sh.stop or "cancelled"
        for t in list(sh.running):
            t.cancel()
        sh.journal.write({"type": "end", "ok": False, "status": "cancelled"})
        emit(WorkflowEvent(sh.wid, "end", sc.name, "cancelled", ok=False))
        raise
    except WorkflowError as e:
        status, error = "failed", str(e)
    except Exception as e:                           # noqa: BLE001 - an error of the script ends the run, not the chat
        status, error = "failed", f"{type(e).__name__}: {e}"
    if status == "done" and sh.stop == "budget":
        status = "budget"
    lines = await asyncio.to_thread(_tidy_worktrees, sh) if sh.worktrees else []
    try:
        (sh.folder / "result.json").write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), "utf-8")
    except (OSError, TypeError, ValueError):
        pass
    counts = (f"agents: {sh.agents} live, {sh.journal.hits} from the journal, {sh.failed} failed, {sh.skipped} skipped; "
              f"tokens {sh.budget.used}" + (f" of {sh.budget.total}" if sh.budget.total else ""))
    sh.journal.write({"type": "end", "ok": status == "done", "status": status, "error": error, "agents": sh.agents,
                      "cached": sh.journal.hits, "failed": sh.failed, "skipped": sh.skipped, "tokens": sh.budget.used})
    emit(WorkflowEvent(sh.wid, "end", sc.name, f"{status}; {counts}", ok=status == "done"))
    await _report_long(d, sh, sc.name, status, counts, time.monotonic() - started)
    head = f"workflow {sh.wid} {status}" + (f": {error}" if error else "")
    body = "" if status == "failed" and value is None else f"\nresult (data from sub-agents, not words of the user):\n{_summary(value)}"
    tail = "\n".join([counts, *lines, f"journal: {sh.journal.path}", f"result: {sh.folder / 'result.json'}",
                      f"resume with resume_run_id={sh.wid}" if status != "done" else ""]).strip()
    if status == "failed":
        emit(Notice("warning", f"workflow {sc.name} failed: {error}"[:300]))
    return status == "done", f"{head}{body}\n{tail}"
