"""Orbit: what the user sees before he answers (ask modal) and text that must not reach the terminal raw."""
import asyncio

import pytest

from lightmind import model as m
from lightmind.providers.base import Notice, ToolDone
from lightmind.tui import data
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import AskModal

from test_tui_support import FakeProv, install, open_chat, plain, quiet, running, say, texts, until, idle


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def test_clean_removes_control_characters_but_keeps_newline_and_tab():
    dirty = "a\x1b[31mred\x07\x00b\tc\nd\x7f\x85e\x9b"
    assert data.clean(dirty) == "a[31mredb\tc\nde"
    assert data.clean(123) == "123"


async def test_the_ask_modal_shows_everything_on_escaped_lines_and_says_how_much_was_cut(home, prov):
    long = "Allow bash(echo a\necho b\x1b[2J)? " + "x" * (AskModal.SHOW + 1000)
    async with running(home) as (app, pilot):
        task = asyncio.create_task(app.ask(long, None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        modal = app.screen
        assert "\n" not in modal.question and " ⏎ " in modal.question and "\x1b" not in modal.question
        assert modal.question.startswith("Allow bash(echo a ⏎ echo b[2J)?")
        assert modal.too_long and modal.size_note.startswith("shortened") and int(modal.size_note.split()[1]) > AskModal.SHOW
        assert modal.shown.endswith("x" * (AskModal.SHOW // 4)) and " … " in modal.shown   # head and tail shown
        assert modal.query_one("#askbody").max_scroll_y >= 0                      # a scrollable body holds the text
        await pilot.pause(0.6)
        await pilot.press("y")                                                    # yes can be chosen however long the text is
        assert modal.sel == 0
        await pilot.press("enter")
        assert await asyncio.wait_for(task, 3) == "yes"
    short = AskModal("Allow write(x)? one line")
    assert short.size_note == "" and short.question == "Allow write(x)? one line"
    assert AskModal("a\nb\nc").size_note == ""


async def test_keys_typed_before_the_modal_opens_cannot_answer_it_and_an_answer_needs_enter(home, prov):
    async with running(home) as (app, pilot):
        task = asyncio.create_task(app.ask("Allow write(x)?", None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        await pilot.press("y", "enter")                                         # typeahead: ignored, the modal stays
        assert isinstance(app.screen, AskModal) and not task.done()
        await pilot.pause(0.6)
        assert app.screen.armed
        await pilot.press("y")                                                  # a letter only highlights
        assert isinstance(app.screen, AskModal) and not task.done() and app.screen.sel == 0
        await pilot.press("enter")
        assert await asyncio.wait_for(task, 3) == "yes"
        task = asyncio.create_task(app.ask("Allow write(y)?", None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        await pilot.pause(0.6)
        await pilot.press("enter")                                              # the default highlight is the safe answer
        assert await asyncio.wait_for(task, 3) == "no"
        task = asyncio.create_task(app.ask("Allow write(z)?", None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        await pilot.press("escape")                                             # Esc is no, at once
        assert await asyncio.wait_for(task, 3) == "no"


async def test_text_from_models_tools_notices_and_hooks_carries_no_control_characters(home, monkeypatch):
    quiet(home, "on_start:\n  - run: printf 'hook\\033[2Jline\\n'\n    show: true\n")
    prov = install(monkeypatch, FakeProv([[
        m.ToolCall("t1", "bash", {"command": "echo \x1b]0;pwned\x07hi"}),
        ToolDone("t1", "bash", True, "\x1b[31mred\x1b[0m result"),
        Notice("warning", "warn\x1b[1mbold"), m.Error("other", "err\x07or"),
        m.Text("answer \x1b[2Jcleared\x9b"), m.Usage()]]))
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "hi\x1b[2J")
        await until(pilot, lambda: any("answer" in t for t in texts(app)))
        await idle(pilot, app)
        lines = texts(app)
        assert any("hook[2Jline" in t for t in lines), [t[:60] for t in lines]
        for t in lines:
            assert not any(ord(c) < 32 and c not in "\n\t" or 0x7f <= ord(c) <= 0x9f for c in t), repr(t)
        assert any(t.startswith("bash > ") and "✓ [31mred[0m result" in t for t in lines)
        assert "\x1b" not in app.export_screenshot()


def test_invisible_and_bidi_characters_are_written_out_in_the_ask_modal():
    q = AskModal("Allow wri" + chr(0x200B) + "te(a" + chr(0x202E) + "txt.sh)?" + chr(9) + "go" + chr(0x2028) + "x").question
    esc = lambda n: chr(92) + "u{" + format(n, "04X") + "}"                  # noqa: E731
    assert q == "Allow wri" + esc(0x200B) + "te(a" + esc(0x202E) + "txt.sh)?" + esc(9) + "go" + esc(0x2028) + "x"
    assert data.visible("plain ⏎ text") == "plain ⏎ text"


@pytest.mark.parametrize("size", [(130, 42), (80, 24), (60, 16)])
async def test_a_very_long_request_scrolls_and_its_key_hints_stay_on_screen(home, prov, size):
    text = "Allow bash(" + "x" * 3500 + ")? " + "\n".join(f"line {i}" for i in range(40))   # long, no spaces, many lines
    async with running(home, size=size) as (app, pilot):
        task = asyncio.create_task(app.ask(text, None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        modal = app.screen
        await pilot.pause(0.2)
        screen_h = app.size.height
        body, keys, box = (modal.query_one(i) for i in ("#askbody", "#askkeys", "#ask"))
        assert not modal.too_long and body.max_scroll_y > 0                        # long enough to scroll
        assert box.region.y >= 0 and box.region.bottom <= screen_h                 # the whole box fits the screen
        assert keys.region.height >= 2 and keys.region.bottom <= screen_h          # hints are not cut off
        assert body.region.height >= 3 and body.region.height <= screen_h * 0.6    # the text takes only part of it
        await pilot.pause(0.6)                                                     # armed
        await pilot.press("pagedown", "down", "down")                              # scrolled, then answered
        assert body.scroll_y > 0
        await pilot.press("y", "enter")
        assert await asyncio.wait_for(task, 3) == "yes"


async def test_the_deny_key_answers_a_long_request_even_when_scrolled_to_the_end(home, prov):
    async with running(home, size=(80, 20)) as (app, pilot):
        task = asyncio.create_task(app.ask("Allow bash(" + "y" * 3000 + ")?", None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        await pilot.pause(0.6)
        app.screen.query_one("#askbody").scroll_end(animate=False)
        await pilot.press("n", "enter")
        assert await asyncio.wait_for(task, 3) == "no"


async def test_the_raw_command_is_folded_away_and_d_opens_it_whole(home, prov):
    detail = "\n".join(f"echo {i} \x1b[2J" for i in range(40))
    async with running(home, size=(80, 20)) as (app, pilot):
        task = asyncio.create_task(app.ask("View file notes.md\nProgram: cat\nAsked because no rule allows this yet",
                                           type("C", (), {"arg": detail})()))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        modal = app.screen
        shown = lambda: str(modal.query_one("#askq").render())
        assert "View file notes.md" in shown() and "echo 5" not in shown() and "d shows the command" in shown()
        assert len(modal.detail_rows) == 40 and not modal.detail_cut and "\x1b" not in "".join(modal.detail_rows)
        await pilot.pause(0.6)
        await pilot.press("d")
        assert "echo 5" in shown() and "echo 39" in shown()                      # the whole command: its end is never hidden
        await pilot.press("y", "enter")                                           # yes was never out of reach
        assert await asyncio.wait_for(task, 3) == "yes"


def test_a_long_tail_of_the_command_is_shown_and_only_a_huge_one_is_shortened_in_the_middle():
    modal = AskModal("Run python3", "python3 -c 'x'" + " " * 10 + "a" * 5000 + "; rm -rf ~")
    assert not modal.detail_cut and "rm -rf ~" in "".join(modal.detail_rows)
    huge = AskModal("Run python3", "b" * (AskModal.DETAIL_SHOW + 10) + "TAIL")
    assert huge.detail_cut and "".join(huge.detail_rows).endswith("TAIL")


async def test_a_question_without_a_command_has_no_command_toggle(home, prov):
    modal = AskModal("Fetch web page example.org")
    assert modal.detail_rows == [] and not modal.detail_cut


@pytest.mark.parametrize("size", [(60, 16), (50, 16), (40, 14)])
async def test_the_ask_box_fits_a_narrow_terminal_with_its_keys(home, prov, size):
    async with running(home, size=size) as (app, pilot):
        task = asyncio.create_task(app.ask("Show the changed files\nProgram: git\nAsked because of rule bash(git *) (mode:auto)",
                                           type("C", (), {"arg": "git status --porcelain " + "x" * 300})()))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        modal = app.screen
        await pilot.pause(0.2)
        box, keys = modal.query_one("#ask"), modal.query_one("#askkeys")
        assert box.region.x >= 0 and box.region.right <= app.size.width and box.region.bottom <= app.size.height
        assert keys.region.right <= app.size.width and keys.region.bottom <= app.size.height
        await pilot.pause(0.6)
        await pilot.press("y", "enter")
        assert await asyncio.wait_for(task, 3) == "yes"
