"""A message typed while a turn runs is read by the agent at its next step (an interjection) and shown as such."""
import asyncio

import pytest
from lightmind.tui.chat import Msg

from lightmind import model as m
from test_tui_support import FakeProv, idle, install, open_chat, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def tags(app) -> dict[str, str]:
    return {w.text: w.tag for w in app.screen.query(Msg) if w.kind == "user"}


def has(app, needle: str) -> bool:
    return any(needle in w.plain for w in app.screen.query(Msg))


def stepwise(gate: asyncio.Event):
    """A turn that calls a tool, waits for the gate, then ends: the tool result is the transition."""
    async def turn(self, req):
        yield m.Text("first answer")
        await gate.wait()
        yield m.Usage()
    return turn


async def test_an_interjection_is_marked_as_waiting_then_read_and_the_agent_answers(home, prov):
    gate = asyncio.Event()
    prov.turns = [stepwise(gate), [m.Text("got it, adapting"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "build the thing")
        await until(pilot, lambda: has(app, "first answer") and app.chat.busy)
        await say(pilot, "look at the desktop")
        assert tags(app)["look at the desktop"] == "interjection · read at the next step"
        assert "· 1 interjected" in app.screen.query_one("#status").content.plain
        assert not app.chat.queue
        gate.set()                                                  # the turn would end: the open message keeps it going
        await until(pilot, lambda: has(app, "got it, adapting"))
        await idle(pilot, app)
        assert tags(app)["look at the desktop"] == "interjection · read by the agent"
        assert len(prov.requests) == 2
        last = prov.requests[-1].messages[-1]
        assert last.origin == "interject" and "look at the desktop" in last.text and "[Interjection]" in last.text
        assert "interjected" not in app.screen.query_one("#status").content.plain


async def test_stopping_the_turn_gives_unread_interjections_back_as_queued(home, prov):
    gate = asyncio.Event()
    prov.turns = [stepwise(gate)]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await until(pilot, lambda: has(app, "first answer") and app.chat.busy)
        await say(pilot, "wait, not that")
        await pilot.press("escape")                                 # Esc takes everything back into the input
        await until(pilot, lambda: not app.chat.open_ij and not app.chat.queue)
        from lightmind.tui.chatinput import ChatInput
        assert app.screen.query_one(ChatInput).value == "wait, not that"
        await idle(pilot, app)
        assert len(prov.requests) == 1
