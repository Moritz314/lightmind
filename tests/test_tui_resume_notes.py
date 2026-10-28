"""Resume menu: the notes of a chat (name and description on top), ending a chat; the `todo` tool's rename / describe.
Textual Pilot."""
import asyncio
import json
import os

import pytest

from lightmind import loop as L
from lightmind import runs
from lightmind.tui.notes import DESC, NAME, NotesPane, TodoView
from lightmind.tui.screens import NotesScreen, ResumeScreen

from test_session_lock import CHILD, other_process
from test_tui_support import FakeProv, earlier_chat, install, quiet, running, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def meta(home, sid):
    return json.loads((home.path("runs") / sid / "meta.json").read_text("utf-8"))


async def to_resume(app, pilot):
    await pilot.press("r")
    await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)


async def type_in(app, pilot, text):
    inp = app.screen.query_one("#np-in")
    await until(pilot, lambda: inp.display)
    inp.value = text
    await pilot.press("enter")
    await until(pilot, lambda: not inp.display)


async def test_notes_from_the_resume_menu_name_and_description_on_top(home, prov):
    sid = await earlier_chat(home, prov)
    (home.path("runs") / sid / "todos.json").write_text(json.dumps({"items": [{"kind": "task", "id": "t1", "title": "buy milk", "state": "open"}]}), "utf-8")
    async with running(home) as (app, pilot):
        await to_resume(app, pilot)
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, NotesScreen))
        view = app.screen.query_one(TodoView)
        await until(pilot, lambda: view.rows)
        assert [r[1] for r in view.rows[:2]] == [NAME, DESC]                         # the very top
        assert view.cur == NAME
        await pilot.press("enter")                                                   # edit the name
        await type_in(app, pilot, "Milk run")
        await pilot.press("down", "enter")                                           # the description
        await type_in(app, pilot, "all about milk")
        d = meta(home, sid)
        assert (d["title"], d["description"]) == ("Milk run", "all about milk")
        assert d["title_manual"] and d["description_manual"] and d["names_rev"] == 2
        assert runs.read_names(home, sid) == ("Milk run", "all about milk")
        assert "Milk run" in "".join(r[2].plain for r in view.rows)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen))


async def test_a_chat_held_elsewhere_shows_its_notes_read_only_but_takes_a_name(home, prov):
    sid = await earlier_chat(home, prov)
    with other_process(CHILD, str(home.root), sid):
        async with running(home) as (app, pilot):
            await to_resume(app, pilot)
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NotesScreen))
            assert app.screen.query_one(NotesPane).readonly
            await pilot.press("enter")
            await type_in(app, pilot, "from outside")
            assert runs.read_names(home, sid)[0] == "from outside"


async def test_end_asks_first_and_closes_a_chat_nobody_holds(home, prov):
    sid = await earlier_chat(home, prov)
    d = meta(home, sid)
    d.update(status="running")
    (home.path("runs") / sid / "meta.json").write_text(json.dumps(d), "utf-8")
    async with running(home) as (app, pilot):
        await to_resume(app, pilot)
        await pilot.press("e")
        assert "end '" in app.screen.note and meta(home, sid)["status"] == "running"
        await pilot.press("x")                                                       # any other key: no
        assert meta(home, sid)["status"] == "running"
        await pilot.press("e", "y")
        await until(pilot, lambda: app.screen.note == "ended")
        assert meta(home, sid)["status"] == "done"


def test_request_stop_asks_the_holder_and_leaves_a_fresh_file(home, prov):
    sid = asyncio.run(earlier_chat(home, prov))
    with other_process(CHILD, str(home.root), sid) as (o, _):
        line = runs.request_stop(home, sid, wait=0.3)
        f = home.path("runs") / sid / runs.STOP_FILE
        assert f.exists() and "no answer yet" in line
        assert runs.take_stop(home.path("runs") / sid) and not f.exists()
        assert not runs.take_stop(home.path("runs") / sid)


def test_the_todo_tool_renames_and_describes_the_chat(home, prov):
    s = L.open_session(home)
    d = s.dispatcher if hasattr(s, "dispatcher") else s._new_dispatcher()
    got = []
    s.on_title.append(lambda t, ds: got.append((t, ds)))
    r = asyncio.run(d._b_todo("rename", title="Bake bread"))
    assert r.ok and "Bake bread" in r.text
    r = asyncio.run(d._b_todo("describe", text="flour, water"))
    m = meta(home, s.id)
    assert (m["title"], m["description"], m["title_manual"], m["names_rev"]) == ("Bake bread", "flour, water", True, 2)
    assert got[-1] == ("Bake bread", "flour, water")
    s.close()
