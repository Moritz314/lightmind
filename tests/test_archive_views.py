"""Seeing archived chats and getting them back where a person picks chats: `resume.restore` and the archive text, `lm resume` on
an archived chat and the archive tab of the resume menu, the slash words `/archive list` and `/archive restore <chat>`, the archive view
of the Orbit resume menu (Enter restores and opens), the `/archive` words of the chat with their popup, and the VS Code
backend (`session.list` with `archived`, `session.archive`, `session.unarchive`, `session.open` restoring)."""
import argparse

import pytest
from textual.widgets import Static

from lightmind import cli, resume, runs
from lightmind import slash as sl
from lightmind.files import Home
from lightmind.tui.chat import ChatScreen
from lightmind.tui.chatinput import ChatInput
from lightmind.tui.screens import ResumeScreen

from test_archive import chat, ids
from test_serve import opened, quiet_tree, start          # noqa: F401 - fixtures of the serve tests, used below
from test_tui_support import FakeProv, earlier_chat, install, open_chat, plain, quiet, running, say, texts, until


def recent(home) -> list[str]:
    return [r.id for r in resume.rows(home)]


def archived(home) -> list[str]:
    return [r.id for r in resume.rows(home, archived=True)]


# ------------------------------------------------------------------------ resume.py
def test_restore_brings_the_chat_back_into_the_resume_list(home):
    rd = chat(home, "20261023-100000-chat-aaaa")
    runs.archive(home, rd.run.id)
    assert recent(home) == [] and archived(home) == [rd.run.id]                    # the counter-probe: it really was gone
    assert resume.restore(home, rd.run.id) == (rd.run.id, "restored")
    assert recent(home) == [rd.run.id] and archived(home) == []
    assert resume.restore(home, "aaaa") == (rd.run.id, "not archived")               # the short id works; a second time says so
    with pytest.raises(ValueError):
        resume.restore(home, "no-such-chat")


def test_the_archive_text_lists_the_ids_and_says_when_it_is_empty(home):
    assert resume.archive_text(home) == "the archive is empty"
    rd = chat(home, "20261023-100000-chat-aaaa")
    kept = chat(home, "20261023-110000-chat-bbbb")
    runs.archive(home, rd.run.id)
    text = resume.archive_text(home)
    assert rd.run.id in text and kept.run.id not in text and "/archive restore" in text
    assert [c[0] for c in resume.archived_choices(home)] == [rd.run.id]


# ------------------------------------------------------------------------ lm resume
def spec_args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"ref": None, "last": False, "dry_run": False, "archived": False, **kw})


def test_lm_resume_by_name_of_an_archived_chat_restores_it_but_a_dry_run_does_not(home):
    rd = chat(home, "20261023-100000-chat-aaaa")
    runs.archive(home, rd.run.id)
    assert cli._resume_spec(home, spec_args(ref=rd.run.id, dry_run=True))["id"] == rd.run.id
    assert runs.is_archived(rd.path)                                                # a dry run changes nothing
    assert cli._resume_spec(home, spec_args(ref="aaaa"))["id"] == rd.run.id
    assert not runs.is_archived(rd.path) and recent(home) == [rd.run.id]


def test_lm_resume_without_a_name_opens_the_resume_menu_and_archived_starts_on_the_archive_tab(home, monkeypatch):
    monkeypatch.setattr(cli, "_person_only", lambda what: None)                     # the tests have no terminal
    assert cli._resume_spec(home, spec_args())["screen"] == "resume" and "archived" not in cli._resume_spec(home, spec_args())
    assert cli._resume_spec(home, spec_args(archived=True))["archived"] is True    # the counter-probe: --archived changes the tab


# ------------------------------------------------------------------------ the words of the slash parser
CAT = sl.Catalog(builtin=("archive", "model"))


@pytest.mark.parametrize("text, arg", [("/archive", ""), ("/archive list", "list"), ("/archive restore abc-1", "restore abc-1"),
                                       ("/archive restore", "restore"), ("/archive nonsense", "nonsense")])
def test_the_archive_words_reach_the_command_as_its_argument(text, arg):
    p = sl.parse(text, CAT)
    assert not p.error and p.text == "" and p.then is not None and (p.then.name, p.then.arg) == ("archive", arg)


def test_text_before_an_archive_command_still_runs_it_after_the_answer():
    p = sl.parse("thanks, bye /archive", CAT)
    assert p.text == "thanks, bye" and p.then is not None and p.then.line == "/archive"
    assert sl.parse("/archive restore a b", CAT).error                              # a third word is not swallowed


# ------------------------------------------------------------------------ the VS Code backend
async def test_serve_lists_the_archive_unarchives_and_session_open_restores(start, quiet_tree):
    a = await start([[["say", "Hi there"]]])
    sid = (await opened(a))["id"]
    await a.request("send", text="hello")
    await a.until("turn.end")
    assert (await a.request("send", text="/archive"))[0]["ok"]
    home = Home(quiet_tree)
    listed = (await a.request("session.list", limit=10))[0]["data"]["sessions"]
    assert sid not in [r["id"] for r in listed]
    shelf = (await a.request("session.list", limit=10, archived=True))[0]["data"]["sessions"]
    assert [r["id"] for r in shelf] == [sid] and shelf[0]["archived"] is True
    assert (await a.request("session.list", limit=10, archived="yes"))[0]["ok"] is False
    done = (await a.request("session.unarchive", session=sid))[0]
    assert done["ok"] and done["data"]["text"] == "restored" and not runs.is_archived(home.path("runs") / sid)
    assert sid in [r["id"] for r in (await a.request("session.list", limit=10))[0]["data"]["sessions"]]
    assert (await a.request("session.list", limit=10, archived=True))[0]["data"]["sessions"] == []
    assert (await a.request("session.unarchive", session="nope"))[0]["ok"] is False
    # the other way: a chat of the list goes into the archive, and opening it brings it back
    assert (await a.request("session.archive", session=sid))[0]["data"]["text"] == "archived"
    assert runs.is_archived(home.path("runs") / sid)
    back = (await a.request("session.open", session=sid))[0]
    assert back["ok"] and back["data"]["restored"] is True and not runs.is_archived(home.path("runs") / sid)
    refused = (await a.request("session.archive", session=sid))[0]                  # it is open here now: that one leaves with /archive
    assert refused["ok"] is False and "open here" in refused["error"] and not runs.is_archived(home.path("runs") / sid)
    await a.close()


async def test_serve_slash_archive_list_and_restore_keep_the_chat_open(start, quiet_tree):
    a = await start([[["say", "one"]], [["say", "two"]]])
    old = (await opened(a))["id"]
    await a.request("send", text="first chat")
    await a.until("turn.end")
    await a.request("send", text="/archive")
    now = (await a.request("session.new"))[0]["data"]["session"]["id"]
    home = Home(quiet_tree)
    ok = (await a.request("send", text="/archive list"))[0]
    assert ok["ok"] and old in ok["data"]["text"] and now not in ok["data"]["text"]
    assert not runs.is_archived(home.path("runs") / now) and not a.of("session.ended")[1:]      # the open chat is untouched
    bad = (await a.request("send", text="/archive restore no-such-chat"))[0]
    assert bad["ok"] is False and "no-such-chat" in bad["error"] and runs.is_archived(home.path("runs") / old)
    assert (await a.request("send", text="/archive restore"))[0]["ok"] is False
    done = (await a.request("send", text=f"/archive restore {old}"))[0]
    assert done["ok"] and "restored" in done["data"]["text"] and not runs.is_archived(home.path("runs") / old)
    assert not runs.is_archived(home.path("runs") / now)
    await a.close()


# ------------------------------------------------------------------------ the Orbit resume menu and chat
@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


async def archived_chat(home, prov, text="remember the milk") -> str:
    sid = await earlier_chat(home, prov, text=text)
    runs.archive(home, sid)
    return sid


def popup(app) -> str:
    box = app.screen.query_one("#pop", Static)
    return box.content.plain if box.display else ""


async def typed(pilot, text: str) -> None:
    inp = pilot.app.screen.query_one(ChatInput)
    inp.value = text
    inp.cursor_position = len(text)
    await pilot.pause(0.1)


async def test_enter_in_the_archive_view_restores_the_chat_and_opens_it(home, prov):
    await earlier_chat(home, prov, text="buy some eggs")
    old = await archived_chat(home, prov)
    async with running(home) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows is not None and len(app.screen.rows) == 1)
        await pilot.press("tab")
        await until(pilot, lambda: app.screen.archived and app.screen.rows is not None and [r.id for r in app.screen.rows] == [old])
        assert "restore and open" in plain(app, "#list") and runs.is_archived(home.path("runs") / old)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.id == old and not runs.is_archived(home.path("runs") / old)
        assert old in recent(home)


async def test_enter_in_the_recent_view_does_not_touch_the_archive(home, prov):
    kept = await earlier_chat(home, prov, text="buy some eggs")
    old = await archived_chat(home, prov)
    async with running(home) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows is not None and len(app.screen.rows) == 1)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.id == kept and runs.is_archived(home.path("runs") / old)


async def test_a_start_menu_row_with_the_archive_action_opens_the_archive_view(home, prov):
    from test_tui_support import edit
    old = await archived_chat(home, prov)
    edit(home, '    - "Quit | q | quit"', '    - "Archive | v | archive"\n    - "Quit | q | quit"')
    async with running(home) as (app, pilot):
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.archived and app.screen.rows is not None)
        assert [r.id for r in app.screen.rows] == [old] and "archived chats" in plain(app, "#list")


async def test_slash_archive_list_and_restore_work_in_the_chat_and_do_not_archive_it(home, prov):
    old = await archived_chat(home, prov)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        here = app.chat.s.id
        await say(pilot, "/archive list")
        await until(pilot, lambda: any(old in t for t in texts(app)))
        assert isinstance(app.screen, ChatScreen) and not runs.is_archived(home.path("runs") / here)
        await say(pilot, "/archive nonsense")
        await until(pilot, lambda: any("/archive archives this chat" in t for t in texts(app)))
        assert isinstance(app.screen, ChatScreen) and not runs.is_archived(home.path("runs") / here)      # a typo closes nothing
        await say(pilot, "/archive restore no-such-chat")
        await until(pilot, lambda: any("no-such-chat" in t for t in texts(app)))
        assert runs.is_archived(home.path("runs") / old)
        await say(pilot, f"/archive restore {old}")
        await until(pilot, lambda: not runs.is_archived(home.path("runs") / old))
        assert any("restored" in t for t in texts(app)) and isinstance(app.screen, ChatScreen)
        assert not runs.is_archived(home.path("runs") / here) and old in recent(home)


async def test_the_popup_completes_list_restore_and_the_archived_chat(home, prov):
    old = await archived_chat(home, prov, text="remember the milk")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await typed(pilot, "/archive ")
        shown = popup(app)
        assert "show the archived chats" in shown and "bring an archived chat back" in shown and "/list" not in shown   # no slash
        await typed(pilot, "/archive re")
        assert "bring an archived chat back" in popup(app) and "show the archived chats" not in popup(app)
        await typed(pilot, "/archive restore ")
        await until(pilot, lambda: old in popup(app))                                # read in a worker: it arrives a moment later
        await pilot.press("tab")
        assert app.screen.query_one(ChatInput).value == f"/archive restore {old}"
        await pilot.press("enter")                                                   # the chat id is typed in full: Enter runs it
        await until(pilot, lambda: not runs.is_archived(home.path("runs") / old))
        await typed(pilot, "/archive l")
        await pilot.press("enter")                                                   # Enter on an open popup completes first ...
        assert app.screen.query_one(ChatInput).value == "/archive list"
        assert not any("archive is empty" in t for t in texts(app))                  # ... and has not run it yet
        await pilot.press("enter")
        await until(pilot, lambda: any("archive is empty" in t for t in texts(app)))


async def test_nothing_after_archive_opens_the_archive_popup_in_a_plain_sentence(home, prov):
    await archived_chat(home, prov)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await typed(pilot, "please /archive")
        assert "/archive" in popup(app) and "bring an archived chat back" not in popup(app)      # the command itself, not its words
        await typed(pilot, "I restore things")
        assert popup(app) == ""
