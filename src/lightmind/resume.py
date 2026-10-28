"""Pick up an earlier chat, `lm resume`: the list, the lookup, the newest; and the archive (list, restore).

A chat is a run whose first event is a `session` event (`runs.list_sessions`).  Its settings (model, effort, mode, device,
account) and the id of the SDK conversation are in the trusted record `loop.resume_session` reads, so continuing it needs
only the run id; this module finds that id.  A name given with `-n` wins over an id prefix (`runs.resolve_session`).

May import: files, runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import runs
from .files import Home

POOL = 300          # newest chats read before they are ordered by last activity


@dataclass
class Row:
    id: str
    name: str           # the session name, else the title, else the agent
    when: str           # last activity, local time `YYYY-MM-DD HH:MM`
    folder: str         # the working folder the chat was started in; "" = none recorded
    server: str
    model: str
    mode: str


def chats(home: Home, limit: int = 15, archived: bool = False) -> list[runs.SessionSummary]:
    """The chats a person had, newest first: not a run that another run started (`parent`, `depth`) and not one of `lm run`
    (it writes `result.json`; a chat never does).  Ordered by last activity (the start time alone puts a long chat that was picked up today far down).
    Archived chats are left out; `archived=True` lists only those."""
    got = [s for s in runs.list_sessions(home, limit=POOL, archived=archived)
           if not s.parent and not s.depth and not (home.path("runs") / s.id / "result.json").exists()]
    got.sort(key=lambda s: s.last_active or s.start, reverse=True)
    return got[:limit]


def rows(home: Home, limit: int = 15, archived: bool = False) -> list[Row]:
    names = runs.session_names(home)
    out: list[Row] = []
    for s in chats(home, limit, archived):
        rec = runs.read_trusted(home, s.id) or {}
        try:
            when = datetime.fromisoformat(s.last_active or s.start).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = ""
        out.append(Row(s.id, names.get(s.id) or s.title or s.agent or "chat", when, str(rec.get("workdir") or ""),
                       s.server, s.model, s.mode))
    return out


def latest(home: Home) -> str | None:
    """The run id of the chat used last, None when there is none."""
    got = chats(home, 1)
    return got[0].id if got else None


def lookup(home: Home, ref: str) -> str:
    """The run id for a session name or an id (a unique prefix).  ValueError says what is wrong.  An archived chat is found
    too when asked for by name or id (`archived_note` says so)."""
    try:
        found = runs.resolve_session(home, ref)
    except ValueError as e:
        raise ValueError(str(e)) from None
    if found is None:
        # the short id the chat header shows (the last part, e.g. 38cc)
        ends = [p.name for p in home.path("runs").iterdir() if p.is_dir() and p.name.endswith("-" + ref)]
        if len(ends) > 1:
            raise ValueError(f"{ref!r} fits more than one chat: {', '.join(sorted(ends))}")
        found = ends[0] if ends else None
    if found is None:
        raise ValueError(f"no chat is called or starts with {ref!r}")
    head = runs._head(home.path("runs") / found / "events.jsonl", 1)
    if not head or head[0].get("kind") != "session":
        raise ValueError(f"{found} is a run, not a chat")
    return found


def table(items: list[Row]) -> str:
    """The numbered list the picker prints."""
    lines = []
    for i, r in enumerate(items, 1):
        lines.append(f"{i:>3}  {r.name[:28]:<28}  {r.when:<16}  {r.server or 'local':<10}  {r.model[:10]:<10}  {r.folder or '-'}")
    return "\n".join(lines)


def archived_note(home: Home, run_id: str) -> str:
    """The line to show next to a chat that was asked for by name or id and is archived ("" when it is not)."""
    if not runs.is_archived(home.path("runs") / run_id):
        return ""
    return f"{run_id} is archived (lm archive restore {run_id} puts it back in the list)"


# ------------------------------------------------------------------------------- the archive
def restore(home: Home, ref: str) -> tuple[str, str]:
    """Take an archived chat out of the archive so it is in the resume list again.  `ref`: a session name, an id or a unique
    prefix of one.  Returns (run id, line); the line is "restored", or "not archived" when the chat was never archived.
    ValueError says what is wrong (no such chat, more than one fit)."""
    run_id = lookup(home, ref)
    return run_id, runs.unarchive(home, run_id)


def archive_text(home: Home, limit: int = 15) -> str:
    """The archive as lines for a chat (`/archive list`): newest first, with the id that `restore` takes and the way back."""
    items = rows(home, limit, archived=True)
    if not items:
        return "the archive is empty"
    lines = [f"archive ({len(items)}, newest first):"]
    lines += [f"  {r.id}  {r.name[:40]}  {r.when}" for r in items]
    lines.append("bring one back with: /archive restore <id>   (or: lm resume <id>, it restores and opens it)")
    return "\n".join(lines)


def archived_choices(home: Home, limit: int = 50) -> list[tuple[str, str]]:
    """The archived chats as (run id, name and time), newest first: what can follow `/archive restore ` (an id is always a
    valid reference).  One disk read; the chat filters the list while the person types."""
    return [(r.id, f"{r.name[:40]}  {r.when}") for r in rows(home, limit, archived=True)]
