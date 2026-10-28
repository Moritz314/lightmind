"""Remote Control link: the chat process opens a local socket, mirrors what the chat does to whoever connects (the node of
this machine, which hands it to the phone) and takes requests back in (send, answer, cancel ...).

The TUI stays the only writer of the chat: every request goes to one handler (`ChatScreen.rc_inbound`) that runs it on the
UI loop, in the order it came.  Nothing here knows Textual.  The wire format is section 4.1 of the Remote Control plan: one
JSON object per line; the link says `link.hello` first, then events with a running `seq`; a request has an `id` and is
answered by `{"type":"result","id","ok","data"|"error"}`.

Files: `<state>/remote/links/<session>.json` (who, where, token) and, on POSIX, the socket `<session>.sock` beside it.  On
Windows the link listens on 127.0.0.1 and the first line a client sends must be `{"type":"auth","token":...}`.

May import: files, runs, core, protocol.  Not serve, not the TUI.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import core, protocol, runs
from .files import Home, write_atomic

VERSION = 1
LINE_MAX = 1_000_000                       # one request line; a client that sends more is dropped
SLOW_MAX = 8_000_000                       # bytes a client may fall behind before it is dropped
AUTH_WAIT = 5.0                            # seconds a TCP client has to send its token
NODE_WAIT = 10.0                           # seconds the chat waits for a node before it says no gateway is connected
ASK_TIMEOUT = 1800.0                       # seconds, `run.ask_timeout` overrides
SOCK_MAX = 100                             # bytes: longer unix socket paths do not fit sockaddr_un
DEVICE_MAX = 40
ACTIVE = ("tui", "vscode")                 # who may open a link
CLOSED = "link closed"                    # the `by` of a question that lost its link before anybody answered
PUSH_TEXT = "Remote Control wurde für Sitzung {name} aktiviert"     # the sentence the phone shows (and the fallback sends)

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class Refused(Exception):
    """A request that cannot be done: the text is the `error` of its result."""


def links_dir(home: Home) -> Path:
    """The folder the nodes watch: one `<session>.json` per open link."""
    return home.path("state") / "remote" / "links"


def _flag(home: Home, key: str, default: bool = False) -> bool:
    """A yes/no key below `remote:` in config.md: true, yes, on and 1 count as yes; a missing key is `default`."""
    cfg = core.settings(home).get("remote")
    v = cfg.get(key) if isinstance(cfg, dict) else None
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v is True or str(v).strip().lower() in ("true", "yes", "on", "1")


def enabled(home: Home) -> bool:
    """Is Remote Control switched on for this tree (`remote.enabled` in config.md)?"""
    return _flag(home, "enabled")


def auto_enabled(home: Home) -> bool:
    """Does every new chat open its link by itself (`remote.auto`)?  Only counts together with `remote.enabled`; when
    Remote Control is on, auto is on unless `auto: false`."""
    return enabled(home) and _flag(home, "auto", default=True)


def clean_text(text: Any, limit: int) -> str:
    """One line without control characters, at most `limit` characters (names and device names come from outside)."""
    return " ".join(_CTRL.sub(" ", str(text or "")).split())[:limit]


def clean_device(name: Any) -> str:
    """The name of the device a request comes from (`from`); a bad or empty one is `phone`."""
    return clean_text(name, DEVICE_MAX) or "phone"


def cleanup_files(home: Home, session_id: str) -> None:
    """Remove the files of a link of `session_id`, the socket too wherever the json says it lies."""
    d = links_dir(home)
    meta = d / f"{session_id}.json"
    with suppress(OSError, ValueError):
        info = json.loads(meta.read_text("utf-8"))
        if info.get("transport") == "unix" and isinstance(info.get("endpoint"), str):
            Path(info["endpoint"]).unlink(missing_ok=True)
    for p in (meta, d / f"{session_id}.sock"):
        with suppress(OSError):
            p.unlink()


@dataclass
class _Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    task: asyncio.Task | None = None
    open: bool = True


@dataclass
class _Question:
    fut: asyncio.Future                    # resolves to (choice, by)
    kind: str
    choices: list[str]
    event: dict


Handler = Callable[[dict], Awaitable[dict | None]]


class Link:
    """One chat's link.  Build it with `await Link.start(...)`; close it with `await link.close(reason)`.

    `handler(msg)` is called, one request at a time and in order, for every request the link does not answer itself
    (send, cancel, session.set, session.archive); it returns the `data` of the result or raises `Refused`.
    `on_close(reason)` is called once when the link has ended, however it ended."""

    def __init__(self, home: Home, session, name: str, origin: str, handler: Handler | None,
                 on_close: Callable[[str], None] | None, tcp: bool, auto: bool = False) -> None:
        self.home, self.s, self.name, self.origin = home, session, name, origin
        self.auto = auto                                 # opened by `remote.auto`, not by /rc: the phone is not pushed about it
        self.handler, self.on_close, self.tcp = handler, on_close, tcp
        self.clients: list[_Client] = []
        self.ever_connected = False
        self.seq = 0
        self.turn_n = 0
        self.turn_id = ""
        self.closed = False
        self.token = secrets.token_urlsafe(24) if tcp else ""
        self.server: asyncio.AbstractServer | None = None
        self.sock_path: Path | None = None
        self.endpoint = ""
        self.questions: dict[str, _Question] = {}
        self.answered: dict[str, str] = {}              # question id -> choice, so a late second answer can be told "already answered"
        self.q_n = 0
        self.calls: dict[str, tuple[str, list[str]]] = {}
        self.bg_calls: dict[str, tuple[str, list[str]]] = {}
        self.tr = protocol.Translator(session, home, mask=False, calls=self.calls)
        self.bg_tr = protocol.Translator(session, home, mask=False, calls=self.bg_calls)
        self.sent_status: dict | None = self.status_fields()      # what the clients know; `sync` sends what differs
        self.sent_busy: tuple[bool, int] = (False, 0)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.loop = asyncio.get_running_loop()
        self.thread = threading.get_ident()
        self.started = time.time()

    # ------------------------------------------------------------------ start and files
    @classmethod
    async def start(cls, home: Home, session, name: str, origin: str = "tui", handler: Handler | None = None,
                    on_close: Callable[[str], None] | None = None, tcp: bool | None = None, auto: bool = False) -> "Link":
        """Open the socket, write the link file and return the running link.  `tcp` None = by platform (Windows: localhost
        and a token).  `auto` marks a link the chat opened by itself; it is in the link file and the hello so no one pushes it.
        An old link of the same chat (a crashed process) is cleaned away first."""
        if origin not in ACTIVE:
            raise ValueError(f"a link is opened by {' or '.join(ACTIVE)}, not {origin!r}")
        link = cls(home, session, clean_text(name, 80) or session.id, origin, handler, on_close,
                   os.name == "nt" if tcp is None else tcp, auto)
        cleanup_files(home, session.id)
        try:
            await link.listen()
            link.write_meta()
        except BaseException:
            await link.shut_server()
            cleanup_files(home, session.id)
            raise
        link.worker = asyncio.create_task(link.work())
        return link

    def socket_path(self) -> Path:
        """Where the unix socket lies: next to the json, or - when that path is too long for a socket - in a short folder of
        the system's temp dir (the json always says where)."""
        d = links_dir(self.home)
        want = d / f"{self.s.id}.sock"
        if len(os.fsencode(str(want))) <= SOCK_MAX:
            return want
        uid = getattr(os, "getuid", lambda: 0)()
        short = Path(tempfile.gettempdir()) / f"lm-rc-{uid}"
        short.mkdir(mode=0o700, exist_ok=True)
        os.chmod(short, 0o700)
        return short / (hashlib.sha256(str(want).encode()).hexdigest()[:16] + ".sock")

    async def listen(self) -> None:
        d = links_dir(self.home)
        d.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(d, 0o700)
        if self.tcp:
            self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0, limit=LINE_MAX)
            self.endpoint = "127.0.0.1:%d" % self.server.sockets[0].getsockname()[1]
            return
        self.sock_path = self.socket_path()
        self.sock_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.serve, path=str(self.sock_path), limit=LINE_MAX)
        os.chmod(self.sock_path, 0o600)
        self.endpoint = str(self.sock_path)

    def write_meta(self) -> None:
        meta = {"version": VERSION, "session": self.s.id, "name": self.name, "pid": os.getpid(),
                "stamp": runs.process_stamp(os.getpid()), "origin": self.origin, "auto": self.auto,
                "transport": "tcp" if self.tcp else "unix", "endpoint": self.endpoint,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if self.tcp:
            meta["token"] = self.token
        write_atomic(links_dir(self.home) / f"{self.s.id}.json", json.dumps(meta, indent=1), mode=0o600)

    async def shut_server(self) -> None:
        if self.server is not None:
            self.server.close()
            with suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 2)
            self.server = None

    # ------------------------------------------------------------------ clients
    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection: check the token (TCP), say hello, then read requests until it ends."""
        client = _Client(reader, writer)
        client.task = asyncio.current_task()
        try:
            if self.tcp and not await self.authenticate(reader):
                return
            if self.closed:
                return
            self.clients.append(client)
            self.ever_connected = True
            self.write(client, self.hello())
            for q in self.questions.values():               # a client that comes late still sees the open questions
                self.write(client, q.event)
            while not self.closed:
                line = await reader.readline()
                if not line:
                    break
                self.inbox.put_nowait((client, line))
        except (asyncio.LimitOverrunError, ValueError, ConnectionError, OSError):
            pass                                            # too long a line, a broken connection: the client is dropped
        finally:
            self.drop(client)

    async def authenticate(self, reader: asyncio.StreamReader) -> bool:
        try:
            line = await asyncio.wait_for(reader.readline(), AUTH_WAIT)
            msg = json.loads(line.decode("utf-8"))
        except (asyncio.TimeoutError, ValueError, OSError, asyncio.LimitOverrunError):
            return False
        got = msg.get("token") if isinstance(msg, dict) and msg.get("type") == "auth" else None
        return isinstance(got, str) and hmac.compare_digest(got.encode(), self.token.encode())

    def drop(self, client: _Client) -> None:
        client.open = False
        if client in self.clients:
            self.clients.remove(client)
        with suppress(Exception):
            client.writer.close()

    def mask(self, obj: Any) -> Any:
        """Every string that leaves is masked, with the masker of the chat as it is now."""
        return protocol.mask(obj, self.s.dispatcher.masker)

    def encode(self, obj: dict) -> bytes:
        return json.dumps(self.mask(obj), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    def write(self, client: _Client, obj: dict) -> None:
        if not client.open:
            return
        try:
            client.writer.write(self.encode(obj))
            if client.writer.transport.get_write_buffer_size() > SLOW_MAX:
                self.drop(client)
        except Exception:                                   # noqa: BLE001 - a broken connection must not stop the chat
            self.drop(client)

    def broadcast(self, obj: dict) -> None:
        for c in list(self.clients):
            self.write(c, obj)

    def hello(self) -> dict:
        return {"type": "link.hello", "version": VERSION, "session": self.s.id, "name": self.name, "origin": self.origin,
                "auto": self.auto, "pid": os.getpid(), "title": self.s.title, "status": self.status_fields(),
                "busy": self.sent_busy[0], "seq": self.seq}

    def status_fields(self) -> dict:
        s = self.s
        return {"model": s.model, "effort": s.effort, "mode": s.mode, "account": s.account, "project": s.project or None}

    # ------------------------------------------------------------------ events out
    def emit(self, type_: str, **fields: Any) -> dict | None:
        """Send one event to every client with the next `seq`.  Safe from another thread (it hops to the loop); then the
        event is not returned."""
        if self.closed:
            return None
        if threading.get_ident() != self.thread:
            self.loop.call_soon_threadsafe(lambda: self.emit(type_, **fields))
            return None
        self.seq += 1
        obj = {"type": type_, "session": self.s.id, "seq": self.seq, **fields}
        self.broadcast(obj)
        return obj

    def events(self, ev: Any, bg: bool = False) -> bool:
        """A loop event of the chat (or, with `bg`, of a background sub-agent) -> protocol events, sent.  True when it was an
        error."""
        got = (self.bg_tr if bg else self.tr).event(ev, self.turn_id)
        for e in got:
            self.emit(e.pop("type"), **{k: v for k, v in e.items() if k != "session"})
        return protocol.is_error(got)

    def raw(self, events: list[dict]) -> None:
        """Events a caller built itself (`protocol.sub_messages`): sent as they are, with a `seq`."""
        for e in events:
            e = dict(e)
            self.emit(e.pop("type"), **{k: v for k, v in e.items() if k != "session"})

    def turn_start(self) -> str:
        self.turn_n += 1
        self.turn_id = f"t{self.turn_n}"
        self.emit("turn.start", turn=self.turn_id)
        return self.turn_id

    def turn_end(self, status: str, cost: float, context: float) -> None:
        """`status`: done | cancelled | error.  `cost` of this turn in dollars, `context` the share of the window used (0 to 1)."""
        self.emit("turn.end", turn=self.turn_id, status=status, cost_usd=round(max(cost, 0.0), 6),
                  context_used=round(min(max(context, 0.0), 1.0), 4))
        self.turn_id = ""

    def user_text(self, text: str, source: str = "") -> None:
        """A line of the user, typed here (`source` empty: `tui`) or sent from a device: every device shows it."""
        self.emit("text", turn=self.turn_id, text=text, delta=False, role="user", **{"from": source or "tui"})

    def sync(self, busy: bool, queued: int) -> None:
        """Send `status` and `busy` when they differ from what the clients were told last."""
        status = self.status_fields()
        if self.sent_status is not None and status != self.sent_status:
            self.emit("status", **status)
        self.sent_status = status
        if (busy, queued) != self.sent_busy:
            self.sent_busy = (busy, queued)
            self.emit("busy", busy=busy, queued=queued)

    def title(self, title: str, description: str = "") -> None:
        self.emit("session.title", title=title, description=description, name=self.name)

    def notice(self, text: str, level: str = "info") -> None:
        self.emit("notice", level=level, text=text)

    # ------------------------------------------------------------------ questions
    async def ask(self, kind: str, text: str, detail: str, choices: list[str], default: str,
                  local: Awaitable[str] | None = None) -> str:
        """Ask a question on the clients and, when `local` is given (the modal on the keyboard), there too: the first
        answer wins, the other side is cancelled, and `question.closed` says who won (`by`).  `kind` is `permission` (the
        answer is one of `choices`) or `ask` (free text).  No answer within `run.ask_timeout` gives `default`.  An answer
        from the keyboard that raises (its own timeout) raises here."""
        self.q_n += 1
        qid = f"q{self.q_n}"
        fut: asyncio.Future = self.loop.create_future()
        extra = {"detail": detail} if detail else {}
        event = self.emit("question", question=qid, kind=kind, text=text, choices=choices, **extra) or {}
        self.questions[qid] = _Question(fut, kind, choices, event)
        loc = asyncio.ensure_future(local) if local is not None else None
        choice, by = "cancelled", ""
        try:
            timeout = core.duration((core.settings(self.home).get("run") or {}).get("ask_timeout"), ASK_TIMEOUT)
            end = time.monotonic() + timeout
            waiting = {fut} | ({loc} if loc is not None else set())
            while True:
                done, _ = await asyncio.wait(waiting, timeout=max(end - time.monotonic(), 0.0),
                                             return_when=asyncio.FIRST_COMPLETED)
                if loc is not None and loc in done:
                    by, choice = "tui", loc.result()        # (an error of the keyboard side, e.g. its own timeout, is raised)
                elif fut in done and fut.result()[1] != CLOSED:
                    choice, by = fut.result()
                elif fut in done:                           # the link ended: the keyboard (if any) is the only one left
                    waiting.discard(fut)
                    if loc is not None:
                        continue
                    choice, by = default, CLOSED
                else:
                    choice, by = default, "timeout"
                return choice
        finally:
            if loc is not None and not loc.done():
                loc.cancel()
                await asyncio.gather(loc, return_exceptions=True)
            fut.cancel()
            self.questions.pop(qid, None)
            self.answered[qid] = choice
            while len(self.answered) > 200:
                self.answered.pop(next(iter(self.answered)))
            self.emit("question.closed", question=qid, choice=choice, by=by)

    def answer(self, msg: dict) -> dict:
        """`answer {question, choice, from}`: the first one wins; a permission question takes yes, always or no only."""
        qid, choice = msg.get("question"), msg.get("choice")
        if not isinstance(qid, str) or not isinstance(choice, str):
            raise Refused("answer: `question` and `choice` are strings")
        q = self.questions.get(qid)
        if q is None or q.fut.done():
            raise Refused("already answered" if qid in self.answered else f"no open question {qid!r}")
        if q.kind == "permission" and choice not in q.choices:
            raise Refused(f"a permission question is answered with one of {', '.join(q.choices)}")
        q.fut.set_result((choice, clean_device(msg.get("from"))))
        return {}

    # ------------------------------------------------------------------ requests in
    async def work(self) -> None:
        """The requests, one after the other, in the order they came: one writer for the chat."""
        while not self.closed:
            client, line = await self.inbox.get()
            rid, msg = None, None
            try:
                msg = json.loads(line.decode("utf-8"))
                if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                    raise Refused("a request is a JSON object with a `type`")
                rid = msg.get("id") if isinstance(msg.get("id"), (str, int)) else None
                msg["from"] = clean_device(msg.get("from"))
                data = await self.dispatch(msg)
                reply: dict = {"type": "result", "id": rid, "ok": True, "data": data or {}}
            except Refused as e:
                reply = {"type": "result", "id": rid, "ok": False, "error": str(e)}
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001 - the link stays up, the request says why not
                reply = {"type": "result", "id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
            self.write(client, reply)
            if msg is not None and msg.get("type") == "rc.off" and reply["ok"]:
                asyncio.create_task(self.close("off"))

    async def dispatch(self, msg: dict) -> dict | None:
        t = msg["type"]
        if t == "ping":
            return {"pong": True}
        if t == "rc.off":
            return {}
        if t == "answer":
            return self.answer(msg)
        if t == "history":
            return {"history": await asyncio.to_thread(protocol.history, self.s, self.home), "seq": self.seq}
        if self.handler is None or t not in ("send", "cancel", "session.set", "session.archive"):
            raise Refused(f"unknown type {t[:60]!r}")
        return await self.handler(msg)

    # ------------------------------------------------------------------ end
    async def close(self, reason: str = "off") -> None:
        """End the link: tell the clients, close the socket, remove the files.  Open questions are cancelled (the ask goes on
        at the keyboard).  Calling it again does nothing."""
        if self.closed:
            return
        self.emit("link.closed", reason=reason)
        self.closed = True
        for q in list(self.questions.values()):
            if not q.fut.done():
                q.fut.set_result(("", CLOSED))
        await self.shut_server()
        me = asyncio.current_task()
        for c in list(self.clients):
            with suppress(Exception):
                c.writer.close()
        for c in list(self.clients):
            if c.task is not None and c.task is not me:
                c.task.cancel()
        self.clients.clear()
        if self.worker is not None and self.worker is not me:
            self.worker.cancel()
        cleanup_files(self.home, self.s.id)
        if self.on_close is not None:
            with suppress(Exception):
                self.on_close(reason)
