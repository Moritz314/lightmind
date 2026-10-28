"""lm sync: the tree is the git working tree of one private repo; secrets travel as age files.

Content moves as plain git (subprocess).  Secrets are never in the repo as text: every `<name>/<account>.env` has a
sibling `<account>.env.age`, encrypted (pyrage) for every instance listed in `<secrets>/recipients`.  This instance's
private key is `<state>/age.key`; its name is `<state>/machine`.  Other files in `<state>`: `sync-secrets.json` (the
hash of each secret as last agreed with the repo, and open conflicts), `sync-status.json` (last result), `sync.lock`.
`<state>/local.md` (settings of this instance) and everything under `system/` stay out of the repo.

A tree is a "sync repo" when it has its own `.git` and a machine name; everything else here is silent without it.
Automatic runs only happen at LM_DEPTH 0.  Nothing in this module raises into a caller that is not `lm sync` itself.

May import: files, secrets (late), tools (late), pyrage.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .files import Home, write_atomic as _atomic

BRANCH = "main"
MAX_BYTES = 5 * 1024 * 1024            # a bigger file is not committed
DEBOUNCE = 30                          # seconds between two automatic commits after agent turns
TICK = 300                             # TUI: pull and push every 5 minutes
NET, LOCAL = 45, 30                    # seconds for a git call with / without network
TRANSFER = 300                         # seconds for a fetch or a push: they move data, a first push over a slow line takes minutes
LOCK_STALE = 600                       # a lock older than this is taken over whatever its owner says
LOCK_WAIT = 20.0                       # how long a command that must not skip (secret write, session end) waits for the lock
RECIPIENTS = "recipients"
CONFIRMED = "recipients-confirmed"      # in the state folder: the keys this instance confirmed itself
AGE = ".age"
MARK = "# lm sync (managed)"
IGNORE = f"""{MARK}
system/
runs/*
!runs/README.md
secrets/**
!secrets/**/
!secrets/**/*.age
!secrets/**/info.md
!secrets/README.md
!secrets/recipients
*.env
*.env.tmp
*.key
*.plain
*.lokal
.credentials.json
*.credentials.json*
__pycache__/
"""
ATTRIBUTES = ("* text=auto eol=lf", "*.age binary")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_NEVER = re.compile(r"(^|/)[^/]*\.env(\.tmp)?$|\.key$|\.plain$|credentials\.json|^system/")
_UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}            # `git status` codes of a path in the middle of a conflict
_MARKERS = re.compile(rb"^<{7}(?: [^\n]*)?\r?$[\s\S]*?^={7}\r?$[\s\S]*?^>{7}(?: [^\n]*)?\r?$", re.M)   # a whole conflict block
_OFFLINE = re.compile(r"resolve|unable to access|connect|timed out|timeout|could not read|unreachable|network|ssh:", re.I)
_AUTH = re.compile(r"authentication failed|permission denied|publickey|denied to |access denied|invalid username or password|"
                   r"terminal prompts disabled|could not read (?:username|password)|returned error: 40[13]\b|"
                   r"\bHTTP(?:/\d(?:\.\d)?)? 40[13]\b|repository .{0,200}not found", re.I)    # a refused login, not a missing network
_AUTH_WARN = "auth: the repo refused the login ({why}); check the deploy key or the credentials of this machine"
AGE_HEAD = b"age-encryption.org/v1\n"                              # the first line of every age file
CONFLICT_WARNS = ("conflict, pull", "push failed (conflict)")     # what `run` says when git stopped at a conflict
_mu = threading.RLock()


class SyncError(ValueError):
    """A problem `lm sync` reports to its caller (message to the user, never a value)."""


@dataclass
class Result:
    status: str = "ok"                                    # ok | offline | auth | conflict | warn | skip
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines + [f"warning: {w}" for w in self.warnings]) or self.status


# ------------------------------------------------------------------------------------------------ basics
def _state(home: Home) -> Path:
    return home.path("state")


def _secrets_dir(home: Home) -> Path:
    return home.path("secrets")


def _depth() -> int:
    try:
        return max(int(os.environ.get("LM_DEPTH") or 0), 0)
    except ValueError:
        return 0


def enabled(home: Home) -> bool:
    """True when the tree is a sync repo: its own .git and a machine name."""
    try:
        return (home.root / ".git").exists() and bool(home.machine)
    except Exception:                                      # noqa: BLE001
        return False


def _cmd(home: Home, args: tuple[str, ...]) -> list[str]:
    return ["git", "-c", "user.name=lightmind", "-c", "user.email=lightmind@localhost", "-c", "core.quotepath=off",
            "-c", "protocol.ext.allow=never", "-C", str(home.root), *args]


def _genv() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1", "GIT_EDITOR": "true",
            "GIT_ALLOW_PROTOCOL": "https:ssh:git:file"}


def _g(home: Home, *args: str, timeout: float = LOCAL, input: str | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of git in the tree.  Never prompts, literal pathspecs, no remote helpers; -1 timeout, -2 no git."""
    try:
        r = subprocess.run(_cmd(home, args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=_genv(), input=input, stdin=None if input is not None else subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -2, "", str(e)


def _gb(home: Home, *args: str, input: bytes, timeout: float = LOCAL) -> tuple[int, bytes]:
    """(exit code, stdout bytes) of git with bytes in and out (`cat-file --batch`); -1 on a timeout or a missing git."""
    try:
        r = subprocess.run(_cmd(home, args), capture_output=True, timeout=timeout, env=_genv(), input=input,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return -1, b""


def _last(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:140] if lines else ""


def _auth_line(rc: int, err: str) -> str | None:
    """The line of git's error text that names a refused login, or None.  (The last line is usually the useless 'and the
    repository exists.'; a timeout or a missing git is never a login problem.)"""
    if rc in (-1, -2):
        return None
    for ln in err.splitlines():
        if _AUTH.search(ln):
            return ln.strip()[:140]
    return None


def _net_fail(rc: int, err: str) -> tuple[str, str]:
    """('auth' | 'offline', detail) of a fetch that failed.  A refused login (HTTP 401/403, 'Authentication failed', 'Permission
    denied (publickey)') is its own state: waiting does not help, the credentials of this machine do."""
    line = _auth_line(rc, err)
    return ("auth", line) if line else ("offline", _last(err))


def _unreachable(rc: int, err: str, limit: float = NET) -> str:
    """What `init` and `join` say when the first contact with the repo fails: a timeout, a refused login, or git's last line."""
    if rc == -1:
        return f"timed out after {limit:g} s"
    st, why = _net_fail(rc, err)
    return f"the repo refused the login ({why}); check the deploy key or the credentials of this machine" if st == "auth" else why


def _branch(home: Home) -> str:
    rc, out, _ = _g(home, "symbolic-ref", "--short", "HEAD")
    return out.strip() if rc == 0 and out.strip() else BRANCH


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pid_alive(pid: int) -> bool:
    """True when a process with this id exists.  (Never `os.kill(pid, 0)` on Windows: there every signal but the two console
    events ends the process.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, pid)                       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5                        # access denied: it exists, it is not ours
        try:
            code = ctypes.c_ulong()
            return not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value == 259     # 259: STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(p: Path) -> bool:
    """Stale: older than LOCK_STALE, or it names a process that is gone.  A lock whose owner cannot be read is kept until it is
    old.  A lock of this very process is never stale: another thread of ours holds it."""
    try:
        age = time.time() - p.stat().st_mtime
        owner = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False                                       # gone meanwhile: the next try gets it
    if age > LOCK_STALE:
        return True
    return owner.isdigit() and int(owner) != os.getpid() and not _pid_alive(int(owner))


def _take_lock(p: Path) -> bool:
    for _ in (0, 1):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _lock_is_stale(p):
                return False
            try:
                p.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")                    # the owner: the lock of a dead process is taken over at once
        return True
    return False


@contextmanager
def _lock(home: Home, wait: float = 0.0):
    """`<state>/sync.lock`, created exclusively, holding the pid of its owner.  With `wait` it polls that many seconds.
    Yields whether it was got."""
    p = _state(home) / "sync.lock"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        end = time.monotonic() + max(wait, 0.0)
        while True:
            got = _take_lock(p)
            if got or time.monotonic() >= end:
                break
            time.sleep(0.2)
    except OSError:
        got = False
    try:
        yield got
    finally:
        if got:
            try:
                if p.read_text(encoding="utf-8", errors="replace").strip() == str(os.getpid()):      # still ours
                    p.unlink()
            except OSError:
                pass


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _store(home: Home) -> dict:
    d = _load(_state(home) / "sync-secrets.json", {})
    d.setdefault("hashes", {})
    d.setdefault("conflicts", [])
    d.setdefault("recipients", {})                         # {ref: {"age": hash of the .age, "fp": fingerprint of the list it was made for}}
    d.setdefault("removing", [])                           # devices taken off the list whose lock-out is not yet certain
    d.setdefault("why", {})                                # {ref: kind of the open conflict}
    d.setdefault("prev_fp", {})                            # {device: fingerprint of the key it had when last confirmed}: warning only, no trust
    d.setdefault("adopted", [])                            # refs whose .age a pull took from the repo's side (made again for our list)
    return d


def _save(home: Home, d: dict) -> None:
    _atomic(_state(home) / "sync-secrets.json", json.dumps(d, indent=1, sort_keys=True).encode())


def last_status(home: Home) -> dict:
    return _load(_state(home) / "sync-status.json", {})


def _record(home: Home, r: Result) -> None:
    try:
        _atomic(_state(home) / "sync-status.json", json.dumps(
            {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": r.status,
             "lines": r.lines[:5], "warnings": r.warnings[:12]}).encode())
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ keys (pyrage)
def key_path(home: Home) -> Path:
    return _state(home) / "age.key"


def make_identity(home: Home) -> str:
    """Create this instance's key pair (never overwrites); returns the public key."""
    from pyrage import x25519
    p = key_path(home)
    if p.is_file():
        pub = public_key(home) or ""
    else:
        ident = x25519.Identity.generate()
        pub = str(ident.to_public())
        _atomic(p, f"# public key: {pub}\n{ident}\n".encode(), mode=0o600)
    if pub:
        confirm_keys(home, {home.machine or "this": pub})                   # the instance's own key is its own to confirm
    return pub


def _identity(home: Home):
    from pyrage import x25519
    try:
        for ln in key_path(home).read_text(encoding="utf-8").splitlines():
            if ln.startswith("AGE-SECRET-KEY-"):
                return x25519.Identity.from_str(ln.strip())
    except (OSError, ValueError, Exception):               # noqa: BLE001 - a missing or broken key is "cannot decrypt"
        pass
    return None


def public_key(home: Home) -> str | None:
    i = _identity(home)
    return str(i.to_public()) if i else None


def _pairs(text: str) -> list[tuple[str, str]]:
    """The `<name> <key>` lines of a file this instance wrote itself (the confirmed list); anything else is skipped."""
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) == 2 and not ln.lstrip().startswith("#") and parts[1].startswith("age1"):
            out.append((parts[0], parts[1]))
    return out


def _show(text: str, n: int = 40) -> str:
    """Something that came from the repo, made safe to print: escaped (no control characters, no look-alike letters), short."""
    return ascii(text)[:n]


def _is_age_key(key: str) -> bool:
    from pyrage import x25519
    try:
        x25519.Recipient.from_str(key)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def _parse_list(text: str) -> tuple[dict[str, str], list[str]]:
    """The recipient list as it is written: one `<name> <age key>` per line, blank lines and `#` comments allowed.  Returns
    (name -> key, problems).  A problem is a line that is not a name and a key, a name outside a-z 0-9 - _ (no look-alike
    letters, no control characters), a text that is no age key, a name or a key that stands twice.  A list with a problem is
    not trusted at all (`read_recipients` returns nothing) and nothing is encrypted until it is fixed: nothing is renamed or
    guessed, or a confirmation could land on another key than the one that was looked at."""
    out: dict[str, str] = {}
    bad: list[str] = []
    for ln in text.lstrip("﻿").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) != 2:
            bad.append(f"the line {_show(s)} is not '<name> <key>'")
            continue
        name, key = parts
        if not _NAME.fullmatch(name):
            bad.append(f"the name {_show(name)} may only hold a-z, 0-9, - and _")
        elif not _is_age_key(key):
            bad.append(f"the key of {name} is no age key")
        elif name in out:
            bad.append(f"the name {name} stands twice")
        elif key in out.values():
            bad.append(f"the key of {name} stands twice")
        else:
            out[name] = key
    return out, bad


def read_list(home: Home) -> tuple[dict[str, str], list[str]]:
    """The repo's list `<secrets>/recipients` as it stands in the tree: (name -> key, problems), see `_parse_list`."""
    try:
        data = (_secrets_dir(home) / RECIPIENTS).read_bytes()
    except OSError:
        return {}, []
    try:
        return _parse_list(data.decode("utf-8"))
    except UnicodeDecodeError:
        return {}, ["the file is not valid UTF-8"]


def read_recipients(home: Home) -> dict[str, str]:
    """The repo's list: only a proposal, the repo is not trusted (see `effective_recipients`).  Empty when the list has a
    problem (see `_parse_list`): then nothing in it is used."""
    rec, bad = read_list(home)
    return {} if bad else rec


def _valid_list(home: Home) -> dict[str, str]:
    """The repo's list, or SyncError when it has a problem: add-device, remove-device and approve act on it only when it is clean."""
    rec, bad = read_list(home)
    if bad:
        raise SyncError("the recipient list is invalid: " + "; ".join(bad[:3]) +
                        " (fix secrets/recipients: one '<name> <age key>' per line)")
    return rec


def write_recipients(home: Home, rec: dict[str, str]) -> None:
    _atomic(_secrets_dir(home) / RECIPIENTS, "".join(f"{n} {k}\n" for n, k in sorted(rec.items())).encode())


FP_HEX = 32                                            # hex digits of a fingerprint: 128 bits of the SHA-256 of the key
APPROVE_MIN = 16                                       # hex digits of it that `approve` insists on


def fingerprint(key: str) -> str:
    """A short form of a public key to compare by eye between two machines: 32 hex digits in groups of four."""
    h = hashlib.sha256(key.encode()).hexdigest()[:FP_HEX]
    return "-".join(h[i:i + 4] for i in range(0, FP_HEX, 4))


def _norm_fp(text: str) -> str:
    return re.sub(r"[\s:-]", "", text or "").lower()


def confirmed_pairs(home: Home) -> dict[str, str]:
    """name -> key of what THIS instance has confirmed (`<state>/recipients-confirmed`, never in the repo)."""
    try:
        return dict(_pairs((_state(home) / CONFIRMED).read_text(encoding="utf-8")))
    except OSError:
        return {}


def confirmed_keys(home: Home) -> set[str]:
    return set(confirmed_pairs(home).values())


def _write_confirmed(home: Home, pairs: dict[str, str]) -> None:
    _atomic(_state(home) / CONFIRMED, "".join(f"{n} {k}\n" for n, k in sorted(pairs.items())).encode())


def confirm_keys(home: Home, pairs: dict[str, str]) -> None:
    """Take (name, key) pairs into what this instance confirmed.  The repo is not trusted: anyone who can write to it can add a
    line to `recipients`, so a key in that list is only used once a human of THIS instance confirmed it (`add-device`,
    `approve`, the answer at `join`, the instance's own key).  A name confirmed with another key before gets the new one: the
    old key is out."""
    have = confirmed_pairs(home)
    new = {**have, **pairs}
    if new != have:
        _write_confirmed(home, new)


def _prune_confirmed(home: Home) -> None:
    """A confirmed (name, key) stays only while the repo's list has exactly that line.  A device that left the list, or whose
    key changed under its name, is not trusted any more: its key may have been taken off because it is compromised, and the
    repo must not be able to put it back unasked.  For the warning "key of <name> has changed" the old fingerprint (never the
    key) is kept apart in the store (`prev_fp`), with no effect on trust; a name that has left the list is forgotten.  This
    instance's own key stays; an empty or an invalid list prunes nothing."""
    listed = read_recipients(home)
    if not listed:
        return
    own, have = public_key(home), confirmed_pairs(home)
    keep = {n: k for n, k in have.items() if listed.get(n) == k or k == own}
    with _mu:
        st = _store(home)
        prev = {n: f for n, f in st["prev_fp"].items() if n in listed}
        for n, k in have.items():
            if n not in keep and n in listed:                # still listed, under another key
                prev[n] = fingerprint(k)
        if prev != st["prev_fp"]:
            st["prev_fp"] = prev
            _save(home, st)
    if keep != have:
        _write_confirmed(home, keep)


def _is_trusted(name: str, key: str, own: str | None, ok: dict[str, str]) -> bool:
    """Is this line of the list one this instance confirmed (the same name with the same key), or is it its own key?"""
    return key == own or ok.get(name) == key


def effective_recipients(home: Home) -> dict[str, str]:
    """The recipients secrets are encrypted for: the repo's list AND what this instance confirmed, name and key together.  A key
    the list gained without this instance's say-so, or a name that now carries another key, is never used, so a stolen
    repo access cannot add itself; a removal is adopted at once (the intersection only shrinks)."""
    own, ok = public_key(home), confirmed_pairs(home)
    return {n: k for n, k in read_recipients(home).items() if _is_trusted(n, k, own, ok)}


RECIPIENT_WARNS = ("new device ", "key of ", "recipient list ")      # what `recipient_warnings` says (`check_lines` shows live copies)


def recipient_warnings(home: Home, short: bool = False) -> list[str]:
    """What is wrong with the recipient list: it is invalid (nothing is encrypted), a device is not confirmed here, or the key of
    a confirmed device has changed.  `short`: for the checklist line (no fingerprints, 160 characters are all there is)."""
    rec, bad = read_list(home)
    if bad:
        return [f"recipient list invalid ({p}): nothing is encrypted until secrets/recipients is fixed" for p in bad[:3]]
    own, ok, prev = public_key(home), confirmed_pairs(home), _store(home)["prev_fp"]
    out = []
    for n, k in rec.items():
        if _is_trusted(n, k, own, ok):
            continue
        fp = fingerprint(k)
        was = fingerprint(ok[n]) if n in ok else prev.get(n)
        if was and was != fp:
            out.append(f"key of {n} has changed, not used: lm sync approve {n}" if short else
                       f"key of {n} has changed (now {fp}, confirmed was {was}) and is not used: compare {fp} "
                       f"with `lm sync status` on that device, then `lm sync approve {n} {fp}`")
        else:
            out.append(f"new device {n} in the recipient list, not confirmed: lm sync approve {n}" if short else
                       f"new device {n} (fingerprint {fp}) in the recipient list, not confirmed: compare it with "
                       f"`lm sync status` on that device, then `lm sync approve {n} {fp}`")
    return out


def _check_key(key: str) -> str:
    from pyrage import x25519
    key = key.strip()
    try:
        x25519.Recipient.from_str(key)
    except Exception:                                      # noqa: BLE001
        raise SyncError("that is not an age public key (age1...)") from None
    return key


def encrypt(data: bytes, rec: dict[str, str]) -> bytes:
    import pyrage
    from pyrage import x25519
    return pyrage.encrypt(data, [x25519.Recipient.from_str(k) for k in rec.values()])


def decrypt(home: Home, blob: bytes) -> bytes | None:
    """The plain text, or None when this instance has no key or is not a recipient."""
    import pyrage
    ident = _identity(home)
    if ident is None:
        return None
    try:
        return pyrage.decrypt(blob, [ident])
    except Exception:                                      # noqa: BLE001
        return None


def _env(home: Home, ref: str) -> Path:
    from . import secrets as S
    return S._env_path(home, ref)


def _age(home: Home, ref: str) -> Path:
    p = _env(home, ref)
    return p.with_name(p.name + AGE)


def _refs(home: Home) -> list[str]:
    from . import secrets as S
    out = set(S.list_secrets(home))
    root = _secrets_dir(home)
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                out |= {f"{d.name}/{f.name[:-len('.env.age')]}" for f in d.glob("*.env.age")}
    return sorted(out)


def _rel(home: Home, p: Path) -> str:
    return p.relative_to(home.root).as_posix()


def _write_env(path: Path, data: bytes) -> None:
    _atomic(path, data, mode=0o600)


def _same_as_origin(home: Home, p: Path, branch: str) -> bool:
    return _g(home, "diff", "--quiet", f"origin/{branch}", "--", _rel(home, p))[0] == 0


# ------------------------------------------------------------------------------------------------ secrets
def _fp(rec: dict[str, str]) -> str:
    """Fingerprint of a recipient list (the keys, not the names): which list a `.age` was made for."""
    return hashlib.sha256("\n".join(sorted(rec.values())).encode()).hexdigest()[:16]


def _put_age(home: Home, ref: str, data: bytes, rec: dict[str, str], st: dict) -> None:
    """Write `<account>.env.age` for `rec` and remember (in `st`, the caller saves) which hash it has and which list it is for."""
    blob = encrypt(data, rec)
    _atomic(_age(home, ref), blob)
    st["recipients"][ref] = {"age": _hash(blob), "fp": _fp(rec)}


def encrypt_ref(home: Home, ref: str, st: dict | None = None) -> bool:
    """Write `<account>.env.age` from the local .env for every recipient.  False: nothing to do (no .env, no recipient).
    `st`: the caller's open store (it saves); without it the store is read and saved here.  Only for the effective recipients."""
    rec = effective_recipients(home)
    p = _env(home, ref)
    if not rec or not p.is_file():
        return False
    if st is not None:
        _put_age(home, ref, p.read_bytes(), rec, st)
    else:
        with _mu:
            own = _store(home)
            _put_age(home, ref, p.read_bytes(), rec, own)
            _save(home, own)
    return True


def rewrap(home: Home, *, foreign: bool = False) -> tuple[list[str], list[str]]:
    """Make every `.age` this instance can open again for the recipients as they are now, content unchanged.  A `.age` is
    "ours" when its hash is the one remembered with the list it was made for (`recipients` in sync-secrets.json).  One of
    ours that was made for another list (a device was removed after it was written, by an instance that had not pulled
    yet) is fixed here.  Someone else's `.age` is left to its writer, or two instances would rewrite each other's files for
    ever; with `foreign` (remove-device) every `.age` that is not known to be current is fixed, whoever wrote it.  Returns
    (refs rewritten, refs this instance cannot open and so cannot judge: only with `foreign`).  A ref whose `.age` a pull
    took from the repo's side (a conflict) counts as foreign once."""
    rec = effective_recipients(home)
    if not rec or public_key(home) not in rec.values():                       # this instance cannot open anything: it cannot judge
        return [], ([r for r in _refs(home) if _age(home, r).is_file()] if foreign else [])
    now = _fp(rec)
    done, unsure = [], []
    with _mu:
        st = _store(home)
        for ref in _refs(home):
            agep = _age(home, ref)
            if not agep.is_file():
                continue
            blob = agep.read_bytes()
            known = st["recipients"].get(ref) or {}
            ours = known.get("age") == _hash(blob)
            adopted = ref in st["adopted"]
            if (ours and known.get("fp") == now) or (not ours and not foreign and not adopted):
                continue
            plain = decrypt(home, blob)
            if plain is None:
                if foreign:
                    unsure.append(ref)
                continue
            _put_age(home, ref, plain, rec, st)
            done.append(ref)
        st["adopted"] = []
        _save(home, st)
    return done, unsure


def on_secret_written(home: Home, path: Path) -> None:
    """Called by secrets.py after every write of a .env: the .age appears at once, a sync is started.  Silent unless sync repo."""
    try:
        if not enabled(home):
            return
        with _mu:
            ref = f"{path.parent.name}/{path.name[:-len('.env')]}"
            encrypt_ref(home, ref)
        if _depth() == 0:
            kick(home)
    except Exception as e:                                 # noqa: BLE001 - a write of a secret never fails because of sync
        _record(home, Result("warn", warnings=[f"secret written but not encrypted for sync: {e.__class__.__name__}"]))


_WHY = {"both": "changed here and in the repo", "deleted_here": "deleted here, changed in the repo",
        "deleted_there": "changed here, deleted in the repo"}


def _tracked(home: Home, p: Path) -> bool:
    """Is the `.age` in HEAD?  One that is not, although it was agreed once, was dropped (by the repo or by a local deletion)."""
    return _g(home, "cat-file", "-e", f"HEAD:{_rel(home, p)}")[0] == 0


def _in_origin(home: Home, p: Path, branch: str) -> bool:
    return _g(home, "cat-file", "-e", f"origin/{branch}:{_rel(home, p)}")[0] == 0


def reconcile(home: Home, branch: str, *, after_pull: bool) -> tuple[list[str], bool]:
    """Bring `.env` and `.env.age` in line, by hash.  L = the local .env, R = the repo's `.age` decrypted, S = what was last
    agreed.  Before a pull only local changes count (a change becomes `.age`, a deleted `.env` deletes the `.age`); after a pull
    remote changes come back to `.env` (a dropped `.age` deletes the `.env`) unless this side changed it as well: then it is a
    conflict (changed both, or deleted on one side and changed on the other) and nothing is touched.  S stays for a secret
    deleted here until that is pushed (`_mark_synced`), so a change that arrives meanwhile is seen as a conflict.  Returns
    (warnings, an `.age` changed or went)."""
    warns: list[str] = []
    changed = False
    rec = effective_recipients(home)
    with _mu:
        st = _store(home)
        hashes, conflicts, why = st["hashes"], set(st["conflicts"]), st["why"]

        def forget(ref: str) -> None:
            for d in (hashes, st["recipients"], why):
                d.pop(ref, None)
            conflicts.discard(ref)

        for ref in _refs(home):
            envp, agep = _env(home, ref), _age(home, ref)
            L = _hash(envp.read_bytes()) if envp.is_file() else None
            S = hashes.get(ref)
            plain = decrypt(home, agep.read_bytes()) if agep.is_file() else None
            R = _hash(plain) if plain is not None else None
            if agep.is_file() and plain is None:
                if L is None:
                    warns.append(f"{ref}: cannot be decrypted here (this instance is not a recipient yet)")
                continue
            if not agep.is_file():                                       # a .env and no .age
                if S is None or _tracked(home, agep) or _in_origin(home, agep, branch):
                    if rec and encrypt_ref(home, ref, st):               # new, lost from the tree, or made again after a local deletion
                        changed = True
                elif L == S:                                             # agreed once, the repo dropped it, unchanged here: dropped here too
                    envp.unlink(missing_ok=True)
                    forget(ref)
                else:
                    conflicts.add(ref)
                    why[ref] = "deleted_there"
                continue
            if L is None:                                                # an .age and no .env
                if S is None:
                    if after_pull:                                       # a secret another instance made: written here
                        _write_env(envp, plain)
                        hashes[ref] = R
                elif R == S:                                             # the repo holds what was agreed, the .env is gone: deleted here
                    agep.unlink(missing_ok=True)
                    st["recipients"].pop(ref, None)
                    conflicts.discard(ref)
                    why.pop(ref, None)
                    changed = True
                else:
                    conflicts.add(ref)
                    why[ref] = "deleted_here"
                continue
            if not after_pull:                                           # only local changes, only on the last synced .age
                if L != R and S is not None and R == S and rec and ref not in conflicts and encrypt_ref(home, ref, st):
                    changed = True
                continue
            origin = _same_as_origin(home, agep, branch)
            if not origin:                                               # the .age was written here, not yet pushed
                if L != R and rec and ref not in conflicts and encrypt_ref(home, ref, st):
                    changed = True
            elif L == R:
                hashes[ref] = R
                conflicts.discard(ref)
                why.pop(ref, None)
            elif S is not None and L == S:                               # unchanged here, changed remote
                _write_env(envp, plain)
                hashes[ref] = R
                conflicts.discard(ref)
                why.pop(ref, None)
            elif S is not None and R == S:                               # changed here only (waits while nothing can be encrypted)
                if rec and encrypt_ref(home, ref, st):
                    changed = True
            else:
                conflicts.add(ref)
                why[ref] = "both"
        st["conflicts"] = sorted(conflicts)
        _save(home, st)
    warns += [f"{r}: {_WHY[why.get(r, 'both')]}; nothing overwritten (lm sync resolve {r} --keep local|remote)"
              for r in sorted(conflicts)]
    return warns, changed


def _mark_synced(home: Home) -> None:
    """After a successful push: every secret whose .age matches the local .env counts as agreed; of a secret that is gone
    on both sides (the deletion is pushed now) nothing is remembered any more."""
    with _mu:
        st = _store(home)
        refs = set(_refs(home))
        for ref in refs:
            envp, agep = _env(home, ref), _age(home, ref)
            if envp.is_file() and agep.is_file() and ref not in st["conflicts"]:
                plain = decrypt(home, agep.read_bytes())
                if plain is not None and plain == envp.read_bytes():
                    st["hashes"][ref] = _hash(plain)
        for gone in [r for r in {*st["hashes"], *st["recipients"], *st["why"]} if r not in refs]:
            for d in (st["hashes"], st["recipients"], st["why"]):
                d.pop(gone, None)
        _save(home, st)


def reencrypt_all(home: Home) -> list[str]:
    """For add-device / remove-device / approve: every secret again, for the recipients as they are now (the effective ones).
    Returns refs it could not do."""
    rec = effective_recipients(home)
    if not rec:
        raise SyncError("no recipients")
    skipped = []
    with _mu:
        st = _store(home)
        for ref in _refs(home):
            envp, agep = _env(home, ref), _age(home, ref)
            if ref in st["conflicts"]:
                skipped.append(ref)
                continue
            data = envp.read_bytes() if envp.is_file() else (decrypt(home, agep.read_bytes()) if agep.is_file() else None)
            if data is None:
                skipped.append(ref)
                continue
            _put_age(home, ref, data, rec, st)
        _save(home, st)
    return skipped


# ------------------------------------------------------------------------------------------------ git: stage, commit, pull, push
def _entries(home: Home) -> list[tuple[str, str]]:
    rc, out, _ = _g(home, "status", "--porcelain", "-z", "--untracked-files=all")
    res, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) > 3:
            res.append((e[:2], e[3:]))
            if e[0] in "RC":
                i += 1                                               # the old name of a rename follows
    return res


def _staged_changes(home: Home) -> list[tuple[str, bytes, str, str]]:
    """[(status, path, mode, sha)] of what the index would commit, from `git diff --cached --raw -z`: the path is the literal
    bytes git stores and the sha is the blob the commit will hold, so no path spelling (`:0:x`, a line break) can pick another
    blob.  Raises OSError / ValueError when git or its answer is not what we expect."""
    rc, raw = _gb(home, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", input=b"", timeout=60)
    if rc != 0:
        raise OSError("git diff --cached failed")
    parts = raw.split(b"\0")
    out = []
    for meta, path in zip(parts[0::2], parts[1::2]):
        fields = meta.lstrip(b":").split()
        if len(fields) != 5:
            raise ValueError("unexpected answer of git diff --raw")
        out.append((fields[4][:1].decode("ascii", "replace"), path, fields[1].decode("ascii", "replace"),
                    fields[3].decode("ascii", "replace")))
    return out


def _blobs(home: Home, shas: list[str]) -> tuple[dict[str, bytes], dict[str, str]]:
    """Blobs by object id, each read only when it is at most MAX_BYTES (memory stays bounded even for a file that grew after
    the size check).  Returns ({sha: content}, {sha: "big" | "unreadable"}); raises OSError when git itself fails."""
    got: dict[str, bytes] = {}
    bad: dict[str, str] = {}
    sizes: dict[str, int] = {}
    uniq = list(dict.fromkeys(shas))
    for i in range(0, len(uniq), 500):
        chunk = uniq[i:i + 500]
        rc, raw = _gb(home, "cat-file", "--batch-check", input="".join(f"{s}\n" for s in chunk).encode(), timeout=120)
        if rc != 0:
            raise OSError("git cat-file --batch-check failed")
        answers = raw.split(b"\n")
        for n, s in enumerate(chunk):
            f = answers[n].split() if n < len(answers) else []
            if len(f) == 3 and f[0] == s.encode() and f[1] == b"blob" and f[2].isdigit():
                sizes[s] = int(f[2])
            else:
                bad[s] = "unreadable"                    # missing, no blob (a nested repo), or an answer we do not understand
    bad.update({s: "big" for s, n in sizes.items() if n > MAX_BYTES})
    groups: list[list[str]] = [[]]
    total = 0
    for s in (x for x in sizes if x not in bad):
        if groups[-1] and (len(groups[-1]) >= 300 or total + sizes[s] > 64 * 2**20):
            groups.append([])
            total = 0
        groups[-1].append(s)
        total += sizes[s]
    for group in (g for g in groups if g):
        rc, raw = _gb(home, "cat-file", "--batch", input="".join(f"{s}\n" for s in group).encode(), timeout=120)
        if rc != 0:
            raise OSError("git cat-file --batch failed")
        pos = 0
        for n, s in enumerate(group):
            nl = raw.find(b"\n", pos)
            f = raw[pos:nl].split() if nl >= 0 else []
            body = raw[nl + 1:nl + 1 + sizes[s]] if nl >= 0 else b""
            if len(f) != 3 or f[0] != s.encode() or f[1] != b"blob" or f[2] != str(sizes[s]).encode() or len(body) != sizes[s]:
                bad.update({r: "unreadable" for r in group[n:]})          # the answer is out of step: trust none of the rest
                break
            got[s] = body
            pos = nl + 1 + sizes[s] + 1
    return got, bad


def _vet(home: Home, changes: list[tuple[str, bytes, str, str]]) -> dict[bytes, str]:
    """The staged changes that must not go up: {path: why}.  Fails closed: whatever cannot be read for the check is refused,
    never passed unchecked.  A deletion carries no content and passes; an `.age` file passes unread only when it starts with the
    age header (`age-encryption.org/v1`): the name alone proves nothing, so any other `.age` file is checked like every file."""
    refused: dict[bytes, str] = {}
    todo: list[tuple[bytes, str, str]] = []
    for status, raw_path, mode, sha in changes:
        if status == "D":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            refused[raw_path] = f"not committed (the name is not valid UTF-8): {raw_path.decode('utf-8', 'replace')!r}"
            continue
        if "\n" in path or "\r" in path:
            refused[raw_path] = f"not committed (a line break in the name): {path!r}"
        elif _NEVER.search(path):
            refused[raw_path] = f"not committed (never goes into the repo): {path}"
        elif mode == "160000":
            refused[raw_path] = f"not committed (nested git repo): {path}"
        else:
            todo.append((raw_path, path, sha))
    if not todo:
        return refused
    try:
        got, bad = _blobs(home, [t[2] for t in todo])
    except (OSError, ValueError):
        got, bad = {}, {}
    from .tools import secret_masker
    masker = secret_masker(home)
    for raw_path, path, sha in todo:
        data = got.get(sha)
        if data is None:
            refused[raw_path] = (f"not committed (over {MAX_BYTES // 2**20} MB): {path}" if bad.get(sha) == "big"
                                 else f"not committed (could not be read for the check): {path}")
        elif path.endswith(AGE) and data.startswith(AGE_HEAD):
            continue                                                       # ciphertext: there is nothing in it to read
        elif _MARKERS.search(data):
            refused[raw_path] = f"not committed (conflict markers in it): {path}"
        else:
            text = data.decode("utf-8", "replace")
            if masker.mask(text) != text:
                refused[raw_path] = f"not committed (contains a secret value): {path}"
    return refused


def _commit(home: Home) -> tuple[int, list[str]]:
    """Stage what may go, commit as `sync <machine>: <n> files`.  Refused (named, Rest goes through): a file over 5 MB,
    a nested git repo, anything that looks like plain secrets, anything holding a value of a local secret, a file with a
    conflict block, a name with a line break.  What is staged is vetted by object id whoever staged it, and what cannot be
    read for the check is refused.  A tree with unmerged entries commits nothing: `git add` would "resolve" them with the
    markers inside."""
    entries = _entries(home)
    stuck = [path for xy, path in entries if xy in _UNMERGED]
    if stuck:
        return 0, [f"not committed (unmerged, a conflict waits for a human): {s}" for s in stuck]
    warns, allowed = [], []
    for xy, path in entries:
        p = home.root / path
        if _NEVER.search(path):
            warns.append(f"not committed (never goes into the repo): {path}")
        elif "D" in xy:
            allowed.append(path)
        elif "\n" in path or "\r" in path:
            warns.append(f"not committed (a line break in the name): {path!r}")
        elif "�" in path:
            warns.append(f"not committed (the name is not valid UTF-8): {path!r}")
        elif path.endswith("/") or (p.is_dir() and (p / ".git").exists()):
            warns.append(f"not committed (nested git repo): {path}")
        elif p.is_file() and p.stat().st_size > MAX_BYTES:
            warns.append(f"not committed (over {MAX_BYTES // 2**20} MB): {path}")
        else:
            allowed.append(path)
    if allowed:
        rc, _, err = _g(home, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(allowed), timeout=120)
        if rc != 0:
            warns.append(f"git add failed: {_last(err)}")
    try:
        changes = _staged_changes(home)
    except (OSError, ValueError) as e:
        _g(home, "reset", "-q")                                             # what cannot be listed is not committed
        return 0, warns + [f"not committed (what is staged could not be listed: {e})"]
    refused = _vet(home, changes)
    if refused:
        rc, _ = _gb(home, "reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul", input=b"\0".join(refused), timeout=60)
        warns += list(refused.values())
        if rc != 0:                                                        # single paths failed: everything out, and no commit
            _g(home, "reset", "-q")
            return 0, warns + ["not committed (a refused file could not be taken out of the index alone: all unstaged)"]
    staged = [c for c in changes if c[1] not in refused]
    if staged:
        rc, _, err = _g(home, "commit", "-q", "-m", f"sync {home.machine}: {len(staged)} files", timeout=60)
        if rc != 0:
            warns.append(f"commit failed: {_last(err)}")
            return 0, warns
    return len(staged), warns


def _refs_in(home: Home, files: list[str]) -> list[str]:
    """`<name>/<account>` of every `<secrets>/<name>/<account>.env.age` among these paths of the repo."""
    prefix = _rel(home, _secrets_dir(home)) + "/"
    refs = []
    for f in files:
        name, _, fname = f[len(prefix):].partition("/") if f.startswith(prefix) else ("", "", "")
        if name and fname.endswith(".env" + AGE) and "/" not in fname:
            refs.append(f"{name}/{fname[:-len('.env' + AGE)]}")
    return refs


def _history_refs(home: Home) -> list[str]:
    """Every secret that has an `.age` anywhere in the history, also one that was deleted since: a device that was a recipient
    could read each of them."""
    try:
        rc, out, _ = _g(home, "log", "--all", "--format=", "--name-only", "--", _rel(home, _secrets_dir(home)), timeout=120)
        return sorted(set(_refs_in(home, out.splitlines()))) if rc == 0 else []
    except ValueError:
        return []


def _note_adopted(home: Home, files: list[str]) -> None:
    """A `.age` conflict was settled with the repo's file: the repo's file may be made for an older recipient list (a device
    was removed meanwhile), so `rewrap` makes it again for ours, once."""
    refs = _refs_in(home, files)
    if refs:
        with _mu:
            st = _store(home)
            st["adopted"] = sorted({*st["adopted"], *refs})
            _save(home, st)


def _distrust_removed(home: Home, branch: str) -> None:
    """Called when a fetch brought commits, before they are rebased in.  A device this instance confirmed that ANY version of
    `<secrets>/recipients` among the new commits does not have any more (the version before it had the same name with the same
    key) loses its confirmation, even when a later version has the line again: a removal can be written back by a commit, the
    fact that the device was removed (maybe because its key is compromised) cannot.  The final list alone (`_prune_confirmed`)
    would trust it again.  It then counts as not confirmed, with a warning, until `lm sync approve`.  The own key is never
    touched; the old fingerprint (never the key) stays for the warning."""
    own, ok = public_key(home), confirmed_pairs(home)
    cand = {n: k for n, k in ok.items() if k != own}
    if not cand:
        return
    try:
        rel = f"{_rel(home, _secrets_dir(home))}/{RECIPIENTS}"
    except ValueError:
        return
    new = f"origin/{branch}"
    base = _g(home, "merge-base", "HEAD", new)[1].strip()
    rc, out, _ = _g(home, "log", "--full-history", "--reverse", "--format=%H", f"{base}..{new}" if base else new, "--", rel, timeout=60)
    versions = out.split() if rc == 0 else []

    def listed(rev: str) -> dict[str, str]:
        rc, text, _ = _g(home, "show", f"{rev}:{rel}")
        return _parse_list(text)[0] if rc == 0 else {}                      # no readable list: nobody is on it

    before, gone = (listed(base) if base else {}), set()
    for sha in versions:
        now = listed(sha)
        gone |= {n for n, k in cand.items() if before.get(n) == k and now.get(n) != k}
        before = now
    if gone:
        with _mu:
            st = _store(home)
            st["prev_fp"].update({n: fingerprint(cand[n]) for n in gone})
            _save(home, st)
        _write_confirmed(home, {n: k for n, k in ok.items() if n not in gone})


def _pull(home: Home, branch: str) -> tuple[str, str]:
    """('ok'|'none'|'offline'|'auth'|'conflict'|'blocked', detail).  A conflict aborts the rebase: the tree stays as before.
    Only `.age` files are resolved by taking the repo's side; `reconcile` then decides by hash."""
    rc, _, err = _g(home, "fetch", "--quiet", "origin", timeout=TRANSFER)
    if rc != 0:
        return _net_fail(rc, err)
    if _g(home, "rev-parse", "--verify", "-q", f"origin/{branch}")[0] != 0:
        return "none", ""
    if _g(home, "merge-base", "--is-ancestor", f"origin/{branch}", "HEAD")[0] == 0:
        return "ok", ""
    _distrust_removed(home, branch)
    if _g(home, "status", "--porcelain", "--untracked-files=no")[1].strip():
        return "blocked", "tracked files with changes that cannot be committed block the pull"
    rc, _, err = _g(home, "rebase", f"origin/{branch}", timeout=120)
    adopted: list[str] = []
    while rc != 0:
        files = [x for x in _g(home, "diff", "--name-only", "--diff-filter=U")[1].splitlines() if x]
        if not files or not all(f.endswith(AGE) for f in files):
            _g(home, "rebase", "--abort")
            return "conflict", ", ".join(files) if files else _last(err)
        for f in files:
            if _g(home, "cat-file", "-e", f"HEAD:{f}")[0] == 0:           # (during a rebase HEAD is the repo's side) it has the file: its version
                ok = _g(home, "checkout", "--ours", "--", f)[0] == 0 and _g(home, "add", "--", f)[0] == 0
                adopted.append(f)
            else:                                                          # the repo dropped it: the drop is taken, the local .env stays
                ok = _g(home, "rm", "-q", "--", f)[0] == 0
            if not ok:
                _g(home, "rebase", "--abort")
                return "conflict", f
        quiet = _g(home, "diff", "--cached", "--quiet")[0] == 0
        rc, _, err = _g(home, "rebase", "--skip" if quiet else "--continue", timeout=120)
    _note_adopted(home, adopted)
    return "ok", ""


def _ahead(home: Home, branch: str) -> int:
    rc, out, _ = _g(home, "rev-list", "--count", f"origin/{branch}..HEAD" if
                    _g(home, "rev-parse", "--verify", "-q", f"origin/{branch}")[0] == 0 else "HEAD")
    return int(out.strip() or 0) if rc == 0 else 0


def _squash(home: Home, branch: str) -> bool:
    """Before a push: when two unpushed commits touch the same `.age` file, ALL unpushed commits become one commit with the same
    tree.  A secret written for a recipient list that has changed meanwhile (a device was removed and this instance had not
    pulled it yet) is made again for the new list after the pull, in a commit of its own: pushed as it is, the older commit goes
    up with it, and the removed device can read that version from the history.  Only a clean index is touched (what is staged
    has not been vetted); a commit that fails leaves the changes staged for the next `_commit`.  Returns whether it squashed."""
    new = f"origin/{branch}"
    if _g(home, "rev-parse", "--verify", "-q", new)[0] != 0 or _ahead(home, branch) < 2:
        return False
    try:
        secrets = _rel(home, _secrets_dir(home))
    except ValueError:
        return False
    rc, out, _ = _g(home, "log", "--format=", "--name-only", f"{new}..HEAD", "--", secrets, timeout=60)
    files = [ln for ln in out.splitlines() if ln.endswith(AGE)]
    if rc != 0 or len(files) == len(set(files)) or _g(home, "diff", "--cached", "--quiet")[0] != 0:
        return False
    if _g(home, "reset", "-q", "--soft", new)[0] != 0:
        return False
    rc, names, _ = _g(home, "diff", "--cached", "--name-only", "-z")
    n = len([x for x in names.split("\0") if x])
    if rc != 0 or not n:                                                    # the commits cancelled each other out: nothing to push
        return rc == 0
    return _g(home, "commit", "-q", "-m", f"sync {home.machine}: {n} files", timeout=60)[0] == 0


def _push(home: Home, branch: str) -> tuple[str, str]:
    """('ok'|'offline'|'auth'|'conflict'|'blocked'|'warn', detail).  Before every attempt unpushed commits that touch one `.age`
    twice become one (`_squash`).  A rejected push pulls and tries once more; the pull may have changed the recipient list, so
    what it made stale is made again first and goes up in the same commit as the file it replaces."""
    for attempt in (0, 1):
        _squash(home, branch)
        rc, _, err = _g(home, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", timeout=TRANSFER)
        if rc == 0:
            return "ok", ""
        if attempt == 0 and re.search(r"rejected|non-fast-forward|fetch first", err):
            st, why = _pull(home, branch)
            if st in ("ok", "none"):
                if rewrap(home)[0]:
                    _commit(home)
                continue
            return st, why
        line = _auth_line(rc, err)
        if line:
            return "auth", line
        return ("offline" if rc in (-1, -2) or _OFFLINE.search(err) else "warn"), _last(err)
    return "warn", "push rejected"


def _ensure_files(home: Home) -> None:
    gi = home.root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if MARK not in text:
        gi.write_text(text.rstrip("\n") + ("\n\n" if text else "") + IGNORE, encoding="utf-8", newline="\n")
    ga = home.root / ".gitattributes"
    have = ga.read_text(encoding="utf-8").splitlines() if ga.is_file() else []
    if any(a not in have for a in ATTRIBUTES):
        ga.write_text("\n".join(have + [a for a in ATTRIBUTES if a not in have]) + "\n", encoding="utf-8", newline="\n")


def _save_dirty(home: Home) -> int:
    """Copy the working copies of tracked files that differ from HEAD to `<state>/rebase-saved/<time>/`: `rebase --abort`
    resets them.  Returns how many were saved."""
    dest = _state(home) / "rebase-saved" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 0
    for xy, path in _entries(home):
        p = home.root / path
        if xy == "??" or not p.is_file() or p.stat().st_size > MAX_BYTES:
            continue
        (dest / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / path)
        n += 1
    return n


def _recover(home: Home) -> tuple[list[str], str | None]:
    """A sync that was cut off can leave git in the middle of a rebase.  The next commit would then take a file with its
    conflict markers and push it from a detached HEAD, so an unfinished rebase is aborted first.  Returns (what to report,
    the branch HEAD is on); the branch is None when the run must stop."""
    notes: list[str] = []
    gd = home.root / ".git"
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        saved = _save_dirty(home)
        rc, _, err = _g(home, "rebase", "--abort", timeout=60)
        if rc != 0 and ((gd / "rebase-merge").exists() or (gd / "rebase-apply").exists()):
            return [f"an unfinished rebase could not be aborted ({_last(err)}): nothing synced; run `git rebase --abort` in the tree"], None
        notes.append("an unfinished rebase (a sync that was cut off) was aborted" +
                     (f"; {saved} changed file{'s' * (saved != 1)} saved under system/state/rebase-saved first" if saved else ""))
    rc, out, _ = _g(home, "symbolic-ref", "-q", "--short", "HEAD")
    if rc != 0 or not out.strip():
        return notes + ["HEAD is on no branch: nothing synced (git switch main in the tree)"], None
    return notes, out.strip()


def run(home: Home, *, pull: bool = True, push: bool = True, lock_wait: float = 0.0, held: bool = False) -> Result:
    """One sync: encrypt local secret changes, commit, pull (rebase), bring remote secrets back, commit, push.
    `lock_wait`: seconds to wait for a sync that is running (secret write, session end); 0 skips at once.
    `held`: the caller holds the lock already (`approve`) and `run` does not take it."""
    if not enabled(home):
        return Result("skip", ["not a sync repo (lm sync init|join <url>)"])
    with (nullcontext(True) if held else _lock(home, lock_wait)) as got:
        if not got:
            return Result("skip", ["another sync is running"])
        r = Result()
        try:
            notes, branch = _recover(home)
            r.warnings += notes
            if branch is None:
                r.status = "warn"
                r.lines.append("stopped")
                _record(home, r)
                return r
            _ensure_files(home)
            w, _ = reconcile(home, branch, after_pull=False)
            r.warnings += w
            n, w = _commit(home)
            r.warnings += w
            state = "ok"
            if pull:
                state, why = _pull(home, branch)
                if state in ("offline", "auth", "conflict", "blocked"):
                    r.status = state if state in ("offline", "auth", "conflict") else "warn"
                    r.warnings.append({"offline": f"offline: {why}", "auth": _AUTH_WARN.format(why=why),
                                       "conflict": f"conflict, pull aborted, nothing overwritten: {why}", "blocked": why}[state])
                w, changed = reconcile(home, branch, after_pull=state in ("ok", "none"))
                r.warnings += [x for x in w if x not in r.warnings]
                if state in ("ok", "none"):
                    _prune_confirmed(home)                                   # a key that left the list is not confirmed any more
                    done, _ = rewrap(home)                                   # the list may have changed with this pull
                    changed = changed or bool(done)
                if changed:
                    n2, w2 = _commit(home)
                    n += n2
                    r.warnings += w2
            if push and r.status == "ok" and (state != "offline"):
                _squash(home, branch)                                        # no older version of a `.age` goes up beside its rewrap
                ahead = _ahead(home, branch)
                if ahead:
                    st, why = _push(home, branch)
                    if st == "ok":
                        _mark_synced(home)
                        r.lines.append(f"pushed {ahead} commit{'s' * (ahead != 1)}")
                        done, _ = rewrap(home)                               # the push may have pulled: list or conflict
                        if done:
                            n2, w2 = _commit(home)
                            n += n2
                            r.warnings += w2
                            st, why = _push(home, branch)
                    if st != "ok":
                        r.status = st if st in ("offline", "auth", "conflict") else "warn"
                        r.warnings.append(_AUTH_WARN.format(why=why) if st == "auth" else f"push failed ({st}): {why}")
                else:
                    _mark_synced(home)
            r.warnings += [w for w in recipient_warnings(home) if w not in r.warnings]
            if r.status == "ok" and r.warnings:
                r.status = "warn"
            if not r.lines:
                r.lines.append(f"{n} file{'s' * (n != 1)} committed" if n else "up to date" if r.status == "ok" else r.status)
        except Exception as e:                             # noqa: BLE001 - sync never takes a caller down
            r.status = "warn"
            r.warnings.append(f"sync stopped: {e.__class__.__name__}: {str(e)[:100]}")
        _record(home, r)
        return r


# ------------------------------------------------------------------------------------------------ the commands
def _machine_name(name: str | None) -> str:
    name = (name or re.sub(r"[^a-z0-9_-]", "-", socket.gethostname().lower())).strip("-_")[:32]
    if not _NAME.fullmatch(name or ""):
        raise SyncError("the machine name may hold lowercase letters, digits, - and _ (start with a letter or digit)")
    return name


def _url(url: str) -> str:
    from .init import check_repo
    try:
        return check_repo(url)
    except ValueError:
        raise SyncError(f"{url!r} is not a git URL or path") from None


def _need_none(home: Home) -> None:
    if (home.root / ".git").exists():
        raise SyncError("the tree already has a .git: it is a repo already (use `lm sync status`)")


def _bare_git(root: Path, *args: str, timeout: float = NET) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of git for `init` and `join`, which run before the tree is a repo; -1 timeout, -2 no git."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:ssh:git:file"})
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -2, "", str(e)
    return r.returncode, r.stdout, r.stderr


def init(home: Home, url: str, machine: str | None = None) -> list[str]:
    """First instance: make the tree a repo, key pair, first recipient, existing .env encrypted, first commit and push."""
    _need_none(home)
    url, name = _url(url), _machine_name(machine)
    rc, out, err = _bare_git(home.root, "ls-remote", "--heads", "--", url)
    if rc != 0:
        raise SyncError(f"cannot reach the repo: {_unreachable(rc, err)}")
    if out.strip():
        raise SyncError("the repo already has content: use `lm sync join <url>` on this tree instead")
    for args in (("init", "-q", "-b", BRANCH), ("remote", "add", "origin", url)):
        _bare_git(home.root, *args)
    _write_env(_state(home) / "machine", (name + "\n").encode())
    pub = make_identity(home)
    write_recipients(home, {name: pub})
    _save(home, {"hashes": {}, "conflicts": []})
    r = run(home)
    _g(home, "branch", "--set-upstream-to", f"origin/{BRANCH}")
    return [f"tree is now a sync repo: machine {name}, remote {url}", r.text(), f"key fingerprint: {fingerprint(pub)}",
            "next: `lm sync join <url>` on the other instance, then `lm sync add-device <name> <its public key>` here"]


def _git_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _ask_confirm(rec: dict[str, str]) -> bool:
    """At join: the repo's recipient list with fingerprints, and the question.  Without a terminal the answer is no: then only
    this instance's own key counts and `lm sync approve <name> <fingerprint>` does the rest."""
    import sys
    if not (sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    print("The repo lists these devices as recipients of your secrets:")
    for n, k in rec.items():
        print(f"  {n}  {fingerprint(k)}")
    return input("Are all of them yours?  Say yes only after comparing the fingerprints [y/N] ").strip().lower() in ("y", "yes")


def _own_name_free(listed: dict[str, str], name: str, own: str | None) -> None:
    """`join` takes a line of the repo's list under this machine's own name only when it is this instance's own key.  Anything
    else is somebody who put another key under a name that is easy to guess ('server' for a server): taken over by a yes at
    the question, it would have replaced the own key and every secret this instance writes would be made for that key.  `own`
    None: no key yet, a fresh one cannot be in a list that was written before it existed."""
    other = listed.get(name)
    if other is not None and other != own:
        raise SyncError(f"recipients already lists {name} with a different key; refusing (listed: {fingerprint(other)}, "
                        f"this machine's key is not that one).  Take another name (--machine), or remove {name} first with "
                        f"`lm sync remove-device {name}` on an instance that is in the list.")


def _rmtree(path: Path) -> None:
    """Remove a folder tree, read-only files included (git writes its objects read-only, which stops `shutil.rmtree` on Windows)."""
    for top, dirs, files in os.walk(path, topdown=False):
        for n in files:
            f = os.path.join(top, n)
            try:
                os.chmod(f, 0o700)
                os.unlink(f)
            except OSError:
                pass
        for n in dirs:
            d = os.path.join(top, n)
            try:
                os.unlink(d) if os.path.islink(d) else os.rmdir(d)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _join_collisions(root: Path, paths: list[str]) -> list[str]:
    """Where a repo file would need a folder here or a folder a file: `join` cannot place that and must stop before it moves anything."""
    bad = []
    for path in paths:
        p = root / path
        if p.is_dir() and not p.is_symlink():
            bad.append(f"{path} (a folder here, a file in the repo)")
            continue
        for parent in Path(path).parents:
            if parent == Path("."):
                break
            q = root / parent
            if q.exists() and not q.is_dir():
                bad.append(f"{parent.as_posix()} (a file here, a folder in the repo)")
                break
    return bad


def _missing_parents(root: Path, file: Path) -> list[Path]:
    """The folders on the way from `root` to `file` that do not exist yet."""
    out, d = [], file.parent
    while d != root and root in d.parents and not d.exists():
        out.append(d)
        d = d.parent
    return out


def _free(p: Path) -> Path:
    """`p`, or `p` with a number if that name is taken (nothing is overwritten by a rename)."""
    q, n = p, 1
    while q.exists():
        n += 1
        q = p.with_name(f"{p.name}{n}")
    return q


def join(home: Home, url: str, machine: str | None = None, *, confirm=None) -> list[str]:
    """Another instance: clone INTO the existing tree.  A local file the repo also has: the repo wins, the local version
    stays as `<file>.lokal`; local files the repo lacks stay and go up with the next sync.  `confirm(list) -> bool`: the
    recipient list in the repo is shown and the user confirms it (default: ask on a terminal, no terminal means no; a list
    with a problem is not shown at all).

    All or nothing: a file where a folder is (or the reverse) stops it before anything is moved; every file it moves is
    kept, and any failure or interruption puts the tree back exactly as it was and removes the `.git` it made."""
    _need_none(home)
    url, name = _url(url), _machine_name(machine)
    root = home.root
    moved: list[tuple[Path, Path]] = []                    # (where it is now, where it was): local files that went aside
    existed: set[str] = set()                              # repo paths that were files here before
    new_dirs: set[Path] = set()                            # folders the checkout and join will make
    saved: dict[Path, bytes | None] = {}                   # files join writes outside the repo's own: their old bytes
    entries: list[tuple[str, str]] = []                    # (blob sha, path) of what the repo holds
    kept: list[str] = []
    backup: Path | None = None
    changing = False                                       # set at the first change: before that only the `.git` is to undo

    def remember(f: Path) -> None:
        if f not in saved:
            saved[f] = f.read_bytes() if f.is_file() else None

    try:
        secrets_rel = _rel(home, _secrets_dir(home))
    except ValueError:
        secrets_rel = "secrets"
    state = _state(home)
    _bare_git(root, "init", "-q", "-b", BRANCH)
    try:
        _bare_git(root, "remote", "add", "origin", url)
        rc, _, err = _bare_git(root, "fetch", "--quiet", "origin", timeout=TRANSFER)
        if rc != 0:
            raise SyncError(f"cannot fetch the repo: {_unreachable(rc, err, TRANSFER)}")
        if _bare_git(root, "rev-parse", "--verify", "-q", f"origin/{BRANCH}")[0] != 0:
            raise SyncError(f"the repo has no branch {BRANCH}: use `lm sync init <url>` on the first instance")
        _, listing, _ = _bare_git(root, "ls-tree", "-r", "-z", f"origin/{BRANCH}")
        for e in (x for x in listing.split("\0") if x):
            meta, _, path = e.partition("\t")
            entries.append((meta.split()[2], path))
        bad_paths = _join_collisions(root, [path for _, path in entries])
        if bad_paths:
            raise SyncError("a file here and a folder in the repo (or the reverse) cannot be joined; nothing was changed: "
                            + ", ".join(bad_paths[:6]) + (" ..." if len(bad_paths) > 6 else ""))
        rc, text, _ = _bare_git(root, "show", f"origin/{BRANCH}:{secrets_rel}/{RECIPIENTS}")
        listed, bad = _parse_list(text) if rc == 0 else ({}, [])
        _own_name_free(listed, name, public_key(home))     # before the user is asked anything: a yes would not change this
        agreed = False
        if listed and not bad:                             # a list with a problem is not shown for confirmation at all
            try:
                agreed = bool((confirm or _ask_confirm)(listed))
            except (EOFError, OSError):
                agreed = False
        # ---- from here on everything has a way back: first what is needed to undo it, then the first change
        for _, path in entries:
            if (root / path).is_file():
                existed.add(path)
            else:
                new_dirs.update(_missing_parents(root, root / path))
        for f in (root / ".gitignore", root / ".gitattributes", state / "machine", state / "age.key", state / CONFIRMED):
            remember(f)
        new_dirs.update(_missing_parents(root, state / "machine"))
        changing = True
        backup = Path(tempfile.mkdtemp(prefix=".lm-join-", dir=root))
        for sha, path in entries:
            p = root / path
            if not p.is_file():
                continue
            if _git_sha(p.read_bytes()) != sha:
                lok = _free(p.with_name(p.name + ".lokal"))
                os.replace(p, lok)
                moved.append((lok, p))
                kept.append(lok.relative_to(root).as_posix())
            else:
                parked = backup / path                     # the same as the repo's: the checkout writes it again
                parked.parent.mkdir(parents=True, exist_ok=True)
                os.replace(p, parked)
                moved.append((parked, p))
        rc, _, err = _bare_git(root, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
        if rc != 0:
            raise SyncError(f"checkout failed: {_last(err)}")
        _bare_git(root, "branch", "--set-upstream-to", f"origin/{BRANCH}")
        home = Home(root)
        state = _state(home)                               # what the repo's own settings say now
        for f in (state / "machine", state / "age.key", state / CONFIRMED):
            remember(f)
        new_dirs.update(_missing_parents(root, state / "machine"))
        _write_env(state / "machine", (name + "\n").encode())
        pub = make_identity(home)
        _own_name_free(listed, name, pub)                  # (the key may be another one than the tree had before the checkout)
        _ensure_files(home)
        if agreed:
            confirm_keys(home, listed)
    except BaseException:
        if changing:                                       # what the checkout and join wrote goes, what was moved comes back
            for _, path in entries:
                if path not in existed:
                    try:
                        (root / path).unlink()
                    except OSError:
                        pass
            for src, dst in reversed(moved):
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
                except OSError:
                    pass
            for f, old in saved.items():
                try:
                    if old is None:
                        f.unlink(missing_ok=True)
                    else:
                        f.write_bytes(old)
                except OSError:
                    pass
            for d in sorted(new_dirs, key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        _rmtree(root / ".git")
        raise
    finally:
        if backup is not None:
            _rmtree(backup)
    ok = confirmed_pairs(home)
    out = [f"joined {url} as machine {name}"]
    if bad:
        out.append("the recipient list in the repo is invalid (" + "; ".join(bad[:3]) + "): nothing in it is confirmed")
    out += [f"recipient {n}  {fingerprint(k)}  {'confirmed' if _is_trusted(n, k, pub, ok) else 'NOT confirmed: lm sync approve ' + n}"
            for n, k in ({} if bad else listed).items()]
    if kept:
        out.append("local versions kept (the repo's version is in place): " + ", ".join(kept))
    out += [f"public key: {pub}", f"key fingerprint: {fingerprint(pub)}",
            f"on an instance that is already in: lm sync add-device {name} {pub}", "then here: lm sync"]
    return out


def _need_ready(home: Home) -> None:
    if not enabled(home):
        raise SyncError("not a sync repo (lm sync init|join <url>)")
    if _identity(home) is None:
        raise SyncError("this instance has no key yet")


def _blocked(r: Result) -> None:
    if r.status in ("conflict", "offline", "auth", "skip"):
        raise SyncError(f"sync first ({r.status}): " + r.text())


def add_device(home: Home, name: str, key: str) -> list[str]:
    """Take a device into the recipient list.  Whoever runs this on this instance names the key itself, so that is the
    confirmation here; the other instances confirm it with `approve`.  The list is read, changed and written with the sync
    lock held, so a pull cannot change it in between and be undone by this write."""
    _need_ready(home)
    name, key = _machine_name(name), _check_key(key)
    _blocked(run(home, lock_wait=LOCK_WAIT))
    with _lock(home, LOCK_WAIT) as got:
        if not got:
            raise SyncError("another sync is running: try again in a moment")
        rec = _valid_list(home)
        if public_key(home) not in rec.values():
            raise SyncError("this instance is not a recipient itself: it cannot hand out access")
        if name in rec and rec[name] != key:
            raise SyncError(f"{name} is in the list with another key (fingerprint {fingerprint(rec[name])}): "
                            f"remove it first (lm sync remove-device {name}) or take another name")
        for other, k in rec.items():
            if k == key and other != name:
                raise SyncError(f"that key is in the list already, as {other}")
        rec[name] = key
        write_recipients(home, rec)
        confirm_keys(home, {name: key})                    # this instance's operator adds it: that is the confirmation
        with _mu:
            st = _store(home)
            st["removing"] = [x for x in st["removing"] if x != name]       # taken in again: nothing is pending for it
            _save(home, st)
        skipped = reencrypt_all(home)
    r = run(home, pull=False, lock_wait=LOCK_WAIT)
    return [f"{name} added; {len(rec)} recipient{'s' * (len(rec) != 1)}", r.text()] + (
        [f"not re-encrypted (conflict or unreadable): {', '.join(skipped)}"] if skipped else [])


def _settle(home: Home, *, pull: bool) -> tuple[Result, list[str]]:
    """After a recipient was removed: push, then make sure no `.age` that this instance can open is still made for the old
    list.  Every such file is rewritten (`rewrap(foreign=True)`: also the ones other instances wrote, also the ones a push
    conflict took from the repo's side) and pushed again, until none is left (4 rounds at most).  Returns the last Result and
    what is still not certain (empty: certain, as far as this instance can open and judge)."""
    branch = _branch(home)
    r = Result("warn", warnings=["nothing ran"])
    for i in range(4):
        r = run(home, pull=pull and i == 0, lock_wait=LOCK_WAIT)
        if r.status in ("offline", "auth", "conflict", "skip"):
            return r, [f"the sync stopped ({r.status}): " + "; ".join(r.warnings or r.lines)]
        done, unsure = rewrap(home, foreign=True)
        if not done:
            why = []
            if _ahead(home, branch):
                why.append("the change is not pushed yet")
            if unsure:
                why.append("cannot be opened here, so not checked: " + ", ".join(unsure))
            return r, why
    return r, ["files made for the old list kept turning up"]


def remove_device(home: Home, name: str) -> list[str]:
    """Take a device off the recipient list and lock it out: every secret is made again for the new list, pushed, and only
    when no `.age` is left that this instance can open and that is still made for the old list does it say "removed".  If
    that cannot be reached (offline, a secret this instance cannot open) it raises, and the same command finishes the job."""
    _need_ready(home)
    rec = _valid_list(home)
    pending = name in _store(home)["removing"]
    if name not in rec and not pending:
        raise SyncError(f"no device {name!r} (known: {', '.join(rec) or 'none'})")
    if name == home.machine:
        raise SyncError("this is the instance itself: remove it from another one")
    if public_key(home) not in rec.values():
        raise SyncError("this instance is not a recipient itself: it cannot open the secrets, so it cannot lock anyone out")
    fresh = name in rec
    if fresh:
        _blocked(run(home, lock_wait=LOCK_WAIT))
        with _lock(home, LOCK_WAIT) as got:                # read, change and write the list with the lock: no pull in between
            if not got:
                raise SyncError("another sync is running: try again in a moment")
            rec = _valid_list(home)
            rec.pop(name, None)
            write_recipients(home, rec)
            _prune_confirmed(home)                          # its key is not confirmed any more
            with _mu:
                st = _store(home)
                st["removing"] = sorted({*st["removing"], name})
                _save(home, st)
            reencrypt_all(home)
    r, why = _settle(home, pull=not fresh)
    if why:
        raise SyncError(f"{name} is off the recipient list but not locked out yet: " + "; ".join(why) +
                        f".  Meanwhile take its access to the repo away (delete its deploy key at the git host).  "
                        f"Repeat `lm sync remove-device {name}` when that is settled.")
    with _mu:
        st = _store(home)
        st["removing"] = [x for x in st["removing"] if x != name]
        _save(home, st)
    rec = read_recipients(home)
    now = _refs(home)                                      # read after the last pull: also what others wrote for it meanwhile
    gone = [x for x in _history_refs(home) if x not in now]
    return [f"{name} removed; {len(rec)} recipient{'s' * (len(rec) != 1)}", r.text(),
            f"First take the access of {name} to the repo away: delete its deploy key at the git host.  Until then it can "
            f"still fetch every file that is pushed from now on, and a removal from the list protects only the versions made after it.",
            f"{name} can still open older versions from the git history: change these secrets now:",
            *("  " + x for x in now), *(f"  {x}  (deleted since, but still in the history)" for x in gone)]


def _listed(rec: dict[str, str], name: str) -> str:
    if name not in rec:
        raise SyncError(f"no device {name!r} in the recipient list (known: {', '.join(rec) or 'none'})")
    return rec[name]


def _show_device(home: Home, name: str) -> list[str]:
    """`approve <name>` without a fingerprint: what stands in the list and how to confirm it.  Confirms nothing."""
    rec = _valid_list(home)
    key = _listed(rec, name)
    fp = fingerprint(key)
    mine, ok = public_key(home), confirmed_pairs(home)
    if _is_trusted(name, key, mine, ok):
        return [f"{name}: fingerprint {fp}, confirmed already"]
    was = fingerprint(ok[name]) if name in ok else _store(home)["prev_fp"].get(name)
    why = (f"The key of {name} has changed: {was} was confirmed before." if was and was != fp
           else f"{name} is not confirmed on this instance.")
    return [f"{name}: fingerprint {fp}", why,
            "Compare the fingerprint with the one `lm sync status` shows on that device itself, not with anything that came "
            "through the repo.",
            f"If they are the same: lm sync approve {name} {fp}"]


def approve(home: Home, name: str, given: str | None = None) -> list[str]:
    """`lm sync approve <name> <fingerprint>`: take a device of the repo's recipient list into what this instance trusts.  The
    confirmation is bound to the KEY, not to the name: it has to name the fingerprint (at least 16 hex digits, dashes do not
    count) of the key that stands under that name in the list at the moment the sync lock is held, otherwise nothing is
    confirmed: a pull may have put another key under the name since it was shown.  Without a fingerprint it only shows
    the device and says how to confirm it."""
    _need_ready(home)
    name = (name or "").strip()
    want = _norm_fp(given or "")
    if not want:
        return _show_device(home, name)
    if not re.fullmatch(rf"[0-9a-f]{{{APPROVE_MIN},{FP_HEX}}}", want):
        raise SyncError(f"name the fingerprint: at least {APPROVE_MIN} of its characters (0-9 and a-f, dashes do not count)")
    with _lock(home, LOCK_WAIT) as got:
        if not got:
            raise SyncError("another sync is running: try again in a moment")
        rec = _valid_list(home)                            # read now, with the lock: no pull can change it before it is used
        key = _listed(rec, name)
        fp = fingerprint(key)
        if not _norm_fp(fp).startswith(want):
            raise SyncError(f"the key that stands under {name!r} in the recipient list is not the one with the fingerprint you "
                            f"named: the list has changed, or the fingerprint is wrong.  Nothing was confirmed.  "
                            f"`lm sync approve {name}` shows what stands there now; compare it again with the device itself.")
        if _is_trusted(name, key, public_key(home), confirmed_pairs(home)):
            return [f"{name} is confirmed already (fingerprint {fp})"]
        confirm_keys(home, {name: key})
        skipped = reencrypt_all(home) if public_key(home) in effective_recipients(home).values() else []
        r = run(home, held=True)
    return [f"{name} confirmed (fingerprint {fp}): secrets are encrypted for it from now on", r.text()] + (
        [f"not re-encrypted (conflict or unreadable): {', '.join(skipped)}"] if skipped else [])


def resolve(home: Home, ref: str, keep: str) -> list[str]:
    """A secret changed here and in the repo: keep the local .env (it goes up) or the repo's (it replaces the local one)."""
    from . import secrets as S
    _need_ready(home)
    if keep not in ("local", "remote"):
        raise SyncError("--keep local|remote")
    ref = next((r for r in _refs(home) if S.canon_ref(r) == S.canon_ref(ref)), ref)
    with _mu:
        st = _store(home)
        if ref not in st["conflicts"]:
            raise SyncError(f"{ref} is not in conflict")
        envp, agep = _env(home, ref), _age(home, ref)
        plain = decrypt(home, agep.read_bytes()) if agep.is_file() else None
        if agep.is_file() and plain is None:
            raise SyncError(f"{ref}: the repo's version cannot be decrypted here")
        if keep == "remote":
            if plain is not None:                          # the repo's value replaces the local one
                _write_env(envp, plain)
                st["hashes"][ref] = _hash(plain)
            else:                                          # the repo dropped it: so does this instance
                envp.unlink(missing_ok=True)
                st["hashes"].pop(ref, None)
                st["recipients"].pop(ref, None)
        elif envp.is_file():                               # the local value goes up
            if not encrypt_ref(home, ref, st):
                raise SyncError(f"{ref}: no recipients")
            st["hashes"][ref] = _hash(plain if plain is not None else envp.read_bytes())
        else:                                              # deleted here: the repo's file goes
            agep.unlink(missing_ok=True)
            st["hashes"].pop(ref, None)
            st["recipients"].pop(ref, None)
        st["conflicts"].remove(ref)
        st.setdefault("why", {}).pop(ref, None)
        _save(home, st)
    r = run(home, lock_wait=LOCK_WAIT)
    return [f"{ref}: kept the {keep} version", r.text()]


def _ago(iso: str) -> str:
    try:
        s = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return "never"
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago"


def _not_pushed(n: int) -> str:
    """'1 commit not pushed': what `status` and the checklist say next to a result that was fine, so a commit that waits for its
    push is never hidden behind a plain 'ok'.  Empty when nothing waits."""
    return f"{n} commit{'s' * (n != 1)} not pushed" if n else ""


def status(home: Home) -> list[str]:
    if not enabled(home):
        return ["not a sync repo (lm sync init|join <url>)"]
    branch, ls = _branch(home), last_status(home)
    rec, bad = read_list(home)
    rec = {} if bad else rec
    mine = public_key(home)
    st = _store(home)
    dirty = len([1 for _ in _entries(home)])
    ahead = _ahead(home, branch)
    last = ", ".join(x for x in ((ls or {}).get("status", ""), _not_pushed(ahead)) if x)
    url = _g(home, "remote", "get-url", "origin")[1].strip()
    locked = [r for r in _refs(home) if _age(home, r).is_file() and not _env(home, r).is_file()
              and decrypt(home, _age(home, r).read_bytes()) is None]
    ok = confirmed_pairs(home)
    out = [f"machine      {home.machine}" + (f"  (key {fingerprint(mine)})" if mine else ""), f"remote       {url or '-'} ({branch})",
           f"last sync    {_ago(ls['time']) if ls else 'never'}" + (f" ({last})" if last else ""),
           f"unsynced     {dirty} changed file{'s' * (dirty != 1)}, {ahead} unpushed commit{'s' * (ahead != 1)}",
           "recipients   " + (f"INVALID ({'; '.join(bad[:3])})" if bad else
                              ", ".join(f"{n}{' (this)' if k == mine else ''}{'' if _is_trusted(n, k, mine, ok) else ' (NOT confirmed)'}"
                                        for n, k in rec.items()) or "none"),
           f"can decrypt  {'yes' if mine in rec.values() else 'no: not a recipient (ask for add-device)' if mine else 'no key'}"]
    out += [f"RECIPIENTS   {w}" for w in recipient_warnings(home)]
    out += [f"REMOVING     {n}: not locked out yet, repeat: lm sync remove-device {n}" for n in st["removing"]]
    out += [f"CONFLICT     {r}: {_WHY[st.get('why', {}).get(r, 'both')]}; lm sync resolve {r} --keep local|remote" for r in st["conflicts"]]
    out += [f"locked       {r}" for r in locked]
    out += [f"warning      {w}" for w in (ls.get("warnings") or [])]
    return out


# ------------------------------------------------------------------------------------------------ automatic runs
EXIT_WAIT = 20.0                       # seconds the process end waits for running sync threads (cli.py and the TUI call nothing)
_workers: set[threading.Thread] = set()
_waited = 0.0                          # seconds `session_end` has waited already: the exit hook waits for the rest only


def _bg(fn, *args) -> threading.Thread:
    """A sync thread.  It is a daemon, so a hung git never keeps the process alive, but it is registered: `_finish` (atexit)
    waits for it, so the end of the process does not cut a run off between its commit and the release of the lock."""
    def work():
        try:
            fn(*args)
        except Exception:                                  # noqa: BLE001 - a sync thread never takes a caller down
            pass
        finally:
            _workers.discard(threading.current_thread())
    t = threading.Thread(target=work, daemon=True, name="lm-sync")
    _workers.add(t)
    t.start()
    return t


def _finish() -> None:
    """Process end (atexit, so cli.py and the TUI need no change): drop a debounce that is not yet due (`session_end` has
    committed what there was), then wait for the running sync threads, at most EXIT_WAIT seconds in all.  No thread is
    started here: from Python 3.12 on that is refused while the interpreter shuts down."""
    with _mu:
        pending = list(_timers.values())
        _timers.clear()
    for t in pending:
        t.cancel()
    end = time.monotonic() + max(0.0, EXIT_WAIT - _waited)
    for t in list(_workers):
        left = end - time.monotonic()
        if left <= 0:
            break
        if t is not threading.current_thread():
            t.join(left)


atexit.register(_finish)


def _lock_owner(home: Home) -> int | None:
    try:
        owner = (_state(home) / "sync.lock").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return int(owner) if owner.isdigit() else None


def kick(home: Home) -> None:
    """A secret was written.  The commit is made before this returns, so the `.age` is in the history whatever the process
    does next (`lm secret set` ends right after).  Pull and push follow in a thread that the process end waits for.  A sync
    of ANOTHER process is waited for; a sync thread of this process (the TUI) is not: the caller may be the UI, and that
    thread's successor commits and pushes as soon as the lock is free."""
    if enabled(home) and _depth() == 0:
        run(home, pull=False, push=False, lock_wait=0.0 if _lock_owner(home) == os.getpid() else LOCK_WAIT)
        _bg(lambda: run(home, lock_wait=LOCK_WAIT))


_pulls: dict[str, Future] = {}
_last_auto: dict[str, float] = {}
_timers: dict[str, threading.Timer] = {}


def start_pull(home: Home) -> Future | None:
    """Session start: one pull (+ push of what waits) per process in the background; the Future holds the Result."""
    if not enabled(home) or _depth() != 0:
        return None
    key = str(home.root)
    with _mu:
        if key not in _pulls:
            fut: Future = Future()
            _pulls[key] = fut

            def work():
                try:
                    fut.set_result(run(home))
                except Exception as e:                     # noqa: BLE001
                    fut.set_result(Result("warn", warnings=[str(e)[:100]]))
            _bg(work)
        return _pulls[key]


def tick(home: Home) -> None:
    """TUI timer: pull and push in the background, never waits."""
    if enabled(home) and _depth() == 0:
        _bg(run, home)


def _dirty_run(home: Home) -> None:
    me = threading.current_thread()
    _workers.add(me)                                       # a debounce Timer counts as a sync thread while it runs
    try:
        with _mu:
            _last_auto[str(home.root)] = time.time()
            _timers.pop(str(home.root), None)
        if enabled(home) and (_entries(home) or _ahead(home, _branch(home))):
            run(home, pull=False)
    finally:
        _workers.discard(me)


def after_turn(home: Home) -> None:
    """After an agent turn: commit + push if files changed, at most once per 30 s (a later change waits for its slot)."""
    if not enabled(home) or _depth() != 0:
        return
    key = str(home.root)
    with _mu:
        if key in _timers:
            return
        wait = DEBOUNCE - (time.time() - _last_auto.get(key, 0))
        if wait <= 0:
            _last_auto[key] = time.time()
            _bg(_dirty_run, home)
        else:
            t = threading.Timer(wait, _dirty_run, (home,))
            t.daemon = True
            _timers[key] = t
            t.start()


def session_end(home: Home, wait: float = 20) -> None:
    """End of a session: commit + push, waits at most `wait` seconds.  A sync that is running (after_turn) is waited for, not
    skipped, so the last change of the session is in the commit.  What still runs after `wait` is waited for by the process
    end (`_finish`) for the rest of EXIT_WAIT."""
    global _waited
    if enabled(home) and _depth() == 0:
        t0 = time.monotonic()
        _bg(lambda: run(home, pull=False, lock_wait=wait)).join(wait)
        _waited += time.monotonic() - t0


# ------------------------------------------------------------------------------------------------ for `lm check`
def check_lines(home: Home, pull: bool = True) -> list[tuple[str, str]]:
    """[(status, detail)] for the checklist line `sync`: ok | warn | info.  With `pull` the start pull is awaited briefly."""
    out: list[tuple[str, str]] = []
    r = None
    if pull:
        fut = start_pull(home)
        if fut is not None:
            try:
                r = fut.result(timeout=8)
            except Exception:                              # noqa: BLE001
                out.append(("info", "pull still running"))
    ls = last_status(home)
    st = _store(home)
    bad = r.status if r else ls.get("status", "")
    warns = list(r.warnings if r else ls.get("warnings", []))
    out += [("warn", w) for w in recipient_warnings(home, short=True)]            # an invalid list, a device nobody here has confirmed
    waits = _not_pushed(_ahead(home, _branch(home)))
    if bad == "offline":
        out.append(("warn", "offline: the next occasion catches up" + (f" ({waits})" if waits else "")))
    elif bad == "auth":                                    # the repo refuses the login: waiting does not help
        out.append(("warn", (next((w for w in warns if w.startswith("auth")), _AUTH_WARN.format(why="see lm sync"))
                             + (f" ({waits})" if waits else ""))[:160]))
    elif bad == "conflict":                                # a text or binary conflict: the pull was aborted, nothing is synced
        files = next((w.partition(": ")[2] for w in warns if w.startswith(CONFLICT_WARNS)), "")
        out.append(("warn", f"text conflict, pull aborted, nothing overwritten: {files or 'see lm sync'}"[:160]))
    elif bad == "skip":                                    # no sync happened at all
        out.append(("warn", "sync skipped: " + (r.lines[0] if r and r.lines else "another sync is running")))
    for ref in st["conflicts"]:
        out.append(("warn", f"{ref} {_WHY[st.get('why', {}).get(ref, 'both')]}: lm sync resolve {ref} --keep local|remote"))
    for w in warns:
        if not w.startswith(("offline", "auth", *CONFLICT_WARNS, *RECIPIENT_WARNS)) and "lm sync resolve" not in w:
            out.append(("warn", w[:160]))
    if not out:
        n = len(_refs(home))
        out.append(("ok", f"{home.machine} · {_branch(home)} · {_ago(ls['time']) if ls else 'not yet synced'}"
                          + (f" · {n} secret{'s' * (n != 1)}" if n else "") + (f" · {waits}" if waits else "")))
    return out


def config_warnings(home: Home) -> list[str]:
    """Machine-specific items that must stand out in a sync repo: `host: local` and triggers without `machine:`."""
    out = []
    try:
        for n in home.list_servers():
            if home.load_server(n).legacy_local:
                out.append(f"server {n}: `host: local` is the old form; add `machine: <name of that instance>`")
    except Exception:                                      # noqa: BLE001
        pass
    try:
        miss = [t for t in home.list_triggers() if not home.load_trigger(t).machine]
        if miss:
            out.append(f"trigger without `machine:` (runs only where always_on is set): {', '.join(miss[:5])}")
    except Exception:                                      # noqa: BLE001
        pass
    return out
