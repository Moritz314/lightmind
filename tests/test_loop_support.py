"""Shared helpers for the provider / loop / session tests (no tests in here).

* `SessionHome`: a Home with the settings API of the new lightmind.md (`settings`,
  `master_prompt()`) and a configurable providers file, independent of how files.py loads them.
* `FakeQuery`: replaces the SDK call with scripted real SDK message objects.
* Message builders for the SDK dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage,
                              SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock,
                              UserMessage)

from lightmind.files import Home
from lightmind.model import Account, ProvidersConfig, Request, Message, RuleContext, Run, SessionState
from lightmind.providers.fake import FakeProvider
from lightmind.runs import RunDir
from lightmind.tools import Dispatcher


def default_settings(**over: Any) -> dict:
    """`tools.as_functions` is on: the session tests are about tool loadouts, which only exist as functions."""
    s = {"defaults": {"server": "laptop", "provider": "abo", "model": "haiku", "effort": "low",
                      "context": "200k", "mode": "auto"},
         "on_start": [], "on_end": [], "context": {"handover_at": 0.7}, "ui": {},
         "tools": {"as_functions": True}}
    s.update(over)
    return s


def default_providers(**dirs: str) -> ProvidersConfig:
    return ProvidersConfig(
        default="abo",
        accounts={"abo": Account("abo", "claude-subscription", config_dir=dirs.get("abo", "")),
                  "abo2": Account("abo2", "claude-subscription", config_dir=dirs.get("abo2", "")),
                  "api": Account("api", "anthropic-api"),
                  "local": Account("local", "openai-compatible", base_url="http://x/v1")},
        aliases={"main": "abo"}, fallback={"abo": ["abo2"], "api": ["main"]},
        tiers={"strong": {"claude": "opus"}, "medium": {"claude": "sonnet"},
               "cheap": {"claude": "haiku", "local": "qwen3-32b"}},
        switch_at=0.9)


class SessionHome(Home):
    def __init__(self, root, settings: dict | None = None, providers: ProvidersConfig | None = None):
        super().__init__(root)
        self.settings = settings if settings is not None else default_settings()
        self._providers = providers if providers is not None else default_providers()

    def load_providers(self) -> ProvidersConfig:
        return self._providers

    def master_prompt(self) -> str:
        return self.entry.body


def make_dispatcher(home: Home, mode: str = "auto", rid: str = "t1", agent=None, asker=None):
    run = RunDir(home, Run(id=rid, account="abo", model="haiku"))
    state = SessionState(mode=mode)
    d = Dispatcher(home, agent=agent, mode=mode, session=state,
                   rule_ctx=RuleContext(home=str(home.root), run_dir=str(run.path)), run=run,
                   asker=asker)
    return d, run


def request(text: str = "hi", *, system: str = "SYSTEM", state: str = "", model: str = "haiku",
            effort: str = "low", history: list[Message] | None = None, **kw) -> Request:
    return Request(system=system, state=state, messages=[*(history or []), Message("user", text)],
                   model=model, effort=effort, **kw)


# ------------------------------------------------------------------ SDK fakes
class FakeQuery:
    """Stands in for claude_sdk._query.  Each call consumes one script: a list of messages (an
    exception instance in the list is raised at that point) or a callable kw -> list."""

    def __init__(self, *scripts: Any) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    def __call__(self, **kw: Any):
        self.calls.append(kw)
        script = self.scripts.pop(0)
        items = script(kw) if callable(script) else script

        async def gen():
            for m in items:
                if isinstance(m, BaseException):
                    raise m
                yield m
        return gen()


def init(sid: str = "sess-1", model: str = "claude-haiku-5-5") -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": sid, "model": model})


def usage_dict(i: int = 2, o: int = 5, cr: int = 0, cw: int = 0) -> dict:
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw}


def assistant(*blocks: Any, usage: dict | None = None, error: str | None = None) -> AssistantMessage:
    blocks = tuple(TextBlock(b) if isinstance(b, str) else b for b in blocks)
    return AssistantMessage(content=list(blocks), model="m", usage=usage, error=error)  # type: ignore[arg-type]


def tool_use(id_: str, name: str, args: dict) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name=name, input=args)


def tool_result(id_: str, text: str, is_error: bool | None = None) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=id_, content=[{"type": "text", "text": text}],
                                                is_error=is_error)])


def result(sid: str = "sess-1", usage: dict | None = None, *, is_error: bool = False,
           text: str | None = "done", subtype: str = "success", status: int | None = None,
           cost: float | None = 0.001, terminal: str | None = "completed") -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id=sid, total_cost_usd=cost,
                         usage=usage if usage is not None else usage_dict(), result=text,
                         api_error_status=status, terminal_reason=terminal)


def ratelimit(five: float = 0.1, seven: float = 0.4, status: str = "allowed") -> RateLimitEvent:
    info = RateLimitInfo(status=status, resets_at=222, rate_limit_type="seven_day", utilization=seven,  # type: ignore[arg-type]
                         raw={"unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": 111},
                                                 "seven_day": {"utilization": seven, "resetsAt": 222}}})
    return RateLimitEvent(rate_limit_info=info, uuid="u1", session_id="sess-1")


async def collect(agen) -> list:
    return [e async for e in agen]


# --------------------------------------------------------------- loop fakes
class SdkFake(FakeProvider):
    """A fake that behaves like claude_sdk: owns the loop, keeps a session id."""
    owns_loop = True

    def __init__(self, script, **kw) -> None:
        super().__init__(script, **kw)
        self.session_id = ""
        self.context_tokens = 0
        self.warnings: list[str] = []
        self.dispatcher = None


class ApiFake(FakeProvider):
    """A fake API provider (the loop executes its tool calls)."""

    def __init__(self, script, **kw) -> None:
        super().__init__(script, **kw)
        self.dispatcher = None


@dataclass
class ToolBox:
    """Minimal stand-in for a Dispatcher inside run_loop unit tests."""
    home: Home
    results: dict[str, Any] = field(default_factory=dict)
    agent: Any = None
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def call(self, name: str, args: dict):
        from lightmind.model import ToolResult
        self.calls.append((name, args))
        return self.results.get(name, ToolResult(True, f"result of {name}"))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path
