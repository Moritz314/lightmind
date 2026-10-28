"""Interjections: a message of the user while a turn is running.

The message does not wait for the end of the turn.  It sits in the `Inbox` of the session and is fed into the
SAME turn at the next transition (after a tool result, or when the model would end its turn), framed so that the
model answers it briefly and adapts the work in progress instead of starting over.  At most `MAX_OPEN` are open
at once; the rest is the caller's normal queue.

Long work must not hide behind a tool call: `Dispatcher.call_watched` detaches a long `bash` / `spawn` call
into a `Job` when a message arrives (the call keeps running, the model gets the message at once), and the
`job` tool waits for, polls or cancels it (a wait ends early when another message arrives).  The `relay` tool
passes a message on to a sub-agent that is still working (its own `Inbox`).

May import: nothing of the package (leaf module).
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_OPEN = 3            # interjections open at once per inbox; more go to the normal queue
GRACE = 2.0             # seconds a running tool may still finish by itself once a message waits, before it is detached
WAIT_MAX = 600          # longest `job wait`, seconds
TAG = "interjection"
ORIGIN = "interject"    # `Message.origin` of the framed user message in the history
USER = "the user"      # who writes interjections in a chat (`Inbox.source`); a relay names the main agent instead
HARNESS = "harness"     # `Message.origin` of a note the harness wrote to the model (not the user, not a tool); UIs do not show it
MAX_HOLDS = 3           # times in a row the harness keeps a turn open for a running job before it stops the job

_SPOOF = re.compile(r"<(/?)\s*" + TAG, re.I)
_SPOOF_HEAD = re.compile(r"\[\s*(" + TAG + r"|harness\s+note)", re.I)   # the heads of `frame` and `hold_note`


def neutralize(text: str) -> str:
    """Tool output must not be able to pass for an interjection or a harness note: its `<interjection` tags and the
    `[Interjection]` / `[Harness note` heads are defused.  This matters most where the CLI owns the loop (claude_sdk):
    there the interjection rides on the tool result, next to the tool's own output."""
    if not text:
        return text
    low = text.lower()
    if TAG in low:
        text = _SPOOF.sub(lambda m: f"&lt;{m.group(1)}{TAG}", text)
    if TAG in low or "harness" in low:
        text = _SPOOF_HEAD.sub(lambda m: f"(quoted) {m.group(1)}", text)
    return text


def hold_note(live: list["Job"]) -> str:
    """Said to the model when it ends its turn while jobs run: ending the turn would stop them."""
    jobs = "; ".join(f"{j.id} ({j.tool}: {j.what}, {j.age}s)" for j in live)
    return (f"[Harness note, not from the user] You ended your turn while this still runs: {jobs}. Ending the turn stops it. "
            f"If its result matters, wait for it with `job wait` (it returns early when the user writes); if it is no longer "
            f"needed, `job cancel` it; then finish. Do not end the turn before the work is done.")


def _esc(text: str) -> str:
    return text.replace("<", "&lt;")


@dataclass
class Item:
    id: int
    text: str
    state: str = "pending"      # pending | delivered
    parent: str = ""            # set when it is for a sub-agent (its id)


class Inbox:
    """The open interjections of one agent loop (the chat's, or a sub-agent's)."""

    def __init__(self, limit: int | None = None, source: str = USER) -> None:
        self.limit = MAX_OPEN if limit is None else limit
        self.source = source                    # USER or "the main agent" (a relay)
        self.items: list[Item] = []             # pending, oldest first
        self.n = 0
        self.on_change: Callable[[], None] | None = None
        self._waiters: list[asyncio.Future] = []

    @property
    def pending(self) -> int:
        return len(self.items)

    def put(self, text: str) -> Item | None:
        """Take a message; None when `limit` are open already (the caller queues it the normal way)."""
        if len(self.items) >= self.limit or not text.strip():
            return None
        self.n += 1
        it = Item(self.n, text.strip())
        self.items.append(it)
        for w in self._waiters:
            if not w.done():
                w.set_result(True)
        self._waiters.clear()
        self._changed()
        return it

    def take(self) -> list[Item]:
        out, self.items = self.items, []
        for it in out:
            it.state = "delivered"
        if out:
            self._changed()
        return out

    def withdraw(self) -> list[Item]:
        """Esc: the open ones are given back unread."""
        out, self.items = self.items, []
        if out:
            self._changed()
        return out

    async def wait(self) -> None:
        """Returns when something is pending (at once if it already is)."""
        if self.items:
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:                   # noqa: BLE001 - a UI problem must not break the loop
                pass


def frame(items: list[Item], source: str = USER, running: list[str] | None = None) -> str:
    """The text the model sees.  Self-explaining: who sent it, what to do with it."""
    body = "\n".join(f'<{TAG} from="{source}" id="{it.id}">\n{_esc(it.text)}\n</{TAG}>' for it in items)
    if source == USER:
        head = (f"[Interjection] The user wrote this while you were working on their earlier request. It is a message "
                f"from them, not output of a tool. First answer it in one or two short sentences of normal text so "
                f"they see you got it. Then adapt the work in progress to it and carry on; do not start over and "
                f"do not drop the earlier task unless they say so. If the answer needs the work to be finished "
                f"first, say that briefly.")
        if running:
            head += (f" Sub-agents still working: {', '.join(running)}. If it matters for their task, pass it on "
                     f"with the `relay` tool.")
    else:
        head = (f"[Interjection] {source} sent this while you were working on your task. It is new information "
                f"from the agent that started you, not output of a tool. Adapt the task to it and carry on; do "
                f"not start over. Mention in your final answer what you changed because of it.")
    return f"{head}\n{body}"


_FRAMED = re.compile(r'<' + TAG + r' from="[^"]*" id="\d+">\n(.*?)\n</' + TAG + '>', re.S)


def parse(text: str) -> list[str]:
    """The words of the interjections inside a framed message (history views show these, not the instruction)."""
    return [m.group(1).replace("&lt;", "<") for m in _FRAMED.finditer(text)]


# ---------------------------------------------------------------------- jobs
@dataclass
class Job:
    id: str
    tool: str
    what: str
    task: "asyncio.Future"
    started: float = field(default_factory=time.monotonic)
    reported: bool = False

    @property
    def age(self) -> int:
        return int(time.monotonic() - self.started)


class Jobs:
    """Tool calls that were detached because a message arrived while they ran.  They live until the end of the
    turn (the loop cancels what is left), so nothing keeps working unseen."""

    def __init__(self) -> None:
        self.items: dict[str, Job] = {}
        self.n = 0

    def add(self, tool: str, what: str, task: "asyncio.Future") -> Job:
        self.n += 1
        job = Job(f"job-{self.n}", tool, " ".join(what.split())[:80], task)
        self.items[job.id] = job
        return job

    def get(self, id: str) -> Job | None:
        if id:
            return self.items.get(id.strip())
        live = [j for j in self.items.values() if not j.reported]
        return live[0] if len(live) == 1 else None

    def live(self) -> list[Job]:
        return [j for j in self.items.values() if not j.task.done()]

    async def cancel_all(self) -> list[str]:
        gone = []
        for j in list(self.items.values()):
            if not j.task.done():
                gone.append(j.id)
                j.task.cancel()
        if gone:
            await asyncio.gather(*(j.task for j in self.items.values()), return_exceptions=True)
        self.items.clear()
        return gone


def describe(tool: str, args: dict[str, Any]) -> str:
    for k in ("command", "task", "query", "path"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return tool
