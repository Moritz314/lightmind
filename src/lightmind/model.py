"""Shared contract: every dataclass that crosses a module boundary lives here.

No logic beyond (de)serialisation helpers. Modules import types from here and
never from each other's internals.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from typing import Any, Literal

Effect = Literal["allow", "ask", "deny"]
ToolKind = Literal["skill", "tool", "connector"]
RunStatus = Literal["running", "done", "failed", "handover", "paused"]


# --------------------------------------------------------------- documents
@dataclass
class Doc:
    """A parsed Markdown file: YAML frontmatter + body."""
    path: str                      # absolute path ("" for in-memory)
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


# ------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Rule:
    """`tool` or `tool(pattern)` with an effect.  `origin` = "file:line"."""
    tool: str
    pattern: str | None = None
    effect: Effect = "allow"
    locked: bool = False
    until: date | None = None
    origin: str = ""
    exclude: tuple["Rule", ...] = ()   # checks these match are not this rule's (a right inside a wider one, modes.py)

    def text(self) -> str:
        return self.tool if self.pattern is None else f"{self.tool}({self.pattern})"


@dataclass
class Layer:
    """One permission layer (1 defaults, 2 mode preset, 3 agent, 4 session)."""
    name: str                      # "defaults" | "mode:auto" | "agent:webdev" | "session"
    rules: list[Rule] = field(default_factory=list)   # may include locked rules
    fallback: Effect | None = None
    no_ask: bool = False           # a mode that never asks (bypass): rules.decide turns its asks into allow


@dataclass(frozen=True)
class Check:
    """One atomic check produced by the dispatcher: e.g. Check("read", "secrets/a/b.env")."""
    tool: str
    arg: str | None = None


@dataclass(frozen=True)
class RuleContext:
    """Values for pattern variables and relative-path resolution."""
    home: str                      # absolute tree root
    run_dir: str = ""              # $RUN
    project_dir: str = ""          # $PROJECT
    now: date | None = None        # for `until`; None = today


@dataclass
class Decision:
    effect: Effect
    rule: Rule | None = None       # deciding rule (None = fallback)
    layer: str = ""
    reason: str = ""


# ----------------------------------------------------------- agent & tools
@dataclass
class ToolRef:
    kind: ToolKind
    name: str                      # connector: "<secret>/<account>" or tool name


@dataclass
class Agent:
    name: str
    description: str = ""
    provider: str = ""             # alias or account; "" = default
    model: str = ""                # "" | tier | fixed name
    effort: str = "medium"
    context_limit: int = 200_000
    budget_tokens: int | None = None
    budget_usd: float | None = None
    lifetime: Literal["task", "resident"] = "task"
    mode: str = ""
    server: str = ""
    may_spawn: list[str] | None = None    # None = not set: only the plain helper (spawn without agent); [x, y] = those; ["*"] = all; [] = no spawn
    tools: list[ToolRef] = field(default_factory=list)     # skills + tools of tools.md
    data: list[str] = field(default_factory=list)          # paths relative to home
    permissions: dict[str, Any] = field(default_factory=dict)   # layer 3 block; the secrets / servers of
                                                           # tools.md are merged in (listing = granting)
    body: str = ""
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)    # unknown frontmatter keys
    secrets: list[str] = field(default_factory=list)       # `# Secrets` of tools.md ("name/account")
    servers: list[str] = field(default_factory=list)       # `# Servers` of tools.md
    project: str = ""              # default project (`project:`): loaded like `-p` when none is chosen; grants nothing


@dataclass
class Skill:
    name: str
    description: str = ""
    preload_when: str = ""
    requires: list[str] = field(default_factory=list)
    secret: str = ""
    body: str = ""
    path: str = ""


@dataclass
class Tool:
    """External tool from tools/<name>/tool.md (built-ins have no folder)."""
    name: str
    description: str = ""
    run: str = ""                  # "python run.py" | "builtin:bash"
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    category: str = "none"         # none | send | money | delete | deploy | account
    match: str = ""
    secrets: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    timeout: int = 60
    memory_mb: int | None = None   # own memory limit in MB (never above 80 % of the RAM); None: tools.memory_mb
    output: str = "auto"
    server: str = "any"
    max_usd_per_call: float = 0.0
    body: str = ""
    path: str = ""                 # tool folder ("" for built-ins)


@dataclass
class ToolResult:
    ok: bool
    text: str = ""                 # already masked, possibly truncated + path
    file: str = ""                 # tool-out file when output was long
    denied_by: Decision | None = None
    image: dict[str, str] | None = None   # {media_type, data (base64)}: a picture for the model; text says "image WxH"


# --------------------------------------------------------------- providers
@dataclass
class Account:
    """One account = one file `providers/<name>.md` (frontmatter below, body = notes)."""
    name: str
    type: str                      # claude-subscription | anthropic-api | openrouter | openai | openai-compatible
    secret: str = ""               # secret folder with the API key / token (read by the core only)
    admin_secret: str = ""
    use: str = ""
    base_url: str = ""
    server: str = ""
    config_dir: str = ""           # claude-subscription: CLAUDE_CONFIG_DIR (the portable login), relative to the tree
    default_model: str = ""
    models: dict[str, dict[str, Any]] = field(default_factory=dict)   # model -> {input, output, cache_write, cache_read}, USD per MTok
    tiers: dict[str, str] = field(default_factory=dict)               # tier (strong | medium | cheap) -> model of this account
    fallback: list[str] = field(default_factory=list)                 # accounts to continue on at the limit
    aliases: list[str] = field(default_factory=list)                  # extra names for this account
    switch_at: float | None = None                                    # utilization (0..1) at which fallback starts
    path: str = ""
    body: str = ""


@dataclass
class ProvidersConfig:
    """Aggregate of all provider files; `default` = lightmind.md defaults.provider."""
    default: str
    accounts: dict[str, Account] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)             # alias -> account
    fallback: dict[str, list[str]] = field(default_factory=dict)      # account -> accounts
    tiers: dict[str, dict[str, str]] = field(default_factory=dict)    # tier -> {account: model}
    switch_at: float = 0.9


# ------------------------------------------------- tree entities
@dataclass
class Server:
    name: str                      # servers/<name>/info.md
    host: str = ""                 # "local" = this machine, no SSH
    user: str = ""
    port: int = 22
    key: str = ""                  # secret folder with the SSH key
    role: list[str] = field(default_factory=list)
    lm_home: str = ""              # tree of Lightmind on that server (default ~/lightmind)
    ssh: str | list[str] = ""      # alias in ~/.ssh/config, or a list of aliases tried in order (remote.py); empty = user@host
    local: bool = False            # this machine: `machine:` equals the name of this instance (else the old `host: local`)
    machine: str = ""              # instance the card describes (see sync.py); empty = unset
    legacy_local: bool = False     # local only through `host: local` while `machine:` is unset (transition, `lm check` warns)
    body: str = ""
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trigger:
    name: str                      # triggers/<name>.md
    when: str = ""                 # cron <expr> | file <glob> | webhook <path> | telegram | mail <account> | quota <account> >= 0.9
    task: str = ""
    agent: str = ""
    workflow: str = ""
    tools: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    server: str = ""
    machine: str = ""              # instance the trigger is installed on; empty = only the always_on instance
    session: str = ""              # name of a fixed session: the run goes on in it (`lm run --continue <name> --create`); with `when: every|in` an existing session to wake
    until: str = ""                # `when: every|in`: a time or `children-done`, after which the trigger ends
    owner: str = ""                # the session (run id) that made the trigger
    enabled: bool = True
    body: str = ""
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    name: str                      # projects/<name>/
    provider: str = ""
    model: str = ""
    repo: str = ""
    workdir: str = ""              # frontmatter `path:` (where the code lives)
    home_server: str = ""
    tools: list[ToolRef] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    permissions: dict[str, Any] = field(default_factory=dict)   # only the grants of tools.md (secrets, servers)
    memory: str = ""               # body of projectMemory.md
    assets: list[list[str]] = field(default_factory=list)       # rows of projectAssets.md: [path or URL, note]
    path: str = ""                 # the project folder
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Caps:
    tools: bool = True
    caching: bool = False
    images: bool = False
    max_context: int = 200_000


@dataclass
class AccountUsage:
    account: str
    five_hour: float | None = None       # utilization 0..1
    seven_day: float | None = None
    balance_usd: float | None = None
    est: bool = True
    note: str = ""


# ------------------------------------------------- provider request/events
@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list["ToolCall"] = field(default_factory=list)   # assistant
    tool_call_id: str = ""                                       # role == "tool"
    origin: str = ""               # "subagent": a user-role text that is data from a sub-agent, not words of the user
    image: dict[str, str] | None = None   # role == "tool": {media_type, data (base64)} the tool result carries


@dataclass
class Request:
    """One model turn.  `system` is the cache-stable prefix; `state` is volatile."""
    system: str
    state: str = ""
    messages: list[Message] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)    # JSON-schema tool defs
    model: str = ""
    effort: str = "medium"
    max_turns: int | None = None
    max_usd: float | None = None   # money limit for a backend that owns the loop (claude_sdk: max_budget_usd)
    context: int = 0           # the chat's context limit; above 200_000 the provider asks for a 1m window where it can


@dataclass
class Text:
    text: str
    parent: str = ""               # id of the tool call that started the sub-agent this comes from ("" = the chat itself)
    agent: str = ""                # that sub-agent's name


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    parent: str = ""
    agent: str = ""


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class RateLimit:
    kind: str                      # "five_hour" | "seven_day" | ...
    utilization: float
    resets_at: float | None = None


@dataclass
class Error:
    kind: str                      # rate_limit | billing_error | auth | overloaded | other
    message: str = ""


Event = Text | ToolCall | Usage | RateLimit | Error


# -------------------------------------------------------------------- runs
@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, u: Usage) -> None:
        self.input += u.input
        self.output += u.output
        self.cache_read += u.cache_read
        self.cache_write += u.cache_write


@dataclass
class Task:
    goal: str
    limits: dict[str, Any] = field(default_factory=dict)   # tokens, usd, time
    sources: list[str] = field(default_factory=list)
    check: str = ""
    return_: str = ""
    parent: str = ""


@dataclass
class Run:
    id: str
    agent: str = ""
    project: str = ""
    server: str = ""
    mode: str = ""
    provider: str = ""
    account: str = ""
    model: str = ""
    start: str = ""                # ISO-8601
    end: str = ""
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: float = 0.0
    cost_est: bool = False
    status: RunStatus = "running"
    exit_reason: str = ""
    parent: str = ""               # run id of the run that started this one (LM_PARENT)
    depth: int = 0                 # how many runs deep (LM_DEPTH): 0 = started by a person or a trigger
    pid: int = 0                   # the process that works the run, 0 = not started yet
    origin: str = ""               # the surface that started the chat: "vscode" for the VS Code extension, "" otherwise
    title: str = ""                # chat title: set by hand (/rename) or by titles.py from the newest work; "" = none yet
    description: str = ""          # the chat's topics, newest first, joined with " · " (/describe or titles.py)
    title_manual: bool = False     # True: set by hand, the automatic naming leaves it alone
    description_manual: bool = False
    results_dir: str = ""          # result folder relative to the tree root (runs/ergebnisse/<date> <title>); "" = none yet, or an older run with a real `out`
    names_rev: int = 0             # +1 at every change of title/description; a process holding the chat adopts a newer one from disk (runs.RunDir)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Run":
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        if isinstance(d.get("tokens"), dict):
            d["tokens"] = Tokens(**d["tokens"])
        return cls(**d)


@dataclass
class SessionState:
    """Layer 4 + live chat settings; persisted as <run>/session.md."""
    mode: str = ""
    account: str = ""
    model: str = ""
    tools_added: list[ToolRef] = field(default_factory=list)
    tools_removed: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)        # /allow /deny /always-this-session
    data_added: list[str] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
