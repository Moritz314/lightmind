"""Ctrl+Q in Orbit: in a chat it archives the chat and ends the app; in the start menu, in a chat that is only read, and with
/quit or Esc it archives nothing.  The marker is `runs/<id>/archived.json`."""
import json
import os

import pytest
from textual.app import App

from lightmind import model as m
from lightmind import runs
from lightmind.files import Home
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import MapScreen

from test_serve import opened, quiet_tree, start          # noqa: F401 - fixtures of the serve tests, used below
from test_session_lock import CHILD, locks, other_process
from test_tui_support import FakeProv, earlier_chat, idle, install, open_chat, pause_forever, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def marker(home, sid) -> dict:
    return json.loads((home.path("runs") / sid / "archived.json").read_text("utf-8"))


def archived_ids(home) -> list[str]:
    return sorted(p.parent.name for p in home.path("runs").glob("*/archived.json"))


def meta(home, sid) -> dict:
    return json.loads((home.path("runs") / sid / "meta.json").read_text("utf-8"))


async def test_orbit_binds_ctrl_q_to_archive_and_quit_not_to_textuals_plain_quit(home, prov):
    async with running(home) as (app, pilot):
        bound = app._bindings.key_to_bindings["ctrl+q"]
        assert [b.action for b in bound] == ["archive_quit"] and bound[0].priority
    assert any(b.key == "ctrl+q" and b.action == "quit" for b in App.BINDINGS)           # (Textual's own, which Orbit replaces)


async def test_ctrl_q_in_a_chat_closes_and_archives_it_and_ends_the_app(home, prov):
    prov.turns = [[m.Text("hello back"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await say(pilot, "hi")
        await idle(pilot, app)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None)
    assert app.return_code == 0 and locks(home) == []
    assert marker(home, sid)["by"] == "tui" and marker(home, sid)["status"] in ("running", "paused", "done")
    assert meta(home, sid)["status"] == "done"                                   # closed properly: hooks, end marker
    assert any(line.startswith("archived:") and sid in line and "lm archive restore" in line for line in app.exit_lines)
    assert runs.is_archived(home.path("runs") / sid)


async def test_ctrl_q_stops_a_running_turn_and_archives_the_chat(home, prov):
    prov.turns = [pause_forever]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await say(pilot, "go")
        await until(pilot, lambda: app.chat.busy)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None, timeout=20)
    assert archived_ids(home) == [sid] and locks(home) == [] and meta(home, sid)["status"] == "done"


async def test_ctrl_q_in_the_start_menu_only_ends_the_app(home, prov):
    async with running(home) as (app, pilot):
        assert isinstance(app.screen, MapScreen) and app.chat is None
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None)
    assert archived_ids(home) == [] and app.exit_lines == []


async def test_quit_and_escape_close_the_chat_without_archiving_it(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await say(pilot, "/quit")
        await until(pilot, lambda: app.return_code is not None)
    assert archived_ids(home) == [] and meta(home, sid)["status"] == "done"
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid2 = app.chat.s.id
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))
        await pilot.press("ctrl+q")                                              # menu again: just ends
        await until(pilot, lambda: app.return_code is not None)
    assert archived_ids(home) == [] and meta(home, sid2)["status"] == "done"


async def test_ctrl_q_in_a_chat_that_another_window_holds_does_not_archive_it(home, prov):
    sid = await earlier_chat(home, prov)
    with other_process(CHILD, str(home.root), sid) as (child, first):
        assert first == "0"
        app = Orbit(home, anim=False, direct={"id": sid})
        async with app.run_test(size=(130, 42)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
            assert app.chat.s.readonly
            await pilot.press("ctrl+q")
            await until(pilot, lambda: app.return_code is not None)
        assert archived_ids(home) == [] and runs.lock_holder(home, sid) == child.pid


async def test_ctrl_q_in_a_direct_tab_archives_and_ends(home, prov):
    sid = await earlier_chat(home, prov)
    app = Orbit(home, anim=False, direct={"id": sid})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None)
    assert archived_ids(home) == [sid] and locks(home) == []


async def test_an_archived_chat_is_gone_from_the_resume_list_and_comes_back_with_unarchive(home, prov):
    from lightmind import resume
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await say(pilot, "remember this")
        await idle(pilot, app)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None)
    assert sid not in [c.id for c in resume.chats(home)] and resume.latest(home) != sid
    assert runs.unarchive(home, sid) == "restored"
    assert sid in [c.id for c in resume.chats(home)]


async def test_lm_open_rc_turns_remote_control_on_when_the_chat_is_shown(home, prov):
    from lightmind import settings
    from lightmind.files import Home
    cfg = home.root / "config.md"
    cfg.write_bytes(settings.edit_text(cfg.read_bytes().decode("utf-8"), {("remote", "enabled"): True}).encode("utf-8"))
    on = Home(home.root)
    sid = await earlier_chat(on, prov)
    app = Orbit(on, anim=False, direct={"id": sid, "rc": "via open"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: app.chat is not None and app.chat.rc is not None)
        assert app.chat.rc.name == "via open"
        assert (os.path.exists(str(on.path("state") / "remote" / "links" / f"{sid}.json")))
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None)
    assert not (on.path("state") / "remote" / "links" / f"{sid}.json").exists() and archived_ids(on) == [sid]


async def test_lm_open_rc_wins_over_remote_auto_and_opens_one_link_with_its_name(home, prov):
    from lightmind import settings
    from lightmind.files import Home
    cfg = home.root / "config.md"
    cfg.write_bytes(settings.edit_text(cfg.read_bytes().decode("utf-8"),
                                       {("remote", "enabled"): True, ("remote", "auto"): True}).encode("utf-8"))
    on = Home(home.root)
    sid = await earlier_chat(on, prov)
    app = Orbit(on, anim=False, direct={"id": sid, "rc": "via open"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: app.chat is not None and app.chat.rc is not None)
        first = app.chat.rc
        await pilot.pause(0.5)
        assert app.chat.rc is first and first.name == "via open" and not first.auto     # the phone's name, not a second auto link
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.return_code is not None)


async def test_serve_slash_archive_closes_the_chat_marks_it_and_the_next_message_has_no_chat(start, quiet_tree):
    c = await start([[["deltas", ["Hi"]]]])
    sess = await opened(c)
    res, _ = await c.request("send", text="/archive")
    assert res["ok"] and res["data"]["archived"] == sess["id"]
    assert [x["session"] for x in c.of("session.ended")] == [sess["id"]]
    home = Home(quiet_tree)
    assert marker(home, sess["id"])["by"] == "vscode" and meta(home, sess["id"])["status"] == "done"
    res, _ = await c.request("send", text="hello again")
    assert res["ok"] is False and "no chat is open" in res["error"]
    await c.close()


async def test_serve_slash_archive_is_not_a_message_to_the_model(start, quiet_tree):
    c = await start([[["deltas", ["never sent"]]]])
    await opened(c)
    res, _ = await c.request("send", text="  /ARCHIVE ")
    assert res["ok"] and not c.of("turn.start") and not c.of("text")
    await c.close()
