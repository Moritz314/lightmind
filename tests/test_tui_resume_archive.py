"""The resume menu and the archive: `a` archives the highlighted chat (in the archive view: restores it), `Tab` switches between
the recent chats and the archive.  Textual Pilot."""
import pytest

from lightmind import runs
from lightmind.tui.screens import ResumeScreen

from test_node import chat
from test_session_lock import CHILD, other_process
from test_tui_support import FakeProv, earlier_chat, install, plain, quiet, running, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


async def to_resume(app, pilot, count: int):
    await pilot.press("r")
    await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows is not None and len(app.screen.rows) == count)


def titles(app) -> list[str]:
    return [r.session.title for r in app.screen.rows if r.session]


async def test_a_archives_the_highlighted_chat_and_it_leaves_the_list(home, prov):
    first = await earlier_chat(home, prov, text="remember the milk")
    second = await earlier_chat(home, prov, text="buy some eggs")
    async with running(home) as (app, pilot):
        await to_resume(app, pilot, 2)
        shown = app.screen.rows[app.screen.c].id
        await pilot.press("a")
        await until(pilot, lambda: len(app.screen.rows) == 1)
        assert runs.is_archived(home.path("runs") / shown) and not runs.is_archived(home.path("runs") / ({first, second} - {shown}).pop())
        assert app.screen.rows[0].id != shown and "archived" in app.screen.note
        assert "a archive" in plain(app, "#list") and "Tab archive" in plain(app, "#list")
    listed = [r.id for r in runs.list_sessions(home)]
    assert len(listed) == 1 and shown not in listed                      # the other chat is still in every list


async def test_tab_shows_the_archive_and_a_restores_from_there(home, prov):
    await earlier_chat(home, prov, text="remember the milk")
    await earlier_chat(home, prov, text="buy some eggs")
    async with running(home) as (app, pilot):
        await to_resume(app, pilot, 2)
        gone = app.screen.rows[app.screen.c].id
        await pilot.press("a")
        await until(pilot, lambda: len(app.screen.rows) == 1)
        await pilot.press("tab")
        await until(pilot, lambda: app.screen.archived and app.screen.rows is not None and len(app.screen.rows) == 1)
        assert app.screen.rows[0].id == gone
        text = plain(app, "#list")
        assert "a restore" in text and "Tab recent chats" in text and "archived chats" in text
        await pilot.press("a")
        await until(pilot, lambda: app.screen.rows == [])
        assert not runs.is_archived(home.path("runs") / gone) and "restored" in app.screen.note
        assert "the archive is empty" in plain(app, "#list")
        await pilot.press("tab")
        await until(pilot, lambda: not app.screen.archived and app.screen.rows is not None and len(app.screen.rows) == 2)
        assert gone in [r.id for r in app.screen.rows]


async def test_the_archive_view_is_empty_when_nothing_is_archived_and_a_does_nothing_there(home, prov):
    await earlier_chat(home, prov)
    async with running(home) as (app, pilot):
        await to_resume(app, pilot, 1)
        await pilot.press("tab")
        await until(pilot, lambda: app.screen.archived and app.screen.rows == [])
        await pilot.press("a")
        await pilot.pause(0.2)
        assert app.screen.rows == [] and "the archive is empty" in plain(app, "#list")
        assert runs.list_sessions(home, archived=True) == []


async def test_a_chat_that_another_process_still_holds_is_not_archived_and_the_list_says_why(home, prov, monkeypatch):
    monkeypatch.setattr(runs, "request_stop", lambda *a, **k: "asked")
    sid = await earlier_chat(home, prov)
    with other_process(CHILD, str(home.root), sid):
        async with running(home) as (app, pilot):
            await to_resume(app, pilot, 1)
            await pilot.press("a")
            await until(pilot, lambda: "still running in process" in app.screen.note)
            assert len(app.screen.rows) == 1 and not runs.is_archived(home.path("runs") / sid)


async def test_a_chat_under_another_cannot_be_archived_alone_and_a_header_line_says_so(home, prov):
    chat(home, "p1", text="the parent")
    chat(home, "p1-kid", text="the child", parent="p1")
    async with running(home) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
        await pilot.press("right")                                      # unfold the group
        await until(pilot, lambda: len(app.screen.rows) == 2)
        await pilot.press("down")
        await until(pilot, lambda: app.screen.rows[app.screen.c].depth == 1)
        await pilot.press("a")
        await until(pilot, lambda: "top of the group" in app.screen.note)
        assert not runs.is_archived(home.path("runs") / "p1-kid") and not runs.is_archived(home.path("runs") / "p1")
        await pilot.press("up")
        await pilot.press("a")                                          # the parent: the whole group goes
        await until(pilot, lambda: app.screen.rows == [])
        assert runs.is_archived(home.path("runs") / "p1") and not runs.is_archived(home.path("runs") / "p1-kid")
        await pilot.press("tab")
        await until(pilot, lambda: app.screen.archived and len(app.screen.rows or []) >= 1)
        assert {r.id for r in app.screen.rows if r.session} >= {"p1"}


async def test_escape_still_goes_back_and_the_old_keys_still_work_in_the_archive_view(home, prov):
    sid = await earlier_chat(home, prov)
    runs.archive(home, sid)
    async with running(home) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows == [])
        await pilot.press("tab")
        await until(pilot, lambda: app.screen.archived and len(app.screen.rows) == 1)
        await pilot.press("n")                                          # notes of the archived chat
        await until(pilot, lambda: type(app.screen).__name__ == "NotesScreen")
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen))
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, ResumeScreen))
