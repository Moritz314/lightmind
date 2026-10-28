"""Sub-agents of a chat, collected from the event stream (live) or from events.jsonl (resume).

A sub-agent is started by one tool call of the main chat (claude_sdk: the CLI's Task/Agent tool); its inner
steps carry that call's id as `parent`.  `SubLog` keeps, per sub-agent: the task the main chat gave it, its
steps (Text, ToolCall, ToolDone) and its answer; and the workflow runs (orchestrate.py) the sub-agents belong to, with
their phases.  Pure data, no UI.  May import: model, providers.base.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Text, ToolCall
from .providers.base import SubagentEnd, SubagentStart, ToolDone, WorkflowEvent


@dataclass
class Sub:
    id: str
    name: str
    task: str = ""
    turn: int = 0                                  # the user message (round) it was started in
    steps: list[Text | ToolCall | ToolDone] = field(default_factory=list)
    done: bool = False
    ok: bool = True
    summary: str = ""
    workflow: str = ""                             # the workflow run it belongs to ("" = started by `spawn`)
    phase: str = ""


@dataclass
class Flow:
    """A workflow run: its meta, the phases seen so far, its last log line, its end."""
    id: str
    name: str
    description: str = ""
    phases: list[str] = field(default_factory=list)
    turn: int = 0
    log: str = ""
    done: bool = False
    ok: bool = True
    summary: str = ""


class SubLog:
    def __init__(self) -> None:
        self.subs: dict[str, Sub] = {}             # by tool call id, in start order
        self.flows: dict[str, Flow] = {}           # workflow runs by id, in start order
        self.turn = 0

    def new_turn(self) -> None:
        self.turn += 1

    def feed(self, ev: Any) -> None:
        if isinstance(ev, SubagentStart):
            self.subs[ev.id] = Sub(ev.id, ev.name, ev.task, self.turn, workflow=ev.workflow, phase=ev.phase)
        elif isinstance(ev, WorkflowEvent):
            self._flow(ev)
        elif isinstance(ev, SubagentEnd):
            s = self.subs.get(ev.id)
            if s is not None:
                s.done, s.ok, s.summary = True, ev.ok, ev.summary
        elif isinstance(ev, (Text, ToolCall, ToolDone)) and ev.parent:
            s = self.subs.get(ev.parent)
            if s is not None:
                if isinstance(ev, Text) and s.steps and isinstance(s.steps[-1], Text):
                    s.steps[-1].text += ev.text    # streamed deltas: one block
                else:
                    s.steps.append(ev)

    def _flow(self, ev: WorkflowEvent) -> None:
        if ev.kind == "start":
            self.flows[ev.id] = Flow(ev.id, ev.name, ev.text, list(ev.phases), self.turn)
            return
        f = self.flows.get(ev.id)
        if f is None:
            return
        if ev.kind == "phase" and ev.text not in f.phases:
            f.phases.append(ev.text)
        elif ev.kind == "log":
            f.log = ev.text
        elif ev.kind == "end":
            f.done, f.ok, f.summary = True, ev.ok, ev.text

    def current_flows(self) -> list[Flow]:
        """The workflow runs of the latest round, and every one still working."""
        return [f for f in self.flows.values() if f.turn == self.turn or not f.done]

    def current(self) -> list[Sub]:
        """The sub-agents of the latest round."""
        return [s for s in self.subs.values() if s.turn == self.turn or not s.done]    # one working in the background stays


def from_events(events: list[dict]) -> SubLog:
    """Rebuild the log from events.jsonl lines ({kind, data}); every user message starts a round."""
    log = SubLog()
    for e in events:
        kind, d = e.get("kind"), e.get("data") or {}
        if kind == "reset":
            log = SubLog()
        elif kind == "message" and d.get("role") == "user":
            log.new_turn()
        elif kind == "subagentstart":
            log.feed(SubagentStart(str(d.get("id", "")), str(d.get("name", "")), str(d.get("task", "")),
                                   str(d.get("workflow", "")), str(d.get("phase", ""))))
        elif kind == "workflowevent":
            log.feed(WorkflowEvent(str(d.get("id", "")), str(d.get("kind", "")), str(d.get("name", "")),
                                   str(d.get("text", "")), [str(x) for x in d.get("phases") or []], bool(d.get("ok", True))))
        elif kind == "subagentend":
            log.feed(SubagentEnd(str(d.get("id", "")), bool(d.get("ok", True)), str(d.get("summary", ""))))
        elif kind in ("text", "toolcall", "tooldone") and d.get("parent"):
            p, a = str(d["parent"]), str(d.get("agent", ""))
            if kind == "text":
                log.feed(Text(str(d.get("text", "")), p, a))
            elif kind == "toolcall":
                log.feed(ToolCall(str(d.get("id", "")), str(d.get("name", "")), dict(d.get("args") or {}), p, a))
            else:
                log.feed(ToolDone(str(d.get("id", "")), str(d.get("name", "")), bool(d.get("ok", True)),
                                  str(d.get("text", "")), p, a))
    return log
