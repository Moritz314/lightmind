"""Orbit: `/upload` and `/datei` (upload.py): the files chosen in the dialog land as `[Anhang: path]` lines in the input at
the place of the word, are copied into the run folder, and nothing is sent.  The dialog itself is replaced by a function
(no test opens one); the parser and the copy are tested in test_upload.py."""
import threading

import pytest
from textual.widgets import Static

from lightmind import upload
from lightmind.tui.chat import ChatScreen
from lightmind.tui.chatinput import ChatInput
from lightmind.upload import Picked

from test_tui_support import FakeProv, install, open_chat, quiet, running, texts, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


@pytest.fixture
def dialog(monkeypatch, tmp_path):
    """`dialog(files...)` makes the next dialog return those files; `dialog.calls` holds the folders it was asked to open."""
    src = tmp_path / "from"
    src.mkdir()
    calls = []

    def choose(*names, notice=""):
        files = []
        for name in names:
            (src / name).write_text("content of " + name, "utf-8")
            files.append(src / name)

        def pick(start=None, platform=None):
            calls.append(start)
            return Picked(files, notice)
        monkeypatch.setattr(upload, "pick", pick)
    choose.calls, choose.src = calls, src
    return choose


def box(app) -> ChatInput:
    return app.screen.query_one(ChatInput)


async def enter(pilot, text: str, cursor: int | None = None):
    inp = box(pilot.app)
    inp.value = text
    inp.cursor_position = len(text) if cursor is None else cursor
    await pilot.pause(0.1)
    await pilot.press("enter")


def has(app, needle: str) -> bool:
    return any(needle in t for t in texts(app))


async def test_the_word_alone_fills_the_input_with_marker_lines_and_sends_nothing(home, prov, dialog):
    dialog("foto.png", "bericht.pdf")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "/upload")
        run = app.chat.s.run.path
        await until(pilot, lambda: box(app).value != "")
        rel = run.relative_to(home.root).as_posix()
        assert box(app).value == f"[Anhang: {rel}/uploads/foto.png]\n[Anhang: {rel}/uploads/bericht.pdf]\n"
        assert (run / "uploads" / "foto.png").read_text() == "content of foto.png"        # copied into the chat's run folder
        assert has(app, "2 Dateien angehängt") and prov.requests == []                  # a note, and nothing went to the model
        assert box(app).cursor_position == len(box(app).value)                          # ready to type below the lines


async def test_datei_is_the_same_and_a_second_pick_gets_a_new_name(home, prov, dialog):
    dialog("foto.png")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "/datei")
        await until(pilot, lambda: box(app).value != "")
        await enter(pilot, "/datei")
        await until(pilot, lambda: "foto-1.png" in box(app).value)
        assert has(app, "1 Datei angehängt")


async def test_inside_a_text_the_lines_take_the_place_of_the_word(home, prov, dialog):
    dialog("a.png")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "Schau dir /upload bitte an")
        await until(pilot, lambda: "[Anhang:" in box(app).value)
        rel = app.chat.s.run.path.relative_to(home.root).as_posix()
        assert box(app).value == f"Schau dir \n[Anhang: {rel}/uploads/a.png]\nbitte an"
        assert prov.requests == []                                                      # the text was not sent either


async def test_the_dialog_opens_in_the_folder_of_the_last_file(home, prov, dialog):
    dialog("a.png")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "/upload")
        await until(pilot, lambda: box(app).value != "")
        await enter(pilot, "/upload")
        await until(pilot, lambda: len(dialog.calls) == 2)
        assert dialog.calls[1] == dialog.src and dialog.calls[0] != dialog.src


async def test_cancel_changes_nothing_but_the_word_leaves_the_line(home, prov, dialog):
    dialog()
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "vorher /upload nachher")
        await until(pilot, lambda: len(dialog.calls) == 1)
        await pilot.pause(0.2)
        assert box(app).value == "vorher nachher" and not has(app, "angehängt") and not has(app, "Error")
        assert not (app.chat.s.run.path / "uploads").exists() and prov.requests == []


async def test_without_a_dialog_a_german_notice_is_shown_and_the_line_stays_usable(home, prov, dialog):
    dialog(notice=upload.NO_PICKER)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "/upload")
        await until(pilot, lambda: has(app, "Kein Datei-Dialog hier möglich"))
        assert box(app).value == "" and prov.requests == []


async def test_a_read_only_chat_says_so_and_attaches_nothing(home, prov, dialog):
    dialog("a.png")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        app.chat.s.readonly_by = 4242
        await enter(pilot, "/upload")
        await until(pilot, lambda: has(app, "read only"))
        assert dialog.calls == [] and not has(app, "angehängt")


async def test_the_ui_keeps_running_while_the_dialog_is_open_and_a_second_one_is_refused(home, prov, monkeypatch, tmp_path):
    release, entered = threading.Event(), threading.Event()
    src = tmp_path / "x.txt"
    src.write_text("x")

    def slow(start=None, platform=None):
        entered.set()
        release.wait(10)
        return Picked([src])
    monkeypatch.setattr(upload, "pick", slow)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await enter(pilot, "/upload")
        await until(pilot, entered.is_set)
        box(app).value = "ich tippe weiter"                                              # not frozen: the input still works
        await pilot.pause(0.1)
        assert box(app).value == "ich tippe weiter"
        await enter(pilot, "ich tippe weiter /datei")
        await until(pilot, lambda: has(app, "schon offen"))
        assert box(app).value == "ich tippe weiter"                                      # the word left the line all the same
        release.set()
        await until(pilot, lambda: "[Anhang:" in box(app).value)
        assert box(app).value.startswith("ich tippe weiter\n[Anhang:")                   # inserted at the cursor of the moment


async def test_it_is_in_the_suggestions_with_a_description(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        box(app).value = "/da"
        box(app).cursor_position = 3
        await pilot.pause(0.1)
        shown = app.screen.query_one("#pop", Static).content.plain
        assert "/datei" in shown and "same as /upload" in shown
        box(app).value = "/upl"
        box(app).cursor_position = 4
        await pilot.pause(0.1)
        assert "/upload" in app.screen.query_one("#pop", Static).content.plain


def test_the_chat_screen_has_a_handler_for_both_words():
    assert hasattr(ChatScreen, "cmd_upload") and hasattr(ChatScreen, "cmd_datei")
