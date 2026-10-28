"""Files for the user: an agent writes a line `[[file: <path>]]` and the chat turns it into a download.

One spelling for every surface (the same line the Telegram bot already knows):

* terminal chat (`lm chat`, Orbit): `linkify` replaces the line by a Markdown link `↓ name (size)` that points to a small
  download service of this process (`http://127.0.0.1:<port>/dl/<token>`, bound to the loopback only, answers with
  `Content-Disposition: attachment`, token valid for `TOKEN_TTL` seconds);
* phone and VS Code: the client finds the same line in the text and fetches the file in chunks with the request `file.get`
  (`chunk`); the gateway authenticates, `check(..., scan=True)` guards the way out;
* a link for somebody else (a public file host of the tree): only on the user's explicit wish, after `check(..., scan=True)`.

`check` refuses everything in `secrets/` and `system/` of the tree, key and token files by name, and (with `scan`) files that
contain a key.  May import: nothing of the core.
"""
from __future__ import annotations

import base64
import mimetypes
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

MARKER = re.compile(r"^[ \t]*\[\[file:[ \t]*(.+?)[ \t]*\]\][ \t]*$", re.MULTILINE)
TOKEN_TTL = 12 * 3600                           # seconds a link of the terminal stays valid
CHUNK = 192 * 1024                              # bytes of one `file.get` answer (before base64)
SCAN_MAX = 20 * 1024 * 1024                     # a bigger file is not scanned and not shared
BLOCKED_DIRS = ("secrets", "system")            # folders of the tree root that never leave
BLOCKED_NAMES = re.compile(r"(^\.env($|\.)|\.(pem|key|p12|pfx|kdbx|ppk|asc|gpg)$|^id_(rsa|dsa|ecdsa|ed25519)|^credentials|^\.netrc$)",
                           re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"\bxox[abp]-[A-Za-z0-9-]{20,}"),
    re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),                               # a Telegram bot token
    re.compile(rb"(?i)\b(password|passwort|passwd|secret|api[_-]?key|access[_-]?token)\b[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,}"),
)


class Refused(Exception):
    """The file may not be offered; the message says why in one short sentence."""


@dataclass
class Offer:
    path: Path
    name: str
    size: int
    url: str = ""


def resolve(raw: str, root: Path) -> Path:
    """The path of a marker: `~` and relative paths (from the tree root) are understood."""
    p = Path(raw.strip().strip("\"'")).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def check(path: Path, root: Path, scan: bool = False) -> Path:
    """The file itself or `Refused`.  `scan`: also look inside for keys (needed whenever the file leaves this machine)."""
    root = root.resolve()
    try:
        path = path.resolve()
    except OSError as e:
        raise Refused(f"path not readable: {e}") from None
    if not path.is_file():
        raise Refused("no such file")
    for d in BLOCKED_DIRS:
        if path.is_relative_to(root / d):
            raise Refused(f"files in {d}/ are never offered")
    if ".ssh" in path.parts or BLOCKED_NAMES.search(path.name):
        raise Refused("key and credential files are never offered")
    if scan:
        if path.stat().st_size > SCAN_MAX:
            raise Refused("file too big to check for keys")
        data = path.read_bytes()
        for rx in SECRET_PATTERNS:
            if rx.search(data):
                raise Refused("the file seems to contain a key or password")
    return path


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{int(x)} B" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{n} B"


def markers(text: str) -> list[str]:
    """The raw paths named in `[[file: ...]]` lines, in order."""
    return [m.group(1) for m in MARKER.finditer(text or "")]


# ---------------------------------------------------------------------------------------------------- the phone / plugin way
def chunk(raw: str, root: Path, offset: int = 0) -> dict:
    """One piece of a file for `file.get`: {name, size, offset, data (base64), done, mime}.  Scans for keys on the first piece."""
    path = check(resolve(raw, root), root, scan=True)
    size = path.stat().st_size
    if not isinstance(offset, int) or offset < 0 or offset > size:
        raise Refused("bad offset")
    with path.open("rb") as f:
        f.seek(offset)
        part = f.read(CHUNK)
    return {"name": path.name, "size": size, "offset": offset, "data": base64.b64encode(part).decode("ascii"),
            "done": offset + len(part) >= size, "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def piece(raw: str, root: Path, number: int = 1) -> dict:
    """Piece `number` (1, 2, ...) of a file: `chunk` plus `piece` and `pieces`."""
    path = check(resolve(raw, root), root, scan=True)
    size = path.stat().st_size
    out = chunk(raw, root, (max(1, number) - 1) * CHUNK)
    out.update(piece=max(1, number), pieces=max(1, -(-size // CHUNK)))
    return out


# ---------------------------------------------------------------------------------------------------- the terminal way
class Service:
    """A tiny download server on 127.0.0.1 with a random port; tokens map to files.  Started on the first link."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.tokens: dict[str, tuple[Path, float]] = {}
        self.by_path: dict[Path, str] = {}
        self.lock = threading.Lock()
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):                                   # no noise in the terminal
                pass

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body: bool):
                port = self.server.server_address[1]
                if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    return self.send_error(403)                          # a rebinding attack arrives with a foreign Host
                m = re.fullmatch(r"/dl/([A-Za-z0-9_-]{16,64})", self.path.split("?", 1)[0])
                found = service.lookup(m.group(1)) if m else None
                if found is None:
                    return self.send_error(404)
                try:
                    f = found.open("rb")
                except OSError:
                    return self.send_error(404)
                with f:
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(found.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(found.stat().st_size))
                    self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(found.name)}")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    while body and (piece := f.read(256 * 1024)):
                        self.wfile.write(piece)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="offer-dl", daemon=True).start()

    def lookup(self, token: str) -> Path | None:
        with self.lock:
            item = self.tokens.get(token)
            if item is None or item[1] < time.time():
                self.tokens.pop(token, None)
                return None
        try:
            return check(item[0], self.root)                              # still allowed (the file may have changed)
        except Refused:
            return None

    def offer(self, path: Path) -> Offer:
        """A link for a file that passed `check`; the same file gets the same link while it is valid."""
        with self.lock:
            token = self.by_path.get(path)
            if token is None or self.tokens.get(token, (path, 0))[1] < time.time():
                token = _secrets.token_urlsafe(18)
                self.by_path[path] = token
            self.tokens[token] = (path, time.time() + TOKEN_TTL)
        return Offer(path, path.name, path.stat().st_size, f"http://127.0.0.1:{self.port}/dl/{token}")


_service: Service | None = None
_service_lock = threading.Lock()


def service(root: Path) -> Service:
    global _service
    with _service_lock:
        if _service is None:
            _service = Service(root)
        return _service


def linkify(text: str, root: Path) -> str:
    """Markdown for the terminal: every `[[file: ...]]` line becomes a link line `↓ name (size)` or a short refusal."""
    if "[[file:" not in text:
        return text

    def one(m: re.Match) -> str:
        try:
            path = check(resolve(m.group(1), root), root)
        except Refused as e:
            return f"⚠ file not offered ({e}): {m.group(1)}"
        o = service(root).offer(path)
        name = re.sub(r"([\[\]\\])", r"\\\1", o.name)
        return f"[↓ {name} ({human(o.size)})]({o.url})"

    return MARKER.sub(one, text)
