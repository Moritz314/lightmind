"""The events of protocol v1 (plan/PROTOCOL.md) made from what a chat does: `Translator` turns the loop's events into protocol
events, `history` turns the saved messages of a chat into the same events, `mask` hides secret values in them.

The VS Code backend (`serve.py`), the Remote Control link and the node all speak this format, so it is made in one place.
Nothing here writes to a channel: the callers send the dicts (and mask them again at their own door).

May import: core, loop, interject, model, spawn, util, providers.base.  Not serve, not the TUI.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import core, loop
from . import interject as ij
from . import model as m
from .files import Home
from .providers.base import Interjected, Notice, SubagentEnd, SubagentStart, ToolDone, WorkflowEvent
from .spawn import parse_frames
from .util import brief as _brief

SUB_TASK_MAX = 4000
SUB_MESSAGE_MAX = 700
HISTORY_MESSAGES = 100                                     # `history` shows the last messages of the chat
WRITES = ("write", "edit")                                 # built-in tools that change the file in their `path`
PATHS = ("read", "write", "edit")                          # built-in tools whose `path` is shown at tool.start


def mask(obj: Any, masker) -> Any:
    """`obj` (strings, lists, dicts, any depth) with every secret value of `masker` hidden; other values stay as they are."""
    if isinstance(obj, str):
        return masker.mask(obj)
    if isinstance(obj, list):
        return [mask(x, masker) for x in obj]
    if isinstance(obj, dict):
        return {k: mask(v, masker) for k, v in obj.items()}
    return obj


def summary(name: str, args: dict) -> str:
    """The one line a client shows for a tool call: the tool's name and its brief arguments."""
    return f"{name.capitalize()} {_brief(args)}".strip()


def first_line(text: str) -> str:
    """The first non-empty line of `text`, at most 200 characters (the line of a finished tool call)."""
    return next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")[:200]


def paths(home: Home, name: str, args: dict) -> list[str]:
    """The absolute file path a built-in file tool (read, write, edit) works on, as a list; [] for any other tool or no path.
    `home` resolves a relative path against the tree."""
    p = args.get("path") if name in PATHS else None
    if not isinstance(p, str) or not p.strip():
        return []
    return [str(Path(os.path.normpath(home.abs(os.path.expanduser(p)))))]


def who(ev) -> dict:
    """`parent` + `agent` of an event that belongs to a sub-agent; nothing for the chat's own events."""
    p = getattr(ev, "parent", "")
    return {"parent": p, "agent": getattr(ev, "agent", "")} if p else {}


def flow_of(ev) -> dict:
    """`workflow` + `phase` of a sub-agent that a workflow started; nothing for one started by `spawn`."""
    return {"workflow": ev.workflow, "phase": ev.phase} if getattr(ev, "workflow", "") else {}


def workflow_event(session: str, turn: str, ev: WorkflowEvent) -> dict:
    """A workflow run changed -> `workflow.start {id, name, description, phases}`, `workflow.phase {id, title}`,
    `workflow.log {id, text}` or `workflow.end {id, ok, summary}`."""
    base = {"type": f"workflow.{ev.kind}", "session": session, "turn": turn, "id": ev.id}
    if ev.kind == "start":
        return {**base, "name": ev.name, "description": ev.text[:SUB_MESSAGE_MAX], "phases": [p[:200] for p in ev.phases[:50]]}
    if ev.kind == "phase":
        return {**base, "title": ev.text[:200]}
    if ev.kind == "end":
        return {**base, "ok": ev.ok, "summary": ev.text[:SUB_MESSAGE_MAX]}
    return {**base, "type": "workflow.log", "text": ev.text[:SUB_MESSAGE_MAX]}


def sub_messages(session: str, turn: str, text: str) -> list[dict]:
    """The framed results of background sub-agents in a message -> one `subagent.message` each (never "You" and
    never a text of the agent); a text that does not parse becomes a plain notice."""
    got = parse_frames(text)
    if not got:
        return [{"type": "notice", "session": session, "turn": turn, "level": "info",
                 "text": "sub-agent result: " + first_line(text.split("untrusted>", 1)[-1])[:120]}]
    return [{"type": "subagent.message", "session": session, "turn": turn, "name": g["name"][:200], "ok": g["ok"],
             "summary": g["summary"][:SUB_MESSAGE_MAX], "file": g["file"][:1000]} for g in got]


def is_error(events: list[dict]) -> bool:
    """Did `Translator.event` report an error (an `error` event among `events`)?"""
    return any(e.get("type") == "error" for e in events)


class Translator:
    """Loop events -> protocol events, for one chat.  `calls` remembers the files of the tool calls that are still open
    (call id -> (tool name, files)) so that `tool.end` of a write names them; give the same dict to every Translator of one
    turn.  `session` is the `loop.Session` (its `id`, `provider.owns_loop`, `dispatcher.masker`); `home` resolves file paths
    (default: the session's).  `mask=False` leaves masking to the caller, who then does it before anything leaves."""

    def __init__(self, session, home: Home | None = None, mask: bool = True, calls: dict | None = None) -> None:
        self.s, self.home, self.masking = session, home, mask
        self.calls: dict[str, tuple[str, list[str]]] = {} if calls is None else calls
        self.turn = ""                                      # the turn id events get when `event` is not given one

    def event(self, ev, turn: str | None = None) -> list[dict]:
        """One event of the loop -> the protocol events it stands for (a list: none for an empty text, one otherwise), each
        `{"type", "session", ...fields}`.  `turn` names the turn (default: `self.turn`).  Check `is_error(result)` to
        know the turn failed."""
        out = self._make(ev, self.turn if turn is None else turn)
        return mask(out, self.s.dispatcher.masker) if self.masking else out

    def _make(self, ev, turn: str) -> list[dict]:
        sid = self.s.id

        def one(type_: str, **fields: Any) -> list[dict]:
            return [{"type": type_, "session": sid, **fields}]

        if isinstance(ev, m.Text):
            if ev.text:
                whole = bool(getattr(self.s.provider, "owns_loop", False))     # claude_sdk sends whole blocks
                return one("text", turn=turn, text=ev.text, delta=not whole, **who(ev))
        elif isinstance(ev, m.ToolCall):
            files = paths(self.home or self.s.home, ev.name, ev.args)
            self.calls[ev.id] = (ev.name, files)
            return one("tool.start", turn=turn, call=ev.id, name=ev.name, summary=summary(ev.name, ev.args), files=files, **who(ev))
        elif isinstance(ev, ToolDone):
            name, files = self.calls.pop(ev.id, (ev.name, []))
            return one("tool.end", turn=turn, call=ev.id, ok=ev.ok, summary=first_line(ev.text),
                       files=files if ev.ok and name in WRITES else [], **who(ev))
        elif isinstance(ev, Interjected):
            if ev.parent:                                    # the main agent's message reached a sub-agent
                return one("interject", id=f"i{ev.id}", state="delivered", text="", **who(ev))
            return one("interject", id=f"i{ev.id}", state="delivered", text=ev.text)
        elif isinstance(ev, SubagentStart):
            return one("subagent.start", turn=turn, id=ev.id, name=ev.name, task=ev.task[:SUB_TASK_MAX], **flow_of(ev))
        elif isinstance(ev, WorkflowEvent):
            return [workflow_event(sid, turn, ev)]
        elif isinstance(ev, SubagentEnd):
            return one("subagent.end", turn=turn, id=ev.id, ok=ev.ok, summary=ev.summary)
        elif isinstance(ev, Notice):
            return one("notice", level="warn" if ev.kind in ("warning", "limit") else "info", text=ev.text)
        elif isinstance(ev, m.Error):
            return one("error", text=f"{ev.kind}: {ev.message}")
        return []


def history(s, home: Home | None = None) -> list[dict]:
    """The last `HISTORY_MESSAGES` messages of the chat `s` and its sub-agents as protocol events (`text`, `tool.start`,
    `tool.end`, `subagent.*`), masked.  Works for a chat that is only read (a chat of another process).  `home` resolves
    file paths (default: the session's)."""
    msgs = s.messages[-HISTORY_MESSAGES:]
    out: list[dict] = []
    seen: set[str] = set()
    n = 0
    for x in msgs:
        turn = f"h{n}" if n else "h0"
        if x.role == "user" and x.origin == ij.HARNESS:  # a note of the harness to the model: not shown
            continue
        if x.role == "user" and x.origin == ij.ORIGIN:   # his words, fed into a running turn: a user line like any other
            for words in ij.parse(x.text):
                n += 1
                out.append({"type": "text", "session": s.id, "turn": f"h{n}", "text": words, "delta": False, "role": "user"})
        elif x.role == "user" and x.origin:              # a sub-agent's result: a line of its own, not "You"
            out.extend(sub_messages(s.id, turn, x.text))
        elif x.role == "user":
            n += 1
            out.append({"type": "text", "session": s.id, "turn": f"h{n}", "text": core.clean_user(x.text),
                        "delta": False, "role": "user"})
        elif x.role == "assistant":
            if x.text.strip():
                out.append({"type": "text", "session": s.id, "turn": turn, "text": x.text, "delta": False})
            for c in x.tool_calls:
                seen.add(c.id)
                out.append({"type": "tool.start", "session": s.id, "turn": turn, "call": c.id, "name": c.name,
                            "summary": summary(c.name, c.args), "files": paths(home or s.home, c.name, c.args)})
        elif x.role == "tool" and x.tool_call_id in seen:
            out.append({"type": "tool.end", "session": s.id, "turn": turn, "call": x.tool_call_id,
                        "ok": not x.text.startswith(("refused:", loop.CANCELLED_TEXT[:20])),
                        "summary": first_line(x.text), "files": []})
    for fl in getattr(s.subs, "flows", {}).values():     # workflow runs of this chat, before their sub-agents
        t = f"h{fl.turn}"
        out.append(workflow_event(s.id, t, WorkflowEvent(fl.id, "start", fl.name, fl.description, fl.phases)))
        if fl.done:
            out.append(workflow_event(s.id, t, WorkflowEvent(fl.id, "end", fl.name, fl.summary, ok=fl.ok)))
    for sb in s.subs.subs.values():                      # sub-agents of this chat (rebuilt from events.jsonl)
        t = f"h{sb.turn}"
        out.append({"type": "subagent.start", "session": s.id, "turn": t, "id": sb.id, "name": sb.name,
                    "task": sb.task[:SUB_TASK_MAX], **flow_of(sb)})
        for x in sb.steps:
            w = {"parent": sb.id, "agent": sb.name}
            if isinstance(x, m.Text):
                out.append({"type": "text", "session": s.id, "turn": t, "text": x.text, "delta": False, **w})
            elif isinstance(x, m.ToolCall):
                out.append({"type": "tool.start", "session": s.id, "turn": t, "call": x.id, "name": x.name,
                            "summary": summary(x.name, x.args), "files": [], **w})
            else:
                out.append({"type": "tool.end", "session": s.id, "turn": t, "call": x.id, "ok": x.ok,
                            "summary": first_line(x.text), "files": [], **w})
        if sb.done:
            out.append({"type": "subagent.end", "session": s.id, "turn": t, "id": sb.id, "ok": sb.ok,
                        "summary": sb.summary})
    return mask(out, s.dispatcher.masker)
