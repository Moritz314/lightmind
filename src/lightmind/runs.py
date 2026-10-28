"""Run directories: task.md, meta.json, session.md, events.jsonl, log.md, tool-out/, cost, plus the
trusted record of a chat session in the `state` location.

Location from home.path('runs'); prices from the provider files (home.prices()).
Also what runs that start runs need: `write_result` / `run_result` / `wait_runs` (a run's outcome as a
dict, also for a process that died), the parallel-slot counter (`claim_slot` / `release_slot`), `pid_alive`
and `duration`, the session lock (`acquire_lock` / `release_lock` / `lock_holder`: one writer per chat) and the session names
(`claim_name` / `find_name` / `resolve_session`: fixed sessions), the archive (`archive` / `unarchive` / `is_archived`: a marker file in the run folder).
May import: model, files.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
import secrets as _rand
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from . import results
from .files import Home, write_atomic as _atomic
from .model import Event, Run, SessionState, Task, Tokens, now_iso

log = logging.getLogger("lightmind.runs")
END_RE = re.compile(r"<!--\s*lm:end status=(\w+)\s*-->")


def new_run_id(agent: str, now: datetime | None = None) -> str:
    """YYYYMMDD-HHMMSS-<agent>-<4 hex>."""
    return f"{(now or datetime.now()):%Y%m%d-%H%M%S}-{agent or 'chat'}-{_rand.token_hex(2)}"


def _fm(meta: dict[str, Any], body: str = "") -> str:
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body


NAME_FIELDS = ("title", "description", "title_manual", "description_manual", "names_rev")
NAME_MAX = 200


def _set_name_fields(run: Run, field: str, text: str) -> str:
    """Set title or description on `run` (by hand: locked when not empty); returns the cleaned text."""
    if field not in ("title", "description"):
        raise ValueError("field is title or description")
    text = " ".join("".join(c for c in str(text or "") if c.isprintable() or c.isspace()).split())[:NAME_MAX]
    setattr(run, field, text)
    setattr(run, field + "_manual", bool(text))
    run.names_rev += 1
    return text


class RunDir:
    run: Run
    path: Path

    def __init__(self, home: Home, run: Run, task: Task | None = None, write: bool = True) -> None:
        """Creates the folder, writes task.md and the initial meta.json.  `write=False` (a chat that is only read,
        see Session `readonly`) touches nothing on disk."""
        self.home, self.run = home, run
        self._write = write
        self.path = home.path("runs") / run.id
        if write:
            for sub in ("", "tool-out"):                 # `out` comes with the first result (`results_path`), not for every chat
                (self.path / sub).mkdir(parents=True, exist_ok=True)
        run.start = run.start or now_iso()
        if task and write:
            meta = {k: v for k, v in (("goal", task.goal), ("limits", task.limits),
                                      ("sources", task.sources), ("check", task.check),
                                      ("return", task.return_), ("parent", task.parent)) if v}
            _atomic(self.path / "task.md", _fm(meta))
        self._prices: dict | None = None
        self._meta_lock = threading.Lock()       # the titler thread and the chat both rewrite meta.json
        if write:
            self._meta()

    def park(self, exit_reason: str = "") -> None:
        """Status `paused` without ending the run (no end marker): a chat that goes on in another process."""
        self.run.status, self.run.exit_reason = "paused", exit_reason          # type: ignore[assignment]
        self._meta()

    def _adopt_names(self) -> None:
        """Take title and description from meta.json when another process (the resume menu, `lm`) changed them since
        (`names_rev`): this process's own write would otherwise bring the old ones back.  Caller holds `_meta_lock`."""
        try:
            disk = json.loads((self.path / "meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(disk, dict) and int(disk.get("names_rev") or 0) > self.run.names_rev:
            for k in NAME_FIELDS:
                setattr(self.run, k, disk.get(k, getattr(self.run, k)))

    def _meta(self) -> None:
        with self._meta_lock:
            self._adopt_names()
            self._sync_results(create=False)
            _atomic(self.path / "meta.json", json.dumps(self.run.to_dict(), indent=1))

    def set_name(self, field: str, text: str) -> str:
        """Title (`field` = "title") or description by hand: text = set and locked against the automatic naming, empty =
        automatic again.  The one place that writes them: `/rename`, the `todo` tool and the resume menu all end here."""
        with self._meta_lock:
            self._adopt_names()
            text = _set_name_fields(self.run, field, text)
            self._sync_results(create=False)
            _atomic(self.path / "meta.json", json.dumps(self.run.to_dict(), indent=1))
        return text

    # ---------------------------------------------------------------- result folder
    def results_path(self) -> Path:
        """The path for `$LM_RESULTS` and the place to write results: `<run>/out`, a link to the result folder
        `runs/ergebnisse/<date> <title>` that is made now when it does not exist yet (lazy: a chat that never asks for it
        has none).  A run that has a real `out` folder (older runs) keeps it.  Without a link the folder itself."""
        out = self.path / "out"
        if not self._write:
            return out
        with self._meta_lock:
            self._adopt_names()
            before = self.run.results_dir
            self._sync_results(create=True)
            if self.run.results_dir != before:
                _atomic(self.path / "meta.json", json.dumps(self.run.to_dict(), indent=1))
            if self.run.results_dir and not results.link_points_to(out, self._results_folder()):
                return self._results_folder()
        return out

    def result_file(self, path: Path) -> Path:
        """`path` (a file below `out`) as it lies in the real result folder, so the path told to a person names the
        folder with the title, not the link.  Unchanged when there is no such folder."""
        if self.run.results_dir and path.parent == self.path / "out":
            return self._results_folder() / path.name
        return path

    def _results_folder(self) -> Path:
        d = Path(self.run.results_dir)
        return d if d.is_absolute() else self.home.root / d

    def _sync_results(self, create: bool) -> None:
        """Bring the result folder in line with the title: make it (only when `create`), rename it when the wanted name
        changed and it is still empty (a folder with files keeps its name, sent file links stay valid), hang the `out`
        link on it.  A rename that fails (open on Windows) keeps the old name.  Caller holds
        `_meta_lock`; sets `run.results_dir`, the caller writes meta.json."""
        if not self._write:
            return
        out = self.path / "out"
        if not self.run.results_dir and out.exists() and not results.is_link(out):
            return                                       # an older run: its real `out` stays as it is
        root = self.path.parent / results.FOLDER
        want = results.base_name(self.run.start, self.run.title, self.run.agent, self.run.id)
        folder = self._results_folder() if self.run.results_dir else None
        try:
            if folder is None or not folder.is_dir():
                if not create:
                    return
                folder = results.claim_folder(root, want)
            elif results.strip_counter(folder.name) != want and not results.has_content(folder):
                folder = self._rename_results(folder, root, want)
            self.run.results_dir = self._rel(folder)
            self._link_results(out, folder)
        except OSError as e:
            log.warning("result folder of %s: %s", self.run.id, e)

    def _rename_results(self, folder: Path, root: Path, want: str) -> Path:
        """Rename our folder to the wanted name (with counter when taken); the old folder stays when it does not work."""
        new = root / results.free_name(root, want, own=folder.name)
        if new == folder:
            return folder
        try:
            folder.rename(new)
        except OSError as e:
            log.warning("result folder %s not renamed to %s: %s", folder.name, new.name, e)
            return folder
        return new

    def _rel(self, folder: Path) -> str:
        try:
            return folder.relative_to(self.home.root).as_posix()
        except ValueError:
            return folder.as_posix()

    def _link_results(self, out: Path, folder: Path) -> None:
        """`out` -> folder (a rename leaves the old link dangling, so it is made again); `ergebnis.txt` when no link works."""
        if results.link_points_to(out, folder):
            return
        if results.is_link(out):
            results.drop_link(out)
        elif out.is_dir():
            try:
                out.rmdir()                              # an empty real `out`; a full one stays and the note says where
            except OSError:
                pass
        if out.exists() or not results.make_link(out, folder):
            results.write_note(self.path, folder)

    def _prune_results(self) -> None:
        """The run ends: a result folder that stayed empty goes away with its link (no folder for every short chat)."""
        if not self._write or not self.run.results_dir:
            return
        folder = self._results_folder()
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                if results.is_link(self.path / "out"):
                    results.drop_link(self.path / "out")
                (self.path / results.NOTE_FILE).unlink(missing_ok=True)
                folder.rmdir()
                self.run.results_dir = ""
        except OSError as e:
            log.warning("empty result folder %s not removed: %s", folder, e)

    def event(self, kind: str, data: dict[str, Any] | Event) -> None:
        """Append one line to events.jsonl (ts, kind, data).  Secret values must already
        be masked by the caller."""
        if is_dataclass(data):
            data = {"type": type(data).__name__, **{k: v for k, v in asdict(data).items() if v != "" or k not in ("parent", "agent")}}   # parent/agent only when set
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_iso(), "kind": kind, "data": data},
                               ensure_ascii=False, default=str) + "\n")
            if kind == "message":                    # a message must survive a frozen machine and a hard reset
                f.flush()
                os.fsync(f.fileno())

    def write_session(self, session: SessionState) -> None:
        meta = {"mode": session.mode, "account": session.account, "model": session.model,
                "tools_added": [{r.kind: r.name} for r in session.tools_added],
                "tools_removed": session.tools_removed,
                "rules": [{r.effect: r.text()} for r in session.rules],
                "data_added": session.data_added}
        _atomic(self.path / "session.md", _fm(meta))

    def write_trusted(self, data: dict[str, Any]) -> None:
        """The part of a chat session that resume must trust (agent, mode, grants ...), written to the
        `state` location: the run folder may be edited by agents, the engine folder may not."""
        p = trusted_file(self.home, self.run.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic(p, json.dumps(data, indent=1, ensure_ascii=False))

    def write_result(self, data: dict[str, Any]) -> None:
        """result.json: what `lm wait` reports (run_id, status, exit_reason, text, cost, result).  Written before
        `finish`, so a run that is over always has it."""
        _atomic(self.path / "result.json", json.dumps(data, indent=1, ensure_ascii=False, default=str))

    def tool_out(self, n: int, tool: str, text: str) -> Path:
        """Write full tool output to tool-out/<n>-<tool>.txt, return the path."""
        p = self.path / "tool-out" / f"{n}-{tool}.txt"
        p.write_text(text, "utf-8")
        return p

    def _cost(self) -> None:
        if self._prices is None:
            self._prices = load_prices(self.home)
        self.run.cost_usd, est = compute_cost(self._prices, self.run.model, self.run.tokens)
        sub = False
        try:   # subscription runs use the same formula but are an estimate
            sub = self.home.load_providers().accounts[self.run.account].type.endswith("subscription")
        except (KeyError, OSError, FileNotFoundError):
            pass
        self.run.cost_est = est or sub

    def add_usage(self, tokens: Tokens) -> None:
        t = self.run.tokens
        t.input += tokens.input
        t.output += tokens.output
        t.cache_read += tokens.cache_read
        t.cache_write += tokens.cache_write
        self._cost()
        self._meta()

    def finish(self, status: str, exit_reason: str = "") -> Run:
        """Set end, status, cost (prices file) and rewrite meta.json; make sure log.md ends
        with the end marker."""
        self.run.end, self.run.status, self.run.exit_reason = now_iso(), status, exit_reason  # type: ignore[assignment]
        with self._meta_lock:
            self._prune_results()
        self._cost()
        self._meta()
        log = self.path / "log.md"
        text = log.read_text("utf-8") if log.exists() else ""
        if not END_RE.search(text):
            log.write_text((text.rstrip("\n") + "\n\n" if text else "")
                           + f"<!-- lm:end status={status} -->\n", "utf-8")
        return self.run


def trusted_file(home: Home, run_id: str) -> Path:
    """<state>/sessions/<run id>.json: see `RunDir.write_trusted`."""
    return home.path("state") / "sessions" / f"{run_id}.json"


def read_trusted(home: Home, run_id: str) -> dict[str, Any] | None:
    """The trusted record of a chat session, or None when it is missing or unreadable."""
    try:
        data = json.loads(trusted_file(home, run_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_prices(home: Home) -> dict[str, tuple[float, float, float, float]]:
    """model -> (input, output, cache_write, cache_read) USD per million tokens, from the `models:`
    of the provider files (`Home.prices`)."""
    return home.prices()


def compute_cost(prices: dict, model: str, tokens: Tokens) -> tuple[float, bool]:
    """(usd, est).  Unknown model -> (0.0, True).  A price key also matches as a substring
    of the full model name (longest key first)."""
    key = model if model in prices else next(
        (k for k in sorted(prices, key=len, reverse=True) if k.lower() in model.lower()), None)
    if key is None:
        return 0.0, True
    i, o, cw, cr = prices[key]
    t = tokens
    return (t.input * i + t.output * o + t.cache_write * cw + t.cache_read * cr) / 1e6, False


def _meta_files(home: Home) -> list[Path]:
    d = home.path("runs")
    if not d.is_dir():
        return []
    return sorted((p / "meta.json" for p in d.iterdir() if (p / "meta.json").is_file()),
                  key=lambda p: p.parent.name, reverse=True)


def _iter_runs(home: Home):
    """Runs newest first, each meta.json read only when the caller asks for the next one."""
    for f in _meta_files(home):
        try:
            yield Run.from_dict(json.loads(f.read_text("utf-8")))
        except (ValueError, TypeError, OSError):
            continue


def list_runs(home: Home, limit: int = 20) -> list[Run]:
    out = list(itertools.islice(_iter_runs(home), limit))
    for r in out:
        if r.status == "running" and reap_dead(home, r.id):          # a dead run does not stay `running` in the list
            r.status, r.exit_reason = "failed", "process died" if r.pid else "never started"
    return out


def show_run(home: Home, run_id: str) -> dict[str, Any]:
    """Meta, task, log tail, event count and tool-out files of one run (prefix match)."""
    hits = [f.parent for f in _meta_files(home) if f.parent.name.startswith(run_id)]
    if len(hits) != 1:
        raise FileNotFoundError(f"run {run_id!r}: {len(hits)} matches")
    d = hits[0]
    reap_dead(home, d.name)
    rd = lambda n: (d / n).read_text("utf-8") if (d / n).is_file() else ""
    ev = rd("events.jsonl")
    return {"run": json.loads(rd("meta.json")), "path": str(d), "task": rd("task.md"),
            "log": rd("log.md"), "events": len(ev.splitlines()),
            "tool_out": sorted(p.name for p in (d / "tool-out").glob("*"))}


def cost_summary(home: Home, since: str | None = None, by: str = "agent") -> dict[str, float]:
    """Sum of cost_usd per `by` (agent|model|account|project|server|status).  `since` =
    '24h' | '7d' | '30m' or an ISO timestamp."""
    cut = None
    if since:
        m = re.fullmatch(r"(\d+)([mhd])", since)
        cut = (datetime.now().astimezone() - timedelta(**{{"m": "minutes", "h": "hours",
               "d": "days"}[m.group(2)]: int(m.group(1))})) if m else datetime.fromisoformat(since)
        if cut.tzinfo is None:
            cut = cut.astimezone()
    out: dict[str, float] = {}
    for r in list_runs(home, limit=10**9):
        if cut and (not r.start or datetime.fromisoformat(r.start) < cut):
            continue
        k = str(getattr(r, by)) or "-"
        out[k] = round(out.get(k, 0.0) + r.cost_usd, 6)
    return out


# ------------------------------------------------------------ chat session list
@dataclass
class SessionSummary:
    """One row of the resume list."""
    id: str
    title: str                  # first words of the first user message
    agent: str
    project: str
    server: str
    account: str
    model: str
    mode: str
    status: str
    start: str
    last_active: str
    cost_usd: float
    cost_est: bool
    origin: str = ""            # "vscode" for a chat started from the VS Code extension
    description: str = ""       # topics of the chat (meta.json); "" = none
    parent: str = ""            # run id of the run that started it (LM_PARENT); "" = a person did
    depth: int = 0
    archived: bool = False      # `archived.json` is in the run folder (see `archive`)




def read_names(home: Home, run_id: str) -> tuple[str, str]:
    """`(title, description)` of a chat as its meta.json has them now ("" = none yet)."""
    try:
        d = json.loads((home.path("runs") / run_id / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return "", ""
    return str(d.get("title") or ""), str(d.get("description") or "")


def set_name_on_disk(home: Home, run_id: str, field: str, text: str) -> str:
    """Title or description of a chat this process does not hold: meta.json gets it with a higher `names_rev`, so the
    process that does hold it adopts it with its next write.  Returns the line to show."""
    f = home.path("runs") / run_id / "meta.json"
    meta = json.loads(f.read_text("utf-8"))
    r = Run.from_dict(meta)
    text = _set_name_fields(r, field, text)
    meta.update({k: getattr(r, k) for k in NAME_FIELDS})
    _atomic(f, json.dumps(meta, indent=1))
    return f"{field}: {text}" if text else f"{field}: automatic again (set with the next answer)"


def _head(path: Path, n: int) -> list[dict]:
    """The first n events of an events.jsonl (cheap: the file is not read to its end)."""
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if len(out) >= n:
                    break
    except OSError:
        pass
    return out


def list_sessions(home: Home, limit: int = 20, archived: bool | None = False) -> list[SessionSummary]:
    """Chat sessions among the runs, newest first.  A run is a session when its first event is
    a `session` event (one-shot runs have none).  `archived`: False = without the archive (the default, so no list
    shows an archived chat by accident), True = only the archive, None = all."""
    out: list[SessionSummary] = []
    for r in _iter_runs(home):        # newest first, stops reading once `limit` sessions are found
        marked = is_archived(home.path("runs") / r.id)
        if archived is not None and marked != archived:
            continue
        ev_file = home.path("runs") / r.id / "events.jsonl"
        head = _head(ev_file, 60)
        if not head or head[0].get("kind") != "session":
            continue
        title = next((e["data"].get("text", "") for e in head
                      if e.get("kind") == "message" and e["data"].get("role") == "user"), "")
        out.append(SessionSummary(
            id=r.id, title=" ".join((r.title or title).split())[:60], description=r.description, agent=r.agent, project=r.project,
            server=r.server, account=r.account, model=r.model, mode=r.mode, status=r.status,
            start=r.start, last_active=datetime.fromtimestamp(ev_file.stat().st_mtime).astimezone()
            .isoformat(timespec="seconds"), cost_usd=r.cost_usd, cost_est=r.cost_est, origin=r.origin, parent=r.parent, depth=r.depth,
            archived=marked))
        if len(out) >= limit:
            break
    return out


def list_session_groups(home: Home, limit: int = 20, names: dict[str, str] | None = None,
                        archived: bool | None = False) -> list[SessionSummary]:
    """About `limit` top-level lines of the resume list with everything below them: the limit counts groups, so a run that
    started fifty others does not push every other chat out.  Reads a pool of newest runs, then cuts by group.
    `archived` as in `list_sessions`; a run that another run started follows the archive state of its parent chain, so the
    children of an archived chat are hidden with it (and shown with it when `archived` is True)."""
    pool = list_sessions(home, limit=max(limit * 10, 300), archived=None)
    if archived is not None:
        by_id = {r.id: r for r in pool}

        def under_archive(r: SessionSummary) -> bool:
            seen: set[str] = set()
            while r.id not in seen:
                if r.archived:
                    return True
                seen.add(r.id)
                if r.parent not in by_id:
                    return False
                r = by_id[r.parent]
            return False
        pool = [r for r in pool if under_archive(r) == archived]
    keep: set[str] = set()
    tops = 0
    for g in group_sessions(pool, names, None):
        if g.depth == 0:
            tops += 1
            if tops > limit:
                break
        keep.add(g.id)
    return [r for r in pool if r.id in keep]


@dataclass
class SessionGroupRow:
    """One line of the grouped resume list: `session` is None for a header of a parent that is not in the list."""
    id: str
    session: SessionSummary | None
    label: str                  # the name shown for a header without a session
    depth: int                  # 0 = top level
    kids: int                   # sessions below it, all levels


def group_sessions(rows: list[SessionSummary], names: dict[str, str] | None = None,
                   expanded: set[str] | None = None) -> list[SessionGroupRow]:
    """The resume list as a tree: a session with `parent` sits under its parent, a session whose parent is not in `rows`
    under a header with the parent's name.  A group is shown closed (only its head) unless its id is in `expanded`; `None` = all open.
    Top level and children are ordered by the newest activity in their subtree."""
    names = names or {}
    by_id = {r.id: r for r in rows}
    kids: dict[str, list[str]] = {}
    top: list[str] = []
    for r in rows:
        p = r.parent if r.parent and r.parent != r.id else ""
        if p and p not in by_id:
            kids.setdefault(p, []).append(r.id)
            if p not in top:
                top.append(p)
        elif p:
            kids.setdefault(p, []).append(r.id)
        else:
            top.append(r.id)
    reached: set[str] = set()                   # a parent loop (a -> b -> a) has no top level: its first member becomes one
    def reach(i: str) -> None:
        if i not in reached:
            reached.add(i)
            for k in kids.get(i, []):
                reach(k)
    for t in top:
        reach(t)
    for r in rows:
        if r.id not in reached:
            top.append(r.id)
            reach(r.id)

    def stamp(i: str, stack: tuple[str, ...] = ()) -> str:
        own = (by_id[i].last_active or by_id[i].start) if i in by_id else ""
        return max([own] + [stamp(k, stack + (i,)) for k in kids.get(i, []) if k not in stack and k != i])

    def count(i: str, stack: tuple[str, ...] = ()) -> int:
        return sum(1 + count(k, stack + (i,)) for k in kids.get(i, []) if k not in stack and k != i)

    out: list[SessionGroupRow] = []
    def walk(i: str, depth: int, stack: tuple[str, ...]) -> None:
        out.append(SessionGroupRow(i, by_id.get(i), "" if i in by_id else (names.get(i) or i), depth, count(i, stack)))
        if (expanded is not None and i not in expanded) or i in stack:
            return
        for k in sorted(kids.get(i, []), key=stamp, reverse=True):
            if k not in stack and k != i:
                walk(k, depth + 1, stack + (i,))
    for t in sorted(top, key=stamp, reverse=True):
        walk(t, 0, ())
    return out


# ------------------------------------------------- the archive: a marker file, the run folder stays where it is
ARCHIVE_FILE = "archived.json"      # runs/<id>/archived.json = {"at": iso time, "by": who, "status": status before}


def is_archived(path: Path) -> bool:
    """Is the run folder `path` (`runs/<id>`) archived?  Archived chats are missing from the resume list and the session list."""
    return (path / ARCHIVE_FILE).is_file()


def archive(home: Home, run_id: str, by: str = "cli") -> str:
    """Archive a chat of this machine: its marker file is written and, when it still runs or waits, the run is closed on disk
    first (`request_stop`: a process that holds it is asked to let go).  `by` says who did it (`tui`, `phone`, `cli`).  The
    folder is not moved, so every path that points into it stays valid.  Returns the line to show; ValueError (with the
    reason) when there is no such chat, this process still holds it (close it first, then archive) or another one does not let go."""
    d = home.path("runs") / run_id
    try:
        meta = json.loads((d / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"no such chat: {run_id}") from None
    if is_archived(d):
        return "already archived"
    before = str(meta.get("status") or "")
    if lock_holder(home, run_id) == os.getpid():
        raise ValueError("this chat is open in this process: close it first")
    if before in ("running", "paused") or lock_holder(home, run_id):
        request_stop(home, run_id)
        holder = lock_holder(home, run_id)
        if holder:
            raise ValueError(f"still running in process {holder}")
    _atomic(d / ARCHIVE_FILE, json.dumps({"at": now_iso(), "by": by, "status": before}, indent=1))
    return "archived"


def unarchive(home: Home, run_id: str) -> str:
    """Take a chat out of the archive: the marker is removed, the chat is in the resume list again (its status is `done`, it can
    be continued).  Returns the line to show; ValueError when there is no such chat."""
    d = home.path("runs") / run_id
    if not (d / "meta.json").is_file():
        raise ValueError(f"no such chat: {run_id}")
    try:
        (d / ARCHIVE_FILE).unlink()
    except FileNotFoundError:
        return "not archived"
    return "restored"


# ------------------------------------------------- runs that start runs: outcome, wait, slots
STOP_FILE, STOP_TTL = "stop.request", 120.0


def take_stop(run_path: Path) -> bool:
    """Has the resume menu asked the process holding this chat to end it (a fresh `stop.request` in the run folder)?
    The file is removed; a stale one (older than STOP_TTL seconds) counts as none."""
    f = run_path / STOP_FILE
    try:
        fresh = time.time() - f.stat().st_mtime < STOP_TTL
        f.unlink()
    except OSError:
        return False
    return fresh


def request_stop(home: Home, run_id: str, wait: float = 4.0) -> str:
    """Resume menu, "end": end a chat of this machine.  Nobody holds it -> the run folder is closed on disk; held by
    another process -> that process is asked (`stop.request`) and has `wait` seconds to let go; this process -> refused.
    Returns the line to show."""
    holder = lock_holder(home, run_id)
    if holder == os.getpid():
        return "this chat is open in this window: leave it with Esc"
    if not holder:
        f = home.path("runs") / run_id / "meta.json"
        try:
            meta = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            return "no such chat"
        if meta.get("status") in ("done", "failed"):
            return "already ended"
        meta.update(status="done", end=now_iso(), exit_reason="ended from the resume menu")
        _atomic(f, json.dumps(meta, indent=1))
        log = f.parent / "log.md"
        text = log.read_text("utf-8") if log.exists() else ""
        if not END_RE.search(text):
            log.write_text((text.rstrip("\n") + "\n\n" if text else "") + "<!-- lm:end status=done -->\n", "utf-8")
        return "ended"
    _atomic(home.path("runs") / run_id / STOP_FILE, str(os.getpid()))
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not lock_holder(home, run_id):
            return "ended"
        time.sleep(0.2)
    return f"asked process {holder} to end it, no answer yet (a terminal window answers within a second)"


def pid_alive(pid: int) -> bool:
    """Is that process still running?  (Windows: an exit code of STILL_ACTIVE; POSIX: signal 0.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32                         # type: ignore[attr-defined]
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return bool(ok) and code.value == 259              # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_stamp(pid: int) -> str:
    """When that process began, as text (Windows: its creation time, Linux: its start time since boot); '' when it cannot
    be told.  Together with the pid it tells a process from another one that got the same number later."""
    if pid <= 0:
        return ""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32                         # type: ignore[attr-defined]
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        try:
            created, *rest = (wintypes.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(ctypes.c_void_p(h), ctypes.byref(created), *(ctypes.byref(x) for x in rest)):
                return ""
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[19].decode()        # field 22, after `pid (comm)`
    except (OSError, IndexError):
        return ""


def duration(value: Any, default: float) -> float:
    """'30m' | '90s' | '2h' | 45 -> seconds; `default` for anything else."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(value or ""))
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else default


STARTUP_GRACE = 120        # seconds a detached run may exist without a process before it counts as never started


def find_runs(home: Home, prefix: str) -> list[Path]:
    """The folders of the runs whose id starts with `prefix` (an empty prefix matches none)."""
    return [f.parent for f in _meta_files(home) if prefix and f.parent.name.startswith(prefix)]


def resolve_run(home: Home, run_id: str) -> Path:
    """The folder of a run (id or unique prefix); FileNotFoundError unless exactly one matches."""
    hits = [f.parent for f in _meta_files(home) if f.parent.name.startswith(run_id)]
    if len(hits) != 1:
        raise FileNotFoundError(f"run {run_id!r}: {len(hits)} matches")
    return hits[0]


DEAD_REASONS = ("process died", "never started")      # exit_reason of a run whose process is gone without an end (status failed)


def is_dead(status: str, reason: str) -> bool:
    """Was this run found dead (not ended by itself)?  Such a run is `failed` with one of DEAD_REASONS."""
    return status == "failed" and reason in DEAD_REASONS


def reap_dead(home: Home, run_id: str, now_ts: float | None = None) -> bool:
    """A run that says `running` while its process is gone (it was killed, the machine restarted, the service that started
    it was restarted) is closed here: status failed, exit_reason `process died` (`never started` when no process ever took
    it), the end marker in its log.  A run that left a result file keeps the status that file holds.  True when it changed."""
    d = resolve_run(home, run_id)
    try:
        meta = json.loads((d / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    r = Run.from_dict(meta)
    if r.status != "running":
        return False
    try:
        age = ((datetime.now().astimezone() if now_ts is None else datetime.fromtimestamp(now_ts).astimezone())
               - datetime.fromisoformat(r.start)).total_seconds() if r.start else 0
    except ValueError:
        age = 0
    holder = lock_holder(home, r.id)
    gone = (not holder and r.pid and not pid_alive(r.pid)) or (not r.pid and not holder and age > STARTUP_GRACE)
    if not gone:
        return False
    try:
        saved = json.loads((d / "result.json").read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    reason = saved.get("exit_reason", "") if saved else ("process died" if r.pid else "never started")
    meta.update(status=saved.get("status", "failed"), exit_reason=reason, end=now_iso())
    _atomic(d / "meta.json", json.dumps(meta, indent=1))
    log = d / "log.md"
    text = log.read_text("utf-8") if log.exists() else ""
    if not END_RE.search(text):
        log.write_text((text.rstrip("\n") + "\n\n" if text else "") + f"<!-- lm:end status={meta['status']} -->\n", "utf-8")
    return True


def run_result(home: Home, run_id: str) -> dict[str, Any]:
    """{run_id, status, exit_reason, text, cost_usd, cost_est [, result]} of a run.  `status` stays `running`
    while it works.  A run whose process is gone without an end is marked failed (`process died`, see `reap_dead`)."""
    d = resolve_run(home, run_id)
    reap_dead(home, d.name)
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    r = Run.from_dict(meta)
    try:
        saved = json.loads((d / "result.json").read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    out: dict[str, Any] = {"run_id": r.id, "status": r.status, "exit_reason": r.exit_reason, "text": "",
                           "cost_usd": r.cost_usd, "cost_est": r.cost_est}
    if r.status != "running":
        out.update({k: saved[k] for k in ("text", "result") if k in saved})
    return out


def wait_runs(home: Home, ids: list[str], timeout: float | None = None, poll: float = 0.5) -> list[dict[str, Any]]:
    """Block until all runs have ended (or `timeout` seconds have passed); one `run_result` per id, in order.
    Runs still working at the end have status `running`."""
    for i in ids:
        resolve_run(home, i)                                # an unknown id fails at once, not after the wait
    end = time.monotonic() + timeout if timeout is not None else None
    while True:
        res = [run_result(home, i) for i in ids]
        if all(r["status"] != "running" for r in res) or (end is not None and time.monotonic() >= end):
            return res
        time.sleep(poll)


@contextmanager
def _lock(path: Path, wait: float = 10.0):
    """A lock file made with O_EXCL; one older than `wait` seconds is taken over (its owner died)."""
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > wait:
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() - t0 > 2 * wait:
                raise TimeoutError(f"lock {path} stays taken")
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass


def _slots(home: Home) -> Path:
    d = home.path("state") / "slots"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ one writer per chat: the session lock
def lock_file(home: Home, run_id: str) -> Path:
    """<state>/sessions/<run id>.lock: `<pid> <process stamp>` of the process that has the chat open."""
    return home.path("state") / "sessions" / f"{run_id}.lock"


def _holder(f: Path) -> tuple[int, str]:
    try:
        parts = f.read_text("utf-8").split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return 0, ""


def _live(pid: int, stamp: str) -> bool:
    """Is that process still the one that wrote the lock (alive, and not another one with the same number)?"""
    return pid_alive(pid) and (not stamp or process_stamp(pid) in ("", stamp))


def lock_holder(home: Home, run_id: str) -> int:
    """The pid of the live process that has the chat open, else 0.  The lock of a dead process counts as none."""
    pid, stamp = _holder(lock_file(home, run_id))
    return pid if _live(pid, stamp) else 0


def acquire_lock(home: Home, run_id: str) -> int:
    """Take the chat: 0 when this process holds it now, else the pid of the live process that has it open (then the
    chat may only be read).  A lock of a dead process, of a process that merely has that pid today, or of ourselves is
    taken over.  The check and the write are one step under a lock file."""
    f = lock_file(home, run_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    with _lock(f.parent / ".lock"):
        pid, stamp = _holder(f)
        if pid and pid != os.getpid() and _live(pid, stamp):
            return pid
        _atomic(f, f"{os.getpid()} {process_stamp(os.getpid())}\n")
    return 0


def release_lock(home: Home, run_id: str) -> None:
    """Give the chat up: removes the lock when it is ours (never another process's)."""
    f = lock_file(home, run_id)
    if _holder(f)[0] == os.getpid():
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass                                          # a lock of ours that stays is taken over by the next opener


def claim_slot(home: Home, run_id: str, limit: int) -> bool:
    """Take one of `limit` parallel-run slots (a file `<state>/slots/<run id>.slot` holding this process's pid).
    Slots of dead processes are freed first.  False when all are taken."""
    d = _slots(home)
    with _lock(d / ".lock"):
        live = 0
        for f in d.glob("*.slot"):
            try:
                pid = int(f.read_text("utf-8").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid_alive(pid) and f.stem != run_id:
                live += 1
            elif f.stem != run_id:
                f.unlink(missing_ok=True)
        if limit and live >= limit:
            return False
        (d / f"{run_id}.slot").write_text(str(os.getpid()), "utf-8")
        return True


def release_slot(home: Home, run_id: str) -> None:
    (_slots(home) / f"{run_id}.slot").unlink(missing_ok=True)


# ------------------------------------------------------------------ fixed sessions: names
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class NameTaken(ValueError):
    """A session name that another session already has."""


def valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name or ""))


def names_file(home: Home) -> Path:
    """<state>/sessions/names.json: `{name: run id}`.  In the state location (agents cannot write there), so a name cannot be
    taken over from inside a run."""
    return home.path("state") / "sessions" / "names.json"


def _names(home: Home) -> dict[str, str]:
    """The names whose run folder still exists (a deleted run frees its name)."""
    try:
        data = json.loads(names_file(home).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    runs_dir = home.path("runs")
    return {str(k): str(v) for k, v in data.items() if (runs_dir / str(v) / "meta.json").is_file()}


def session_names(home: Home) -> dict[str, str]:
    """run id -> name, for `lm runs`."""
    return {v: k for k, v in _names(home).items()}


def find_name(home: Home, name: str) -> str | None:
    """The run id of the session with that name (compared without regard to case), else None."""
    return next((v for k, v in _names(home).items() if k.casefold() == name.casefold()), None)


def claim_name(home: Home, name: str, run_id: str) -> None:
    """Give session `run_id` the name `name`: unique without regard to case (a case-insensitive disk would mix them up
    anyway).  ValueError for a bad name, NameTaken when another session has it; the same session may claim it again."""
    if not valid_name(name):
        raise ValueError(f"session name {name!r}: only letters, digits, . _ - and at most 64 characters")
    f = names_file(home)
    f.parent.mkdir(parents=True, exist_ok=True)
    with _lock(f.parent / ".names.lock"):
        names = _names(home)
        taken = next((v for k, v in names.items() if k.casefold() == name.casefold() and v != run_id), None)
        if taken:
            raise NameTaken(f"session name {name!r} is taken by {taken}")
        names = {k: v for k, v in names.items() if k.casefold() != name.casefold()}
        names[name] = run_id
        _atomic(f, json.dumps(names, indent=1, ensure_ascii=False))


def free_name(home: Home, base: str) -> str:
    """`base` when no session has it, else `base-2`, `base-3` ... (the first free one; the 64 character limit holds)."""
    n, name = 1, base
    while find_name(home, name):
        n += 1
        name = f"{base[:60]}-{n}"
    return name


def resolve_session(home: Home, ref: str) -> str | None:
    """The run id for a name or a run id (unique prefix); None when nothing matches, ValueError when a prefix is ambiguous.
    A name wins over a prefix."""
    hit = find_name(home, ref)
    if hit:
        return hit
    found = find_runs(home, ref)
    if len(found) > 1:
        raise ValueError(f"{ref!r} matches {len(found)} runs: give more of the id")
    return found[0].name if found else None
