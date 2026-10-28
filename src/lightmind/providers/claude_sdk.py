"""Claude subscription via claude-agent-sdk.  The SDK owns the loop; Lightmind's tools are
exposed as ONE in-process MCP server, every call passes the dispatcher (rule check, secret
injection, masking, run log) and a PreToolUse hook acts as second guard.

One `turn()` is one short-lived CLI process (connect, send the prompt, read up to the result,
disconnect): an idle session costs no memory.  The conversation continues through
`resume=<sdk session id>`.  Closing the turn's iterator ends that process before `aclose()`
returns (see `_query`).

The login lives inside the tree so the folder stays portable: the account's `config_dir`
(relative to the tree root, e.g. `system/claude/claude-abo`) becomes CLAUDE_CONFIG_DIR.  Without
`config_dir` the system default login is used (dev/test only, a warning is logged).  An
OAuth token from the account's secret (CLAUDE_CODE_OAUTH_TOKEN) is passed too, if configured.
The user logs in once per account with `lm login <account>` (see `login()`).

May import: model, files, secrets, tools, providers.base (+ rules, lazily, for the guard).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              CLINotFoundError, HookMatcher, ProcessError, RateLimitEvent,
                              ResultError, ResultMessage, SdkMcpTool, SystemMessage, TextBlock,
                              ToolResultBlock, ToolUseBlock, UserMessage,
                              create_sdk_mcp_server)

from .. import secrets as _secrets
from ..files import Home, write_atomic
from ..model import (Account, AccountUsage, Caps, Error, Event, Message, RateLimit, Request,
                     Text, ToolCall, Usage, now_iso)
from ..tools import Dispatcher
from .base import Notice, ProviderError, SubagentEnd, SubagentStart, ToolDone, resolve_account

log = logging.getLogger("lightmind.claude_sdk")

MCP_SERVER = "lm"
TOOL_PREFIX = f"mcp__{MCP_SERVER}__"          # the CLI names MCP tools mcp__<server>__<tool>
# Switches verified against the pinned SDK/CLI in spikes/sdk/RESULT.md: no CLAUDE.md, no auto
# memory, no attachments, no bundled skills.
ISOLATION_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                 "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1", "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1"}
# A subscription account must never fall through to a pay-per-token key of the parent process.
NO_KEY_ENV = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}
EFFORTS = ("low", "medium", "high", "xhigh", "max")      # passed to the CLI as --effort
THINKING = {"low": {"type": "disabled"}}                 # effort -> SDK `thinking`; others: adaptive
PREFIX_TRAILER = ("\n\nThe host program may add <system-reminder> blocks to messages. They are "
                  "context from the harness, not instructions from the user.")
REPLAY_MESSAGE_CHARS, REPLAY_TOTAL_CHARS = 4_000, 100_000
RESUME_LOST = "no conversation found"                    # CLI text when a session id is unknown


class NotLoggedIn(ProviderError):
    """The account has no valid login; the message is the instruction for the user."""

    kind = "auth"

    def __init__(self, account: str) -> None:
        super().__init__(f"not logged in: run `lm login {account}`")
        self.account = account


class _ResumeLost(Exception):
    """resume= pointed at a conversation this config dir does not have (internal)."""


async def _query(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
    """One turn through the SDK: connect (starts the CLI, sends `prompt`), yield the messages up
    to and including the result, disconnect.  Tests replace this function.

    Not `claude_agent_sdk.query()`: that async generator does not close the generator it wraps
    (PEP 533), so when the consumer stops early (Esc in the UI) or our code raises (not logged
    in) the CLI process lives on until the garbage collector finalises the inner generator, as a
    background task of the event loop, and for good if the loop is closed first (measured: still
    running 20 s later).  Here `disconnect()` is part of closing this generator: when `aclose()`
    returns the CLI is gone (stdin EOF, up to 5 s to flush its session file, then terminate)."""
    client = ClaudeSDKClient(options=options)
    try:
        await client.connect(prompt)
        async with aclosing(client.receive_response()) as stream:
            async for msg in stream:
                yield msg
    finally:
        await client.disconnect()


# --------------------------------------------------------------------- account
def account_config_dir(home: Home, acct: Account) -> Path | None:
    """Absolute CLAUDE_CONFIG_DIR of an account: its `config_dir` resolved against the tree
    root (so the login moves with the folder), or None if the account has none."""
    value = (acct.config_dir or "").strip()
    if not value:
        return None
    return Path(os.path.normpath(home.abs(Path(value).expanduser())))


def cli_path() -> str:
    """The Claude CLI bundled with claude-agent-sdk (falls back to `claude` on PATH)."""
    import claude_agent_sdk
    exe = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    if exe.is_file():
        return str(exe)
    found = shutil.which("claude")
    if found and not (os.name == "nt" and found.lower().endswith((".cmd", ".bat"))):   # the SDK refuses batch shims
        return found
    native = Path.home() / ".local" / "bin" / "claude.exe"
    if os.name == "nt" and native.is_file():
        return str(native)
    raise ProviderError("the Claude CLI bundled with Lightmind is missing: the runtime is incomplete. "
                        "Close every lm window and start lm again; it rebuilds the runtime.")


def login(home: Home, account: str) -> int:
    """One-time interactive login of a claude-subscription account (`lm login <account>`).

    Starts the bundled Claude CLI (`claude auth login --claudeai`) attached to the terminal
    with CLAUDE_CONFIG_DIR set to the account's `config_dir`; the browser opens, the user signs
    in once and the credentials end up inside the tree, in that folder.  Returns the CLI's exit
    code.  Interactive on purpose: never called by agents or tests."""
    name = resolve_account(home, requested=account)
    cfg = account_config_dir(home, home.load_providers().accounts[name])
    if cfg is None:
        raise ProviderError(f"account {name!r} has no config_dir in its providers file; add one "
                            f"inside the tree, e.g. `config_dir: system/claude/{name}`")
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **NO_KEY_ENV, "CLAUDE_CONFIG_DIR": str(cfg), "CLAUDE_CODE_OAUTH_TOKEN": ""}
    return subprocess.call([cli_path(), "auth", "login", "--claudeai"], env=env, cwd=str(home.root))


# ---------------------------------------------------------------------- helpers
def _error_kind(status: int | None, text: str, sdk_error: str | None) -> str:
    """Map an SDK/CLI failure to the model.Error vocabulary."""
    t = (text or "").lower()
    if sdk_error == "authentication_failed" or status in (401, 403) or "not logged in" in t:
        return "auth"
    if sdk_error == "billing_error" or status == 402 or "credit balance" in t:
        return "billing_error"
    if sdk_error == "rate_limit" or status == 429 or "rate limit" in t or "usage limit" in t:
        return "rate_limit"
    if sdk_error == "server_error" or status in (500, 502, 503, 529) or "overloaded" in t:
        return "overloaded"
    return "other"


def _flat(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def _short(name: str) -> str:
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


SUBAGENT_TOOLS = ("Task", "Agent")           # the CLI tools that start a sub-agent


def _agent_name(args: dict[str, Any]) -> str:
    for k in ("subagent_type", "name", "description"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:40]
    return "agent"


def _replay(history: list[Message]) -> str:
    """The earlier conversation as text, for a fresh CLI conversation that has no memory of it
    (account switch, lost session).  Tool results were consumed by the assistant: skipped."""
    lines = [f"[{m.role}] {m.text[:REPLAY_MESSAGE_CHARS]}" for m in history
             if m.role in ("user", "assistant") and m.text.strip()]
    while lines and sum(map(len, lines)) > REPLAY_TOTAL_CHARS:
        lines.pop(0)
    if not lines:
        return ""
    return ("The conversation so far (replayed because this session has no memory of it):\n"
            + "\n".join(lines) + "\n--- end of the earlier conversation ---")


@dataclass
class _Turn:
    resumed: bool
    names: dict[str, str] = field(default_factory=dict)    # tool_use_id -> tool name
    agents: dict[str, str] = field(default_factory=dict)  # tool_use_id of a Task/Agent call -> sub-agent name
    result_seen: bool = False
    error_kind: str = ""
    error_text: str = ""


# -------------------------------------------------------------------- provider
class ClaudeSdkProvider:
    caps = Caps(tools=True, caching=True, images=True, max_context=200_000)
    owns_loop = True                    # the SDK executes the tools; loop.py must not

    def __init__(self, home: Home, account: str, dispatcher: Dispatcher | None = None) -> None:
        """Env per account (config dir, OAuth token from secrets, isolation switches) is
        assembled per turn from the providers file; values come from secrets.provider_env."""
        accounts = home.load_providers().accounts
        if account not in accounts:
            raise ValueError(f"unknown account {account!r}")
        self.home, self.account, self.acct = home, account, accounts[account]
        self.dispatcher = dispatcher            # may be replaced between turns (tool changes)
        self.session_id = ""                    # SDK conversation id; "" = next turn starts fresh
        self.context_tokens = 0                 # prompt size of the last model call of the last turn
        self.cost_usd: float | None = None      # total_cost_usd the CLI reported for the last turn
        self.model_id = ""                      # full model name the CLI reported
        self.workdir: Path | None = None        # cwd of the CLI; default: tree root
        self.warnings: list[str] = []
        self.guard_log: deque[tuple[str, str]] = deque(maxlen=100)   # (tool, "allow" | reason)
        self.limits: dict[str, RateLimit] = {}
        self._seen = ""
        self._state_sent = ""
        self._stderr: deque[str] = deque(maxlen=20)
        self._mask = _secrets.Masker()
        self._warned = False

    # ---- configuration
    def config_dir(self) -> Path | None:
        return account_config_dir(self.home, self.acct)

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = dict(ISOLATION_ENV)
        env.update(NO_KEY_ENV)
        token: dict[str, str] = {}
        if self.acct.secret:
            try:
                token = _secrets.provider_env(self.home, self.acct.secret)
            except FileNotFoundError:
                pass                            # no token file: the config_dir login is used
            except ValueError as e:
                raise ProviderError(f"account {self.account!r}: {e}") from e
        cfg = self.config_dir()
        if cfg is not None:
            cfg.mkdir(parents=True, exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = str(cfg)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = ""  # a token in the parent's environment must not override this login
        elif not token and not self._warned:
            self._warned = True
            msg = (f"account {self.account!r} has no config_dir: using the system default Claude "
                   "login (dev/test only; set config_dir so the login lives inside the tree)")
            self.warnings.append(msg)
            log.warning(msg)
        env.update(token)
        for k, v in token.items():
            self._mask.add(self.acct.secret, k, v)
        return env

    def _prompt_file(self, system: str) -> Path:
        """The cache-stable prefix as a file (a command line cannot carry it on Windows).  Kept
        in the state folder, which the rules lock against agents; named by content hash."""
        text = system.rstrip() + PREFIX_TRAILER + "\n"
        d = self.home.path("state")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"prompt-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}.md"
        if not p.is_file():
            write_atomic(p, text)
        return p

    # ---- tools as MCP
    def _handler(self, name: str):
        async def handle(args: dict[str, Any]) -> dict[str, Any]:
            d = self.dispatcher
            res = await getattr(d, "call_watched", d.call)(name, dict(args or {}))    # type: ignore[union-attr]
            content: list[dict[str, Any]] = [{"type": "text", "text": res.text or "(no output)"}]
            if res.image:       # MCP image content: the model sees the picture, not its bytes as text
                content.append({"type": "image", "data": res.image["data"], "mimeType": res.image["media_type"]})
            note = d.deliver()[0] if hasattr(d, "deliver") else ""   # the CLI owns the loop: a message of the user reaches the model on this tool result
            if note:
                content.append({"type": "text", "text": note})
            return {"content": content, "is_error": not res.ok}
        return handle

    def _sdk_tools(self) -> list[SdkMcpTool]:
        return [SdkMcpTool(name=s["name"], description=s.get("description", ""),
                           input_schema=s["input_schema"], handler=self._handler(s["name"]))
                for s in self.dispatcher.schemas()]          # type: ignore[union-attr]

    def _guard(self, name: str, args: dict[str, Any]) -> str:
        """Second rule check (the dispatcher repeats it): the reason to deny, '' to let the call
        go to the dispatcher.  Only a deny stops here; asks are the dispatcher's job."""
        from .. import rules                    # late: keeps the import graph of INTERFACES.md
        from ..tools import hash_approved
        d = self.dispatcher
        assert d is not None
        declared = {s["name"]: set(s["input_schema"].get("properties", {})) for s in d.schemas()}
        if name not in declared:
            return f"unknown tool {name!r}"
        if name in getattr(d, "local_tools", {}):
            return ""               # a tool of the core that only hands data back (StructuredOutput): no rule applies
        tool = d.ext.get(name)
        args = {k: v for k, v in (args or {}).items() if k in declared[name]}
        checks = rules.checks_for_call(name, args, tool_meta=tool,
                                       hash_approved=hash_approved(self.home, tool) if tool else True)
        dec = rules.decide_all(rules.build_layers(d.home, d.agent, d.mode, d.session, None),
                               checks, d.ctx)
        return (dec.reason or "denied by rule") if dec.effect == "deny" else ""

    async def _pre_tool_use(self, inp: dict[str, Any], tool_use_id: Any, ctx: Any) -> dict[str, Any]:
        name = str(inp.get("tool_name", ""))
        try:
            why = (f"{name} is not a Lightmind tool" if not name.startswith(TOOL_PREFIX)
                   else self._guard(_short(name), inp.get("tool_input") or {}))
        except Exception as e:                  # noqa: BLE001 - the guard fails closed
            why = f"guard error: {type(e).__name__}: {e}"
        self.guard_log.append((_short(name), why or "allow"))
        if not why:
            return {}
        if self.dispatcher is not None:
            self.dispatcher.run.event("guard", {"tool": name, "decision": "deny", "reason": why})
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"denied by Lightmind rule check: {why}"}}

    # ---- request -> SDK
    def _options(self, req: Request, resume: str) -> ClaudeAgentOptions:
        tools = self._sdk_tools()
        kw: dict[str, Any] = dict(
            system_prompt={"type": "file", "path": str(self._prompt_file(req.system))},
            setting_sources=[], tools=[], skills=[],
            mcp_servers={MCP_SERVER: create_sdk_mcp_server(MCP_SERVER, tools=tools)},
            strict_mcp_config=True,             # else the account's claude.ai connectors leak in as tools
            allowed_tools=[TOOL_PREFIX + t.name for t in tools],
            hooks={"PreToolUse": [HookMatcher(hooks=[self._pre_tool_use])]},
            env=self._env(), cwd=str(self.workdir or self.home.root), stderr=self._stderr.append)
        if req.model:
            kw["model"] = req.model + ("[1m]" if req.context > 200_000 and not req.model.endswith("]") else "")   # the 1m window
        if req.effort in EFFORTS:
            kw["effort"] = req.effort
        if req.effort in THINKING:
            kw["thinking"] = THINKING[req.effort]
        if req.max_turns:
            kw["max_turns"] = req.max_turns
        if req.max_usd:
            kw["max_budget_usd"] = req.max_usd
        if resume:
            kw["resume"] = resume
        return ClaudeAgentOptions(**kw)

    def _prompt(self, req: Request, fresh: bool) -> str:
        """Only the new user message goes in; the CLI remembers the rest.  A fresh conversation
        gets the volatile state block and, if there is history, a replay of it."""
        if not req.messages or req.messages[-1].role != "user":
            raise ProviderError("claude_sdk needs the new user message last in Request.messages")
        parts = []
        if req.state and (fresh or req.state != self._state_sent):
            parts.append(req.state)
            self._state_sent = req.state
        if fresh and len(req.messages) > 1:
            parts.append(_replay(req.messages[:-1]))
        parts.append(req.messages[-1].text)
        return "\n\n".join(p for p in parts if p)

    # ---- SDK -> events
    def _rate_events(self, info: Any) -> list[RateLimit]:
        windows = (info.raw or {}).get("unifiedWindows") or {}
        out = [RateLimit(k, float(w["utilization"]), w.get("resetsAt"))
               for k in ("five_hour", "seven_day")
               if isinstance(w := windows.get(k), dict) and w.get("utilization") is not None]
        if not out and (info.utilization is not None or info.status == "rejected"):
            out = [RateLimit(str(info.rate_limit_type or "five_hour"),
                             1.0 if info.utilization is None else float(info.utilization),
                             info.resets_at)]
        for r in out:
            self.limits[r.kind] = r
        if out:
            self._seen = now_iso()
            self._save_limits()
        return out

    def _map(self, msg: Any, st: _Turn) -> Iterator[Event | ToolDone | SubagentStart | SubagentEnd]:
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self.session_id = str(msg.data.get("session_id") or self.session_id)
                self.model_id = str(msg.data.get("model") or self.model_id)
        elif isinstance(msg, AssistantMessage):
            parent = msg.parent_tool_use_id or ""
            who = st.agents.get(parent, "") if parent else ""
            if parent:                          # a sub-agent's step: shown, but its usage is already in the result's total
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        yield Text(b.text, parent, who)
                    elif isinstance(b, ToolUseBlock):
                        st.names[b.id] = _short(b.name)
                        yield ToolCall(b.id, _short(b.name), dict(b.input), parent, who)
                return
            if msg.usage:
                u = msg.usage
                self.context_tokens = (int(u.get("input_tokens") or 0)
                                       + int(u.get("cache_read_input_tokens") or 0)
                                       + int(u.get("cache_creation_input_tokens") or 0))
            if msg.error:
                st.error_kind = _error_kind(None, "", msg.error)
                st.error_text = " ".join(b.text for b in msg.content if isinstance(b, TextBlock))
                if st.error_kind == "auth":
                    raise NotLoggedIn(self.account)
                return                          # the text is the error, not model output
            for b in msg.content:
                if isinstance(b, TextBlock):
                    yield Text(b.text)
                elif isinstance(b, ToolUseBlock):
                    st.names[b.id] = _short(b.name)
                    yield ToolCall(b.id, _short(b.name), dict(b.input))
                    if b.name in SUBAGENT_TOOLS:
                        st.agents[b.id] = _agent_name(dict(b.input))
                        yield SubagentStart(b.id, st.agents[b.id], str(b.input.get("prompt") or ""))
        elif isinstance(msg, UserMessage):
            for b in (msg.content if isinstance(msg.content, list) else []):
                if isinstance(b, ToolResultBlock):
                    parent = msg.parent_tool_use_id or ""
                    text = _flat(b.content)[:2000]
                    yield ToolDone(b.tool_use_id, st.names.get(b.tool_use_id, ""), not b.is_error, text,
                                   parent, st.agents.get(parent, "") if parent else "")
                    if b.tool_use_id in st.agents:
                        yield SubagentEnd(b.tool_use_id, not b.is_error, text)
        elif isinstance(msg, RateLimitEvent):
            yield from self._rate_events(msg.rate_limit_info)
        elif isinstance(msg, ResultMessage):
            st.result_seen = True
            self.session_id = msg.session_id or self.session_id
            self.cost_usd = msg.total_cost_usd
            u = msg.usage or {}
            yield Usage(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                        cache_read=int(u.get("cache_read_input_tokens") or 0),
                        cache_write=int(u.get("cache_creation_input_tokens") or 0))
            if msg.is_error or st.error_kind or msg.subtype in ("error_max_turns", "error_max_budget_usd"):
                text = msg.result or "; ".join(msg.errors or []) or st.error_text or msg.subtype
                kind = st.error_kind or _error_kind(msg.api_error_status, text, None)
                if msg.subtype == "error_max_turns" or msg.terminal_reason == "max_turns":
                    kind = "max_turns"
                if msg.subtype == "error_max_budget_usd":
                    kind = "budget"
                if kind == "auth":
                    raise NotLoggedIn(self.account)
                if st.resumed and RESUME_LOST in text.lower():
                    raise _ResumeLost()
                yield Error(kind, self._mask.mask(text))

    async def turn(self, req: Request) -> AsyncIterator[Event]:
        if self.dispatcher is None:
            raise ProviderError("claude_sdk needs a dispatcher: its tools are exposed to the CLI as an MCP server")
        for attempt in (0, 1):
            resume = self.session_id
            st = _Turn(resumed=bool(resume))
            lost = False                             # the CLI does not know the conversation we asked it to resume
            try:
                prompt = self._prompt(req, fresh=not resume)
                async with aclosing(_query(prompt=prompt, options=self._options(req, resume))) as stream:
                    async for msg in stream:
                        for ev in self._map(msg, st):
                            yield ev                 # type: ignore[misc]
            except _ResumeLost:                      # only possible on the first attempt (resumed)
                lost = True
            except ResultError as e:
                if not st.result_seen:               # a result was already mapped otherwise
                    text = e.result or str(e)
                    kind = _error_kind(e.api_error_status, text, None)
                    if kind == "auth":
                        raise NotLoggedIn(self.account) from e
                    if st.resumed and RESUME_LOST in text.lower():      # the same case as above, reported as an error instead of a result
                        lost = True
                    else:
                        yield Error(kind, self._mask.mask(text))
            except CLINotFoundError as e:
                raise ProviderError(str(e)) from e
            except ProcessError as e:
                tail = self._mask.mask(" | ".join(self._stderr))[-500:]
                if st.resumed and RESUME_LOST in tail.lower():
                    lost = True
                else:
                    yield Error("other", f"claude CLI failed ({e.exit_code}){': ' + tail if tail else ''}")
            if lost:                                 # a conversation of another account or machine: start fresh, replay the history
                self.session_id, self._state_sent = "", ""
                yield Notice("warning", "the CLI does not know this conversation (other config "  # type: ignore[misc]
                             "dir or machine); continuing with a replay of the history")
                continue
            return

    # ---- account usage
    def _limits_file(self) -> Path:
        return self.home.path("state") / f"limits-{self.account}.json"

    def _read_limits(self) -> tuple[str, dict[str, RateLimit]]:
        """(seen, limits) as the state file of this account holds them; empty when there is none."""
        try:
            data = json.loads(self._limits_file().read_text("utf-8"))
            return str(data.get("seen", "")), {k: RateLimit(k, float(v["utilization"]), v.get("resets_at"))
                                               for k, v in data.get("limits", {}).items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return "", {}

    def limits_now(self) -> dict[str, float]:
        """kind -> 0..1 from the state file: what any run of this account saw last (no model call, a small file read)."""
        return {k: r.utilization for k, r in self._read_limits()[1].items()}

    def _save_limits(self) -> None:
        try:
            p = self._limits_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            kept = {k: r for k, r in self._read_limits()[1].items() if k not in self.limits}      # a window this run has not seen stays
            p.write_text(json.dumps({"seen": self._seen, "limits": {
                k: {"utilization": r.utilization, "resets_at": r.resets_at}
                for k, r in {**kept, **self.limits}.items()}}), "utf-8")
        except OSError:
            pass                                 # usage display is best effort

    async def usage(self) -> AccountUsage:
        """5-hour and 7-day utilization as last seen in a RateLimitEvent (of any run of this
        account, kept in the state folder).  Subscriptions have no balance."""
        if not self.limits:
            self._seen, self.limits = self._read_limits()
        five, seven = self.limits.get("five_hour"), self.limits.get("seven_day")
        note = f"last seen {self._seen}" if self._seen else "no run seen yet"
        return AccountUsage(account=self.account, five_hour=five.utilization if five else None,
                            seven_day=seven.utilization if seven else None, est=True, note=note)
