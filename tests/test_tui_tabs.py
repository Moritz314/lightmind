"""Orbit and `sessions.open`: a chat made in the menu goes to a new tab (`lm open <id>` resumes it there), a chat that
another process has open is only shown, and a tab that `lm open` started has no menu below it.  No real terminal is
started (conftest refuses `launch.spawn`; these tests record it), the other process is a real Python process."""
import json
import os

import pytest
from lightmind.tui.chatinput import ChatInput

from lightmind import launch
from lightmind import loop as L
from lightmind import model as m
from lightmind import runs
from lightmind import settings as S
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import MapScreen, ResumeScreen, SetupScreen

from test_session_lock import CHILD, locks, other_process
from test_tui_support import FakeProv, earlier_chat, idle, install, open_chat, plain, quiet, running, say, texts, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


@pytest.fixture
def spawned(monkeypatch):
    """A terminal that can open tabs (tmux), and a `launch.spawn` that only records; `.result` is what it answers."""
    calls = []

    def fake(plan, tree, *a, **k):
        calls.append((plan, tree))
        return fake.result
    fake.result, fake.calls = (True, ""), calls
    monkeypatch.setattr(launch, "spawn", fake)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.delenv("WT_SESSION", raising=False)
    return fake


def wish(home, value):
    cfg = home.root / "config.md"
    cfg.write_bytes(S.edit_text(cfg.read_bytes().decode("utf-8"), {("sessions", "open"): value}).encode("utf-8"))


def notes(app):
    return [n.message for n in app._notifications]


def hint(app) -> str:
    c = app.screen.query_one("#hint").content            # a Text once the chat has drawn it, an empty str before
    return str(getattr(c, "plain", c))


def meta(home, rid):
    return json.loads((home.root / "runs" / rid / "meta.json").read_text())


# ------------------------------------------------------------------ the menu hands a chat to a new tab
async def test_a_new_chat_goes_to_a_new_tab_with_the_choices_of_the_setup_and_the_menu_stays(home, prov, spawned):
    wish(home, "tab")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        choice = dict(app.screen.S)
        await pilot.press("s")
        await until(pilot, lambda: spawned.calls)
        await until(pilot, lambda: isinstance(app.screen, MapScreen))            # the start menu is still there
        assert app.chat is None and any("new tmux window" in n for n in notes(app))
    (plan, tree), = spawned.calls
    rid = plan.argv[-1]
    assert plan.how == "tmux" and plan.argv[:2] == ("tmux", "new-window") and plan.argv[-2] == "open" and tree == home.root
    assert (meta(home, rid)["status"], meta(home, rid)["end"]) == ("paused", "") and locks(home) == []   # open for the next process
    r = L.resume_session(home, rid)                                              # what `lm open <id>` does in the new tab
    assert not r.readonly and r.resumed
    assert (r.info.account, r.info.model, r.info.effort, r.info.mode) == (choice["account"], choice["model"], choice["effort"], choice["mode"])
    r.close()


async def test_resuming_from_the_menu_opens_a_tab_too_and_a_window_when_that_is_the_wish(home, prov, spawned):
    sid = await earlier_chat(home, prov)
    for value, word in (("tab", "tab"), ("window", "window")):
        spawned.calls.clear()
        wish(home, value)
        async with running(home) as (app, pilot):
            await pilot.press("r")
            await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
            await pilot.press("enter")
            await until(pilot, lambda: spawned.calls)
            await until(pilot, lambda: isinstance(app.screen, MapScreen))
            assert app.chat is None
        (plan, _), = spawned.calls
        assert plan.argv[-2:] == ("open", sid) and plan.how == "tmux"
        assert locks(home) == []                                                 # handed over, not held by the menu
        L.resume_session(home, sid).close()                                      # (and the next process can take it)


async def test_replace_keeps_the_chat_in_this_window_as_before(home, prov, spawned):
    wish(home, "replace")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        assert app.chat is not None and not app.chat.s.readonly
        assert spawned.calls == [] and runs.lock_holder(home, app.chat.s.id) == os.getpid()
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))            # the menu is below, as before
    assert locks(home) == []


async def test_a_failed_spawn_brings_the_chat_back_into_this_window_and_says_why(home, prov, spawned):
    wish(home, "tab")
    spawned.result = (False, "tmux failed (exit 1)")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        s = app.chat.s
        assert len(spawned.calls) == 1 and not s.readonly and s.resumed
        assert runs.lock_holder(home, s.id) == os.getpid()                       # taken back
        assert any("could not open a new tmux window (tmux failed (exit 1))" in n for n in notes(app))
        prov.turns.append([m.Text("works"), m.Usage()])
        await say(pilot, "hello")
        await until(pilot, lambda: "works" in " ".join(texts(app)))


async def test_no_terminal_that_can_do_it_keeps_the_chat_here_with_a_note(home, prov, spawned, monkeypatch):
    monkeypatch.delenv("TMUX")
    monkeypatch.setattr("sys.platform", "linux")
    wish(home, "tab")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        assert spawned.calls == [] and not any("no tab or window" in n for n in notes(app))   # a tab staying here is no news


# ------------------------------------------------------------------ a chat that is open elsewhere
async def test_a_chat_that_another_process_has_open_is_shown_read_only_even_in_tab_mode(home, prov, spawned):
    sid = await earlier_chat(home, prov)
    wish(home, "tab")
    with other_process(CHILD, str(home.root), sid) as (child, first):
        assert first == "0"
        async with running(home) as (app, pilot):
            await pilot.press("r")
            await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
            assert spawned.calls == []                                           # no tab for a chat that cannot be written
            s = app.chat.s
            assert s.readonly and s.readonly_by == child.pid
            assert any("open in another window" in t and str(child.pid) in t for t in texts(app))
            await until(pilot, lambda: "open in another window" in hint(app))
            assert "read only" in app.chat.query_one(ChatInput).placeholder
            before = {p: p.read_bytes() for p in (home.root / "runs" / sid).rglob("*") if p.is_file()}
            await say(pilot, "a second writer")                                  # refused with the hint, nothing sent
            await pilot.pause(0.2)
            assert prov.requests == [] or all("a second writer" not in x.text for r in prov.requests for x in r.messages)
            await say(pilot, "/mode plan")
            await pilot.pause(0.2)
            assert s.mode == "auto" and sum("read only" in t for t in texts(app)) >= 3
            await pilot.press("escape")                                          # back to the menu, which closes the read-only view
            await until(pilot, lambda: isinstance(app.screen, MapScreen))
            assert {p: p.read_bytes() for p in (home.root / "runs" / sid).rglob("*") if p.is_file()} == before
        assert runs.lock_holder(home, sid) == child.pid                          # the other process keeps its chat


# ------------------------------------------------------------------ a tab that `lm open` started
async def test_a_direct_tab_shows_the_chat_without_a_menu_and_escape_ends_the_app(home, prov):
    sid = await earlier_chat(home, prov)
    app = Orbit(home, anim=False, direct={"id": sid})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.id == sid and not app.chat.s.readonly and len(app.screen_stack) == 2
        assert not any(isinstance(s, (MapScreen, SetupScreen)) for s in app.screen_stack)       # no menu, no checklist
        assert "remember the milk" in " ".join(texts(app)) and runs.lock_holder(home, sid) == os.getpid()
        assert app.return_code is None
        await pilot.press("escape")
        await until(pilot, lambda: app.return_code is not None)
    assert app.return_code == 0 and locks(home) == []
    assert meta(home, sid)["status"] == "done"                                   # the chat ended properly: hooks, end marker


async def test_a_direct_new_chat_takes_the_agent_and_the_project(home, prov):
    app = Orbit(home, anim=False, direct={"new": {"agent": "", "project": ""}})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert not app.chat.s.resumed and len(app.screen_stack) == 2
        prov.turns.append([m.Text("hello back"), m.Usage()])
        await say(pilot, "hi")
        await until(pilot, lambda: "hello back" in " ".join(texts(app)))
        await idle(pilot, app)
        sid = app.chat.s.id
        await pilot.press("escape")
        await until(pilot, lambda: app.return_code is not None)
    assert locks(home) == [] and meta(home, sid)["status"] == "done"


async def test_a_direct_tab_that_cannot_open_the_chat_exits_with_the_reason(home, prov):
    app = Orbit(home, anim=False, direct={"id": "20200101-000000-chat-0000"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: app.return_code is not None)
    assert app.return_code == 1 and any(l.startswith("lm open:") and "matches" in l for l in app.exit_lines)


async def test_a_direct_tab_shows_a_chat_open_elsewhere_read_only_and_leaves_it_alone(home, prov):
    sid = await earlier_chat(home, prov)
    with other_process(CHILD, str(home.root), sid) as (child, first):
        app = Orbit(home, anim=False, direct={"id": sid})
        async with app.run_test(size=(130, 42)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
            assert app.chat.s.readonly and app.chat.s.readonly_by == child.pid
            await pilot.press("escape")
            await until(pilot, lambda: app.return_code is not None)
        assert runs.lock_holder(home, sid) == child.pid and meta(home, sid)["status"] == "done"


async def test_escape_in_the_setup_card_of_a_direct_tab_ends_the_app(home, prov):
    sid = await earlier_chat(home, prov)
    app = Orbit(home, anim=False, direct={"id": sid})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        await say(pilot, "/clear")                                               # a new chat: no menu below, the card is the only screen
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("escape")
        await until(pilot, lambda: app.return_code is not None)
    assert locks(home) == []
