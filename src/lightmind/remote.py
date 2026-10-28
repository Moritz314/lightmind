"""The permanent session on another server: `ssh -t <alias> 'tmux new -A -s lm <tree>/system/lm'`.

The values come from `servers/<name>/info.md`: `ssh` (an alias of ~/.ssh/config, or a list of aliases tried in order and the
first one that answers is used; else `user@host` and `port`),
`lm_home` (the tree on that server, default `~/lightmind`).  `tmux new -A` attaches to the session `lm` when it
exists and creates it otherwise, so closing the terminal leaves the session running and the next call finds it.
No secret is involved: the key stays in the ssh config of this machine."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys

from .model import Server

SESSION = "lm"                                   # the tmux session name
DEFAULT_TREE = "~/lightmind"
_WORD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$")          # an ssh alias or user/host: never an option, never a space
_TREE = re.compile(r"^(~/)?[A-Za-z0-9_./+@%-]+$")             # a path without shell syntax


class RemoteError(ValueError):
    """The server cannot be reached this way (local, or its info.md is incomplete or unsafe)."""


def targets(s: Server) -> list[list[str]]:
    """The ssh destinations of `s`, in the order they are tried: one `[alias]` per entry of `ssh`, or `[-p, port, user@host]`."""
    if s.local:
        raise RemoteError(f"{s.name} is this machine; nothing to connect to")
    raw = s.ssh if isinstance(s.ssh, (list, tuple)) else [s.ssh]
    aliases = [str(a or "").strip() for a in raw]
    if any(a and not _WORD.match(a) for a in aliases):
        bad = next(a for a in aliases if a and not _WORD.match(a))
        raise RemoteError(f"{s.name}: `ssh: {bad}` is not a plain ssh alias")
    if any(aliases):
        return [[a] for a in aliases if a]
    host = s.host.strip()
    dest = f"{s.user.strip()}@{host}" if s.user.strip() else host
    if not _WORD.match(dest):
        raise RemoteError(f"{s.name}: host/user in info.md are not plain names")
    return [(["-p", str(int(s.port))] if int(s.port) != 22 else []) + [dest]]


def target(s: Server) -> list[str]:
    """The first ssh destination of `s` (no network): `[alias]` or `[-p, port, user@host]`."""
    return targets(s)[0]


PROBE = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]


def reachable(dest: list[str]) -> bool:
    """Does `ssh <dest> true` work without asking for anything?  FileNotFoundError (no ssh) is not caught."""
    try:
        return subprocess.run(["ssh", *PROBE, *dest, "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def pick(s: Server) -> list[str]:
    """The destination to connect to: the only one as it is (no probe), else the first of the list that is reachable."""
    ts = targets(s)
    if len(ts) == 1:
        return ts[0]
    for dest in ts:
        if reachable(dest):
            return dest
    raise RemoteError(f"{s.name}: none of {', '.join(' '.join(d) for d in ts)} is reachable")


def tree_path(s: Server) -> str:
    tree = (s.lm_home or DEFAULT_TREE).strip().rstrip("/")
    if not _TREE.match(tree) or ".." in tree.split("/"):
        raise RemoteError(f"{s.name}: `lm_home: {tree}` must be a plain path (no spaces or shell characters)")
    return tree


def remote_command(s: Server, dest: list[str] | None = None) -> list[str]:
    """The full command line, ready for `subprocess` (a list, no local shell); `dest` as `pick` chose it, default the first."""
    tree = tree_path(s)
    path = ("~/" + shlex.quote(tree[2:])) if tree.startswith("~/") else shlex.quote(tree)   # ~ must stay unquoted
    return ["ssh", "-t", *(dest or target(s)), f"tmux new -A -s {SESSION} {path}/system/lm"]


def server_of(home, name: str) -> Server:
    if name not in home.list_servers():
        raise RemoteError(f"no server {name!r} in servers/ (known: {', '.join(home.list_servers()) or 'none'})")
    return home.load_server(name)


def open_remote(home, name: str, say=None) -> int:
    """Run the permanent session of server `name` in this terminal and return ssh's exit code.

    Says once what it opens (`say`, default stdout), including which entry of an `ssh` list answered.  The caller owns the
    terminal (the CLI directly, the TUI after `suspend()`).  Raises RemoteError/ValueError for a server that is local,
    unknown, unsafe or not reachable by any entry; FileNotFoundError when ssh is not installed."""
    s = server_of(home, name)
    dest = pick(s)
    via = f" via {' '.join(dest)}" if len(targets(s)) > 1 else ""
    (say or (lambda m: print(m, file=sys.stdout, flush=True)))(
        f"\nLightmind: opening the permanent session on {name}{via} (detach with Ctrl+B D, the session keeps running).\n")
    return subprocess.call(remote_command(s, dest))
