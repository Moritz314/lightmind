"""To-dos and notes of one chat: data and tool logic, no Textual.

Everything lives in the run folder of the chat (`todos.json`), so it travels with the history and is there again when
the chat is resumed.  The model is a minimal file system: the root `To-dos` holds loose tasks, notes and folders, a
folder (what used to be a "list") holds tasks and notes, no deeper.  Order is creation order, nothing moves when a
task is done.  A task is `open`, `doing` or `done`; its `info` is written by the agent, its `remarks` are annotations
(the user's carry `seen: false` until the session was told).  A note is a title and a text.

The TUI shows and edits it, the built-in `todo` tool lets the session do the same, and `context_block` gives the
session a short block in the volatile context when something changed.  Old files (`lists` in todos.json, the single
`notes.md`) are carried over into this model on loading without loss."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Union

from .files import write_atomic

STATES = ("open", "doing", "done")
FILE, OLD_NOTES_FILE = "todos.json", "notes.md"
OLD_NOTES_TITLE = "Notes"
INFO_IN_CONTEXT, TITLE_MAX, TEXT_MAX = 160, 200, 4000


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clock(ts: str) -> str:
    """`2026-10-21T14:05:00+02:00` -> `10-21 14:05` (what the TUI shows next to an annotation)."""
    return ts[5:16].replace("T", " ") if len(ts) >= 16 else ts


def _text(v: Any, limit: int = TEXT_MAX) -> str:
    return str(v if v is not None else "").strip()[:limit]


OWNER = "user"                  # `by` of a remark the person in the chat wrote


@dataclass
class Remark:
    """An annotation to a task."""
    id: str
    text: str
    ts: str = ""
    by: str = OWNER
    seen: bool = False


@dataclass
class Task:
    id: str
    title: str
    state: str = "open"
    info: str = ""
    remarks: list[Remark] = field(default_factory=list)
    updated: str = ""


@dataclass
class Note:
    id: str
    title: str
    text: str = ""
    updated: str = ""


@dataclass
class Folder:
    id: str
    title: str
    items: list[Union[Task, Note]] = field(default_factory=list)
    created: str = ""

    def count(self) -> tuple[int, int]:
        """(done, all tasks)"""
        ts = [i for i in self.items if isinstance(i, Task)]
        return sum(t.state == "done" for t in ts), len(ts)


Item = Union[Task, Note, Folder]


# ===================================================================== loading and dumping
def _load_remark(d: dict) -> Remark:
    return Remark(_text(d.get("id")), _text(d.get("text")), _text(d.get("ts")), _text(d.get("by")) or OWNER,
                  bool(d.get("seen", False)))


def _load_task(d: dict) -> Task:
    st = d.get("state")
    rem = d.get("remarks", d.get("notes")) or []                      # `notes` is the name in the old format
    return Task(_text(d.get("id")), _text(d.get("title"), TITLE_MAX), st if st in STATES else "open",
                _text(d.get("info")), [_load_remark(n) for n in rem if isinstance(n, dict)], _text(d.get("updated")))


def _load_item(d: dict, nested: bool = True) -> Item | None:
    kind = d.get("type") or "task"
    if kind == "note":
        return Note(_text(d.get("id")), _text(d.get("title"), TITLE_MAX), _text(d.get("text")), _text(d.get("updated")))
    if kind == "folder" and nested:
        sub = [_load_item(x, False) for x in d.get("items") or [] if isinstance(x, dict)]
        return Folder(_text(d.get("id")), _text(d.get("title"), TITLE_MAX), [x for x in sub if x is not None],
                      _text(d.get("created")))
    if kind == "task":
        return _load_task(d)
    return None


def _dump_item(x: Item) -> dict:
    if isinstance(x, Task):
        return {"type": "task", "id": x.id, "title": x.title, "state": x.state, "info": x.info, "updated": x.updated,
                "remarks": [{"id": n.id, "text": n.text, "ts": n.ts, "by": n.by, "seen": n.seen} for n in x.remarks]}
    if isinstance(x, Note):
        return {"type": "note", "id": x.id, "title": x.title, "text": x.text, "updated": x.updated}
    return {"type": "folder", "id": x.id, "title": x.title, "created": x.created, "items": [_dump_item(i) for i in x.items]}


def dump(items: list) -> list[dict]:
    """The tree as plain JSON (what todos.json holds in `items`); the VS Code notes view shows it."""
    return [_dump_item(x) for x in items]


def _walk(items: list):
    for x in items:
        yield x
        if isinstance(x, Folder):
            yield from x.items


class Store:
    """The data of one chat.  `Store(run_folder)` reads what is there (nothing = empty); every change method saves.
    Stateless between calls, so the TUI and the tool can both use their own instance of the same folder: `reload()`
    after the other side may have written (`changed_on_disk()` says so cheaply)."""

    def __init__(self, folder: Path | str) -> None:
        self.dir = Path(folder)
        self.items: list[Item] = []                         # the root
        self.seq = 0
        self._stamp: tuple = ()
        self.reload()

    # ------------------------------------------------------------- disk
    @property
    def path(self) -> Path:
        return self.dir / FILE

    @property
    def old_notes_path(self) -> Path:
        return self.dir / OLD_NOTES_FILE

    def _sig(self) -> tuple:
        out = []
        for p in (self.path, self.old_notes_path):
            try:
                s = p.stat()
                out.append((s.st_mtime_ns, s.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def changed_on_disk(self) -> bool:
        return self._sig() != self._stamp

    def reload(self) -> None:
        self._stamp = self._sig()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.seq = int(raw.get("seq") or 0) if str(raw.get("seq") or 0).isdigit() else 0
        old = "lists" in raw and "items" not in raw
        if old:
            self.items = self._from_old(raw)
        else:
            self.items = [x for x in (_load_item(d) for d in raw.get("items") or [] if isinstance(d, dict)) if x]
        self._bump_seq()
        old_notes = self._read_old_notes()
        if old or old_notes:
            self._migrate(old_notes)

    def _bump_seq(self) -> None:
        for x in _walk(self.items):                        # ids made by hand (`t3`, `n2`) must not be reused
            ids = [x.id] + ([r.id for r in x.remarks] if isinstance(x, Task) else [])
            for i in ids:
                if i[1:].isdigit():
                    self.seq = max(self.seq, int(i[1:]))

    @staticmethod
    def _from_old(raw: dict) -> list[Item]:
        """Old format: `lists` of `tasks` (status active/paused/closed is dropped, a list becomes a folder)."""
        out: list[Item] = []
        for ld in raw.get("lists") or []:
            if not isinstance(ld, dict):
                continue
            tasks = [_load_task(t) for t in ld.get("tasks") or [] if isinstance(t, dict)]
            out.append(Folder(_text(ld.get("id")), _text(ld.get("title"), TITLE_MAX), tasks, _text(ld.get("created"))))
        return out

    def _read_old_notes(self) -> str:
        try:
            return self.old_notes_path.read_text("utf-8").strip()
        except OSError:
            return ""

    def _migrate(self, old_notes: str) -> None:
        """The single notes field becomes one note in the root; the old file is kept as `notes.md.old`."""
        if old_notes:
            self.items.append(Note(self._id("n"), OLD_NOTES_TITLE, old_notes[:TEXT_MAX], now()))
        self.save()
        try:
            if self.old_notes_path.exists():
                os.replace(self.old_notes_path, self.dir / (OLD_NOTES_FILE + ".old"))
        except OSError:
            pass
        self._stamp = self._sig()

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"seq": self.seq, "items": dump(self.items)}, indent=1, ensure_ascii=False)
        write_atomic(self.path, body)
        self._stamp = self._sig()

    # ------------------------------------------------------------- lookup
    def _id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}{self.seq}"

    def folders(self) -> list[Folder]:
        return [x for x in self.items if isinstance(x, Folder)]

    def find(self, ref: str) -> Item:
        ref = _text(ref)
        for x in _walk(self.items):
            if x.id == ref:
                return x
        raise KeyError(f"no item {ref!r}")

    def parent(self, ref: str) -> Folder | None:
        """The folder an item is in; None = the root."""
        for f in self.folders():
            if any(i.id == ref for i in f.items):
                return f
        return None

    def folder(self, ref: str | None) -> Folder | None:
        """A folder by id or by name (case does not matter); no reference = the root (None)."""
        ref = _text(ref)
        if not ref or ref.lower() in ("root", "to-dos", "todos"):
            return None
        for f in self.folders():
            if f.id == ref:
                return f
        hits = [f for f in self.folders() if f.title.lower() == ref.lower()]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(f"no folder {ref!r}" if not hits else f"folder name {ref!r} is not unique, use its id")

    def _bucket(self, folder: str | None) -> list:
        f = self.folder(folder)
        return self.items if f is None else f.items

    def task(self, ref: str) -> Task:
        x = self.find(ref)
        if not isinstance(x, Task):
            raise KeyError(f"{ref!r} is not a task")
        return x

    # ------------------------------------------------------------- changes
    def add_task(self, title: str, info: str = "", folder: str | None = None) -> Task:
        title = _text(title, TITLE_MAX)
        if not title:
            raise ValueError("a task needs a title")
        bucket = self._bucket(folder)
        t = Task(self._id("t"), title, "open", _text(info), [], now())
        bucket.append(t)
        self.save()
        return t

    def add_note(self, title: str, text: str = "", folder: str | None = None) -> Note:
        title = _text(title, TITLE_MAX)
        if not title:
            raise ValueError("a note needs a title")
        bucket = self._bucket(folder)
        n = Note(self._id("n"), title, _text(text), now())
        bucket.append(n)
        self.save()
        return n

    def add_folder(self, title: str) -> Folder:
        title = _text(title, TITLE_MAX)
        if not title:
            raise ValueError("a folder needs a name")
        if any(f.title.lower() == title.lower() for f in self.folders()):
            raise ValueError(f"a folder {title!r} exists already")
        f = Folder(self._id("f"), title, [], now())
        self.items.append(f)
        self.save()
        return f

    def update(self, ref: str, *, state: str | None = None, info: str | None = None, title: str | None = None,
               text: str | None = None) -> Item:
        x = self.find(ref)
        if title is not None:
            if not _text(title):
                raise ValueError("a title cannot be empty")
            x.title = _text(title, TITLE_MAX)
        if isinstance(x, Task):
            if state is not None:
                if state not in STATES:
                    raise ValueError(f"status must be one of {', '.join(STATES)}")
                x.state = state
            if info is not None:
                x.info = _text(info)
            x.updated = now()
        elif isinstance(x, Note):
            if text is not None:
                x.text = _text(text)
            x.updated = now()
        self.save()
        return x

    def cycle(self, ref: str) -> Task:
        t = self.task(ref)
        self.update(ref, state=STATES[(STATES.index(t.state) + 1) % len(STATES)])
        return t

    def annotate(self, ref: str, text: str, by: str = OWNER) -> Remark:
        text = _text(text)
        if not text:
            raise ValueError("an annotation needs text")
        t = self.task(ref)
        n = Remark(self._id("r"), text, now(), by, seen=by != OWNER)
        t.remarks.append(n)
        t.updated = n.ts
        self.save()
        return n

    def delete(self, ref: str) -> Item:
        x = self.find(ref)
        f = self.parent(x.id)
        (self.items if f is None else f.items).remove(x)
        self.save()
        return x

    # ------------------------------------------------------------- views for the session
    def digest(self) -> str:
        """What the session would need to hear again when it changes.  The `seen` flags are not part of it (marking
        remarks as told must not count as a change)."""
        h = hashlib.sha1()
        for x in _walk(self.items):
            h.update(f"{type(x).__name__}|{x.id}|{x.title}\n".encode())
            if isinstance(x, Task):
                h.update(f"{x.state}|{x.info}\n".encode())
                for n in x.remarks:
                    h.update(f"{n.id}|{n.by}|{n.text}\n".encode())
            elif isinstance(x, Note):
                h.update(x.text.encode())
        for f in self.folders():
            h.update(("|".join(i.id for i in f.items) + "\n").encode())
        return h.hexdigest()

    def unseen(self) -> list[tuple[Task, Remark]]:
        return [(t, n) for t in _walk(self.items) if isinstance(t, Task) for n in t.remarks
                if n.by == OWNER and not n.seen]


# ===================================================================== text for the session
MARK = {"open": " ", "doing": "~", "done": "x"}


def _one(text: str, limit: int) -> str:
    s = " ".join(text.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _render_item(x: Item, indent: str, full: bool) -> list[str]:
    lim = TEXT_MAX if full else INFO_IN_CONTEXT
    if isinstance(x, Task):
        out = [f"{indent}[{MARK[x.state]}] {x.id} {x.title}"]
        if x.info:
            out.append(f"{indent}    info: {x.info if full else _one(x.info, lim)}")
        for n in x.remarks:
            out.append(f"{indent}    remark ({n.by}, {clock(n.ts)}): {n.text if full else _one(n.text, lim)}")
        return out
    out = [f"{indent}note {x.id} {x.title}"]
    if x.text:
        out.append(f"{indent}    {x.text if full else _one(x.text, lim)}")
    return out


def render_tree(items: list, full: bool = False) -> list[str]:
    out: list[str] = []
    for x in items:
        if isinstance(x, Folder):
            d, n = x.count()
            out.append(f"folder {x.id} {x.title} ({d}/{n} done)")
            for i in x.items:
                out += _render_item(i, "  ", full)
        else:
            out += _render_item(x, "", full)
    return out


def context_block(store: Store, last_digest: str = "") -> tuple[str, str]:
    """`(block, digest)`: the short block "to-dos, changes and new remarks since the last round" for the volatile
    context.  Empty when nothing changed since `last_digest` (and no new remark of the user is waiting) or there is
    nothing at all.  The remarks it shows are marked as told (saved), so they come once.  Keep the returned digest
    and pass it in next time."""
    dig = store.digest()
    fresh = store.unseen()
    if (dig == last_digest and not fresh) or not store.items:
        return "", dig
    lines = ["To-dos changed since the last round (`todo` tool; the user sees the same tree):"]
    lines += render_tree(store.items)
    if fresh:
        lines.append("New remarks from the user:")
        for t, n in fresh:
            lines.append(f"  on {t.id} ({_one(t.title, 60)}), {clock(n.ts)}: {_one(n.text, 400)}")
            n.seen = True
        store.save()
    return "\n".join(lines), store.digest()


# ===================================================================== the `todo` tool
ACTIONS = ("list", "add_task", "add_note", "add_folder", "update", "comments", "delete")


def run_tool(store: Store, action: str = "", **a: Any) -> tuple[bool, str]:
    """The built-in `todo` tool: `(ok, text)`.  Never raises for bad input."""
    store.reload()
    try:
        action = _text(action).lower()
        if action == "list":
            f = store.folder(a.get("folder"))
            items = store.items if f is None else f.items
            if not items:
                return True, "nothing yet" if f is None else f"folder {f.title} is empty"
            return True, "\n".join(render_tree(items, full=True))
        if action == "add_task":
            t = store.add_task(a.get("title", ""), a.get("info", ""), a.get("folder"))
            return True, f"added task {t.id}: {t.title}"
        if action == "add_note":
            n = store.add_note(a.get("title", ""), a.get("text", ""), a.get("folder"))
            return True, f"added note {n.id}: {n.title}"
        if action == "add_folder":
            f = store.add_folder(a.get("title", ""))
            return True, f"added folder {f.id}: {f.title}"
        if action == "update":
            if not _text(a.get("id")):
                return False, "update needs the id of a task, note or folder"
            if all(a.get(k) in (None, "") for k in ("status", "info", "title", "text")):
                return False, "update needs status, info, title or text"
            x = store.find(a.get("id", ""))
            if isinstance(x, Folder) and a.get("title") in (None, ""):
                return False, "a folder can only be renamed (title)"
            x = store.update(x.id, state=a.get("status") or None, info=a.get("info"), title=a.get("title") or None,
                             text=a.get("text"))
            return True, f"{x.id}: " + (f"{x.state}, " if isinstance(x, Task) else "") + x.title
        if action == "comments":
            ref = _text(a.get("id"))
            tasks = [store.task(ref)] if ref else [x for x in _walk(store.items) if isinstance(x, Task)]
            out = [f"{t.id} {t.title}: " + " | ".join(f"({n.by}, {clock(n.ts)}) {n.text}" for n in t.remarks)
                   for t in tasks if t.remarks]
            return True, "\n".join(out) or "(no remarks)"
        if action == "delete":
            if not _text(a.get("id")):
                return False, "delete needs an id"
            x = store.delete(a.get("id", ""))
            return True, f"deleted {x.id}: {x.title}"
    except (KeyError, ValueError) as e:
        return False, str(e.args[0] if e.args else e)
    return False, f"unknown action {action!r}; use one of: {', '.join(ACTIONS)}"
