"""Messages typed while a turn runs wait in a queue and go out as one message when the turn is over."""
import asyncio

import pytest
from lightmind.tui.chat import ChatScreen, Msg
from lightmind.tui.chatinput import ChatInput

from lightmind import model as m
from test_tui_support import FakeProv, idle, install, open_chat, plain, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def gated(gate: asyncio.Event, fail: bool = False):
    """A turn that shows a line, waits for the gate, then ends (or raises)."""
    async def turn(self, req):
        yield m.Text("first answer")
        await gate.wait()
        if fail:
            raise RuntimeError("boom")
        yield m.Text("first done")
        yield m.Usage()
    return turn


def waiting(app) -> list[Msg]:
    return [w for w in app.screen.query(Msg) if w.queued]


def has(app, needle: str) -> bool:
    return any(needle in w.plain for w in app.screen.query(Msg))


async def start_busy(app, pilot, text="go"):
    await open_chat(app, pilot)
    await say(pilot, text)
    await until(pilot, lambda: has(app, "first answer") and app.chat.busy)


async def fill(pilot, n: int = 3):
    """The open interjections of the running turn (at most three); what comes after them is queued."""
    for i in range(n):
        await say(pilot, f"note {i + 1}")


async def test_a_message_typed_during_a_turn_waits_in_the_transcript(home, prov):
    """The first three are interjections (see test_tui_interject.py); the fourth waits in line."""
    gate = asyncio.Event()
    prov.turns = [gated(gate)]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await fill(pilot)
        assert not waiting(app)
        await say(pilot, "and then this")
        assert app.screen.query_one(ChatInput).value == ""
        (w,) = waiting(app)
        assert w.kind == "user" and w.speaker == "You" and w.text == "and then this"
        assert "queued" in w.render_line(0).text
        await say(pilot, "and this too")
        assert len(waiting(app)) == 2 and "· 2 queued" in plain(app, "#status")
        assert len(prov.requests) == 1                               # nothing is sent while the turn runs
        gate.set()
        await idle(pilot, app)


async def test_waiting_messages_go_out_as_one_after_the_turn(home, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate), [m.Text("second answer"), m.Usage()], [m.Text("third answer"), m.Usage()]]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await fill(pilot)
        await say(pilot, "one")
        await say(pilot, "two")
        gate.set()
        await until(pilot, lambda: has(app, "third answer"))
        await idle(pilot, app)
        assert len(prov.requests) == 3 and prov.requests[-1].messages[-1].text == "one\n\ntwo"
        assert not waiting(app) and "queued" not in plain(app, "#status")
        users = [w.plain for w in app.screen.query(Msg) if w.kind == "user"]
        assert users == ["go", "note 1", "note 2", "note 3", "one", "two"]
        assert all("queued" not in w.render_line(0).text for w in app.screen.query(Msg))


async def test_esc_stops_the_turn_and_takes_the_waiting_messages_back(home, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate)]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await fill(pilot)
        await say(pilot, "one")
        await say(pilot, "two")
        await pilot.press("escape")
        await until(pilot, lambda: not app.chat.queue)
        assert app.screen.query_one(ChatInput).value == "note 1\nnote 2\nnote 3\none\ntwo"     # the open interjections come back too
        assert not waiting(app) and not any(w.plain in ("one", "two", "note 1") for w in app.screen.query(Msg))
        await idle(pilot, app)
        assert len(prov.requests) == 1 and has(app, "stopped")


async def test_after_an_error_the_messages_keep_waiting_until_enter_or_esc(home, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate, fail=True), [m.Text("recovered"), m.Usage()], gated(gate), ]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await say(pilot, "one")
        gate.set()
        await idle(pilot, app)
        await until(pilot, lambda: has(app, "queued messages wait: Enter sends them, Esc takes them back"))
        assert len(waiting(app)) == 1 and len(prov.requests) == 1 and "· 1 queued" in plain(app, "#status")
        await say(pilot, "")                                         # Enter in the empty line sends them
        await until(pilot, lambda: has(app, "recovered"))
        await idle(pilot, app)
        assert prov.requests[-1].messages[-1].text == "one" and not waiting(app)


async def test_esc_after_an_error_takes_the_messages_back_without_leaving(home, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate, fail=True)]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await say(pilot, "one")
        gate.set()
        await idle(pilot, app)
        await until(pilot, lambda: has(app, "queued messages wait"))
        await pilot.press("escape")
        await until(pilot, lambda: not app.chat.queue)
        assert app.screen.query_one(ChatInput).value == "one" and isinstance(app.screen, ChatScreen)


async def test_a_command_in_a_waiting_message_runs_only_after_the_turn(home, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate), [m.Text("second answer"), m.Usage()]]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        before = app.chat.s.info.effort
        await say(pilot, "/effort low after that please")
        (w,) = waiting(app)
        assert w.text.startswith("/effort low") and app.chat.s.info.effort == before
        gate.set()
        await until(pilot, lambda: has(app, "second answer"))
        await idle(pilot, app)
        assert app.chat.s.info.effort == "low"
        assert prov.requests[-1].messages[-1].text == "after that please"


async def test_a_failing_command_sends_nothing_and_returns_the_text(home, prov, monkeypatch):
    gate = asyncio.Event()
    prov.turns = [gated(gate)]

    async def broken(self, arg):
        self.bad = True
    monkeypatch.setattr(ChatScreen, "cmd_effort", broken)
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await say(pilot, "/effort low hello")
        await say(pilot, "second")
        gate.set()
        await idle(pilot, app)
        await until(pilot, lambda: not app.chat.queue)
        assert app.screen.query_one(ChatInput).value == "/effort low hello\nsecond"
        assert len(prov.requests) == 1 and not waiting(app)


async def test_a_command_that_leaves_the_chat_is_not_queued(home, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate)]
    async with running(home) as (app, pilot):
        await start_busy(app, pilot)
        await say(pilot, "bye /clear")                               # `/clear` leaves the chat and stands last
        assert not app.chat.queue and has(app, "still on the last message: Esc stops it")
        assert app.screen.query_one(ChatInput).value == "bye /clear"
        gate.set()
        await idle(pilot, app)


async def test_a_read_only_chat_queues_nothing(home, prov, monkeypatch):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        monkeypatch.setattr(type(app.chat.s), "readonly", property(lambda self: True), raising=False)
        monkeypatch.setattr(type(app.chat.s), "readonly_by", property(lambda self: 4242), raising=False)
        await say(pilot, "hello")
        assert not app.chat.queue and has(app, "read only")
