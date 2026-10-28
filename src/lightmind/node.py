"""The node of Remote Control (`lm node`): one process per machine that lets the gateway of the phone app see and steer the
chats of this machine.

Towards the gateway it speaks node protocol 4.2 (one JSON object per line, tools/remote/PROTOCOL.md) over stdio, or over
the ssh channel of an uplink (laptop).  Towards a chat it is the client of that chat's link (4.1, `rc.py`): a unix socket
(Windows: localhost plus token) listed in `<state>/remote/links/`.  The node never writes into a chat itself: sending,
answering, cancelling and archiving an open chat go through its link, so the one process that holds the chat stays its
only writer.  Chats nobody holds are read from disk (history) or changed on disk (archive marker); new chats are created
here and opened as a TUI in tmux (`lm open <id> --rc <name>`), exactly like a chat typed at a terminal.

The gateway's requests are answered concurrently (a transcript may take minutes, pings must still be answered).
May import: everything except tui.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Callable

from . import __version__, core, launch, offer, protocol, remote, runs, settings, upload
from .files import Home
from .model import Message, ToolCall
from .tools import secret_masker

VERSION = 1                                     # node protocol version (the gateway ends the connection on another one)
LINE_MAX = 1 << 20                              # one inbound line: an upload chunk is 256 KiB raw, about 350 KB as base64
TEXT_MAX = 64 * 1024                            # `send` text, as in serve.LINE_MAX and the gateway
POLL_S = 1.0                                    # how often the links folder is read
HELLO_WAIT_S = 5.0                              # a link must say `link.hello` this fast
NEW_LINK_WAIT_S = 20.0                          # a chat opened in tmux must show its link this fast
LINK_ASK_S = 20.0                               # a link must answer a request this fast
UPLINK_IDLE_S = 600                             # the uplink ends this long after the last link
DRAIN_S = 5.0                                   # requests already read are answered this long after the gateway closed its end
LIST_LIMIT = 50
LIST_LIMIT_MAX = 200
UPLOADS_OPEN_MAX = 8
MEDIA_TIMEOUT_S = 900
EXIT_BUSY = 3                                   # another node of this tree is running
SESSION_NAME = remote.SESSION                   # the tmux session the chats open in
LOOPBACK = ("127.0.0.1", "localhost", "::1")    # the only hosts a link file may name (it carries a token)
DEFAULTS = {"enabled": False, "gateway": "", "default_node": "", "auto": True, "agent": "", "model": "", "effort": "",
            "mode": "", "push": "webpush", "push_questions": True, "upload_mb": 200}
FORBIDDEN = {"upload", "datei", "settings", "config", "login", "key", "accounts", "resume", "server", "clear", "new", "quit",
             "exit", "rc", "remote-control"}                    # slash words that need a screen or leave the chat
AUDIO_TYPES = ("mp3", "m4a", "wav", "ogg", "oga", "opus", "aac", "flac", "weba", "amr", "caf")
VIDEO_TYPES = ("mp4", "mov", "mkv", "avi", "webm", "m4v", "3gp")
DOC_TYPES = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "epub")
CONVERT_TYPES = ("heic", "heif", "tif", "tiff")                 # images the model cannot read as they are: media.py makes a PNG
SECRET_ENDS = ("_KEY", "_TOKEN", "_SECRET", "_PASS", "_PASSWORD")
RUN_VARIABLES = ("LM_DEPTH", "LM_RUN", "LM_RESULTS", "LM_PARENT")             # a child started for a person must not look like an agent's run
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BAD_FILE_CHARS = re.compile(r'[<>:"|?*]')


class Refuse(Exception):
    """A request that cannot be done; the text goes to the gateway as `error` (it is meant for the person)."""


class Busy(RuntimeError):
    """Another node of this tree runs; `pid` is its process."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"another node is running in process {pid}")
        self.pid = pid


# ---------------------------------------------------------------------------------------------- places and settings
def state_dir(home: Home) -> Path:
    return home.path("state") / "remote"


def links_dir(home: Home) -> Path:
    return state_dir(home) / "links"


def gateway_socket(home: Home) -> Path:
    return state_dir(home) / "gateway.sock"


def truthy(value: Any) -> bool:
    """A yes in a config or info.md: `true`, or the words people write for it."""
    return value is True or str(value).strip().lower() in ("true", "yes", "on", "1", "an", "ja")


def remote_conf(home: Home) -> dict[str, Any]:
    section = home.settings.get("remote")
    return section if isinstance(section, dict) else {}


def remote_on(home: Home) -> bool:
    return truthy(remote_conf(home).get("enabled"))


def remote_value(home: Home, key: str) -> Any:
    """`remote.<key>` of config.md, else its default (empty = the `defaults:` of the tree apply)."""
    value = remote_conf(home).get(key)
    return DEFAULTS[key] if value in (None, "") else value


def machine_name(home: Home) -> str:
    """The name the gateway knows this machine by: the instance name, else the server card of this machine, else the host."""
    if home.machine:
        return home.machine
    for name in home.list_servers():
        with contextlib.suppress(Exception):
            if home.load_server(name).local:
                return name
    return socket.gethostname().split(".")[0].lower()


def modes_of(home: Home) -> dict[str, dict]:
    """The permission modes of the tree; none when permissions.md cannot be read (the choices are then empty)."""
    try:
        return home.modes()
    except ValueError:
        return {}


def alive(pid: int, stamp: str) -> bool:
    """Is that still the process that wrote the file (alive, and not another one with the same number)?"""
    return runs.pid_alive(pid) and (not stamp or runs.process_stamp(pid) in ("", stamp))


# ---------------------------------------------------------------------------------------------- one node per tree
def take_lock(home: Home) -> Path:
    """Write `<state>/remote/node.lock` (pid and start stamp).  A lock of a dead process is taken over; `Busy` when a live one
    holds it.  The file is created exclusively, so two nodes starting together cannot both win."""
    path = state_dir(home) / "node.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pid, stamp = read_lock(path)
            if pid and alive(pid, stamp):
                raise Busy(pid) from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {runs.process_stamp(os.getpid())}\n")
        return path
    raise Busy(0)


def read_lock(path: Path) -> tuple[int, str]:
    try:
        parts = path.read_text("utf-8").split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return 0, ""


def drop_lock(path: Path) -> None:
    """Remove the lock when it is ours (never another node's)."""
    if read_lock(path)[0] == os.getpid():
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------- targets for the gateway
def targets(home: Home) -> list[dict[str, Any]]:
    """`lm node targets --json`: one entry per `servers/<name>/info.md` that has an `lm_home` or is this machine.  `argv` is the
    whole command line that starts the node there (the gateway runs it as given, no shell); empty for a machine that comes
    in by itself (`transport: uplink`, the laptop) or whose ssh data is unusable (`note` says why).  `enabled` is the
    `remote:` key of info.md (missing = off), `default` is `remote.default_node`."""
    default = str(remote_value(home, "default_node"))
    out: list[dict[str, Any]] = []
    for name in sorted(home.list_servers()):
        try:
            server = home.load_server(name)
        except Exception:                                   # noqa: BLE001 - one broken card hides only itself
            continue
        if not (server.lm_home or server.local):
            continue
        enabled = truthy(server.extra.get("remote"))
        entry: dict[str, Any] = {"name": name, "machine": server.machine or name, "enabled": enabled, "default": name == default,
                                 "transport": "local" if server.local else "uplink", "argv": [], "role": list(server.role)}
        if server.local:
            entry["argv"] = [str(launch.launcher(home.root, sys.platform)), "node", "--stdio"]
        elif server.ssh or server.host.strip():
            entry["transport"] = "ssh"
            try:
                destination = remote.pick(server) if enabled else remote.target(server)
                entry["argv"] = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20", *destination,
                                 f"{remote.tree_path(server)}/system/lm node --stdio"]
            except remote.RemoteError as err:
                entry["note"] = str(err)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------------------------- the chats' links
@dataclass
class Link:
    """The node's end of one chat's link: the stream, the requests waiting for an answer, what `link.hello` said."""
    sid: str
    name: str
    origin: str
    pid: int
    stamp: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    hello: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    ids: Any = field(default_factory=lambda: itertools.count(1))
    task: asyncio.Task | None = None

    async def ask(self, kind: str, timeout: float = LINK_ASK_S, **fields: Any) -> dict[str, Any]:
        """Send a request into the chat and wait for its `result`; `Refuse` with the chat's reason when it says no."""
        request_id = f"n{next(self.ids)}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.writer.write(encode({"type": kind, "id": request_id, **fields}))
            await self.writer.drain()
            answer = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise Refuse("the chat did not answer in time") from None
        except (ConnectionError, OSError):
            raise Refuse("the link to the chat is closed") from None
        finally:
            self.pending.pop(request_id, None)
        if not answer.get("ok"):
            raise Refuse(str(answer.get("error") or "the chat refused"))
        data = answer.get("data")
        return data if isinstance(data, dict) else {}

    def fail_pending(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("link closed"))
        self.pending.clear()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()
        if self.task is not None:
            self.task.cancel()


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------------------------- history of a chat nobody holds
class DiskChat:
    """The parts of a `loop.Session` that `protocol.history` reads, built from `events.jsonl` without taking the chat (taking
    it would change its status and lock).  Nothing is written."""

    def __init__(self, home: Home, sid: str) -> None:
        from . import subagents
        from .providers.base import SubagentEnd
        events = read_events(home.path("runs") / sid / "events.jsonl")
        self.id, self.home = sid, home
        self.messages: list[Message] = []
        for event in events:
            if event.get("kind") == "reset":
                self.messages.clear()
            elif event.get("kind") == "message":
                data = event["data"]
                self.messages.append(Message(data["role"], data.get("text", ""), [ToolCall(**c) for c in data.get("tool_calls", [])],
                                             data.get("tool_call_id", ""), str(data.get("origin") or "")))
        self.subs = subagents.from_events(events)
        for sub in self.subs.subs.values():
            if not sub.done:
                self.subs.feed(SubagentEnd(sub.id, False, "interrupted: the chat was not open"))
        self.dispatcher = SimpleNamespace(masker=secret_masker(home))

    def first_words(self) -> str:
        """The first words of the first message of the person, for a chat that has no title yet."""
        for message in self.messages:
            if message.role == "user" and not message.origin:
                return " ".join(core.clean_user(message.text).split())[:60]
        return ""


def read_events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------------------------- uploads
@dataclass
class Upload:
    """A file on its way: written to `<target>.part`, renamed to `target` when the last chunk is in."""
    sid: str
    kind: str
    mime: str
    target: Path
    part: Path
    handle: BinaryIO
    limit: int
    received: int = 0
    next_seq: int = 0


def clean_filename(name: Any, kind: str = "file") -> str:
    """The base name of an uploaded file: no folders (either slash), no control or Windows-invalid characters, no leading dots
    or spaces, at most 120 characters with the ending kept.  Nothing left: `upload` (`voice` for a recording)."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = _BAD_FILE_CHARS.sub("_", _CONTROL.sub("", base)).strip().lstrip(". ")
    if len(base) > 120:
        stem, ext = os.path.splitext(base)
        base = stem[:max(1, 120 - len(ext))] + ext[:20]
    return base if base and base not in (".", "..") else ("voice" if kind == "voice" else "upload")


def classify(name: str, mime: str, kind: str) -> str:
    """`image`, `audio`, `video`, `doc` or `file`: what `tools/media` is asked to do with an upload.  A recording is audio
    whatever its container says (iOS makes mp4, Chrome webm)."""
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    mime = (mime or "").lower()
    if kind == "voice" or mime.startswith("audio/") or ext in AUDIO_TYPES:
        return "audio"
    if mime.startswith("image/") or ext in upload.IMAGE_TYPES:
        return "image"
    if mime.startswith("video/") or ext in VIDEO_TYPES:
        return "video"
    return "doc" if ext in DOC_TYPES else "file"


def media_environment(home: Home) -> dict[str, str]:
    """The environment of a media call: ours without anything that looks like a credential, plus exactly the ElevenLabs
    secret of the tree (injected here, never on the command line)."""
    from . import secrets
    base = {k: v for k, v in os.environ.items() if not k.upper().endswith(SECRET_ENDS) and k not in RUN_VARIABLES}
    return secrets.build_tool_env(home, ["elevenlabs/default"], lambda ref: True, base_env=base).env


def run_media(home: Home, mode: str, file: Path, folder: Path) -> dict[str, Any]:
    """`tools/media/media.py <mode> <file> --out <folder> --json`; its JSON answer, or `{"error": ...}`."""
    uv = home.root / "system" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    command = [str(uv) if uv.is_file() else (shutil.which("uv") or "uv"), "run", "--quiet",
               str(home.root / "tools" / "media" / "media.py"), mode, str(file), "--out", str(folder), "--json"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=MEDIA_TIMEOUT_S, env=media_environment(home), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": f"processing took more than {MEDIA_TIMEOUT_S // 60} minutes"}
    except OSError as err:
        return {"error": f"media could not be started: {err}"}
    if done.returncode != 0:
        return {"error": " | ".join((done.stderr or done.stdout).strip().splitlines()[-3:]) or "media failed"}
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "media returned no JSON"}
    return answer if isinstance(answer, dict) else {"error": "media returned no JSON object"}


def transcript_of(result: dict[str, Any]) -> tuple[str, str]:
    """`(text, problem)` from the `transcript` part of a media answer: the spoken text, or why there is none."""
    part = result.get("transcript")
    if not isinstance(part, dict):
        return "", str(result.get("error") or "")
    return str(part.get("text") or "").strip(), str(part.get("error") or "")


def process_upload(home: Home, target: Path, kind: str, mime: str, media: Callable[..., dict] = run_media) -> dict[str, Any]:
    """The `upload.end` answer for a finished file: `{path, marker, kind, media, transcript?, note?}`.  `marker` is the input
    line(s) for the chat (`[Anhang: ...]`, the contract with the model).  An image is only marked, a HEIC or TIFF is made a
    PNG first; a recording or video gets a transcript; a document its text.  `media` is the function that runs the tool
    (replaced in tests)."""
    what = classify(target.name, mime, kind)
    answer: dict[str, Any] = {"path": upload.marker_path(target, home.root), "kind": kind, "media": what,
                              "marker": upload.marker(target, home.root)}
    folder = target.parent / f"{target.stem}-{what}"
    ext = target.suffix.lower().lstrip(".")
    if what == "image" and ext in CONVERT_TYPES:
        result = media(home, "image", target, folder)
        png = Path(str(result.get("image") or ""))
        if result.get("error") or not png.is_file():
            answer["note"] = f"converting the picture failed: {result.get('error') or 'no image made'}"
        else:
            answer["marker"] = upload.marker(png, home.root)
    elif what in ("audio", "video"):
        result = media(home, what, target, folder)
        text, problem = transcript_of(result)
        if text:
            answer["transcript"] = text
        if problem or result.get("error"):
            answer["note"] = f"no transcript: {problem or result.get('error')}"
        report = Path(str(result.get("report") or ""))
        if what == "video" and report.is_file():
            answer["marker"] = upload.marker(report, home.root)
    elif what == "doc":
        result = media(home, "doc", target, folder)
        text_file = Path(str(result.get("report") or ""))
        if result.get("error") or not text_file.is_file():
            answer["note"] = f"reading the document failed: {result.get('error') or 'no text made'}"
        else:
            answer["marker"] = upload.marker(text_file, home.root) + "\n" + answer["marker"]
    return answer


# ---------------------------------------------------------------------------------------------- tmux
def tmux_exe() -> str:
    """The tmux to use (`LM_TMUX` overrides, for tests); '' when there is none."""
    return os.environ.get("LM_TMUX") or shutil.which("tmux") or ""


def child_environment() -> dict[str, str]:
    """The environment for tmux (whose server every window inherits): without the variables of an agent's run."""
    return {k: v for k, v in os.environ.items() if k not in RUN_VARIABLES}


def on_windows() -> bool:
    """Whether this is Windows (one place, so tests can switch it without breaking pathlib)."""
    return os.name == "nt"


def open_console(home: Home, sid: str, name: str) -> None:
    """Windows has no tmux: start `lm.cmd open <sid> --rc <name>` in a new, visible console window.  A console is the
    counterpart of a tmux window (the chat needs a terminal, and it lives on after this request and after the node).
    Argument list, no shell: `sid` and `name` were checked (`launch.SAFE`).  RuntimeError when the launcher is missing."""
    lm = launch.launcher(home.root, "win32")
    if not lm.is_file():
        raise RuntimeError(f"{lm} is missing: the launcher of this tree is not installed")
    subprocess.Popen([str(lm), "open", sid, "--rc", name], env=child_environment(), cwd=str(home.root),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]  # Windows only


def can_start(home: Home) -> bool:
    """Whether this machine can start a chat for the phone: tmux on Linux/macOS, the lm.cmd launcher on Windows."""
    if on_windows():
        return launch.launcher(home.root, "win32").is_file()
    return bool(tmux_exe()) and os.name == "posix"


def start_chat(home: Home, sid: str, name: str) -> None:
    """Start chat `sid` the way this operating system does it (console window on Windows, tmux window elsewhere)."""
    (open_console if on_windows() else open_window)(home, sid, name)


def open_window(home: Home, sid: str, name: str) -> None:
    """Open chat `sid` as `lm open <sid> --rc <name>` in a new window of the tmux session `lm` (created with the Orbit menu
    when it is missing).  Argument lists only: the parts are ids and names that were checked (`launch.SAFE`), never a shell string
    with someone's text.  RuntimeError with tmux's own words when it fails."""
    tmux, lm = tmux_exe(), str(launch.launcher(home.root, sys.platform))
    if not tmux:
        raise RuntimeError("tmux is not installed on this machine")
    env = child_environment()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([tmux, *args], capture_output=True, text=True, timeout=20, env=env, stdin=subprocess.DEVNULL)

    if run("has-session", "-t", SESSION_NAME).returncode != 0:
        made = run("new-session", "-d", "-s", SESSION_NAME, lm)
        if made.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {made.stderr.strip() or made.returncode}")
    opened = run("new-window", "-d", "-t", f"{SESSION_NAME}:", "-n", name, lm, "open", sid, "--rc", name)
    if opened.returncode != 0:
        raise RuntimeError(f"tmux new-window failed: {opened.stderr.strip() or opened.returncode}")


def window_name(home: Home, sid: str) -> str:
    """A name for the window and for `--rc`: the session's own name, else `phone-` and the end of its id."""
    name = runs.session_names(home).get(sid, "")
    return name if launch.SAFE.fullmatch(name) else "phone-" + re.sub(r"[^A-Za-z0-9]", "", sid)[-4:]


# ---------------------------------------------------------------------------------------------- the node
class Node:
    """The node's state and its answers.  `write(bytes)` sends a line to the gateway (called from the event loop only; the
    caller makes it safe for its channel).  Run `await node.run(inbox)`."""

    def __init__(self, home: Home, write: Callable[[bytes], None], name: str = "", idle_exit_s: float = 0.0) -> None:
        self.home, self.write = home, write
        self.name = name or machine_name(home)
        self.links: dict[str, Link] = {}
        self.uploads: dict[str, Upload] = {}
        self.tasks: set[asyncio.Task] = set()
        self.retry: dict[str, tuple[int, float]] = {}        # sid -> (failures, not before) for a link that does not answer
        self.idle_exit_s, self.idle_since = idle_exit_s, time.monotonic()
        self.stop: asyncio.Event | None = None               # set in run(): the uplink ends when it is idle
        self.scanning = asyncio.Lock()                       # the poll and a waiting `session.new` must not both connect
        self.requests = {
            "ping": self.do_ping, "sessions.list": self.do_sessions_list, "session.open": self.do_session_open,
            "send": self.do_send, "answer": self.do_answer, "cancel": self.do_cancel, "session.set": self.do_session_set,
            "session.archive": self.do_session_archive, "session.unarchive": self.do_session_unarchive,
            "session.new": self.do_session_new, "session.resume": self.do_session_resume, "options": self.do_options,
            "commands": self.do_commands, "settings.get": self.do_settings_get, "settings.set": self.do_settings_set,
            "upload.begin": self.do_upload_begin, "upload.chunk": self.do_upload_chunk, "upload.end": self.do_upload_end,
            "upload.abort": self.do_upload_abort, "file.get": self.do_file_get,
        }

    # ------------------------------------------------------------------ the gateway side
    def emit(self, message: dict[str, Any]) -> None:
        self.write(encode(message))

    def hello(self) -> dict[str, Any]:
        tmux = bool(tmux_exe())
        return {"type": "node.hello", "version": VERSION, "node": self.name, "machine": self.home.machine or self.name,
                "lm": __version__, "tree": str(self.home.root), "platform": sys.platform,
                "can_start": can_start(self.home), "tmux": tmux}

    def changed(self) -> None:
        self.emit({"type": "sessions.changed"})

    async def run(self, inbox: "asyncio.Queue[bytes | None]") -> int:
        """Say hello, then read the links and answer the gateway until its stream ends (or the uplink has been idle)."""
        self.stop = asyncio.Event()
        self.emit(self.hello())
        watcher = asyncio.create_task(self.poll())
        reader = asyncio.create_task(self.read_requests(inbox))
        idle = asyncio.create_task(self.stop.wait())
        try:
            await asyncio.wait([idle, reader], return_when=asyncio.FIRST_COMPLETED)
            if reader.done() and self.tasks:                # the gateway closed its end: answer what it already asked
                await asyncio.wait(list(self.tasks), timeout=DRAIN_S)
        finally:
            running = [watcher, reader, idle, *self.tasks]
            for task in running:
                task.cancel()
            for link in list(self.links.values()):
                link.close()                                # the chats keep running; only our end goes
            for pending in list(self.uploads.values()):
                self.discard_upload(pending)
            await asyncio.gather(*running, return_exceptions=True)
        return 0

    async def read_requests(self, inbox: "asyncio.Queue[bytes | None]") -> None:
        while (line := await inbox.get()) is not None:
            task = asyncio.create_task(self.handle(line))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def handle(self, line: bytes) -> None:
        """One request line: run its handler and send the `result` (errors become `ok:false` with the reason)."""
        try:
            message = json.loads(line)
        except ValueError:
            return self.emit({"type": "error", "error": "not JSON"})
        if not isinstance(message, dict):
            return self.emit({"type": "error", "error": "not a JSON object"})
        kind, request_id = str(message.get("type") or ""), message.get("id")
        handler = self.requests.get(kind)
        if handler is None:
            if request_id is None:
                return self.emit({"type": "error", "error": f"unknown type {kind!r}"})
            return self.reply(request_id, error=f"unknown type {kind!r}")
        if kind not in ("ping", "settings.get", "settings.set") and not remote_on(self.home):
            return self.reply(request_id, error="Remote Control is off in config.md (remote.enabled)")
        try:
            data = await handler(message)
        except Refuse as err:
            return self.reply(request_id, error=str(err))
        except asyncio.CancelledError:
            raise
        except Exception as err:                            # noqa: BLE001 - a bug in one request must not end the node
            print(f"lm node: {kind}: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
            return self.reply(request_id, error=f"{type(err).__name__}: {err}")
        if kind != "ping":
            self.reply(request_id, data=data)

    def reply(self, request_id: Any, data: dict[str, Any] | None = None, error: str = "") -> None:
        answer: dict[str, Any] = {"type": "result", "id": request_id, "ok": not error}
        answer.update({"error": error} if error else {"data": data or {}})
        self.emit(answer)

    async def do_ping(self, message: dict) -> dict:
        self.emit({"type": "pong", "id": message.get("id")})
        return {}

    # ------------------------------------------------------------------ links
    async def poll(self) -> None:
        while True:
            try:
                await self.scan()
            except Exception as err:                        # noqa: BLE001 - the watch goes on
                print(f"lm node: links: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
            if self.links or not self.idle_exit_s:
                self.idle_since = time.monotonic()
            elif time.monotonic() - self.idle_since > self.idle_exit_s and self.stop is not None:
                self.stop.set()
            await asyncio.sleep(POLL_S)

    async def scan(self) -> None:
        """Read the links folder once: connect to a new link, drop the files of a dead one, forget a link that is gone."""
        async with self.scanning:
            await self.scan_once()

    async def scan_once(self) -> None:
        for sid, link in list(self.links.items()):
            if not alive(link.pid, link.stamp):
                self.lose(link, "gone")
                self.remove_files(sid)
        folder = links_dir(self.home)
        if not remote_on(self.home) or not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            sid, info = path.stem, read_json(path)
            pid, stamp = int(info.get("pid") or 0), str(info.get("stamp") or "")
            if not launch.SAFE.fullmatch(sid) or info.get("session", sid) != sid:
                continue
            if not alive(pid, stamp):
                self.remove_files(sid)
                continue
            failures, not_before = self.retry.get(sid, (0, 0.0))
            if sid in self.links or time.monotonic() < not_before:
                continue
            try:
                await self.connect(sid, info, pid, stamp)
            except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
                failures += 1
                self.retry[sid] = (failures, time.monotonic() + min(30.0, 1.5 ** failures))
                if failures == 1:
                    print(f"lm node: link {sid}: {type(err).__name__}: {err}", file=sys.stderr, flush=True)

    async def connect(self, sid: str, info: dict[str, Any], pid: int, stamp: str) -> None:
        """Open the link of chat `sid` (unix socket next to its info file; with a token in the info file: localhost TCP and
        the token as first line), wait for `link.hello` and announce `rc.on`."""
        token = info.get("token")
        if token:
            host, _, port = str(info.get("endpoint") or "").removeprefix("tcp://").rpartition(":")
            if host not in LOOPBACK or not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError("a link with a token must name a localhost port")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port), limit=LINE_MAX), HELLO_WAIT_S)
            writer.write(encode({"type": "auth", "token": str(token)}))
            await writer.drain()
        else:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(links_dir(self.home) / f"{sid}.sock"),
                                                                                 limit=LINE_MAX), HELLO_WAIT_S)
        try:
            first = json.loads(await asyncio.wait_for(reader.readline(), HELLO_WAIT_S))
            if not isinstance(first, dict) or first.get("type") != "link.hello":
                raise ValueError("the link did not say hello")
        except BaseException:
            writer.close()
            raise
        name = str(first.get("name") or info.get("name") or sid)
        origin = str(first.get("origin") or info.get("origin") or "tui")
        link = Link(sid, name, origin, pid, stamp, reader, writer, hello=first)
        link.task = asyncio.create_task(self.read_link(link))
        self.links[sid] = link
        self.retry.pop(sid, None)
        auto = first.get("auto") is True or info.get("auto") is True      # opened by remote.auto: the gateway must not push
        self.emit({"type": "rc.on", "session": sid, "name": name, "origin": origin, **({"auto": True} if auto else {})})
        self.changed()

    async def read_link(self, link: Link) -> None:
        """Events of a chat go to the gateway with `node` and `session`; answers go to the request that waits for them."""
        try:
            while line := await link.reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                kind = message.get("type") if isinstance(message, dict) else None
                if kind == "result":
                    future = link.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif kind and kind not in ("link.hello", "pong"):
                    self.emit({**message, "node": self.name, "session": link.sid})
        except (OSError, asyncio.IncompleteReadError, ValueError):
            pass
        if self.links.get(link.sid) is link:                # not reached when the node itself is ending (cancelled above)
            self.lose(link, "closed")

    def lose(self, link: Link, reason: str) -> None:
        """A link ended: tell the gateway once; when its process is gone, its files go too."""
        if self.links.get(link.sid) is link:
            del self.links[link.sid]
            self.emit({"type": "rc.off", "session": link.sid, "reason": reason})
            self.changed()
        link.fail_pending()
        link.close()
        if not alive(link.pid, link.stamp):
            self.remove_files(link.sid)

    def remove_files(self, sid: str) -> None:
        for suffix in (".json", ".sock"):
            with contextlib.suppress(OSError):
                (links_dir(self.home) / f"{sid}{suffix}").unlink()
        self.retry.pop(sid, None)

    async def wait_link(self, sid: str, seconds: float) -> Link:
        """The link of a chat that was just opened in tmux; Refuse when it does not appear in time."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await self.scan()
            if sid in self.links:
                return self.links[sid]
            await asyncio.sleep(0.25)
        raise Refuse(f"the chat {sid} was opened in tmux but showed no Remote Control link within {int(seconds)} s "
                     f"(look at it with `lm remote`)")

    # ------------------------------------------------------------------ sessions
    def known(self, message: dict) -> str:
        """The id of a chat of this machine named in a request (exact id, checked: it becomes part of a path)."""
        sid = message.get("session")
        if not isinstance(sid, str) or not launch.SAFE.fullmatch(sid) or not (self.home.path("runs") / sid / "meta.json").is_file():
            raise Refuse("unknown session")
        return sid

    def linked(self, message: dict) -> Link:
        """The link of the chat a request names, or Refuse when nobody has it open with Remote Control."""
        sid = self.known(message)
        link = self.links.get(sid)
        if link is None:
            raise Refuse("this chat is not open with Remote Control; it can only be read (continue it first)")
        return link

    def status_of(self, sid: str, disk_status: str) -> str:
        """`rc` (a link), `running` (open elsewhere, no link), `paused` (waiting or died), `done`."""
        if sid in self.links:
            return "rc"
        if runs.lock_holder(self.home, sid):
            return "running"
        return "paused" if disk_status in ("running", "paused") else "done"

    def row(self, summary: runs.SessionSummary, names: dict[str, str]) -> dict[str, Any]:
        link = self.links.get(summary.id)
        row = {"id": summary.id, "name": names.get(summary.id, ""), "title": summary.title, "description": summary.description,
               "agent": summary.agent, "project": summary.project, "status": self.status_of(summary.id, summary.status),
               "archived": summary.archived, "last_active": summary.last_active, "model": summary.model,
               "origin": summary.origin, "parent": summary.parent}
        if link is not None:
            row["rc_name"] = link.name
        return row

    async def do_sessions_list(self, message: dict) -> dict:
        limit = message.get("limit")
        limit = min(max(int(limit), 1), LIST_LIMIT_MAX) if isinstance(limit, int) and not isinstance(limit, bool) else LIST_LIMIT
        wanted = bool(message.get("archived"))

        def read() -> list[dict[str, Any]]:
            names, hide = runs.session_names(self.home), secret_masker(self.home)
            return [protocol.mask(self.row(s, names), hide) for s in runs.list_session_groups(self.home, limit, names, archived=wanted)]

        return {"sessions": await asyncio.to_thread(read)}

    async def do_session_open(self, message: dict) -> dict:
        sid = self.known(message)
        link = self.links.get(sid)
        names = runs.session_names(self.home)

        def one(chat: DiskChat | None) -> dict[str, Any]:
            """The row of this chat for the list; masked like everything else that leaves the machine (a title is a user's words)."""
            meta = read_json(self.home.path("runs") / sid / "meta.json")
            title, description = runs.read_names(self.home, sid)
            events = self.home.path("runs") / sid / "events.jsonl"
            stamp = events.stat().st_mtime if events.is_file() else 0
            return protocol.mask({"id": sid, "name": names.get(sid, ""), "title": " ".join((title or (chat.first_words() if chat else "")).split())[:60],
                    "description": description, "agent": str(meta.get("agent") or ""), "project": str(meta.get("project") or ""),
                    "status": self.status_of(sid, str(meta.get("status") or "")),
                    "archived": runs.is_archived(self.home.path("runs") / sid),
                    "last_active": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stamp)) if stamp else "",
                    "model": str(meta.get("model") or ""), "origin": str(meta.get("origin") or ""),
                    "parent": str(meta.get("parent") or ""), **({"rc_name": link.name} if link else {})}, secret_masker(self.home))

        if link is not None:
            data = await link.ask("history")
            return {"session": await asyncio.to_thread(one, None), "history": data.get("history") or [],
                    "seq": int(data.get("seq") or 0), "writable": True}

        def read() -> tuple[dict[str, Any], list[dict]]:
            chat = DiskChat(self.home, sid)
            return one(chat), protocol.history(chat)

        info, history = await asyncio.to_thread(read)
        return {"session": info, "history": history, "seq": 0, "writable": False}

    async def do_send(self, message: dict) -> dict:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            raise Refuse("nothing to send")
        if len(text.encode("utf-8")) > TEXT_MAX:
            raise Refuse(f"the text is longer than {TEXT_MAX // 1024} KiB")
        return await self.linked(message).ask("send", text=text, **{"from": str(message.get("from") or "phone")[:60]})

    async def do_answer(self, message: dict) -> dict:
        choice = message.get("choice")
        if not isinstance(choice, (str, bool)) or not message.get("question"):
            raise Refuse("an answer needs `question` and `choice`")
        return await self.linked(message).ask("answer", question=message["question"], choice=choice,
                                              **{"from": str(message.get("from") or "phone")[:60]})

    async def do_cancel(self, message: dict) -> dict:
        return await self.linked(message).ask("cancel")

    async def do_session_set(self, message: dict) -> dict:
        wanted = {k: message[k] for k in ("model", "effort", "mode") if isinstance(message.get(k), str) and message[k]}
        if not wanted:
            raise Refuse("nothing to set: give model, effort or mode")
        return await self.linked(message).ask("session.set", **wanted)

    async def do_session_archive(self, message: dict) -> dict:
        sid = self.known(message)
        link = self.links.get(sid)
        if link is not None:
            data = await link.ask("session.archive", timeout=30.0)
            text = str(data.get("text") or "archived")
        else:
            try:
                text = await asyncio.to_thread(runs.archive, self.home, sid, "phone")
            except ValueError as err:
                raise Refuse(str(err)) from None
        self.changed()
        return {"text": text}

    async def do_session_unarchive(self, message: dict) -> dict:
        sid = self.known(message)
        try:
            text = await asyncio.to_thread(runs.unarchive, self.home, sid)
        except ValueError as err:
            raise Refuse(str(err)) from None
        self.changed()
        return {"text": text}

    # ------------------------------------------------------------------ new and resumed chats
    def need_start(self) -> None:
        if not self.hello()["can_start"]:
            raise Refuse("this machine cannot start sessions for the phone (needs tmux on Linux or macOS, or the lm.cmd launcher on Windows)")

    def new_settings(self, message: dict) -> dict[str, Any]:
        """What a new chat gets: the request, else `remote.*` of config.md, else (left out) the `defaults:` of the tree.  Every
        value is checked here so nothing wrong reaches `open_session` or a command line."""
        def pick(key: str) -> str:
            value = message.get(key)
            if value in (None, ""):
                value = remote_conf(self.home).get(key) if key in ("agent", "model", "effort", "mode") else None
            return "" if value in (None, "") else str(value)

        agent, project, model, effort, mode = (pick(k) for k in ("agent", "project", "model", "effort", "mode"))
        if agent and agent not in self.home.list_agents():
            raise Refuse(f"unknown agent {agent!r}")
        if project and project not in self.home.list_projects():
            raise Refuse(f"unknown project {project!r}")
        if model and not settings.MODEL_NAME.fullmatch(model):
            raise Refuse(f"{model!r} is not a model name")
        if effort and effort not in core.efforts():
            raise Refuse(f"unknown effort {effort!r}; one of {', '.join(core.efforts())}")
        if mode and mode not in modes_of(self.home):
            raise Refuse(f"unknown mode {mode!r}")
        name = message.get("name")
        if name not in (None, ""):
            if not isinstance(name, str) or not runs.valid_name(name) or not launch.SAFE.fullmatch(name):
                raise Refuse(f"the name {name!r} is not allowed: letters, digits, . _ - and at most 64 characters, no spaces")
            if runs.find_name(self.home, name):
                raise Refuse(f"a session called {name!r} exists")
        text = message.get("text")
        if text not in (None, "") and (not isinstance(text, str) or len(text.encode("utf-8")) > TEXT_MAX):
            raise Refuse("the first message is not text, or longer than 64 KiB")
        return {"agent": agent or None, "project": project or None, "model": model or None, "effort": effort or None,
                "mode": mode or None, "name": name or "", "text": text or ""}

    def create_chat(self, wanted: dict[str, Any]) -> str:
        """The new chat on disk: opened, named, handed over (status `paused`, lock free) for the TUI that resumes it."""
        from . import loop
        session = loop.open_session(self.home, agent=wanted["agent"], project=wanted["project"], model=wanted["model"],
                                    effort=wanted["effort"], mode=wanted["mode"], origin="phone")
        sid = session.id
        try:
            if wanted["name"]:
                runs.claim_name(self.home, wanted["name"], sid)
        finally:
            session.hand_over()
        return sid

    async def do_session_new(self, message: dict) -> dict:
        self.need_start()
        wanted = await asyncio.to_thread(self.new_settings, message)
        sid = await asyncio.to_thread(self.create_chat, wanted)
        name = wanted["name"] or window_name(self.home, sid)
        return await self.start_window(sid, name, wanted["text"])

    async def do_session_resume(self, message: dict) -> dict:
        sid = self.known(message)
        if sid in self.links:
            return {"session": sid}
        self.need_start()
        if runs.is_archived(self.home.path("runs") / sid):
            raise Refuse("this chat is archived: restore it first")
        if runs.lock_holder(self.home, sid):
            raise Refuse("this chat runs in a terminal without Remote Control: type /rc there")
        return await self.start_window(sid, window_name(self.home, sid), "")

    async def start_window(self, sid: str, name: str, text: str) -> dict:
        try:
            await asyncio.to_thread(start_chat, self.home, sid, name)
        except (RuntimeError, OSError, subprocess.SubprocessError) as err:
            raise Refuse(f"chat {sid}: {err}") from None
        link = await self.wait_link(sid, NEW_LINK_WAIT_S)
        if text:
            await link.ask("send", text=text, **{"from": "phone"})
        self.changed()
        return {"session": sid}

    # ------------------------------------------------------------------ choices for the phone
    def account_of(self, sid: str) -> str:
        from .providers.base import resolve_account
        record = runs.read_trusted(self.home, sid) if sid else None
        if record and record.get("account"):
            return str(record["account"])
        try:
            return resolve_account(self.home)
        except Exception:                                   # noqa: BLE001 - no account: no model list, not a failure
            return ""

    async def do_options(self, message: dict) -> dict:
        sid = message.get("session")
        sid = self.known(message) if sid else ""

        def read() -> dict[str, Any]:
            models = [{"id": n, "label": n, "hint": note} for n, note in core.model_options(self.home, self.account_of(sid))]
            modes = [{"id": k, "label": k, "hint": str(v.get("description") or "")} for k, v in modes_of(self.home).items()]
            agents = []
            for name in sorted(self.home.list_agents()):
                try:
                    agents.append({"name": name, "project": self.home.load_agent(name).project or ""})
                except Exception:                           # noqa: BLE001 - a broken agent file hides only itself
                    agents.append({"name": name, "project": ""})
            return {"models": models, "efforts": list(core.efforts()), "modes": modes, "agents": agents,
                    "projects": [{"name": p} for p in sorted(self.home.list_projects())]}

        return await asyncio.to_thread(read)

    async def do_commands(self, message: dict) -> dict:
        if message.get("session"):
            self.known(message)
        forbidden = set(getattr(core, "RC_DENY", ())) | FORBIDDEN

        def read() -> list[dict[str, str]]:
            rows = [{"name": n, "help": h, "kind": "builtin"} for n, h in core.BUILTIN_SLASH]
            seen = {r["name"] for r in rows}
            rows += [{"name": n, "help": a, "kind": "ui"} for n, a in core.slash_rows(self.home) if n not in seen]
            seen |= {r["name"] for r in rows}
            rows += [{"name": n, "help": (s.description or "")[:200], "kind": "skill"} for n, s in core.skill_commands(self.home).items()
                     if n not in seen]
            return [r for r in rows if r["name"] not in forbidden]

        return {"commands": await asyncio.to_thread(read)}

    # ------------------------------------------------------------------ settings
    def gateway_machine_ok(self) -> None:
        """Settings are changed on the machine of the gateway only (the files reach the others through `lm sync`)."""
        gateway = str(remote_value(self.home, "gateway"))
        try:
            here = self.home.load_server(gateway).local
        except Exception:                                   # noqa: BLE001 - no card for it: nothing says it is elsewhere
            return
        if not here:
            raise Refuse(f"settings are changed on {gateway}, the machine of the gateway; this is {self.name}")

    def eligible_servers(self) -> list[Any]:
        out = []
        for name in sorted(self.home.list_servers()):
            with contextlib.suppress(Exception):
                server = self.home.load_server(name)
                if server.lm_home or server.local:
                    out.append(server)
        return out

    def settings_data(self) -> dict[str, Any]:
        page = settings.read_general(self.home)
        if page.error:
            raise Refuse(page.error)
        rows = [r for r in page.rows if r.field.path[0] in ("remote", "defaults")]
        values = page.values()
        fields = [{"path": ".".join(r.field.path), "label": r.field.label, "kind": r.field.kind, "help": r.field.help,
                   "value": r.value, "effective": r.effective, "unset": r.field.unset, "danger": r.field.danger,
                   "low": r.field.low, "high": r.field.high,
                   "options": [{"value": o[0], "label": o[1], "help": o[2]} for o in settings.options(self.home, r.field, values)]
                   if r.field.kind == "choice" else []} for r in rows]
        current = {r.field.path[1]: r.value for r in rows if r.field.path[0] == "remote"}
        remote_values = {key: (default if current.get(key) in (None, "") else current[key]) for key, default in DEFAULTS.items()}
        return {"remote": remote_values, "rev": hashlib.sha256(page.loaded).hexdigest()[:16],
                "servers": [{"name": s.name, "remote": truthy(s.extra.get("remote"))} for s in self.eligible_servers()],
                "fields": fields}

    async def do_settings_get(self, message: dict) -> dict:
        self.gateway_machine_ok()
        return await asyncio.to_thread(self.settings_data)

    def apply_settings(self, message: dict) -> dict[str, Any]:
        """Check every change first, then write: `remote.*` and `defaults.*` as lines of config.md, `servers.<name>.remote`
        as a line of that server's info.md.  `rev` (from `settings.get`), when given, must still be the file's state."""
        changes = message.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise Refuse("`changes` must be an object with at least one key")
        page = settings.read_general(self.home)
        if page.error:
            raise Refuse(page.error)
        if message.get("rev") and message["rev"] != hashlib.sha256(page.loaded).hexdigest()[:16]:
            raise Refuse("config.md changed since you loaded the settings; load them again")
        known = {".".join(r.field.path): r.field for r in page.rows}
        config: dict[tuple[str, ...], Any] = {}
        servers: dict[str, bool] = {}
        for key, value in changes.items():
            parts = str(key).split(".")
            if parts[0] == "servers" and len(parts) == 3 and parts[2] == "remote":
                if parts[1] not in {s.name for s in self.eligible_servers()}:
                    raise Refuse(f"{parts[1]!r} is no server with a Lightmind tree")
                if not isinstance(value, bool):
                    raise Refuse(f"{key}: true or false")
                servers[parts[1]] = value
            elif parts[0] in ("remote", "defaults") and len(parts) == 2 and key in known:
                config[tuple(parts)] = coerce(known[key], value)
            else:
                raise Refuse(f"{key}: this cannot be changed from the phone")
        try:
            if config:
                settings.save_page(self.home, page, config)
            for name, value in servers.items():
                path = Path(self.home.load_server(name).path)
                settings.save(self.home, path, {("remote",): value})
        except settings.SettingsError as err:
            raise Refuse(str(err)) from None
        return {"saved": sorted(str(k) for k in changes)}

    async def do_settings_set(self, message: dict) -> dict:
        self.gateway_machine_ok()
        return await asyncio.to_thread(self.apply_settings, message)

    # ------------------------------------------------------------------ downloads
    async def do_file_get(self, message: dict) -> dict:
        """A file the agent offered with `[[file: ...]]`, piece by piece (`piece` 1, 2, ...); keys and secrets are refused."""
        self.known(message)
        path, number = message.get("path"), message.get("piece", 1)
        if not isinstance(path, str) or not path.strip() or len(path) > 1000 or type(number) is not int or number < 1:
            raise Refuse("file.get needs `path` and a whole `piece` number from 1")
        try:
            return await asyncio.to_thread(offer.piece, path, self.home.root, number)
        except offer.Refused as e:
            raise Refuse(str(e)) from None

    # ------------------------------------------------------------------ uploads
    async def do_upload_begin(self, message: dict) -> dict:
        sid, upload_id = self.known(message), message.get("id")
        if sid not in self.links:
            raise Refuse("session is read-only: nobody has it open with Remote Control")
        if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", upload_id) or upload_id in self.uploads:
            raise Refuse("bad or repeated upload id")
        if len(self.uploads) >= UPLOADS_OPEN_MAX:
            raise Refuse("too many uploads at once")
        limit = int(float(remote_value(self.home, "upload_mb")) * 1024 * 1024)
        size = message.get("size")
        if isinstance(size, int) and size > limit:
            raise Refuse(f"file larger than {limit // (1024 * 1024)} MB")
        kind = "voice" if message.get("kind") == "voice" else "file"
        folder = self.home.path("runs") / sid / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        name = clean_filename(message.get("name"), kind)
        while True:
            target = upload.unique_target(folder, name)
            try:
                target.open("xb").close()                   # reserved: a second upload of the same name picks the next one
                break
            except FileExistsError:
                continue
        part = target.with_name(target.name + ".part")
        self.uploads[upload_id] = Upload(sid, kind, str(message.get("mime") or "")[:100], target, part, part.open("wb"), limit)
        return {}

    async def do_upload_chunk(self, message: dict) -> dict:
        current = self.uploads.get(message.get("id"))
        if current is None:
            raise Refuse("unknown upload")
        try:
            raw = base64.b64decode(str(message.get("data") or ""), validate=True)
        except ValueError:
            self.discard_upload(current, message["id"])
            raise Refuse("chunk is not base64") from None
        if message.get("seq") != current.next_seq or current.received + len(raw) > current.limit:
            self.discard_upload(current, message["id"])
            raise Refuse("chunk out of order" if message.get("seq") != current.next_seq else
                         f"file larger than {current.limit // (1024 * 1024)} MB")
        current.handle.write(raw)
        current.received += len(raw)
        current.next_seq += 1
        return {}

    async def do_upload_end(self, message: dict) -> dict:
        current = self.uploads.pop(message.get("id"), None)
        if current is None:
            raise Refuse("unknown upload")
        try:
            current.handle.close()
            os.replace(current.part, current.target)
        except OSError as err:
            self.discard_upload(current)
            raise Refuse(f"the file could not be saved: {err}") from None
        return await asyncio.to_thread(process_upload, self.home, current.target, current.kind, current.mime, run_media)

    async def do_upload_abort(self, message: dict) -> dict:
        current = self.uploads.pop(message.get("id"), None)
        if current is not None:
            self.discard_upload(current)
        return {}

    def discard_upload(self, current: Upload, upload_id: str | None = None) -> None:
        """Forget an upload that failed or was broken off: no `.part`, no reserved empty target."""
        if upload_id is not None:
            self.uploads.pop(upload_id, None)
        with contextlib.suppress(OSError):
            current.handle.close()
        for leftover in (current.part, current.target):
            with contextlib.suppress(OSError):
                if leftover == current.part or leftover.stat().st_size == 0:
                    leftover.unlink()


def coerce(definition: settings.Field, value: Any) -> Any:
    """A value from the phone as `settings.check_value` wants it: empty = not set, "true"/"false" as booleans where the field
    is yes/no, digits as a number for a number field."""
    if value in (None, ""):
        return None
    if definition.kind == "int" and isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, str) and value.lower() in ("true", "false") and definition.path[-1] in ("enabled", "auto", "push_questions"):
        return value.lower() == "true"
    return value


# ---------------------------------------------------------------------------------------------- entry points
def start_reader(lines: Any, loop: asyncio.AbstractEventLoop, inbox: "asyncio.Queue[bytes | None]") -> None:
    """Lines of a blocking stream into the asyncio queue, from a thread; None at the end.  A line longer than LINE_MAX is
    skipped (the rest of it with it)."""
    def work() -> None:
        try:
            while True:
                line = lines.readline(LINE_MAX + 1)
                if not line:
                    break
                if len(line) > LINE_MAX and not line.endswith(b"\n"):
                    while (rest := lines.readline(LINE_MAX)) and not rest.endswith(b"\n"):
                        pass
                elif line.strip():
                    loop.call_soon_threadsafe(inbox.put_nowait, line)
        except (OSError, ValueError):
            pass
        finally:
            loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=work, daemon=True).start()


def start_writer(stream: Any) -> "tuple[Callable[[bytes], None], Callable[[], None]]":
    """A thread that writes the queued lines to `stream`, so a slow pipe never blocks the event loop.  Returns `(write, finish)`;
    `finish` waits (a few seconds) until everything queued is written."""
    lines: "queue.SimpleQueue[bytes | None]" = queue.SimpleQueue()

    def work() -> None:
        while (item := lines.get()) is not None:
            try:
                stream.write(item)
                stream.flush()
            except (OSError, ValueError):
                return

    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    def finish() -> None:
        lines.put(None)
        thread.join(5)

    return lines.put, finish


async def serve_channel(home: Home, lines: Any, stream: Any, idle_exit_s: float = 0.0) -> int:
    """The node over one channel: `lines` is read for requests, `stream` gets the answers."""
    inbox: "asyncio.Queue[bytes | None]" = asyncio.Queue()
    start_reader(lines, asyncio.get_running_loop(), inbox)
    write, finish = start_writer(stream)
    try:
        return await Node(home, write, idle_exit_s=idle_exit_s).run(inbox)
    finally:
        await asyncio.to_thread(finish)


def lock_or_exit(home: Home) -> Path | None:
    try:
        return take_lock(home)
    except Busy as err:
        print(f"lm node: {err}", file=sys.stderr, flush=True)
        return None


def serve_stdio(home: Home) -> int:
    """`lm node --stdio`: the gateway talks over this process's stdin and stdout.  Exit 3 when another node of this tree runs."""
    from . import serve
    lock = lock_or_exit(home)
    if lock is None:
        return EXIT_BUSY
    try:
        read_end, write_end = serve.take_channel()
        return asyncio.run(serve_channel(home, serve._Lines(read_end), write_end))
    finally:
        drop_lock(lock)


def relay(home: Home, node_name: str) -> int:
    """`lm node --relay <name>` (runs on the gateway machine, started over ssh by an uplink): stdin and stdout of this process
    joined to the gateway's unix socket.  The gateway learns the node's name from its `node.hello`, so `node_name` is only
    for messages."""
    from . import serve
    if not hasattr(socket, "AF_UNIX"):
        print("lm node --relay needs unix sockets", file=sys.stderr)
        return 1
    path = gateway_socket(home)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except OSError as err:
        print(f"lm node --relay {node_name}: the gateway is not listening at {path}: {err}", file=sys.stderr)
        return 1
    read_end, write_end = serve.take_channel()

    def to_gateway() -> None:
        try:
            while chunk := read_end.read(65536):
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)

    threading.Thread(target=to_gateway, daemon=True).start()
    try:
        while chunk := sock.recv(65536):
            write_end.write(chunk)
            write_end.flush()
    except OSError:
        pass
    return 0


def uplink_command(home: Home) -> list[str]:
    """The ssh command that starts the relay on the gateway machine (`remote.gateway` of config.md; no default)."""
    gateway = str(remote_value(home, "gateway"))
    if not gateway:
        raise ValueError("remote.gateway is not set in config.md: name the server the gateway runs on")
    server = home.load_server(gateway)
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20", *remote.pick(server),
            f"{remote.tree_path(server)}/system/lm node --relay {machine_name(home)}"]


def serve_uplink(home: Home) -> int:
    """`lm node --uplink` (laptop): the node protocol over the stdio of `ssh <gateway> ... node --relay`.  Ends when the ssh
    ends or 10 minutes after the last link."""
    lock = lock_or_exit(home)
    if lock is None:
        return EXIT_BUSY
    try:
        child = subprocess.Popen(uplink_command(home), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                                 bufsize=0)             # raw pipes: `_Lines` reads what has arrived, a buffered reader would wait for 64 KiB
    except (OSError, remote.RemoteError, ValueError) as err:
        print(f"lm node --uplink: {err}", file=sys.stderr)
        drop_lock(lock)
        return 1
    try:
        from . import serve
        return asyncio.run(serve_channel(home, serve._Lines(child.stdout), child.stdin, UPLINK_IDLE_S))
    finally:
        child.terminate()
        drop_lock(lock)
