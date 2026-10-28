"""Scripted fake provider for tests: yields pre-defined events per turn."""
from __future__ import annotations

from typing import AsyncIterator

from ..model import AccountUsage, Caps, Event, Request, Text, ToolCall, Usage


class FakeProvider:
    """`script` is a list of turns; each turn is a list of events (or a callable
    Request -> list[Event]).  Records every Request in `requests`.  When the script is
    exhausted it yields an empty Text and a zero Usage (the loop then ends)."""

    def __init__(self, script: list, caps: Caps | None = None, name: str = "fake") -> None:
        self.script = list(script)
        self.caps = caps or Caps()
        self.name = name
        self.requests: list[Request] = []
        self._i = 0

    async def turn(self, req: Request) -> AsyncIterator[Event]:
        self.requests.append(req)
        if self._i < len(self.script):
            item = self.script[self._i]
            self._i += 1
            events = item(req) if callable(item) else item
        else:
            events = [Text(""), Usage()]
        for e in events:
            yield e

    async def usage(self) -> AccountUsage:
        return AccountUsage(account=self.name, est=True)


def say(text: str, input: int = 10, output: int = 5) -> list[Event]:
    """One plain-text turn."""
    return [Text(text), Usage(input=input, output=output)]


def call(name: str, args: dict | None = None, id: str = "c1",
         input: int = 10, output: int = 5) -> list[Event]:
    """One turn that issues a single tool call."""
    return [ToolCall(id=id, name=name, args=args or {}), Usage(input=input, output=output)]
