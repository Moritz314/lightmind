"""Turn runner + chat sessions.

Part 1, `stream_loop` / `run_loop`: tool loop for any Provider (turn -> tool calls ->
dispatcher -> next turn), usage and cost recording, and the between-turn checks (handover,
fallback, budget).  Providers whose backend owns the loop (`owns_loop`, i.e. claude_sdk) run
one turn; the loop does not execute their tool calls again.

Part 2, the Session API used by the CLI and the Orbit TUI: `open_session`, `resume_session`,
`list_sessions`, `run_on_start` / `run_on_end`, class `Session`.  A session persists
everything needed to resume it in its run folder (events.jsonl via runs.py).

May import: model, files, context, tools, runs, hooks, providers.base (+ rules, lazily).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import interject as ij
from . import accountpick, spawn, subagents, titles, todos, ultracode
from .context import (Context, build_context, handover_instruction, handover_needed,
                      handover_task, initial_messages)
from .files import Home, parse_size
from .util import to_int
from .model import (Agent, Error, Message, RateLimit, Request, Rule, Run, RuleContext,
                    SessionState, Task, Text, Tokens, ToolCall, ToolRef, Usage)
from .providers.base import (Interjected, Notice, SubagentEnd, Provider, SessionEvent, ToolDone, make_provider,
                             resolve_account, resolve_alias, resolve_model, settings,
                             settings_defaults)
from .hooks import (HOOK_CONTEXT_MAX, HOOK_EVENTS, HOOK_TIMEOUT, HookOutput, _run_hooks,   # noqa: F401 - re-exported
                    run_on_end, run_on_prompt, run_on_start)
from .runs import (RunDir, SessionSummary, acquire_lock, list_sessions, new_run_id,   # noqa: F401 - re-exported
                   read_trusted, release_lock, show_run)
from .tools import Dispatcher

HANDOVER_GOAL = "Continue the conversation with the user where it stopped."


class ReadOnlyChat(RuntimeError):
    """The chat is open in another process (`Session.readonly_by` is its pid): this one may only read it."""
CANCELLED_TEXT = "cancelled by user"   # result of a tool call that was stopped before it finished
TOOL_TEXT_LIMIT = 2000         # characters of a tool result mirrored in a ToolDone event


# ===================================================================== part 1: loop
@dataclass
class LoopResult:
    status: str = "done"                    # done | failed | handover | paused | budget
    final_text: str = ""
    tokens: Tokens = field(default_factory=Tokens)
    messages: list[Message] = field(default_factory=list)
    exit_reason: str = ""
    switch_to: str = ""                     # account to continue on (fallback), if any
    context_tokens: int = 0                 # prompt size of the last model call


@asynccontextmanager
async def _closing(stream):
    """Close an async generator when its consumer is done or gone (also on cancellation), so a
    stopped chat ends the backend call (the CLI process) at once instead of at garbage collection."""
    try:
        yield stream
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


class _Prov:
    """A provider event on the queue shared with the sub-agents (see `_merged`)."""
    def __init__(self, ev) -> None:
        self.ev = ev


class _End:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc


async def _merged(stream, q: "asyncio.Queue", dispatcher) -> AsyncIterator[SessionEvent]:
    """The provider's events and the events of sub-agents (`spawn`, put on `q` by the dispatcher) as one
    stream, in the order they happen: a backend that owns the loop emits nothing while a sub-agent works.
    Ending early (cancel, close) stops the provider's stream and the sub-agents."""
    async def pump() -> None:
        try:
            async for ev in stream:
                if isinstance(ev, ToolCall) and ev.name == "spawn" and not ev.parent:
                    dispatcher.note_call(ev)       # before the backend can call the tool: the tool takes the id from here
                q.put_nowait(_Prov(ev))
        except Exception as e:                      # noqa: BLE001 - handed to the consumer
            q.put_nowait(_End(e))
        else:
            q.put_nowait(_End())
    task = asyncio.ensure_future(pump())
    try:
        while True:
            item = await q.get()
            if isinstance(item, _End):
                if item.exc is not None:
                    raise item.exc
                return
            yield item.ev if isinstance(item, _Prov) else item
    finally:
        task.cancel()
        dispatcher.cancel_spawned()
        await asyncio.gather(task, return_exceptions=True)


async def _drained(task: "asyncio.Future", q: "asyncio.Queue") -> AsyncIterator[SessionEvent]:
    """Events of sub-agents from `q` until `task` (a tool call) is done."""
    while True:
        getter = asyncio.ensure_future(q.get())
        try:
            await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            getter.cancel()
            raise
        if getter.done():
            yield getter.result()
            continue
        getter.cancel()
        break
    while not q.empty():
        yield q.get_nowait()


def _log_message(run: RunDir, m: Message) -> None:
    """Every message of the conversation goes to events.jsonl: that is what resume rebuilds."""
    run.event("message", {"role": m.role, "text": m.text, "tool_call_id": m.tool_call_id, "origin": m.origin,
                          "tool_calls": [asdict(c) for c in m.tool_calls]})


def _fallback(dispatcher: Dispatcher, run: RunDir) -> str:
    """Next account of the fallback chain of the run's account that can actually run (a subscription
    not logged in or an API account without its key is skipped); "" for agents with a fixed provider
    (they pause instead) or when no chain is configured.  An agent whose `provider` is a list of accounts moves on within
    that list instead, to the first other one that is free (`accountpick.free`)."""
    agent = getattr(dispatcher, "agent", None)
    cfg = dispatcher.home.load_providers()
    here = run.run.account
    listed = accountpick.names_of(agent.provider) if agent is not None else []
    if len(listed) > 1:
        for name in listed:
            name = resolve_alias(cfg, name)
            if name in cfg.accounts and name != here and _usable(dispatcher.home, name) and accountpick.free(dispatcher.home, name)[0]:
                return name
        return ""
    if agent is not None and agent.provider:
        return ""
    for name in cfg.fallback.get(here, []):
        name = resolve_alias(cfg, name)
        if name in cfg.accounts and name != here and _usable(dispatcher.home, name):
            return name
    return ""


def _usable(home, name: str) -> bool:
    from .accounts import account_status
    try:
        return account_status(home, name)["state"] not in ("login-missing", "key-missing")
    except Exception:                                    # noqa: BLE001  a status problem must not block a switch
        return True


async def stream_loop(provider: Provider, ctx: Context, dispatcher: Dispatcher, run: RunDir,
                      messages: list[Message], **kw: Any) -> AsyncIterator[SessionEvent]:
    """`run_loop` as an event stream.  `messages` already ends with the new user message and
    grows in place.  The outcome is written to `result` (also when the consumer stops early).

    Keyword arguments: model, effort, context_limit, handover_at, switch_at, max_turns=0 (no limit), budget_tokens,
    budget_usd, on_event, result, handover_when_done (also ask for a handover after a turn that ends the work:
    chat sessions do; a finished run does not need one).

    Exceptions of the provider (ProviderError, e.g. NotLoggedIn) are logged and re-raised.  Interjections
    (`dispatcher.inbox`, see interject.py) are fed in at the next transition: after the tool results of a turn,
    or instead of ending it.  Jobs (long calls that an interjection detached) end with this stream."""
    jobs = getattr(dispatcher, "jobs", None)

    def rest() -> list[SessionEvent]:
        """What sub-agents put on the event queue after the last model call (e.g. the end of one that was stopped just now)."""
        events, out = getattr(dispatcher, "events", None), []
        while events is not None and not events.empty():
            ev = events.get_nowait()
            if not isinstance(ev, (_Prov, _End)):
                out.append(ev)
        return out
    try:
        async with _closing(_stream_loop(provider, ctx, dispatcher, run, messages, **kw)) as stream:
            async for ev in stream:
                yield ev
        if jobs is not None and jobs.items:
            left = [f"{j.id} ({j.what})" for j in jobs.live()]
            gone = await jobs.cancel_all()
            run.event("jobs", {"cancelled": gone})
            if left:                                # the turn ended for good with work still running: it is stopped, and said so
                n = Notice("warning", "stopped with the turn: " + ", ".join(left))
                run.event("notice", n)
                yield n
            for ev in rest():
                run.event(type(ev).__name__.lower(), ev)
                yield ev
    finally:
        if jobs is not None and jobs.items:         # stopped from outside (Esc, close)
            gone = await jobs.cancel_all()
            run.event("jobs", {"cancelled": gone})
            for ev in rest():
                run.event(type(ev).__name__.lower(), ev)


async def _stream_loop(provider: Provider, ctx: Context, dispatcher: Dispatcher, run: RunDir,
                       messages: list[Message], *, model: str, effort: str, context_limit: int,
                       handover_at: float, switch_at: float, max_turns: int = 0,
                       budget_tokens: int | None = None, budget_usd: float | None = None,
                       on_event: Callable[[SessionEvent], None] | None = None,
                       result: LoopResult | None = None,
                       handover_when_done: bool = False) -> AsyncIterator[SessionEvent]:
    res = result if result is not None else LoopResult()
    res.messages = messages
    effort = ultracode.model_effort(effort)        # no backend knows `ultracode`: it is effort max plus a system text
    owns = bool(getattr(provider, "owns_loop", False))
    handover_sent = warned = False

    def log(ev: SessionEvent) -> None:
        run.event(type(ev).__name__.lower(), ev)     # type: ignore[arg-type]
        if on_event:
            on_event(ev)

    inbox = getattr(dispatcher, "inbox", None)      # open interjections of the user (or of the main agent, for a sub-agent)
    deliver = getattr(dispatcher, "deliver", None)
    watched = getattr(dispatcher, "call_watched", dispatcher.call)    # a long call does not hide an interjection

    def feed() -> list[Interjected]:
        """The open interjections become one user message after what the model has so far (tool results)."""
        if deliver is None or inbox is None or not inbox.pending:
            return []
        text, evs = deliver(emit=False)
        m = Message("user", text, origin=ij.ORIGIN)
        messages.append(m)
        _log_message(run, m)
        return evs

    def wake_note() -> bool:
        """A timer or the watch wrote into the inbox of this session while it works: the framed note goes into the turn
        (wake.py).  Only the chat itself reads its inbox, not its sub-agents."""
        home = getattr(run, "home", None)
        if home is None or getattr(getattr(dispatcher, "spawn", None), "level", 0):
            return False
        try:
            from . import wake
            text = wake.take_framed(home, run.run.id)
        except Exception:                           # noqa: BLE001 - an unreadable inbox must not stop the turn
            return False
        if not text:
            return False
        wm = Message("user", text, origin=ij.HARNESS)
        messages.append(wm)
        _log_message(run, wm)
        run.event("wake", {"text": text[:600]})
        return True

    sub = hasattr(dispatcher, "emit")               # a real Dispatcher: sub-agents can put events on its queue
    if sub:
        dispatcher.cur = SimpleNamespace(model=model, effort=effort, context=context_limit,
                                         budget_usd=budget_usd, max_turns=max_turns)
    turns = held = 0
    q: "asyncio.Queue | None" = None
    while not max_turns or turns < max_turns:                 # 0 = no turn limit
        turns += 1
        prev, q = q, None
        if sub:                                     # a fresh queue per turn: nothing stale from a stopped one
            q = dispatcher.events = asyncio.Queue()
            while prev is not None and not prev.empty():     # ...but what a detached sub-agent said between two turns of THIS run is not stale
                q.put_nowait(prev.get_nowait())
        req = Request(system=ctx.prefix, state=ctx.state, messages=list(messages),
                      tools=[] if owns else ctx.tool_schemas, model=model, effort=effort,
                      max_turns=(max_turns or None) if owns else None, context=context_limit,
                      max_usd=max(budget_usd - run.run.cost_usd, 0.01) if owns and budget_usd else None)
        turn, texts = Tokens(), []
        calls: list[ToolCall] = []
        late: list[Interjected] = []                # a backend that owns the loop took these at a tool result itself
        errors: list[Error] = []
        hit: RateLimit | None = None
        try:
            async with _closing(provider.turn(req)) as stream:    # a stopped consumer ends the backend too
                async for ev in (_merged(stream, q, dispatcher) if sub else stream):
                    log(ev)
                    if getattr(ev, "parent", ""):
                        pass                        # a sub-agent's step: shown and logged, not part of this chat's turn
                    elif isinstance(ev, Text):
                        texts.append(ev.text)
                    elif isinstance(ev, ToolCall):
                        calls.append(ev)
                    elif isinstance(ev, Interjected):
                        late.append(ev)
                    elif isinstance(ev, Usage):
                        turn.add(ev)
                        res.tokens.add(ev)
                    elif isinstance(ev, Error):
                        errors.append(ev)
                    elif isinstance(ev, RateLimit) and ev.utilization >= switch_at and (
                            hit is None or ev.utilization > hit.utilization):
                        hit = ev
                    yield ev
        except Exception as e:                      # noqa: BLE001 - logged, the caller decides
            run.event("error", {"type": type(e).__name__, "message": str(e)[:500]})
            raise
        if turn != Tokens():
            run.add_usage(turn)
        text = "".join(texts)
        res.final_text = text
        res.context_tokens = (int(getattr(provider, "context_tokens", 0) or 0)
                              or turn.input + turn.cache_read + turn.cache_write)
        if text or calls:
            am = Message("assistant", text, tool_calls=[] if owns else list(calls))
            messages.append(am)
            _log_message(run, am)
        for ev in late:                             # for the record (and a replay); the backend has its own copy
            lm = Message("user", ij.frame([ij.Item(ev.id, ev.text)], inbox.source if inbox else ij.USER), origin=ij.ORIGIN)
            messages.append(lm)
            _log_message(run, lm)
        more = bool(calls) and not owns             # an API provider wants the tool results
        if more:
            answered: set[str] = set()
            tasks: dict[str, asyncio.Future] = {}
            try:
                if sub:                             # spawn calls of one turn work in parallel (run.max_parallel bounds them)
                    for c in calls:
                        if c.name == "spawn":
                            tasks[c.id] = asyncio.ensure_future(watched(c.name, c.args))
                for c in calls:
                    if sub:
                        t = tasks.get(c.id) or asyncio.ensure_future(watched(c.name, c.args))
                        tasks[c.id] = t
                        async for sev in _drained(t, q):
                            log(sev)
                            yield sev
                        r = t.result()
                    else:
                        r = await watched(c.name, c.args)
                    tm = Message("tool", r.text, tool_call_id=c.id, image=r.image)
                    messages.append(tm)
                    _log_message(run, tm)
                    answered.add(c.id)
                    done = ToolDone(c.id, c.name, r.ok, r.text[:TOOL_TEXT_LIMIT])
                    log(done)
                    yield done
            except BaseException:                   # cancelled (Esc, Ctrl+C) or closed: no call stays unanswered
                for t in tasks.values():
                    t.cancel()
                if sub:
                    dispatcher.cancel_spawned()
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                for c in calls:                     # the API rejects tool_use without a result, forever
                    if c.id not in answered:
                        tm = Message("tool", CANCELLED_TEXT, tool_call_id=c.id)
                        messages.append(tm)
                        _log_message(run, tm)
                raise

        if more and all(c.name == "job" for c in calls):
            turns -= 1                              # waiting for a job is not work: a long wait must not use up max_turns
        # ---- between turns: nothing below interrupts a model call
        if errors:
            e = errors[0]
            if e.kind in ("rate_limit", "billing_error"):
                res.status, res.exit_reason = "paused", e.kind
                res.switch_to = _fallback(dispatcher, run)
            elif e.kind == "max_turns":
                res.status, res.exit_reason = "paused", "max_turns"
            elif e.kind == "budget":                # the backend that owns the loop hit the money limit
                res.status, res.exit_reason = "budget", f"${budget_usd:g}" if budget_usd else "usd"
            else:
                res.status, res.exit_reason = "failed", f"{e.kind}: {e.message}"[:500]
            return
        if hit is not None:
            nxt = _fallback(dispatcher, run)
            if nxt:
                res.switch_to = nxt
                if more:
                    res.status, res.exit_reason = "paused", "rate_limit"
                    return
            elif not warned:
                warned = True
                n = Notice("limit", f"{hit.kind} at {hit.utilization:.0%} "
                                    f"(switch at {switch_at:.0%}); no fallback account configured")
                log(n)
                yield n
        if more and budget_tokens is not None and (
                res.tokens.input + res.tokens.output + res.tokens.cache_write) >= budget_tokens:
            res.status, res.exit_reason = "budget", f"{budget_tokens} tokens"
            return
        if more and budget_usd is not None and run.run.cost_usd >= budget_usd:
            res.status, res.exit_reason = "budget", f"${budget_usd:g}"
            return
        if (not handover_sent and context_limit > 0 and (more or handover_when_done)
                and handover_needed(res.context_tokens, context_limit, handover_at)):
            m = Message("user", handover_instruction())
            messages.append(m)
            _log_message(run, m)
            handover_sent = True
            n = Notice("handover", f"context {res.context_tokens}/{context_limit} tokens: "
                                   "asking for a handover")
            log(n)
            yield n
            continue
        if (not max_turns or turns < max_turns) and wake_note():
            continue
        evs = feed() if not max_turns or turns < max_turns else []   # a message of the user that came in during this turn: same turn goes on
        if evs:
            for ev in evs:
                log(ev)
                yield ev
            continue
        live = dispatcher.jobs.live() if hasattr(dispatcher, "jobs") else []
        if not more and live and held < ij.MAX_HOLDS:     # ending the turn would stop what runs in the background: say so first
            held += 1
            hm = Message("user", ij.hold_note(live), origin=ij.HARNESS)
            messages.append(hm)
            _log_message(run, hm)
            n = Notice("jobs", f"{', '.join(j.id for j in live)} still running: the turn waits for it")
            log(n)
            yield n
            continue
        if not more:
            res.status = "handover" if handover_sent else "done"
            return
    res.status, res.exit_reason = "paused", "max_turns"


async def run_loop(provider: Provider, ctx: Context, dispatcher: Dispatcher, run: RunDir,
                   messages: list[Message], *, model: str, effort: str,
                   context_limit: int, handover_at: float, switch_at: float,
                   max_turns: int = 0, budget_tokens: int | None = None,
                   budget_usd: float | None = None,
                   on_event: Callable[[SessionEvent], None] | None = None) -> LoopResult:
    """Loop until a turn has no ToolCall.  Logs every event via run.event().
    After each completed turn: Usage -> tokens; RateLimit >= switch_at or Error(rate_limit|
    billing_error) -> result.switch_to/paused; handover_needed -> inject handover
    instruction once, end with status 'handover'; budget_tokens exceeded -> 'budget'.
    Raises ProviderError when the provider cannot start a turn (not logged in, CLI missing)."""
    res = LoopResult()
    async for _ in stream_loop(provider, ctx, dispatcher, run, messages, model=model, effort=effort,
                               context_limit=context_limit, handover_at=handover_at,
                               switch_at=switch_at, max_turns=max_turns,
                               budget_tokens=budget_tokens, budget_usd=budget_usd,
                               on_event=on_event, result=res):
        pass
    return res


# ================================================================== part 2: sessions
@dataclass
class SessionInfo:
    """What a status line shows."""
    id: str
    server: str
    model: str
    account: str
    mode: str
    effort: str
    agent: str
    project: str
    ctx_used: int
    ctx_limit: int
    cost_usd: float
    cost_est: bool
    status: str
    limits: dict[str, float] = field(default_factory=dict)    # five_hour / seven_day, 0..1, last seen
    title: str = ""                                           # "" = none yet: show the first words of the first message
    description: str = ""

    @property
    def ctx_pct(self) -> float:
        return self.ctx_used / self.ctx_limit if self.ctx_limit else 0.0


def _context_block(parts: list[str]) -> str:
    return '\n\n<context source="on_prompt">\n' + "\n\n".join(parts) + "\n</context>"


def _state_dump(s: SessionState) -> dict:
    return {"mode": s.mode, "account": s.account, "model": s.model,
            "tools_added": [[r.kind, r.name] for r in s.tools_added],
            "tools_removed": list(s.tools_removed), "data_added": list(s.data_added),
            "rules": [{"tool": r.tool, "pattern": r.pattern, "effect": r.effect,
                       "until": r.until.isoformat() if r.until else None, "origin": r.origin}
                      for r in s.rules]}


SESSION_ORIGIN = "session:"      # every session rule the core mints has an origin that starts like this


def _state_load(d: dict) -> SessionState:
    """The layer-4 state back from `_state_dump`; a rule whose origin the core does not mint (it was not
    granted through open_session, an `always` answer or the CLI flags) is dropped, not trusted."""
    return SessionState(
        mode=d.get("mode", ""), account=d.get("account", ""), model=d.get("model", ""),
        tools_added=[ToolRef(k, n) for k, n in d.get("tools_added", [])],
        tools_removed=list(d.get("tools_removed", [])), data_added=list(d.get("data_added", [])),
        rules=[Rule(r["tool"], r.get("pattern"), r.get("effect", "allow"),
                    until=date.fromisoformat(r["until"]) if r.get("until") else None,
                    origin=r["origin"]) for r in d.get("rules", [])
               if str(r.get("origin", "")).startswith(SESSION_ORIGIN)])


def _events(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for line in path.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _toolref(home: Home, spec: str | ToolRef) -> ToolRef:
    """'name' -> skill, tool or connector (name with a slash); a ToolRef passes through."""
    if isinstance(spec, ToolRef):
        return spec
    if "/" in spec:
        return ToolRef("connector", spec)
    if any(s.name == spec for s in home.list_skills()):
        return ToolRef("skill", spec)
    if any(t.name == spec for t in home.list_tools()):          # also a tool with a README only
        return ToolRef("tool", spec)
    raise ValueError(f"unknown skill or tool {spec!r}")


def _project_value(home: Home, project: str | None, key: str) -> str:
    if not project:
        return ""
    try:
        meta = home.doc(home.path("projects") / project / home.filename("project_memory")).meta
    except OSError:
        return ""
    return accountpick.as_text(meta.get(key)) if key == "provider" else str(meta.get(key) or "")     # a provider may be a list


def _check_server(home: Home, server: str | None) -> str:
    local = str(settings_defaults(home).get("server") or "")
    server = server or local or "local"
    if server not in {"", "local", "localhost", local}:
        raise NotImplementedError("remote servers come in the next step")
    return server


def _check_mode(home: Home, mode: str) -> str:
    modes = home.modes()                                    # raises with the reason when the permissions file gives none
    if not mode or mode not in modes:
        raise ValueError(f"unknown mode {mode!r}; modes: {', '.join(modes)}")
    return mode


class Session:
    """One chat: settings, conversation, dispatcher, provider.  Create with open_session /
    resume_session.  `set()` records changes; they take effect at the start of the next turn."""

    def __init__(self, home: Home, run: RunDir, *, agent: Agent | None, project: str, server: str,
                 account: str, model_req: str, effort: str, context_limit: int, mode: str,
                 state: SessionState, asker=None, hook_gate=None, sdk_session_id: str = "",
                 messages: list[Message] | None = None, handover: str = "", ctx_used: int = 0,
                 resumed: bool = False, readonly_by: int = 0, questioner=None, workdir: str = "") -> None:
        self.home, self.run, self.agent, self.project, self.server = home, run, agent, project, server
        self.readonly_by = readonly_by              # pid of the process that has this chat open; 0 = this process has it
        self.account, self.effort, self.context_limit = account, effort, context_limit
        self.effort_before = ""                     # the effort the chat had before ultracode went on: `/ultracode off` returns to it
        self.mode, self.state, self._asker = mode, state, asker
        self._questioner = questioner               # optional async (question) -> answer for the `ask` tool; None = the terminal
        self.workdir = workdir                      # absolute folder the user works in (VS Code workspace); "" = none.  Context + $LM_WORKDIR, never the cwd
        self.hook_gate = hook_gate                  # optional (event, command) -> reason | None; see _run_hooks
        self._model_req = model_req
        self.model = resolve_model(home, account, model_req)
        self.state.model = self.model
        self.messages: list[Message] = messages if messages is not None else []
        self.ctx_used = ctx_used
        self.limits: dict[str, float] = {}
        self.start_context: list[str] = []          # on_start outputs for the volatile block
        self.last_result: LoopResult | None = None
        self.max_turns: int | None = None           # per-run limits (`lm run --max-turns / --max-usd`)
        self.max_usd: float | None = None
        self.resumed = resumed
        self.closed = False
        self.subs = subagents.SubLog()              # sub-agents of this chat (resume fills it from events.jsonl)
        self.bg = spawn.Background(self)            # sub-agents working while the chat is free (their result comes as a message)
        self.inbox = ij.Inbox()                     # messages of the user during a turn (`interject`): fed into the running turn
        self.leftover: list[ij.Item] = []           # interjections a turn ended without reading (stopped, failed): the UI sends them on
        self._busy = False
        self.auto_title = False                     # a surface of a person's chat turns it on (`enable_titles`); runs never name themselves
        self.personal = False                       # a person types here (set with `enable_titles`): only then the word `ultracode` switches it on
        self.on_title: list[Callable[[str, str], None]] = []   # called as (title, description) when either changes; may run in a thread
        self._turns_done, self._titled_at = 0, 0    # finished turns, and how many there were at the last automatic naming
        self._title_busy, self._title_lock = False, threading.Lock()
        self._persist_warning = ""                  # a failed save after a turn, told at the start of the next one
        self._todo_digest = ""                      # what the session was last told about the to-dos (todos.context_block)
        self._warned = 0
        self._dirty: set[str] = {"ctx"}
        self._next_provider: Provider | None = None   # built by set(account=), swapped in at the next turn
        self._handover_text = handover
        self._handover_task: Task | None = handover_task(handover, Task(goal=HANDOVER_GOAL)) if handover else None
        self.rule_ctx = RuleContext(
            home=str(home.root), run_dir=str(run.path),
            project_dir=str(home.path("projects") / project) if project else "")
        self.dispatcher = self._new_dispatcher()
        self.provider = None if readonly_by else make_provider(home, account, self.dispatcher)    # a chat that is only read has none
        if sdk_session_id and hasattr(self.provider, "session_id"):
            self.provider.session_id = sdk_session_id
        self.refresh_limits()
        self.ctx: Context | None = None
        self._persist()

    def refresh_limits(self) -> None:
        """5h / 7d utilization of the active account into `limits`, from what its provider last saw (a state file: also
        the runs of other chats of the account).  A provider without that, or no value yet, leaves `limits` as it is."""
        try:
            self.limits.update(self.provider.limits_now())
        except (AttributeError, OSError, ValueError, TypeError):
            pass

    @property
    def readonly(self) -> bool:
        """True when another live process has the chat open: it can be shown, nothing in it can change, and this
        session writes nothing (no events, no meta, no lock, no hooks)."""
        return bool(self.readonly_by)

    def _need_write(self) -> None:
        if self.readonly_by:
            raise ReadOnlyChat(f"this chat is open in another window (process {self.readonly_by}): read only here")

    # ---- read side
    @property
    def id(self) -> str:
        return self.run.run.id

    @property
    def info(self) -> SessionInfo:
        r = self.run.run
        return SessionInfo(
            id=r.id, server=self.server, model=self.model, account=self.account, mode=self.mode,
            effort=self.effort, agent=self.agent.name if self.agent else "", project=self.project,
            ctx_used=self.ctx_used, ctx_limit=self.context_limit, cost_usd=r.cost_usd,
            cost_est=r.cost_est, status=r.status, limits=dict(self.limits),
            title=r.title, description=r.description)

    @property
    def asker(self):
        return self._asker

    @asker.setter
    def asker(self, value) -> None:
        self._asker = value
        self.dispatcher.asker = value

    @property
    def questioner(self):
        return self._questioner

    @questioner.setter
    def questioner(self, value) -> None:
        self._questioner = value
        self.dispatcher.questioner = value

    # ---- changes (effective from the next turn)
    def set(self, *, model: str | None = None, mode: str | None = None,
            account: str | None = None, effort: str | None = None,
            tools_on: list[str | ToolRef] | None = None, tools_off: list[str] | None = None) -> None:
        self._need_write()
        if mode is not None and mode != self.mode:
            self.mode = self.state.mode = _check_mode(self.home, mode)
        if account is not None:
            name = resolve_account(self.home, requested=account)
            if name != self.account:
                self._next_provider = make_provider(self.home, name, self.dispatcher)   # fails now, not mid-chat
                self.account = self.state.account = name
                self.model = resolve_model(self.home, name, self._model_req)
        if model is not None:
            self._model_req = model
            self.model = resolve_model(self.home, self.account, model)
        if effort:
            if ultracode.is_on(effort) and not ultracode.is_on(self.effort):
                self.effort_before = self.effort    # remembered here, so the word, /effort and /ultracode all go back the same way
            self.effort = effort
        if tools_on or tools_off:
            for spec in tools_on or []:
                ref = _toolref(self.home, spec)
                self.state.tools_removed = [n for n in self.state.tools_removed if n != ref.name]
                if ref not in self.state.tools_added:
                    self.state.tools_added.append(ref)
            for off in tools_off or []:
                self.state.tools_added = [r for r in self.state.tools_added if r.name != off]
                if off not in self.state.tools_removed:
                    self.state.tools_removed.append(off)
            self._dirty.add("dispatcher")
        self.state.model = self.model
        self._dirty.add("ctx")
        self._persist()

    # ---- building blocks
    def _new_dispatcher(self) -> Dispatcher:
        d = Dispatcher(self.home, agent=self.agent, mode=self.mode, session=self.state,
                       rule_ctx=self.rule_ctx, run=self.run, asker=self._asker, questioner=self._questioner,
                       workdir=self.workdir, background=self.bg)
        d.todo_told = self._todo_told
        d.inbox = self.inbox
        d.names_set = lambda field, text: titles.set_manual(self, field, text)
        return d

    def _todo_told(self, before: str, after: str) -> None:
        """The agent changed the to-dos itself through `todo`: it knows, so do not tell it again.  Only when nothing
        else changed in between (`before` is what it was last told); changes by the user still come as news."""
        if before == self._todo_digest:
            self._todo_digest = after

    def _state_info(self) -> dict[str, str]:
        info = {"server": self.server, "mode": self.mode, "model": self.model, "account": self.account,
                "run": f"{self.home.rel('runs')}/{self.run.run.id}"}
        if self.workdir:
            info["working folder"] = self.workdir     # absolute: work there with absolute paths or `cd "$LM_WORKDIR" && ...`; the shell starts in the tree
        if self.start_context:
            info["start"] = "\n".join(self.start_context)
        kids = self._children_line()
        if kids:
            info["sub-runs"] = kids                   # the children book (watch.py): what the watch is following for this chat
        return info

    def _children_line(self) -> str:
        if self.readonly_by:
            return ""
        from . import watch
        try:
            self._kids = watch.state_line(self.home, self.run.run.id)
        except Exception:                             # noqa: BLE001 - a broken book must not stop a turn
            self._kids = ""
        return self._kids

    def _build_ctx(self) -> Context:
        try:
            rules_text = self.home.key_doc("permissions").body.strip()
        except (OSError, KeyError):
            rules_text = ""
        ctx = build_context(self.home, agent=self.agent, mode=self.mode, task=None,
                            session=self.state, project=self.project, rules_text=rules_text,
                            builtin_schemas=self.dispatcher.schemas(),
                            state_info=self._state_info())
        if ultracode.is_on(self.effort):
            ctx.blocks.append(("ultracode", ultracode.system_text(ultracode.read(self.home))))   # above the cache line: it stays the same for the whole chat
        return ctx

    def _sync(self) -> None:
        """Apply what set() recorded.  Runs at the start of a turn, never inside one."""
        if "dispatcher" in self._dirty:
            old, self.dispatcher = self.dispatcher, self._new_dispatcher()
            self.dispatcher.n, self.dispatcher.masker = old.n, old.masker   # keep tool-out numbering and masking
        if self._next_provider is not None:
            self.provider, self._next_provider, self._warned = self._next_provider, None, 0
            self.limits = {}                                                # the other account's windows, not the old one's
            self.refresh_limits()
        self.provider.dispatcher = self.dispatcher                          # type: ignore[attr-defined]
        self.dispatcher.mode = self.mode
        if "ctx" in self._dirty or self.ctx is None:
            self.ctx = self._build_ctx()
        self._dirty.clear()

    def _persist(self) -> None:
        """Run meta, session.md, a `session` event and the trusted record: everything resume_session needs.
        The run folder is writable by agents, so the same facts, plus the session rules, also go where
        they cannot write (the `state` location); resume reads the conversation from the run folder and
        everything else from there.  A chat that is only read writes nothing."""
        if self.readonly_by:
            return
        r = self.run.run
        r.account, r.model, r.mode, r.server = self.account, self.model, self.mode, self.server
        acct = self.home.load_providers().accounts.get(self.account)
        r.provider = acct.type if acct else ""
        self.run.add_usage(Tokens())                      # rewrites meta.json
        self.run.write_session(self.state)
        # After `set(account=)` the next turn starts fresh on the new account: the old account's conversation id must not
        # stay on disk under the new account's name (resume would hand it to a CLI that does not know it).
        upcoming = self._next_provider or self.provider if hasattr(self, "provider") else None
        info = {
            "sdk_session_id": getattr(upcoming, "session_id", "") if upcoming is not None else "",
            "sdk_account": getattr(upcoming, "account", self.account) if upcoming is not None else self.account,
            "server": self.server, "agent": self.agent.name if self.agent else "",
            "project": self.project, "account": self.account, "model": self.model,
            "model_req": self._model_req, "effort": self.effort, "effort_before": self.effort_before, "context": self.context_limit,
            "mode": self.mode, "ctx_used": self.ctx_used, "origin": self.run.run.origin, "workdir": self.workdir,
            "handover": self._handover_text if self._handover_task is not None else ""}
        self.run.write_trusted({**info, "state": _state_dump(self.state)})
        self.run.event("session", info)

    # ---- the turn
    def interject(self, text: str) -> ij.Item | None:
        """A message of the user while a turn runs: it goes into that turn at the next transition (after a tool result, or
        instead of the turn ending) as an interjection, so the model answers it and adapts without a restart, and a long
        tool call is detached to make room for it.  None = not taken (no turn is running, read only, or `interject.MAX_OPEN`
        are open already): the caller sends it as a normal message or queues it.  No on_prompt hook runs for it."""
        if self.closed or self.readonly or not self._busy:
            return None
        return self.inbox.put(text)

    def take_leftover(self) -> list[ij.Item]:
        """The interjections the last turn never read (it was stopped or failed, or they came in at its very end)."""
        out, self.leftover = self.leftover, []
        return out

    async def send(self, text: str, origin: str = "") -> AsyncIterator[SessionEvent]:
        """One user message -> events of the answer (tool calls, tool results, text, usage,
        notices).  Between-turn checks run inside; the session stays usable after an Error.
        `origin="subagent"`: `text` is the framed result of a background sub-agent (spawn.Background.take): data, not
        words of the user.  No on_prompt hook runs for it, it counts as an automatic turn (`spawn.auto_turns` in a row
        at most) and that turn may not start background sub-agents.  A message of the user resets the count and takes
        the results that waited with it.  A cancelled turn also cancels the sub-agents working in the background."""
        if self.closed:
            raise RuntimeError("session is closed")
        self._need_write()
        if self._busy:
            raise RuntimeError("a turn is already running in this session")
        if not text.strip():
            raise ValueError("empty message")
        self._busy = True
        try:
            if self._ultracode_word(text, origin):
                self.set(effort=ultracode.ULTRACODE)
                yield Notice("effort", "ultracode is on for this chat (effort max, substantial tasks as workflows); /ultracode off switches it off")
            before = getattr(self, "_kids", "")
            if self._children_line() != before:
                self._dirty.add("ctx")                   # the list of open sub-runs changed: the State block is made again
            self._sync()
            assert self.ctx is not None
            self.bg.auto = self.bg.auto + 1 if origin else 0
            if not origin:
                self.bg.warned = False
            self.bg.auto_turn = bool(origin)
            hooks = (SimpleNamespace(lines=[], warnings=[], context=[]) if origin
                     else await asyncio.to_thread(run_on_prompt, self.home, self, text))   # blocking shell commands
            for line in hooks.lines:
                yield Notice("hook", line)
            for w in hooks.warnings:
                yield Notice("warning", w)
            if self._persist_warning:
                yield Notice("warning", self._persist_warning)
                self._persist_warning = ""
            parts = list(hooks.context)
            block, self._todo_digest = todos.context_block(todos.Store(self.run.path), self._todo_digest)
            if block:
                parts.append(block)                      # changed to-dos, new notes of the user: after the cache line
            body = text + _context_block(parts) if parts else text    # below the cache line
            task, self._handover_task = self._handover_task, None
            if not origin:                               # results that waited (limit of automatic turns) come before the user's words
                for held in self.bg.ready:
                    hm = Message("user", held, origin="subagent")
                    self.messages.append(hm)
                    _log_message(self.run, hm)
                self.bg.ready.clear()
            for m in initial_messages(self.ctx, task, body):
                m.origin = origin
                self.messages.append(m)
                _log_message(self.run, m)
            res = LoopResult()
            self.subs.new_turn()
            cfg = settings(self.home)
            async with _closing(stream_loop(
                    self.provider, self.ctx, self.dispatcher, self.run, self.messages,
                    model=self.model, effort=self.effort, context_limit=self.context_limit,
                    handover_at=float((cfg.get("context") or {}).get("handover_at", 0.7)),
                    switch_at=self.home.load_providers().switch_at,
                    max_turns=self.max_turns or int(settings_defaults(self.home).get("max_turns", 0)),
                    budget_tokens=self.agent.budget_tokens if self.agent else None,
                    budget_usd=min((x for x in (self.max_usd, self.agent.budget_usd if self.agent else None)
                                   if x), default=None),     # the stricter of --max-usd and the agent's budget
                    handover_when_done=True, result=res)) as stream:
                async for ev in stream:
                    if isinstance(ev, RateLimit):
                        self.limits[ev.kind] = ev.utilization
                    self.subs.feed(ev)
                    yield ev
            async for ev in self._after(res):
                yield ev
            if not origin:
                titles.after_turn(self)                  # a thread: the chat does not wait for it
        except asyncio.CancelledError:
            self.bg.cancel_all()                         # Esc / cancel stops the sub-agents working in the background too
            raise
        finally:
            self.bg.auto_turn = False
            self._busy = False
            self.leftover += self.inbox.withdraw()
            if not self.closed:
                try:
                    self._persist()
                except OSError as e:     # disk full, a virus scanner's lock: must not hide the turn's own outcome
                    self._persist_warning = f"could not save the session ({e.__class__.__name__}: {e}); it will retry after the next turn"
            from . import sync
            sync.after_turn(self.home)                   # a sync repo: commit + push in the background, debounced; silent otherwise

    async def _after(self, res: LoopResult) -> AsyncIterator[SessionEvent]:
        self.last_result = res
        self.ctx_used = res.context_tokens
        for w in getattr(self.provider, "warnings", [])[self._warned:]:
            self._warned += 1
            self.run.event("warning", {"text": w})
            yield Notice("warning", w)
        if res.switch_to:
            old = self.account
            self.set(account=res.switch_to)
            yield Notice("switch", f"{old} -> {res.switch_to} from the next turn ({res.exit_reason or 'limit'})")
        if res.status == "handover":
            log = self.run.path / "log.md"
            try:
                text = log.read_text("utf-8")
            except OSError:
                text = ""
            self._handover_text = text if "## Handover" in text else res.final_text
            self._handover_task = handover_task(self._handover_text, Task(goal=HANDOVER_GOAL))
            self.messages.clear()
            self.run.event("reset", {"reason": "handover"})
            self.ctx_used = 0
            if hasattr(self.provider, "session_id"):
                self.provider.session_id = ""
            yield Notice("handover", "the conversation continues fresh from the handover")

    def close(self, status: str = "done", reason: str = "closed") -> list[str]:
        """Run on_end, finish the run folder.  Returns the on_end lines to show."""
        if self.closed:
            return []
        if self.readonly_by:                         # nothing here was started, so nothing ends
            self.closed = True
            return []
        try:
            gone = self.bg.cancel_all()
            self.bg.on_message = None
            lines = run_on_end(self.home, self)
            if gone:
                lines.append(f"{len(gone)} sub-agent(s) still working in the background were cancelled: {', '.join(gone)}")
            self._persist()
            self.closed = True
            self.run.finish(status, reason)
            titles.at_close(self)
        finally:
            release_lock(self.home, self.id)
        return lines

    def hand_over(self) -> None:
        """Give this chat to another process (a new tab resumes it by its id).  Everything that process needs is on disk:
        the run stays open (`paused`, no end marker), the lock is released and no on_end hook runs, because the chat
        has not ended."""
        if self.closed:
            return
        self.closed = True
        if self.readonly_by:
            return
        try:
            self.bg.cancel_all()
            self._persist()
            self.run.park()
        finally:
            release_lock(self.home, self.id)


    def enable_titles(self) -> None:
        """Name this chat automatically (`sessions.auto_title` of the settings; on by default).  Called by the surfaces of a
        person's chat, not by `lm run`; it also marks the chat as one a person types in (`personal`)."""
        self.auto_title = titles.settings(self.home)["auto_title"] and not self.readonly
        self.personal = not self.readonly

    def _ultracode_word(self, text: str, origin: str) -> bool:
        """Does this message switch ultracode on?  Only words of the user in a chat a person types in (no sub-agent result,
        timer, trigger, `lm run` or run started by an agent), only when `workflow.keyword` is not off, not when it is on."""
        if origin or not self.personal or self.run.run.depth or self.run.run.parent or ultracode.is_on(self.effort):
            return False
        from .util import setting
        return setting(self.home, "workflow.keyword", True) is not False and ultracode.mentioned(text)

    def rename(self, text: str) -> str:
        """`/rename`: set the title by hand; empty = automatic again.  Returns the line to show."""
        self._need_write()
        return titles.set_manual(self, "title", text)

    def describe(self, text: str) -> str:
        """`/describe`: set the description by hand; empty = automatic again.  Returns the line to show."""
        self._need_write()
        return titles.set_manual(self, "description", text)

    @property
    def title(self) -> str:
        return self.run.run.title

    @property
    def description(self) -> str:
        return self.run.run.description

    def reload(self, providers: bool = False) -> list[str]:
        """Rebuild the prefix and the dispatcher (skills, agents, tools, settings) at the start of the next turn.  The
        agent file is read again and the model is resolved again; with `providers` the provider is built anew (its
        account config changed), which replays the history once like `/account`.  A file that no longer loads keeps the
        old value; the returned lines say so.  The history, the sub-agents working and the session rules stay."""
        self._need_write()
        problems: list[str] = []
        if self.agent is not None:
            try:
                self.agent = self.home.load_agent(self.agent.name)
            except Exception as e:                                    # noqa: BLE001 - keep the agent that loaded
                problems.append(f"agent {self.agent.name}: {e} (kept the old one)")
        try:
            self.model = resolve_model(self.home, self.account, self._model_req)
            self.state.model = self.model
        except Exception as e:                                        # noqa: BLE001
            problems.append(f"model: {e} (kept {self.model})")
        if providers:
            try:
                new = make_provider(self.home, self.account, self.dispatcher)
                if getattr(self.provider, "session_id", "") and hasattr(new, "session_id"):
                    new.session_id = self.provider.session_id         # the SDK conversation goes on; a lost one is replayed
                self._next_provider = new
            except Exception as e:                                    # noqa: BLE001
                problems.append(f"account {self.account}: {e} (kept the old provider)")
        self._dirty.update({"dispatcher", "ctx"})
        self._persist()
        return problems

    def set_project(self, project: str) -> None:
        """Bind the chat to a project entry after it was opened (the VS Code backend makes the entry at the first message).
        Takes effect at the start of the next turn."""
        self._need_write()
        self.project = project
        self.run.run.project = project
        self.rule_ctx = replace(self.rule_ctx, project_dir=str(self.home.path("projects") / project) if project else "")
        self._dirty.update({"dispatcher", "ctx"})
        self._persist()

# ---------------------------------------------------------------------- factories
def open_session(home: Home, server: str | None = None, agent: str | None = None,
                 project: str | None = None, account: str | None = None,
                 model: str | None = None, effort: str | None = None,
                 context: str | int | None = None, mode: str | None = None,
                 tools_on: list[str | ToolRef] | None = None, tools_off: list[str] | None = None,
                 secrets: list[str] | None = None, rules: list[Rule] | None = None, asker=None,
                 hook_gate=None, run_id: str | None = None, max_turns: int | None = None,
                 max_usd: float | None = None, origin: str = "", questioner=None, workdir: str = "") -> Session:
    """New chat.  No model call happens here.  Precedence, first hit wins: argument -> agent ->
    project (account only) -> settings defaults.  `server` None = defaults.server; any other
    server raises NotImplementedError for now.  `secrets` = secret refs ("name/account") the
    session may inject into tools (session rules).  `rules` = ready-made layer-4 rules (the CLI's
    --allow / --deny); their origin must start with `session:` or resume drops them.  `asker`
    answers rule asks (see rules.Asker).  `hook_gate(event, command) -> reason | None` may veto a
    hook command (see _run_hooks).  `run_id` = a run id made beforehand (`lm run --detach` names the
    run before the process exists); `max_turns` / `max_usd` = limits of this run only.  The run
    records its `parent` and `depth` from LM_PARENT / LM_DEPTH (set by the tool environment of the run
    that started it) and its own `pid`."""
    d = settings_defaults(home)
    server = _check_server(home, server)
    ag = home.load_agent(agent) if agent else None
    if not project and ag is not None and ag.project:           # the agent's own default; `-p` and `project=` win over it
        if not home.has_project(ag.project):
            raise ValueError(f"agent {ag.name!r}: its project {ag.project!r} does not exist")
        project = ag.project
    if project and not (home.path("projects") / project).is_dir():
        raise ValueError(f"unknown project {project!r}")
    acct = resolve_account(home, account or "", ag.provider if ag else "",
                           _project_value(home, project, "provider"))
    if not account and not (ag and ag.provider):          # not asked for by name, not pinned by an agent: a week over 95 % moves the start to the fallback
        cfg = home.load_providers()
        acct = accountpick.start_account(home, acct, [resolve_alias(cfg, n) for n in cfg.fallback.get(acct, [])])
    mode = _check_mode(home, mode or (ag.mode if ag else "") or str(d.get("mode") or ""))
    model_req = model or (ag.model if ag else "") or ""
    state = SessionState(mode=mode, account=acct,
                         tools_added=[_toolref(home, t) for t in tools_on or []],
                         tools_removed=list(tools_off or []))
    if secrets:
        from .rules import literal
        state.rules += [Rule("secret", literal(ref), "allow", origin="session:open") for ref in secrets]
    state.rules += rules or []
    run = Run(id=run_id or new_run_id(ag.name if ag else ""), agent=ag.name if ag else "",
              project=project or "", server=server, mode=mode, account=acct, pid=os.getpid(),
              parent=os.environ.get("LM_PARENT", ""), depth=max(to_int(os.environ.get("LM_DEPTH")), 0), origin=origin)
    acquire_lock(home, run.id)                       # a new id: nobody else has this chat
    try:
        s = Session(home, RunDir(home, run), agent=ag, project=project or "", server=server,
                    account=acct, model_req=model_req,
                    effort=effort or (ag.effort if ag else "") or str(d.get("effort") or "medium"),
                    context_limit=parse_size(context or (ag.context_limit if ag else 0)
                                             or d.get("context") or 200_000),
                    mode=mode, state=state, asker=asker, hook_gate=hook_gate, questioner=questioner, workdir=workdir)
    except BaseException:
        release_lock(home, run.id)
        raise
    s.max_turns, s.max_usd = max_turns, max_usd
    return s


def _unanswered(messages: list[Message]) -> list[Message]:
    """Cancelled results for the tool calls of a trailing assistant message that never got one
    (a session that died or was stopped inside a tool call); [] when the history is consistent."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            calls = messages[i].tool_calls
            have = {m.tool_call_id for m in messages[i + 1:] if m.role == "tool"}
            return [Message("tool", CANCELLED_TEXT, tool_call_id=c.id) for c in calls if c.id not in have]
        if messages[i].role != "tool":
            return []
    return []


def _own_sdk_session(rec: dict, account: str) -> str:
    """The SDK conversation id of a record when it was made by `account`; "" when another account made it (that account's CLI
    does not know it: "No conversation found").  The chat then starts fresh on this account with the logged history.  A record
    without `sdk_account` (written before the field existed) is trusted."""
    owner = str(rec.get("sdk_account") or "")
    return str(rec.get("sdk_session_id") or "") if not owner or owner == account else ""


def resume_session(home: Home, id: str, asker=None, hook_gate=None, questioner=None) -> Session:
    """Continue a chat (id or unique prefix).  The same SDK conversation continues for
    claude_sdk; if the CLI no longer knows it, the provider replays the logged history.

    The run folder is writable by agents, so only the conversation is read from it.  Everything
    else (agent, project, server, account, mode, model, the session rules, the SDK conversation id)
    comes from the trusted record in the `state` location and is checked again: agent and project
    must still exist, the mode must still be defined, a rule without a session origin is dropped.
    A session without that record cannot be resumed.

    One writer per chat: the session lock (`runs.acquire_lock`, `<state>/sessions/<id>.lock`, the pid of the process).
    When another live process has the chat open, the result is a read-only session (`Session.readonly`, `readonly_by`
    = that pid): the conversation can be shown, `send` and `set` raise `ReadOnlyChat`, nothing is written.  The lock of
    a process that is gone (or whose pid another program has now) is taken over, and the run's `pid` is written anew."""
    found = show_run(home, id)                      # FileNotFoundError unless exactly one match
    folder = Path(found["path"])
    run = Run.from_dict(found["run"])
    run.id = folder.name                            # the folder names the run, not a field an agent could have edited
    events = _events(folder / "events.jsonl")
    if not any(e.get("kind") == "session" for e in events):
        raise ValueError(f"run {run.id} is not a chat session")
    rec = read_trusted(home, run.id)
    if rec is None:
        raise ValueError(f"run {run.id} cannot be resumed: its record in {home.rel('state')}/sessions is missing")
    messages: list[Message] = []
    for e in events:
        if e.get("kind") == "reset":
            messages.clear()
        elif e.get("kind") == "message":
            d = e["data"]
            messages.append(Message(d["role"], d.get("text", ""), [ToolCall(**c) for c in d.get("tool_calls", [])],
                                    d.get("tool_call_id", ""), str(d.get("origin") or "")))
    pending = _unanswered(messages)                 # a chat stopped inside a tool call: close it, or the API says 400 forever
    name, project = str(rec.get("agent") or ""), str(rec.get("project") or "")
    for kind, value, known in (("agent", name, home.list_agents()), ("project", project, home.list_projects())):
        if value and value not in known:
            raise ValueError(f"run {run.id}: the {kind} {value!r} of this session does not exist any more")
    server = _check_server(home, rec.get("server"))
    mode = _check_mode(home, str(rec.get("mode") or ""))
    account = resolve_account(home, requested=str(rec.get("account") or ""))
    holder = acquire_lock(home, run.id)             # 0: the chat is ours now, else the pid of the process that has it
    try:
        if not holder:
            run.status, run.end, run.pid = "running", "", os.getpid()
        rd = RunDir(home, run, write=not holder)
        nums = [int(p.name.split("-")[0]) for p in (rd.path / "tool-out").glob("*-*.txt")
                if p.name.split("-")[0].isdigit()]
        s = Session(home, rd, agent=home.load_agent(name) if name else None, project=project, server=server,
                    account=account, model_req=str(rec.get("model_req") or ""),
                    effort=str(rec.get("effort") or "medium"), context_limit=int(rec.get("context") or 200_000),
                    mode=mode, state=_state_load(rec.get("state") or {}), asker=asker, hook_gate=hook_gate, questioner=questioner, workdir=str(rec.get("workdir") or ""),
                    sdk_session_id=_own_sdk_session(rec, account), messages=messages,
                    handover=str(rec.get("handover") or ""), ctx_used=int(rec.get("ctx_used") or 0),
                    resumed=True, readonly_by=holder)
        s.dispatcher.n = max(nums, default=0)
        s.effort_before = str(rec.get("effort_before") or "")
        s.subs = subagents.from_events(events)
        for sb in s.subs.subs.values():              # one that worked in the background when the process ended is not working now
            if not sb.done:
                s.subs.feed(SubagentEnd(sb.id, False, "interrupted: the chat was not open"))
        if not holder:                               # a chat the other process still works in is not ours to close
            for tm in pending:
                messages.append(tm)
                _log_message(rd, tm)
    except BaseException:
        if not holder:
            release_lock(home, run.id)
        raise
    return s
