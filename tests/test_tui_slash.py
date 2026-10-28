"""Orbit: slash words anywhere in the input (plan/PLAN-SLASH.md): the popup in the middle of the text, completion of
that one word, commands and skills inside a sentence, a command that leaves the chat at the end, and what is refused.
The parser itself is tested in test_slash.py."""
import asyncio

import pytest
from textual.widgets import Static

from lightmind import model as m
from lightmind.tui.chatinput import ChatInput
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import Pick, SetupScreen

from test_tui_support import FakeProv, idle, install, open_chat, pause_forever, plain, quiet, running, say, texts, until

REFUSED = "effort: unknown 'super'; use low, medium, high, xhigh, max"


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def popup(app) -> str:
    box = app.screen.query_one("#pop", Static)
    return box.content.plain if box.display else ""


def has(app, needle: str) -> bool:
    return any(needle in t for t in texts(app))


async def typed(pilot, text: str, cursor: int | None = None) -> ChatInput:
    """Put `text` in the input like typing would, the cursor at the end or at `cursor`, and let the popup settle."""
    inp = pilot.app.screen.query_one(ChatInput)
    inp.value = text
    inp.cursor_position = len(text) if cursor is None else cursor
    await pilot.pause(0.1)
    return inp


# ---------------------------------------------------------------------------- the popup
async def test_the_popup_opens_for_a_slash_word_anywhere_in_the_text(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await typed(pilot, "compare this with /run")
        assert "/run-tracking" in popup(app) and "/effort" not in popup(app)         # filtered by the word under the cursor
        await typed(pilot, "compare this with /run and more", len("compare this with /run"))      # the cursor in the middle
        assert "/run-tracking" in popup(app)
        await typed(pilot, "compare this with /run and more", len("compare this with /run") + 3)  # ... or in the next word
        assert popup(app) == ""
        await typed(pilot, "a\n/run")                                                # a later line of the input
        assert "/run-tracking" in popup(app)


@pytest.mark.parametrize("text, cursor", [("a/run", None), ("https://x.de/run", None), ("see C:/run", None), ("with //run", None),
                                          ("`compare /ru and` more", 15), ("```\n/ru", None)])
async def test_nothing_opens_inside_a_word_a_url_a_path_or_code(home, prov, text, cursor):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await typed(pilot, text, cursor)
        assert popup(app) == "", text


async def test_an_open_popup_follows_the_cursor_and_a_closed_one_stays_closed(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = await typed(pilot, "with /run and /mod")                                # typed to the end: the popup is for /mod
        assert "/mode" in popup(app) and "/run-tracking" not in popup(app)
        inp.cursor_position = len("with /run")                                         # onto the other word: it follows
        await pilot.pause(0.1)
        assert "/run-tracking" in popup(app) and "/mode" not in popup(app)
        inp.cursor_position = 2                                                      # off every slash word: it goes
        await pilot.pause(0.1)
        assert popup(app) == ""
        inp.cursor_position = len("with /run")                                         # moving does not open a closed one ...
        await pilot.pause(0.1)
        assert popup(app) == ""
        await pilot.press("backspace")                                               # ... typing does
        assert "/run-tracking" in popup(app)
        await pilot.press("escape")                                                  # Esc closes it, and it stays closed
        await pilot.press("left")
        assert popup(app) == "" and isinstance(app.screen, ChatScreen)


async def test_tab_completes_only_the_word_under_the_cursor(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = await typed(pilot, "compare this with /run and more", len("compare this with /run"))
        await pilot.press("tab")
        assert inp.value == "compare this with /run-tracking and more"
        assert inp.cursor_position == len("compare this with /run-tracking ") and popup(app) == ""
        inp = await typed(pilot, "x /mod")                                           # the popup keeps its keys: Down, then Tab
        await pilot.press("down", "tab")
        assert inp.value == "x /model "


async def test_enter_on_a_popup_in_the_middle_completes_and_the_next_enter_sends(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = await typed(pilot, "compare with /run")
        await pilot.press("enter")
        assert inp.value == "compare with /run-tracking " and not prov.requests
        await pilot.press("enter")
        await until(pilot, lambda: prov.requests)
        text = prov.requests[-1].messages[-1].text
        assert text.startswith('Use the skill "run-tracking"') and text.endswith("\n\ncompare with /run-tracking")


# ------------------------------------------------------------------------ what runs
async def test_commands_in_the_middle_run_and_the_rest_is_sent(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/model haiku /effort high explain X")                   # the plan's example
        await until(pilot, lambda: prov.requests)
        await idle(pilot, app)
        assert prov.requests[-1].messages[-1].text == "explain X"
        assert (prov.requests[-1].model, prov.requests[-1].effort) == ("haiku", "high")
        assert has(app, "model: haiku") and has(app, "effort: high") and has(app, "explain X")
        assert app.screen.query_one(ChatInput).value == ""
        await say(pilot, "and /effort low onward")                                   # in the middle of a sentence
        await until(pilot, lambda: len(prov.requests) == 2)
        assert prov.requests[-1].messages[-1].text == "and onward" and prov.requests[-1].effort == "low"


async def test_a_command_without_an_argument_in_the_middle_opens_its_picker_and_the_text_goes_out(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "explain X /mode")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        await until(pilot, lambda: prov.requests)
        assert prov.requests[-1].messages[-1].text == "explain X"


async def test_every_skill_named_anywhere_is_loaded_in_order_and_stays_in_the_sentence(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "compare this with /example-skill and /run-tracking, then /example-skill")
        await until(pilot, lambda: prov.requests)
        text = prov.requests[-1].messages[-1].text
        assert text.count('Use the skill "') == 2                                    # each once
        assert text.index('Use the skill "example-skill"') < text.index('Use the skill "run-tracking"')
        assert text.endswith("\n\ncompare this with /example-skill and /run-tracking, then /example-skill")
        assert has(app, "compare this with /example-skill and /run-tracking")        # the transcript shows the sentence


async def test_unknown_words_double_slashes_and_code_are_plain_text(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        for n, (line, want) in enumerate([("how goes /nope this", "how goes /nope this"), ("read /etc/hosts", "read /etc/hosts"),
                                          ("//model opus", "/model opus"), ("run `x /model opus y` now", "run `x /model opus y` now")], 1):
            await say(pilot, line)
            await until(pilot, lambda n=n: len(prov.requests) == n)
            assert prov.requests[-1].messages[-1].text == want
            await idle(pilot, app)
        assert app.chat.s.info.model == "sonnet"                                     # no command ran


# ----------------------------------------------------- commands that leave the chat
async def test_a_command_that_leaves_the_chat_runs_after_the_answer(home, prov):
    gate = asyncio.Event()

    async def slow(self, req):
        yield m.Text("working on it")
        await gate.wait()
        yield m.Text(" done")
        yield m.Usage()
    prov.turns = [slow]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "explain X /clear")                                      # `/clear → new_chat`, a ui.slash row
        await until(pilot, lambda: has(app, "working on it") and app.chat.busy)
        await pilot.pause(0.3)
        assert isinstance(app.screen, ChatScreen)                                    # the answer is not in yet
        gate.set()
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))
        assert [r.messages[-1].text for r in prov.requests] == ["explain X"]


async def test_a_command_that_leaves_the_chat_does_not_run_after_esc(home, prov):
    prov.turns = [pause_forever]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "explain X /clear")
        await until(pilot, lambda: has(app, "thinking about it") and app.chat.busy)
        await pilot.press("escape")
        await idle(pilot, app)
        await pilot.pause(0.3)
        assert isinstance(app.screen, ChatScreen) and has(app, "stopped")            # Esc stopped the turn, the chat stays


async def test_a_command_that_leaves_the_chat_alone_runs_at_once(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/clear")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))
        assert not prov.requests


async def test_text_after_a_command_that_leaves_the_chat_sends_nothing_and_the_line_stays(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/clear and then something more")
        await until(pilot, lambda: has(app, "goes last or alone"))
        assert isinstance(app.screen, ChatScreen) and not prov.requests
        assert app.screen.query_one(ChatInput).value == "/clear and then something more"


# ------------------------------------------------------------------------- what is refused
async def test_an_invalid_argument_sends_nothing_runs_nothing_and_the_line_stays(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        line = "/model haiku /effort super explain X"
        await say(pilot, line)
        await until(pilot, lambda: has(app, REFUSED))
        assert not prov.requests and app.chat.s.info.model == "sonnet"               # not even the model changed
        assert app.screen.query_one(ChatInput).value == line
        await say(pilot, "/model explain X")                                      # prose is not a model name
        await until(pilot, lambda: has(app, "model: unknown 'explain'; use "))
        assert not prov.requests


async def test_a_command_the_core_refuses_keeps_the_line_and_sends_nothing(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/mode wild explain X")
        await until(pilot, lambda: has(app, "unknown mode 'wild'"))
        await pilot.pause(0.1)
        assert not prov.requests and app.screen.query_one(ChatInput).value == "/mode wild explain X"


async def test_a_line_command_must_come_first_and_a_lone_typo_is_a_note(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "explain /tools")
        await until(pilot, lambda: has(app, "must come first"))
        await say(pilot, "/frobnicate")
        await until(pilot, lambda: has(app, "unknown command /frobnicate"))
        assert not prov.requests


async def test_text_is_queued_while_a_turn_runs_but_a_command_alone_runs_at_once(home, prov):
    prov.turns = [pause_forever]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "do it")
        await until(pilot, lambda: has(app, "thinking about it") and app.chat.busy)
        await say(pilot, "/effort low and more")                                     # text in the line: it waits, its command too
        assert app.chat.s.info.effort != "low" and len(app.chat.queue) == 1 and app.screen.query_one(ChatInput).value == ""
        await say(pilot, "/effort low")                                              # alone it still works
        await until(pilot, lambda: app.chat.s.info.effort == "low")
        await pilot.press("escape")                                                  # Esc stops the turn and takes the waiting line back
        await until(pilot, lambda: not app.chat.queue)
        assert app.screen.query_one(ChatInput).value == "/effort low and more"
        await idle(pilot, app)


# -------------------------------------------------------------------- the line above
async def test_the_line_above_the_input_names_what_was_recognized(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        keys = plain(app, "#hint")
        assert keys == ""                                                            # no key hint line any more
        await typed(pilot, "/model haiku /effort high compare with /run-tracking")
        await pilot.pause(0.3)                                                       # the spinner tick must leave it alone
        assert plain(app, "#hint") == "model haiku · effort high · skills: run-tracking"
        await typed(pilot, "/effort super x")                                        # a refused line shows nothing: Enter says why
        await pilot.pause(0.3)
        assert plain(app, "#hint") == keys
        await typed(pilot, "explain X /clear")
        await pilot.pause(0.3)
        assert plain(app, "#hint") == "then clear"
        await typed(pilot, "")
        await pilot.pause(0.3)
        assert plain(app, "#hint") == keys


async def test_typing_a_sentence_key_by_key_opens_the_popup_for_each_slash_word(home, prov):
    def keys(text: str) -> list[str]:
        return [{" ": "space", "/": "slash", "-": "minus"}.get(c, c) for c in text]

    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = app.screen.query_one(ChatInput)
        await pilot.press(*keys("compare it with /ru"))
        assert "/run-tracking" in popup(app) and "/mode" not in popup(app)
        await pilot.press("tab")
        assert inp.value == "compare it with /run-tracking "
        await pilot.press(*keys("and /exa"))
        assert "/example-skill" in popup(app)
        await pilot.press("tab")
        assert inp.value == "compare it with /run-tracking and /example-skill " and popup(app) == ""
        await pilot.press(*keys("or a/b"))                                           # a slash inside a word opens nothing
        assert popup(app) == ""
        await pilot.press("enter")
        await until(pilot, lambda: prov.requests)
        text = prov.requests[-1].messages[-1].text
        assert text.endswith("\n\ncompare it with /run-tracking and /example-skill or a/b")
        assert text.index('Use the skill "run-tracking"') < text.index('Use the skill "example-skill"')


# ------------------------------------------------------------------ the multi-line input
@pytest.mark.parametrize("key", ["ctrl+j", "ctrl+space", "ctrl+@", "shift+enter"])
async def test_a_new_line_per_key_and_enter_sends_the_text_as_it_is(home, prov, key):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = app.screen.query_one(ChatInput)
        await pilot.press("a", "b", key, "c", "space", "d")
        assert inp.value == "ab\nc d" and inp.cursor_location == (1, 3) and not prov.requests
        await pilot.press("enter")
        await until(pilot, lambda: prov.requests)
        assert prov.requests[-1].messages[-1].text == "ab\nc d" and inp.value == ""
        await idle(pilot, app)


async def test_enter_sends_a_multi_line_text_unchanged(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await typed(pilot, "first\n\n  second, indented\nthird")
        await pilot.press("enter")
        await until(pilot, lambda: prov.requests)
        assert prov.requests[-1].messages[-1].text == "first\n\n  second, indented\nthird"
        await idle(pilot, app)


async def test_the_input_wraps_long_text_and_grows_to_eight_rows(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = app.screen.query_one(ChatInput)
        assert inp.size.height == 1
        await typed(pilot, "wort " * 60)                                             # longer than the width: wrapped, no new line
        assert "\n" not in inp.value and 1 < inp.size.height <= 8
        await typed(pilot, "\n".join(f"line {i}" for i in range(20)))
        assert inp.size.height == 8
        await typed(pilot, "")
        assert inp.size.height == 1


async def test_the_popup_works_anywhere_in_a_later_line(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        inp = await typed(pilot, "first line\nsecond with /ru and more\nthird", len("first line\nsecond with /ru"))
        assert "/run-tracking" in popup(app)
        await pilot.press("down", "up")                                              # the arrows choose, the cursor stays
        assert inp.cursor_location == (1, len("second with /ru"))
        await pilot.press("enter")                                                   # Enter on a popup completes, it does not send
        assert inp.value == "first line\nsecond with /run-tracking and more\nthird" and not prov.requests
        assert popup(app) == ""
        inp.cursor_position = 3                                                      # the first line has none: no popup
        await pilot.pause(0.1)
        assert popup(app) == ""
        await typed(pilot, "x\n/mod")                                                # a word at the start of line 2
        assert "/mode" in popup(app)
