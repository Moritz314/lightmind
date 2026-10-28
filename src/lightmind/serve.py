"""`lm serve --stdio --workdir <folder>`: the backend of the VS Code extension (protocol v1, plan/PROTOCOL.md).

One JSON line per message over stdin (requests, to the backend) and stdout (events and results, from it); stderr is the log.
One backend serves one VS Code window and has at most one open chat.  What a chat does is the Session API of loop.py, the same
code the TUI runs; what both surfaces share around it lives in core.py (hooks, slash words, settings reads, sync).

The channel is kept clean: at the start fd 0 and fd 1 are copied (`os.dup`) for the protocol and replaced by the null device
and by stderr, so nothing a hook, a tool or a library prints can land in the stream.  Everything that leaves goes through
`Backend.emit`, the one place that masks secret values.  The events themselves (loop event -> protocol event, the
history of a chat) are made by `protocol.py`, which the Remote Control link uses too.

Questions of the AI (the `ask` tool) and of the rules (permission) are callbacks that send a `question` event and wait for
`answer`; a cancelled turn closes its open questions.  A request is checked against `SPEC` before anything runs.

May import: core, loop, protocol, settings, runs, files, titles, todos.  Not the TUI.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import sys
import threading
import traceback
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, codename, core, loop, offer, protocol, resume, settings as S, titles, todos
from . import slash as sl
from .files import Home, write_atomic
from .runs import (archive as archive_run, is_archived, list_session_groups, lock_holder, read_names, request_stop,
                   session_names, set_name_on_disk, take_stop, unarchive as unarchive_run)
from .tools import secret_masker

PROTOCOL_VERSION = 1

ARCHIVE_WORDS = re.compile(r"/archive\s+(list|restore)(?:\s+(\S+))?", re.IGNORECASE)     # the two words after /archive that keep the chat open
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")          # an id, an agent or a project name (launch.SAFE)
TEXT_MAX = 200_000
LINE_MAX = 1_000_000                                       # one request line; longer is refused
ASK_TIMEOUT = 1800.0                                       # seconds, `run.ask_timeout` overrides

# type -> field -> (kind, required, limit).  kind: ident | str | int | bool | dict.  Anything not in a row is ignored.
SPEC: dict[str, dict[str, tuple[str, bool, int]]] = {
    "session.list": {"limit": ("int", False, 200), "archived": ("bool", False, 1)},     # archived: only the archive
    "session.archive": {"session": ("ident", True, 128)},    # a chat of the list into the archive (not the one open here: /archive)
    "session.unarchive": {"session": ("ident", True, 128)},  # a chat of the archive back into the list
    "session.new": {"agent": ("ident", False, 64), "project": ("ident", False, 64), "model": ("str", False, 80),
                    "effort": ("str", False, 16), "mode": ("str", False, 40)},
    "session.open": {"session": ("ident", True, 128)},
    "session.end": {"session": ("ident", True, 128)},        # the resume menu's "end" (runs.request_stop)
    "notes.get": {"session": ("ident", True, 128)},          # name, description and the to-do tree of a chat
    "notes.name": {"session": ("ident", True, 128), "field": ("str", True, 16), "text": ("str", False, 400)},
    "session.set": {"model": ("str", False, 80), "effort": ("str", False, 16), "mode": ("str", False, 40)},
    "send": {"text": ("str", True, TEXT_MAX)},
    "cancel": {},
    "subagent.cancel": {"sub": ("str", True, 128)},          # `id` is the id of the request itself
    "answer": {"question": ("str", True, 64), "choice": ("str", True, 10_000)},
    "file.get": {"path": ("str", True, 1000), "piece": ("int", False, 100_000)},       # a file named in `[[file: ...]]`, piece by piece
    "settings.get": {},
    "settings.set": {"changes": ("dict", True, 64)},
    "reload": {},
    "shutdown": {},
}


class Reject(Exception):
    """A request that cannot be done: the text goes to the extension as the error of the result."""


def check(msg: Any) -> str:
    """'' when `msg` is a well-formed request of the table, else why not."""
    if not isinstance(msg, dict):
        return "a message is a JSON object"
    t = msg.get("type")
    if not isinstance(t, str) or t not in SPEC:
        return f"unknown type {str(t)[:60]!r}"
    if t != "shutdown":
        rid = msg.get("id")
        if not isinstance(rid, str) or not 0 < len(rid) <= 64:
            return f"{t}: `id` is a string of 1 to 64 characters"
    for name, (kind, required, limit) in SPEC[t].items():
        v = msg.get(name)
        if v is None:
            if required:
                return f"{t}: `{name}` is required"
            continue
        if kind in ("ident", "str"):
            if not isinstance(v, str) or len(v) > limit:
                return f"{t}: `{name}` is a string of at most {limit} characters"
            if kind == "ident" and not SAFE.fullmatch(v):
                return f"{t}: `{name}` may hold only letters, digits and . _ -"
            if required and not v.strip():
                return f"{t}: `{name}` is empty"
        elif kind == "int":
            if type(v) is not int or not 1 <= v <= limit:
                return f"{t}: `{name}` is a whole number from 1 to {limit}"
        elif kind == "bool":
            if type(v) is not bool:
                return f"{t}: `{name}` is true or false"
        elif kind == "dict":
            if not isinstance(v, dict) or len(v) > limit:
                return f"{t}: `{name}` is an object with at most {limit} entries"
            for k, x in v.items():
                if not isinstance(k, str) or not 0 < len(k) <= 64:
                    return f"{t}: a key of `{name}` is a string of 1 to 64 characters"
                if not (x is None or type(x) in (bool, int) or (isinstance(x, str) and len(x) <= 200)):
                    return f"{t}: the value of {k!r} is a string, a number, a boolean or null"
    return ""


# ------------------------------------------------------------------ the folder the user works in
def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def workdirs_file(home: Home) -> Path:
    return home.path("state") / "workdirs.json"


def load_workdirs(home: Home) -> dict[str, str]:
    """folder -> project of this machine (never synced: the folders differ per machine)."""
    try:
        d = json.loads(workdirs_file(home).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}


def save_workdirs(home: Home, d: dict[str, str]) -> None:
    f = workdirs_file(home)
    write_atomic(f, json.dumps(d, indent=1, ensure_ascii=False))


def mapped_project(home: Home, workdir: str | None) -> str | None:
    """The project this machine has linked to the folder (exact match), if its entry still exists."""
    if not workdir:
        return None
    name = load_workdirs(home).get(_key(workdir))
    return name if name and home.has_project(name) else None


def is_workspace(home: Home, workdir: str) -> bool:
    """A folder that gets a project: not the tree itself or anything in it, not the user's home folder."""
    k, tree = _key(workdir), _key(home.root)
    return not (k == tree or k.startswith(tree + os.sep) or k == _key(Path.home()))


def project_name(workdir: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", Path(workdir).name.lower()).strip("-") or "workspace"


def ensure_project(home: Home, workdir: str) -> tuple[str, str]:
    """The project of a folder, made now (first message): -> (name, note), ("", "") when the folder gets none.  An entry of
    the same name is linked, never a second one (`-2`).  The entry holds no path: the link lives in workdirs.json."""
    if not workdir or not is_workspace(home, workdir):
        return "", ""
    name = mapped_project(home, workdir)
    if name:
        return name, ""
    name = project_name(workdir)
    folder = home.path("projects") / name
    if home.has_project(name):
        note = f"linked project {name} for {workdir}"
    else:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / home.filename("project_memory")).write_text(
            f'---\nrepo: ""\n---\n\n# {name}\n\nMade for a working folder of the VS Code extension. '
            "Facts and decisions about the project go here.\n", "utf-8")
        (folder / home.filename("project_assets")).write_text(
            "The files and links that belong to the project, one row each: path or URL, then a note.\n\n"
            "# Assets          # path or URL | note\n", "utf-8")
        note = f"new project {name} for {workdir}"
    d = load_workdirs(home)
    d[_key(workdir)] = name
    save_workdirs(home, d)
    return name, note


# ------------------------------------------------------------------ small helpers
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ the backend
class Backend:
    def __init__(self, home: Home, workdir: str | None, out: "queue.SimpleQueue[bytes | None]") -> None:
        self.home, self.workdir, self.out = home, workdir, out
        self.s: loop.Session | None = None
        self.ready = asyncio.Event()                         # the on_start hooks of the open chat are done
        self.start_task: asyncio.Task | None = None
        self.turn_task: asyncio.Task | None = None
        self.bg_calls: dict = {}
        self.turn_n = 0
        self.q_n = 0
        self.waiting: list[tuple[str, str, str, str]] = []     # (id, words, message, raw line) beyond the open interjections: they follow the turn; a raw line (slash words) is prepared then, not now
        self.ij_n = 0
        self.questions: dict[str, tuple[asyncio.Future, str, list[str], str]] = {}   # id -> (future, kind, choices, session)
        self.base_masker = secret_masker(home)
        self.sig = core.signature(home)

    # -------------------------------------------------------------- output: the one place that masks
    @property
    def sid(self) -> str | None:
        return self.s.id if self.s is not None else None

    def emit(self, type_: str, session: str | None | bool = True, **fields: Any) -> None:
        """Send one event.  `session` True = the open chat's id.  Every string is masked here, nowhere else."""
        sid = self.sid if session is True else session
        masker = self.s.dispatcher.masker if self.s is not None else self.base_masker
        line = json.dumps(protocol.mask({"type": type_, "session": sid, **fields}, masker), ensure_ascii=False, separators=(",", ":"))
        self.out.put(line.encode("utf-8") + b"\n")

    def result(self, rid: str, data: dict | None = None, error: str = "") -> None:
        masker = self.s.dispatcher.masker if self.s is not None else self.base_masker
        obj: dict[str, Any] = {"type": "result", "id": rid, "ok": not error}
        if error:
            obj["error"] = error
        else:
            obj["data"] = data or {}
        self.out.put(json.dumps(protocol.mask(obj, masker), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

    def status(self, session: str | None | bool = True) -> None:
        s = self.s
        if s is None:
            return
        self.emit("status", session, model=s.model, effort=s.effort, mode=s.mode, project=s.project or None, account=s.account)

    # -------------------------------------------------------------- the loop of requests
    async def run(self, inbox: "asyncio.Queue[bytes | None]") -> int:
        self.emit("hello", None, version=PROTOCOL_VERSION, lm=__version__, codename=codename(), tree=str(self.home.root),
                  workdir=self.workdir, project=mapped_project(self.home, self.workdir))
        core.sync_start(self.home)
        tick = asyncio.create_task(self._tick())
        stopper = asyncio.create_task(self._poll_stop())
        clean = False
        try:
            while True:
                raw = await inbox.get()
                if raw is None:                              # the extension is gone: park the chat, do not end it
                    break
                if await self._handle(raw):
                    clean = True
                    break
        finally:
            tick.cancel()
            stopper.cancel()
            await self._close_session(park=not clean)
            with suppress(Exception):
                await core.sync_end(self.home, 15)
        return 0

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(core.TICK)
            with suppress(Exception):
                await core.sync_tick(self.home)

    async def _handle(self, raw: bytes) -> bool:
        """One line.  True = shutdown."""
        if raw is _TOO_LONG:
            self.emit("error", text=f"a message line is longer than {LINE_MAX} bytes: dropped")
            return False
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.emit("error", text="a message line is not valid UTF-8 JSON: dropped")
            return False
        why = check(msg)
        t = msg.get("type") if isinstance(msg, dict) else None
        rid = msg.get("id") if isinstance(msg, dict) else None
        if why:
            if t in SPEC and isinstance(rid, str) and 0 < len(rid) <= 64:
                self.result(rid, error=why)                  # a known request with a bad field: its answer says so
            else:
                self.emit("error", text=why, **({"id": rid} if isinstance(rid, str) and len(rid) <= 64 else {}))
            return False
        if t == "shutdown":
            return True
        try:
            data = await getattr(self, "do_" + t.replace(".", "_"))(msg)
        except Reject as e:
            self.result(rid, error=str(e))
        except Exception as e:                               # noqa: BLE001 - the backend stays up, the request says why not
            traceback.print_exc(file=sys.stderr)
            self.result(rid, error=f"{type(e).__name__}: {e}")
        else:
            self.result(rid, data)
        return False

    # -------------------------------------------------------------- sessions
    def _name(self, s: "loop.Session") -> str:
        first = next((core.clean_user(x.text) for x in s.messages if x.role == "user"), "")
        return " ".join((s.title or first).split())[:60] or s.info.agent or "new chat"

    def _session(self, s: "loop.Session") -> dict:
        i = s.info
        return {"id": i.id, "name": self._name(s), "title": i.title, "description": i.description, "agent": i.agent, "project": i.project, "status": "running",
                "last_active": _now(), "model": i.model, "origin": s.run.run.origin, "readonly": s.readonly}

    async def do_session_list(self, msg: dict) -> dict:
        names = await asyncio.to_thread(session_names, self.home)
        rows = await asyncio.to_thread(list_session_groups, self.home, msg.get("limit") or 50, names, bool(msg.get("archived")))
        have = {r.id for r in rows}
        return {"sessions": [{
            "parent": r.parent, "parent_name": names.get(r.parent, "") if r.parent and r.parent not in have else "",
            "id": r.id, "name": " ".join((r.title or r.agent or "chat").split()), "title": r.title,
            "description": r.description, "agent": r.agent, "project": r.project,
            "status": "running" if r.id == self.sid else core.state_of(r), "last_active": r.last_active,
            "model": r.model, "origin": r.origin, "archived": r.archived,
            "readonly": r.id != self.sid and lock_holder(self.home, r.id) not in (0, os.getpid())} for r in rows]}

    async def do_session_set(self, msg: dict) -> dict:
        """Model, effort or mode of the open chat; takes effect at its next turn.  Answers with the chat and sends `status`."""
        s = self.s
        if s is None:
            raise Reject("no session is open")
        if s.readonly:
            raise Reject(f"read only: this chat is open in another window (process {s.readonly_by})")
        kw = {k: msg[k].strip() for k in ("model", "effort", "mode") if isinstance(msg.get(k), str) and msg[k].strip()}
        if not kw:
            raise Reject("session.set: give a model, an effort or a mode")
        if "effort" in kw and kw["effort"] not in S.EFFORTS:
            raise Reject(f"effort is one of {', '.join(S.EFFORTS)}")
        try:
            await asyncio.to_thread(lambda: s.set(**kw))
        except (ValueError, NotImplementedError, KeyError) as e:
            raise Reject(str(e) or type(e).__name__) from None
        self.status()
        return {"session": self._session(s)}

    def _defaults(self, msg: dict) -> dict[str, str | None]:
        """agent, model, effort, mode of a new chat: the request, else `vscode:` of config.md, else the usual defaults."""
        cfg = self.home.settings.get("vscode")
        cfg = cfg if isinstance(cfg, dict) else {}
        return {k: (msg.get(k) or cfg.get(k) or None) for k in ("agent", "model", "effort", "mode")} | {
            "project": msg.get("project") or mapped_project(self.home, self.workdir) or None}

    async def do_session_new(self, msg: dict) -> dict:
        kw = self._defaults(msg)
        await self._close_session()
        try:
            s = await asyncio.to_thread(lambda: loop.open_session(
                self.home, agent=kw["agent"], project=kw["project"], model=kw["model"], effort=kw["effort"],
                mode=kw["mode"], asker=self._asker, questioner=self._questioner, origin="vscode",
                workdir=self.workdir or ""))
        except (ValueError, NotImplementedError, FileNotFoundError, KeyError) as e:
            raise Reject(str(e) or type(e).__name__) from None
        self._attach(s)
        return {"session": self._session(s)}

    async def do_session_open(self, msg: dict) -> dict:
        want = msg["session"]
        if self.s is not None and self.s.id.startswith(want) and not self.s.closed:
            return {"session": self._session(self.s), "history": self._history(self.s)}     # already open here
        await self._close_session()
        try:
            s = await asyncio.to_thread(lambda: loop.resume_session(
                self.home, want, asker=self._asker, questioner=self._questioner))
        except (ValueError, FileNotFoundError, NotImplementedError, KeyError) as e:
            raise Reject(str(e) or type(e).__name__) from None
        restored = False
        if is_archived(self.home.path("runs") / s.id):          # going on in an archived chat means wanting it back in the list
            restored = unarchive_run(self.home, s.id) == "restored"
        self._attach(s)
        return {"session": self._session(s), "history": self._history(s), **({"restored": True} if restored else {})}

    async def do_session_archive(self, msg: dict) -> dict:
        """Put a chat of the list into the archive (the extension's "Archive").  Not the one open here: that one leaves with
        /archive.  Another process that holds the chat is asked to let go first (`runs.archive`, up to 4 s)."""
        sid = msg["session"]
        self._run_dir(sid)
        if self.s is not None and self.s.id == sid and not self.s.closed:
            raise Reject("this chat is open here: type /archive in it")
        try:
            line = await asyncio.to_thread(archive_run, self.home, sid, "vscode")
        except ValueError as e:
            raise Reject(str(e)) from None
        return {"session": sid, "text": line}

    async def do_session_unarchive(self, msg: dict) -> dict:
        """Take a chat out of the archive: it is in the list (and in every resume menu) again."""
        sid = msg["session"]
        self._run_dir(sid)
        try:
            line = await asyncio.to_thread(unarchive_run, self.home, sid)
        except ValueError as e:
            raise Reject(str(e)) from None
        return {"session": sid, "text": line}

    # -------------------------------------------------------------- the resume menu: notes, name, end (the TUI's n / e keys)
    def _run_dir(self, sid: str) -> Path:
        d = self.home.path("runs") / sid
        if not (d / "meta.json").is_file():
            raise Reject(f"no such chat: {sid}")
        return d

    async def do_file_get(self, msg: dict) -> dict:
        """One piece (base64, `offer.CHUNK` bytes) of a file the agent offered with `[[file: ...]]`; piece 1 is the default."""
        try:
            return await asyncio.to_thread(offer.piece, msg["path"], self.home.root, msg.get("piece") or 1)
        except offer.Refused as e:
            raise Reject(str(e)) from None

    async def do_notes_get(self, msg: dict) -> dict:
        """Name, description and the to-do tree (todos.json) of any chat of this tree; it is only read."""
        sid = msg["session"]
        d = self._run_dir(sid)

        def read() -> dict:
            title, description = read_names(self.home, sid)
            return {"session": sid, "title": title, "description": description, "items": todos.dump(todos.Store(d).items)}
        return await asyncio.to_thread(read)

    async def do_notes_name(self, msg: dict) -> dict:
        """Set the name (`field` title) or the description of a chat by hand; empty text = automatic again.  The chat open
        here goes through `titles.set_manual` (like /rename), any other through `runs.set_name_on_disk`: the process that
        holds it adopts the change with its next write."""
        sid, field = msg["session"], msg["field"]
        if field not in ("title", "description"):
            raise Reject("field is title or description")
        self._run_dir(sid)
        text = msg.get("text") or ""
        s = self.s
        if s is not None and s.id == sid and not s.readonly:
            line = await asyncio.to_thread(titles.set_manual, s, field, text)
        else:
            line = await asyncio.to_thread(set_name_on_disk, self.home, sid, field, text)
        title, description = await asyncio.to_thread(read_names, self.home, sid)
        return {"session": sid, "title": title, "description": description, "text": line}

    async def do_session_end(self, msg: dict) -> dict:
        """End a chat from the list.  Not the one open here (leave it by opening another or a new one); nobody holds it ->
        closed on disk; another process holds it -> asked to let go (`runs.request_stop`, up to 4 s)."""
        sid = msg["session"]
        self._run_dir(sid)
        if self.s is not None and self.s.id == sid and not self.s.closed:
            raise Reject("this chat is open here: open another chat or start a new one to leave it")
        line = await asyncio.to_thread(request_stop, self.home, sid)
        return {"session": sid, "text": line}

    async def _poll_stop(self) -> None:
        """Another window's resume menu asked to end the chat open here (`stop.request`): close it, tell the extension."""
        while True:
            await asyncio.sleep(1.0)
            s = self.s
            if s is None or s.readonly or s.closed or not take_stop(s.run.path):
                continue
            try:
                await self._close_session()
            except Exception:                                # noqa: BLE001 - the poll goes on, the error is in the log
                traceback.print_exc(file=sys.stderr)
            self.emit("session.ended", s.id, text="This chat was ended from the resume menu. Your next message starts a new chat.")

    def _bg_message(self, s: "loop.Session", text: str) -> None:
        """A background sub-agent is done: its result is a turn when the chat is free, else it goes after the running one
        (see `_turn`).  Never a message of the user: the chat gets it framed as untrusted data, the client a `notice`."""
        self._bg_flush(s)

    def _bg_flush(self, s: "loop.Session") -> None:
        if s is not self.s or s.closed or s.readonly or (self.turn_task is not None and not self.turn_task.done()):
            return
        text = s.bg.take()
        if text is None:
            return
        for ev in protocol.sub_messages(s.id, f"t{self.turn_n + 1}", text):
            self.emit(ev.pop("type"), ev.pop("session"), **ev)
        self.turn_task = asyncio.create_task(self._turn(s, text, "subagent"))

    async def do_subagent_cancel(self, msg: dict) -> dict:
        s = self.s
        if s is None:
            raise Reject("no chat is open")
        if not s.bg.cancel(msg["sub"]):
            raise Reject(f"no sub-agent {msg['sub']!r} is working in the background")
        return {}

    def _bg_event(self, s: "loop.Session", ev) -> None:
        if s is self.s:
            self._event(s, f"t{self.turn_n}", ev, self.bg_calls)

    def _attach(self, s: "loop.Session") -> None:
        s.bg.on_message = lambda text, s=s: self._bg_message(s, text)
        s.bg.on_event = lambda ev, s=s: self._bg_event(s, ev)
        if not s.readonly:
            s.enable_titles()
            loop_ = asyncio.get_running_loop()
            s.on_title.append(lambda title, description, s=s: loop_.call_soon_threadsafe(    # the naming runs in a thread
                self._title_event, s, title, description))
        self.s = s
        self.ready = asyncio.Event()
        self.sig = core.signature(self.home)
        if s.readonly:
            self.ready.set()
        else:
            self.start_task = asyncio.create_task(self._start(s))
        self.status()

    def _title_event(self, s: "loop.Session", title: str, description: str) -> None:
        """The title or description of a chat changed (by hand or by the automatic naming): `session.title`."""
        self.emit("session.title", s.id, name=self._name(s), title=title, description=description)

    async def _start(self, s: "loop.Session") -> None:
        """The on_start hooks (their lines come as notices); the first message waits for them."""
        try:
            lines, warning = await core.start_hooks(self.home, s)
            for line in lines:
                self.emit("notice", s.id, level="info", text=line)
            if warning:
                self.emit("notice", s.id, level="warn", text=warning)
        finally:
            self.ready.set()

    async def _close_session(self, park: bool = False) -> None:
        s = self.s
        if s is None:
            return
        try:                                                 # (self.s stays set meanwhile: the unwinding turn's events name this chat)
            await self._stop_turn()
            if self.start_task is not None:
                with suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self.start_task), 20)
        finally:
            self.s = None
        if park:
            await asyncio.to_thread(s.hand_over)
        else:
            for line in await asyncio.to_thread(s.close):
                self.emit("notice", s.id, level="info", text=line)

    def _history(self, s: "loop.Session") -> list[dict]:
        return protocol.history(s, self.home)

    # -------------------------------------------------------------- the turn
    def _paths(self, name: str, args: dict) -> list[str]:
        return protocol.paths(self.home, name, args)

    async def do_send(self, msg: dict) -> dict:
        s = self.s
        if s is None:
            raise Reject("no chat is open: session.new or session.open first")
        if s.readonly:
            raise Reject(f"read only: this chat is open in another window (process {s.readonly_by})")
        if msg["text"].strip().lower() == "/archive":        # the one command that leaves the chat and is allowed here
            return await self._archive(s)
        if (m := ARCHIVE_WORDS.fullmatch(msg["text"].strip())) is not None:      # /archive list, /archive restore <chat>: the chat stays open
            return await self._archive_words(m[1].lower(), (m[2] or "").strip())
        running = self.turn_task is not None and not self.turn_task.done()
        if running and sl.has_slash(msg["text"]):             # commands and skills wait for the end of the turn, as in the TUI
            return self._interject(s, msg["text"].strip(), "", raw=msg["text"])
        prep = core.prepare(self.home, s, msg["text"])
        for level, text in prep.notes:
            self.emit("notice", level=level, text=text)
        if prep.notes:
            self.status()
        if prep.error:
            raise Reject(prep.error)
        if prep.message is None:                             # only commands
            return {}
        if running:                                          # a message while the agent works: into the running turn, or in line behind it
            return self._interject(s, msg["text"].strip(), prep.message)
        if not s.project and self.workdir:                   # the entry of the folder is made by the first message
            name, note = await asyncio.to_thread(ensure_project, self.home, self.workdir)
            if name:
                s.set_project(name)
                if note:
                    self.emit("notice", level="info", text=note)
                self.status()
        self.turn_task = asyncio.create_task(self._turn(s, prep.message))
        return {}

    async def _archive(self, s: "loop.Session") -> dict:
        """`/archive`: stop the turn, close the chat and mark it archived (`runs.archive`); the client gets `session.ended` and
        its next message starts a new chat."""
        await self._close_session()
        try:
            text = await asyncio.to_thread(archive_run, self.home, s.id, "vscode")
        except ValueError as e:
            raise Reject(str(e)) from None
        self.emit("session.ended", s.id, text="This chat was archived. Your next message starts a new chat.")
        return {"archived": s.id, "text": text}

    async def _archive_words(self, word: str, ref: str) -> dict:
        """`/archive list` shows the archive, `/archive restore <chat>` puts a chat back into the list; the answer is a notice."""
        if (word == "list") == bool(ref):
            raise Reject("use /archive list or /archive restore <chat>")
        if word == "list":
            text = await asyncio.to_thread(resume.archive_text, self.home)
        else:
            try:
                run_id, line = await asyncio.to_thread(resume.restore, self.home, ref)
            except ValueError as e:
                raise Reject(f"/archive restore: {e}") from None
            text = f"{run_id}: {line}"
        self.emit("notice", level="info", text=text)
        return {"text": text}

    def _interject(self, s: "loop.Session", words: str, message: str, raw: str = "") -> dict:
        """The agent is working.  The message is read at its next step (at most `interject.MAX_OPEN` open at once, and only
        when no earlier one waits in line), else it joins the line behind the turn.  Events `interject` say which."""
        self.ij_n += 1
        it = s.interject(message) if not (self.waiting or raw) else None
        if it is not None:
            self.emit("interject", s.id, id=f"i{it.id}", state="pending", text=words)
            return {"interject": f"i{it.id}", "state": "pending"}
        qid = f"q{self.ij_n}"
        self.waiting.append((qid, words, message, raw))
        self.emit("interject", s.id, id=qid, state="queued", text=words)
        return {"interject": qid, "state": "queued"}

    def _after_turn(self, s: "loop.Session", status: str) -> None:
        """What was written during the turn and never read: it follows the turn as the next message when this one ended
        well, else it is handed back (`dropped`) so the client can put the words in its input again."""
        left = [(f"i{it.id}", it.text) for it in s.take_leftover() + s.inbox.withdraw()]
        waiting, self.waiting = self.waiting, []
        if status != "done" or s.closed or s is not self.s:
            for qid, text in left + [(q, w) for q, w, _, _ in waiting]:
                self.emit("interject", s.id, id=qid, state="dropped", text=text)
            return
        parts = [(qid, m_) for qid, m_ in left]
        for qid, w, msg_, raw in waiting:
            if raw:                                           # a line with slash words is prepared now: its commands run between the turns
                prep = core.prepare(self.home, s, raw)
                for level, text in prep.notes:
                    self.emit("notice", s.id, level=level, text=text)
                if prep.error:
                    self.emit("notice", s.id, level="warn", text=prep.error)
                    self.emit("interject", s.id, id=qid, state="dropped", text=w)
                    continue
                msg_ = prep.message or ""
                if not msg_:                                  # only commands: done
                    self.emit("interject", s.id, id=qid, state="sent", text="")
                    continue
            parts.append((qid, msg_))
        if not parts:
            return
        for qid, _ in parts:
            self.emit("interject", s.id, id=qid, state="sent", text="")
        self.turn_task = asyncio.create_task(self._turn(s, "\n\n".join(m_ for _, m_ in parts)))

    async def _turn(self, s: "loop.Session", message: str, origin: str = "") -> None:
        self.turn_n += 1
        turn, status, cost0, agen = f"t{self.turn_n}", "done", s.info.cost_usd, None
        calls: dict[str, tuple[str, list[str]]] = {}
        errored = False
        try:
            await self.ready.wait()
            self.emit("turn.start", s.id, turn=turn)
            agen = s.send(message, origin=origin) if origin else s.send(message)
            async for ev in agen:
                errored |= self._event(s, turn, ev, calls)
        except asyncio.CancelledError:
            status = "cancelled"
            if (me := asyncio.current_task()) is not None:
                me.uncancel()
        except Exception as e:                               # noqa: BLE001 - not logged in and friends: the chat stays usable
            status = "error"
            self.emit("error", s.id, text=str(e) or type(e).__name__)
        finally:
            with suppress(BaseException):
                if agen is not None:
                    await agen.aclose()
            self._close_questions(s.id)
            await asyncio.sleep(0)                           # let the waiting askers write their question.closed first
            lr = s.last_result
            if status == "done" and (errored or (lr is not None and lr.status == "failed")):
                status = "error"
            i = s.info
            self.emit("turn.end", s.id, turn=turn, status=status, cost_usd=round(max(i.cost_usd - cost0, 0.0), 6),
                      context_used=round(min(max(i.ctx_pct, 0.0), 1.0), 4))
            self.status(s.id)
            self._after_turn(s, status)
            if status != "cancelled":
                asyncio.get_running_loop().call_soon(self._bg_flush, s)     # results that came in while this turn ran (once this task is done)

    def _event(self, s: "loop.Session", turn: str, ev, calls: dict) -> bool:
        """One event of the loop -> protocol events (`protocol.Translator`), sent.  True when it was an error."""
        got = protocol.Translator(s, self.home, mask=False, calls=calls).event(ev, turn)      # `emit` masks
        errored = protocol.is_error(got)
        for e in got:
            self.emit(e.pop("type"), e.pop("session"), **e)
        return errored

    async def _stop_turn(self) -> None:
        t = self.turn_task
        if t is not None and not t.done():
            t.cancel()
            await asyncio.wait({t}, timeout=15)

    async def do_cancel(self, msg: dict) -> dict:
        t = self.turn_task
        if t is not None and not t.done():
            t.cancel()                                       # the turn closes its own questions and says turn.end
        return {}

    # -------------------------------------------------------------- questions
    async def _ask(self, kind: str, text: str, detail: str, choices: list[str], default: str) -> str:
        self.q_n += 1
        qid, sid = f"q{self.q_n}", self.sid or ""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.questions[qid] = (fut, kind, choices, sid)
        extra = {"detail": detail} if detail else {}
        self.emit("question", sid, question=qid, kind=kind, text=text, choices=choices, **extra)
        choice = "cancelled"
        try:
            timeout = core.duration((core.settings(self.home).get("run") or {}).get("ask_timeout"), ASK_TIMEOUT)
            choice = await asyncio.wait_for(fut, timeout)
            return choice
        except asyncio.TimeoutError:
            choice = default
            return default
        finally:
            self.questions.pop(qid, None)
            self.emit("question.closed", sid, question=qid, choice=choice)

    async def _asker(self, question, check) -> str:
        """The asker of the rules (rules.Asker): yes | always | no."""
        return await self._ask("permission", str(question), str(getattr(check, "arg", "") or ""),
                               ["yes", "always", "no"], "no")

    async def _questioner(self, question) -> str:
        """The `ask` tool: a free answer."""
        return await self._ask("ask", str(question), "", [], "(no answer: nobody answered in time)")

    def _close_questions(self, sid: str) -> None:
        for qid, (fut, _, _, qsid) in list(self.questions.items()):
            if qsid == sid and not fut.done():
                fut.cancel()

    async def do_answer(self, msg: dict) -> dict:
        q = self.questions.get(msg["question"])
        if q is None or q[0].done():
            raise Reject(f"no open question {msg['question']!r}")
        fut, kind, choices, _ = q
        if kind == "permission" and msg["choice"] not in choices:
            raise Reject(f"a permission question is answered with one of {', '.join(choices)}")
        fut.set_result(msg["choice"])
        return {}

    # -------------------------------------------------------------- settings, reload
    async def do_settings_get(self, msg: dict) -> dict:
        page = await asyncio.to_thread(S.read_general, self.home)
        if page.error:
            raise Reject(page.error)
        vals = page.values()
        fields = []
        for r in page.rows:
            f = r.field
            opts = [o[0] for o in S.options(self.home, f, vals) if o[0] is not None]
            fields.append({"key": ".".join(f.path), "label": f.label, "help": f.help,
                           "value": r.value if r.value is not None else r.effective,
                           "choices": opts if f.kind == "choice" or f.kind == "int" else None, "scope": "config"})
        return {"fields": [{k: v for k, v in x.items() if v is not None or k == "value"} for x in fields],
                "agents": await asyncio.to_thread(self._agents),
                **({"modes_error": err} if (err := await asyncio.to_thread(S.modes_error, self.home)) else {})}

    def _agents(self) -> list[dict]:
        """The agents/ folder for the settings screen: own values (null = uses the default) and what applies."""
        out = []
        for name in S.agent_names(self.home):
            page = S.read_agent(self.home, name)
            own = {".".join(r.field.path): r.value for r in page.rows}
            eff = {".".join(r.field.path): r.effective for r in page.rows}
            out.append({"name": name, "error": page.error or None,
                        **{k: own.get(k) for k in ("project", "provider", "model", "effort", "mode")},
                        "effective": {k: eff.get(k) for k in ("project", "provider", "model", "effort", "mode")}})
        return out

    async def do_settings_set(self, msg: dict) -> dict:
        changes = {tuple(k.split(".")): v for k, v in msg["changes"].items()}
        page = await asyncio.to_thread(S.read_general, self.home)
        have = page.values()
        try:
            await asyncio.to_thread(S.save_page, self.home, page, changes)
        except S.SettingsError as e:
            raise Reject(str(e)) from None
        return {"saved": sum(1 for p, v in changes.items() if not (p in have and S._is(have[p], v)))}

    async def do_reload(self, msg: dict) -> dict:
        now = await asyncio.to_thread(core.signature, self.home)
        old = self.sig
        changed = sorted({core.label(p) for p in set(now) | set(old) if now.get(p) != old.get(p)})
        self.sig = now
        if self.s is not None and not self.s.readonly:
            for problem in self.s.reload(providers=any(c.startswith("provider ") for c in changed)):
                self.emit("notice", level="warning", text="reload: " + problem)
        if changed:
            self.emit("notice", level="info", text="reloaded: " + ", ".join(changed))
        return {"changed": changed}


_TOO_LONG = object()


# ------------------------------------------------------------------ the process
def _reader(rf, loop_: asyncio.AbstractEventLoop, inbox: "asyncio.Queue") -> None:
    """stdin -> inbox, one line per item; None at the end of the stream."""
    try:
        while True:
            line = rf.readline(LINE_MAX + 1)
            if not line:
                break
            if len(line) > LINE_MAX and not line.endswith(b"\n"):
                while (rest := rf.readline(LINE_MAX)) and not rest.endswith(b"\n"):
                    pass
                loop_.call_soon_threadsafe(inbox.put_nowait, _TOO_LONG)
            elif line.strip():
                loop_.call_soon_threadsafe(inbox.put_nowait, line)
    except (OSError, ValueError):
        pass
    finally:
        loop_.call_soon_threadsafe(inbox.put_nowait, None)


def _writer(wf, out: "queue.SimpleQueue[bytes | None]") -> None:
    while (item := out.get()) is not None:
        try:
            wf.write(item)
            wf.flush()
        except (OSError, ValueError):
            break


def take_channel() -> tuple[Any, Any]:
    """Copy fd 0 and fd 1 for the protocol, then put the null device on fd 0 and stderr on fd 1 (and in sys.stdin / sys.stdout):
    from here nothing that prints can write into the channel, and no child process inherits it."""
    rfd, wfd = os.dup(0), os.dup(1)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull, "r", encoding="utf-8")                # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stderr
    return os.fdopen(rfd, "rb", buffering=0), os.fdopen(wfd, "wb", buffering=0)


async def _main(home: Home, workdir: str | None, rf, wf) -> int:
    out: "queue.SimpleQueue[bytes | None]" = queue.SimpleQueue()
    inbox: asyncio.Queue = asyncio.Queue()
    wt = threading.Thread(target=_writer, args=(wf, out), daemon=True)
    wt.start()
    threading.Thread(target=_reader, args=(rf, asyncio.get_running_loop(), inbox), daemon=True).start()
    try:
        return await Backend(home, workdir, out).run(inbox)
    finally:
        out.put(None)
        await asyncio.to_thread(wt.join, 5)


def serve_stdio(home: Home, workdir: str | None) -> int:
    rf, wf = take_channel()
    return asyncio.run(_main(home, workdir, _Lines(rf), wf))


class _Lines:
    """Line reader over a raw stream (readline(limit)), buffered here so a pipe is read in chunks, not byte by byte."""

    def __init__(self, raw) -> None:
        self.raw, self.buf = raw, b""

    def readline(self, limit: int = -1) -> bytes:
        while True:
            i = self.buf.find(b"\n")
            if 0 <= i < (limit if limit > 0 else len(self.buf) + 1):
                line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                return line
            if limit > 0 and len(self.buf) >= limit:
                line, self.buf = self.buf[:limit], self.buf[limit:]
                return line
            chunk = self.raw.read(65536)
            if not chunk:
                line, self.buf = self.buf, b""
                return line
            self.buf += chunk
