"""Helpers of the Orbit tests (no tests in here): a scripted provider that acts like claude_sdk's (it owns the
loop), a tree with quiet hooks, and small Pilot helpers.  No network, no CLI, no model."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from textual.widgets import Static

from lightmind import loop as L
from lightmind import model as m
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen, Msg
from lightmind.tui.chatinput import ChatInput
from lightmind.tui.screens import SetupScreen

QUIET_HOOK = """on_start:
  - run: uv run {tools}/vault/status.py "{vault}"
    show: true
    context: true
    timeout: 30
"""


class FakeProv:
    """`turns`: one item per turn, a list of events or an async generator function (self, req)."""
    owns_loop = True
    caps = m.Caps()

    def __init__(self, turns=()):
        self.turns, self.requests, self.dispatcher = list(turns), [], None
        self.context_tokens, self.session_id, self.warnings, self.closed = 0, "", [], 0

    async def turn(self, req):
        self.requests.append(req)
        item = self.turns.pop(0) if self.turns else [m.Text("ok"), m.Usage()]
        try:
            if callable(item):
                async for ev in item(self, req):
                    yield ev
            else:
                for ev in item:
                    yield ev
        finally:
            self.closed += 1

    async def usage(self):
        return m.AccountUsage(account="fake")


def install(monkeypatch, prov: FakeProv) -> FakeProv:
    """Every account of the tree is served by `prov` (what make_provider would build)."""
    def factory(home, account, dispatcher=None):
        prov.dispatcher = dispatcher
        return prov
    monkeypatch.setattr(L, "make_provider", factory)
    return prov


def edit(home, old: str, new: str) -> None:
    """Replace text in lightmind.md (the way the user edits it by hand)."""
    p = home.root / "lightmind.md"
    s = p.read_text("utf-8")
    assert old in s, old
    p.write_text(s.replace(old, new, 1), "utf-8")


def quiet(home, on_start: str = "on_start: []\n") -> None:
    """The shipped on_start hook runs uv and git: tests use none, or a plain `echo`."""
    edit(home, QUIET_HOOK, on_start)


@asynccontextmanager
async def running(home, anim: bool = False, size=(130, 42)):
    app = Orbit(home, anim=anim)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        yield app, pilot


async def until(pilot, cond, timeout: float = 8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting for the UI")


def plain(app, sel: str) -> str:
    return app.screen.query_one(sel, Static).content.plain


def texts(app) -> list[str]:
    return [w.plain for w in app.screen.query(Msg)]


async def open_chat(app, pilot, keys=("n", "s")):
    """Landing -> New chat -> start now."""
    await pilot.press(keys[0])
    await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
    await pilot.press(keys[1])
    await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
    await pilot.pause(0.1)


async def say(pilot, text: str):
    pilot.app.screen.query_one(ChatInput).value = text
    await pilot.press("enter")


async def idle(pilot, app):
    await until(pilot, lambda: app.chat is not None and not app.chat.busy)


async def earlier_chat(home, prov: FakeProv, text="remember the milk", answer="noted, milk it is") -> str:
    """A chat that was closed before the UI started, made through the Session API like `lm chat` does."""
    prov.turns.append([m.Text(answer), m.Usage(10, 5)])
    s = L.open_session(home)
    async for _ in s.send(text):
        pass
    s.close()
    return s.id


async def pause_forever(self, req):
    """A turn that shows something and then never ends (until it is stopped); a slow close like the real CLI."""
    yield m.Text("thinking about it")
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.5)
