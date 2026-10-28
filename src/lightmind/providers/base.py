"""Provider protocol + factory.  May import: model, files, secrets.

A provider turns one `Request` into a stream of `Event`s.  Two shapes exist:

* API providers (phase 2) answer one model call per `turn()`; `loop.py` executes the tool
  calls and calls `turn()` again.
* Providers whose backend owns the tool loop (`claude_sdk`) set `owns_loop = True`; one
  `turn()` is a whole run of model calls and tool calls, the ToolCall events it yields are
  informational and `loop.py` must not execute them again.

Optional attributes, read with getattr by the loop: `owns_loop` (bool, default False) and
`context_tokens` (int, prompt size of the last model call of the last turn).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from ..files import Home
from ..model import AccountUsage, Caps, Event, ProvidersConfig, Request

# type of an account -> (module in this package, class).  Imported lazily, so a missing
# optional dependency only hurts the accounts that need it.
ADAPTERS: dict[str, tuple[str, str]] = {
    "claude-subscription": ("claude_sdk", "ClaudeSdkProvider"),
    "anthropic-api": ("anthropic_api", "AnthropicApiProvider"),
    "openrouter": ("openai_compat", "OpenAICompatProvider"),
    "openai": ("openai_compat", "OpenAICompatProvider"),
    "openai-compatible": ("openai_compat", "OpenAICompatProvider"),
    "openai-compat": ("openai_compat", "OpenAICompatProvider"),
}
# accounts of these types share one row of the `tiers:` table (key `claude`)
FAMILY: dict[str, str] = {"claude-subscription": "claude", "anthropic-api": "claude"}


class ProviderError(RuntimeError):
    """The provider cannot even start a turn (not logged in, CLI missing, bad config).
    `kind` uses the vocabulary of model.Error (auth, other, ...).  Failures *inside* a turn
    (rate limit, billing, overload) are not raised: they arrive as Error events."""

    kind = "other"

    def __init__(self, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        if kind:
            self.kind = kind


@dataclass
class ToolDone:
    """Result of one tool call, for UIs.  The model gets the result through the provider;
    this event only mirrors it (text is truncated)."""
    id: str
    name: str
    ok: bool = True
    text: str = ""
    parent: str = ""               # see model.Text: set for the inner steps of a sub-agent
    agent: str = ""


@dataclass
class SubagentStart:
    """A sub-agent began: `id` is the id of the tool call (Task/Agent) that started it, `task` its prompt.  A sub-agent
    of a workflow (orchestrate.py) names the workflow run in `workflow` and its progress group in `phase`."""
    id: str
    name: str
    task: str = ""
    workflow: str = ""
    phase: str = ""


@dataclass
class SubagentEnd:
    """The sub-agent finished: `summary` is the result its tool call returned to the main chat."""
    id: str
    ok: bool = True
    summary: str = ""


@dataclass
class Notice:
    """Something the core did that the user should see: handover, account switch, limit
    warning, configuration or hook warning, shown hook output.
    kind: handover | switch | limit | warning | hook."""
    kind: str
    text: str = ""


@dataclass
class Interjected:
    """A message of the user (or, with `parent`, of the main agent to a sub-agent) was fed into the running turn:
    `text` is what was written, `id` the number the UI got when it accepted the message."""
    id: int
    text: str
    parent: str = ""               # see model.Text: set when a sub-agent received it
    agent: str = ""


@dataclass
class WorkflowEvent:
    """A workflow run (orchestrate.py) changed.  `kind`: `start` (`name`, `text` = description, `phases` = the titles of
    its meta), `phase` (`text` = the new phase title), `log` (`text` = a line of the script), `end` (`ok`, `text` = a short
    summary).  `id` is the workflow run id; its sub-agents carry it in `SubagentStart.workflow`."""
    id: str
    kind: str
    name: str = ""
    text: str = ""
    phases: list[str] = field(default_factory=list)
    ok: bool = True


SessionEvent = Event | ToolDone | Notice | SubagentStart | SubagentEnd | Interjected | WorkflowEvent


@runtime_checkable
class Provider(Protocol):
    caps: Caps

    def turn(self, req: Request) -> AsyncIterator[Event]:
        """Async generator of events for ONE model turn.  Tool calls are yielded as
        ToolCall; the caller (loop.py, or the SDK itself for claude_sdk) executes them.
        Ends after the turn's Usage."""
        ...

    async def usage(self) -> AccountUsage: ...


# ------------------------------------------------------------------ resolution
def resolve_alias(cfg: ProvidersConfig, name: str) -> str:
    """Follow alias entries until a real account name (cycle safe)."""
    seen: set[str] = set()
    while name in cfg.aliases and name not in seen:
        seen.add(name)
        name = cfg.aliases[name]
    return name


def settings(home: Home) -> dict:
    """The settings block of lightmind.md (`home.settings`); {} when the tree has none."""
    return dict(getattr(home, "settings", None) or {})


def settings_defaults(home: Home) -> dict:
    """`defaults:` of the settings (server, provider, model, effort, context, mode)."""
    return dict(settings(home).get("defaults") or {})


def resolve_account(home: Home, requested: str = "", agent_provider: str = "",
                    project_provider: str = "") -> str:
    """First hit wins: chat switch -> agent provider -> project provider -> providers default.
    Aliases are resolved via providers config.  A name that is set but unknown is an error,
    never silently skipped.  Every source may hold a list of accounts (`a, b`): the first one that is free right now
    wins (`accountpick.first_free`), and the first one when none is."""
    from .. import accountpick
    cfg = home.load_providers()
    for source, value in (("chat switch", requested), ("agent", agent_provider),
                          ("project", project_provider), ("providers default", cfg.default)):
        names = accountpick.names_of(value)
        if not names:
            continue
        found: list[str] = []
        for name in names:
            account = resolve_alias(cfg, name)
            if account not in cfg.accounts:
                raise ValueError(f"unknown account or alias {name!r} ({source}); "
                                 f"accounts: {', '.join(sorted(cfg.accounts)) or '-'}")
            found.append(account)
        return found[0] if len(found) == 1 else accountpick.first_free(home, found)
    raise ValueError("no provider account configured (providers default is empty)")


def resolve_model(home: Home, account: str, requested: str = "") -> str:
    """Agent/chat model (fixed name or tier) -> tier table by provider family -> default_model.
    A name that is not a tier is a fixed model name and passes through unchanged."""
    cfg = home.load_providers()
    acct = cfg.accounts.get(account)
    if acct is None:
        raise ValueError(f"unknown account {account!r}")
    name = requested or str(settings_defaults(home).get("model") or "")
    if not name:
        raise ValueError("no model requested and no default model configured")
    row = cfg.tiers.get(name)
    if row is None:
        return name
    for key in (acct.name, acct.type, FAMILY.get(acct.type, "")):
        if key and key in row:
            return str(row[key])
    raise ValueError(f"tier {name!r} has no model for account {account!r} "
                     f"({acct.type}); tier row: {', '.join(row) or '-'}")


def make_provider(home: Home, account: str, dispatcher=None) -> Provider:
    """Instantiate the adapter for the account's `type` (lazy imports).  `dispatcher` is the
    chat's tool dispatcher; adapters that own the tool loop expose its tools to the backend."""
    acct = home.load_providers().accounts.get(account)
    if acct is None:
        raise ValueError(f"unknown account {account!r}")
    spec = ADAPTERS.get(acct.type)
    if spec is None:
        raise NotImplementedError(f"provider type {acct.type!r} (account {account!r}) "
                                  "comes with phase 2")
    module = importlib.import_module(f".{spec[0]}", __package__)
    return getattr(module, spec[1])(home, account, dispatcher)
