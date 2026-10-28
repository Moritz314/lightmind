"""The resume list groups runs under the chat that started them (`parent`): folded with a count, unfolded on request."""
from __future__ import annotations

import pytest

from lightmind import runs
from lightmind.tui import screens

from test_tui_support import plain, running, until


def row(id, parent="", when="2026-10-23T10:00:00"):
    return runs.SessionSummary(id, f"title {id}", "a", "", "", "acc", "opus", "auto", "done", when, when, 0.0, False, parent=parent)


ROWS = [row("a", when="2026-10-23T10:00:00"), row("b", "a", "2026-10-23T09:00:00"), row("c", "b", "2026-10-23T08:00:00"),
        row("x", "gone", "2026-10-23T11:00:00"), row("y", "gone", "2026-10-22T11:00:00"), row("solo", when="2026-10-21T10:00:00")]


def shape(expanded):
    return [(g.id, g.depth, g.kids) for g in runs.group_sessions(ROWS, {"gone": "Telegram bot"}, expanded)]


def test_children_sit_folded_under_their_parent_with_a_count():
    assert shape(set()) == [("gone", 0, 2), ("a", 0, 2), ("solo", 0, 0)]


def test_unfolding_works_over_several_levels_and_orders_by_newest_activity():
    assert shape({"a"}) == [("gone", 0, 2), ("a", 0, 2), ("b", 1, 1), ("solo", 0, 0)]
    assert shape({"a", "b"})[1:5] == [("a", 0, 2), ("b", 1, 1), ("c", 2, 0), ("solo", 0, 0)]
    assert [g.id for g in runs.group_sessions(ROWS, {}, None)] == ["gone", "x", "y", "a", "b", "c", "solo"]


def test_a_missing_parent_becomes_a_header_with_its_name_or_its_id():
    head = runs.group_sessions(ROWS, {"gone": "Telegram bot"}, set())[0]
    assert head.session is None and head.label == "Telegram bot"
    assert runs.group_sessions(ROWS, {}, set())[0].label == "gone"


def test_a_parent_loop_or_self_parent_does_not_hang_or_lose_rows():
    got = runs.group_sessions([row("p", "p"), row("q", "r"), row("r", "q")], {}, None)
    assert {g.id for g in got} == {"p", "q", "r"}


@pytest.mark.asyncio
async def test_the_tui_list_folds_and_unfolds_a_group(home, monkeypatch):
    monkeypatch.setattr(screens, "list_session_groups", lambda h, n, names, archived=False: ROWS)
    async with running(home, anim=False) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, screens.ResumeScreen) and app.screen.rows)
        scr = app.screen
        text = lambda: plain(app, "#list")
        assert "title a (2)" in text() and "title b" not in text()
        scr.c = next(i for i, g in enumerate(scr.rows) if g.id == "a")
        await pilot.press("right")
        assert "title b (1)" in text() and "title c" not in text()
        await pilot.press("left")
        assert "title b" not in text()
        scr.c = 0                                                  # the header of the missing parent
        await pilot.press("enter")
        assert "title x" in text() and scr.rows[scr.c].id == "gone"


def test_the_limit_counts_groups_so_fifty_children_do_not_push_other_chats_out(monkeypatch):
    kids = [row(f"k{i}", "boss", f"2026-10-23T10:{i:02d}:00") for i in range(40)]
    chats = [row("boss", when="2026-10-23T11:00:00"), row("other", when="2026-10-22T11:00:00"), row("old", when="2026-10-01T11:00:00")]
    monkeypatch.setattr(runs, "list_sessions", lambda home, limit=20, archived=False: kids + chats)
    got = {r.id for r in runs.list_session_groups(None, 2)}
    assert "boss" in got and "other" in got and "old" not in got and "k39" in got


async def test_a_long_title_keeps_the_count_of_its_folded_group(home, monkeypatch):
    long = row("a", when="2026-10-23T10:00:00")
    long.title = "a very long title that would eat the whole column"
    monkeypatch.setattr(screens, "list_session_groups", lambda h, n, names, archived=False: [long, row("b", "a")])
    async with running(home, anim=False) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, screens.ResumeScreen) and app.screen.rows)
        assert "(1)" in plain(app, "#list")
