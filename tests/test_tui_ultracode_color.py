"""Orbit TUI: the word `ultracode` stands out in colour (bold magenta) in the input line while typing, in the text of the user and in
the answers; same rule as the effort switch, off with `workflow.keyword: false`, only styles change (never the text)."""
from io import StringIO

import pytest
from rich.color import Color
from rich.console import Console
from textual.app import App

from lightmind import ultracode
from lightmind.tui.chat import Md, Msg, agent_grid
from lightmind.tui.chatinput import ChatInput
from lightmind.tui.theme import MAG

KEYWORD_OFF = {"workflow": {"keyword": False}}


class Bare(App):
    """The smallest app that holds a chat input and has a `home` (the settings come from it, as in Orbit)."""

    def __init__(self, home):
        super().__init__()
        self.home = home

    def compose(self):
        yield ChatInput(id="inp")


def is_word_style(style) -> bool:
    """Is this the style of the coloured word: bold, in the magenta of the theme?"""
    return style is not None and bool(style.bold) and style.color == Color.parse(MAG)


def marked(renderable, width: int = 60) -> str:
    """The characters of a Rich renderable that carry the word style, in order (what the eye sees in colour)."""
    c = Console(width=width, color_system="truecolor", force_terminal=True)
    chars = []
    for line in c.render_lines(renderable, c.options.update(width=width), pad=False):
        chars += [ch for seg in line for ch in seg.text if is_word_style(seg.style)]
    return "".join(chars)


def text_of(renderable, width: int = 60) -> str:
    """The plain text of a renderable as the terminal shows it."""
    c = Console(width=width, record=True, color_system=None, file=StringIO())
    c.print(renderable)
    return c.export_text()


def ansi(renderable, width: int = 60) -> str:
    """The renderable as the escape codes a truecolor terminal gets."""
    out = StringIO()
    Console(width=width, file=out, force_terminal=True, color_system="truecolor").print(renderable)
    return out.getvalue()


def row_marked(strip) -> str:
    """The characters of one rendered input row that carry the word style."""
    return "".join(ch for seg in strip for ch in seg.text if is_word_style(seg.style))


def input_marked(box: ChatInput) -> str:
    """The coloured characters of every visible row of the input line."""
    return "".join(row_marked(box.render_line(y)) for y in range(box.size.height))


# -- the rule: the same as the effort switch -------------------------------------------------------------------------

@pytest.mark.parametrize("text,found", [
    ("ultracode", ["ultracode"]),
    ("Ultracode", ["Ultracode"]),
    ("bitte ultracode nutzen", ["ultracode"]),
    ("das ist ultracode.", ["ultracode"]),
    ("ULTRACODE und ultracode", ["ULTRACODE", "ultracode"]),
])
def test_spans_find_the_word_where_it_switches_the_effort_on(text, found):
    assert [text[a:b] for a, b in ultracode.spans(text)] == found
    assert ultracode.mentioned(text)                                # one rule: where it is coloured, it also switches on


@pytest.mark.parametrize("text", ["skills/ultracode/", "ultracode.py", "/ultracode on", "ultracoder", "no keyword here", ""])
def test_spans_skip_paths_files_the_slash_command_and_longer_words(text):
    assert ultracode.spans(text) == []
    assert not ultracode.mentioned(text)


# -- the transcript: the text of the user ------------------------------------------------------------------------------

def test_user_text_colours_exactly_the_word_and_leaves_the_rest():
    msg = Msg("user", "You", "bitte ultracode nutzen")
    assert marked(msg.build()) == "ultracode"                       # exactly these characters, nothing around them
    assert text_of(msg.build()).strip() == "You > bitte ultracode nutzen"     # the text itself is untouched


def test_user_text_without_the_word_has_no_colour():
    assert marked(Msg("user", "You", "skills/ultracode/ and ultracode.py and ultracoder").build()) == ""


# -- the transcript: the answers -------------------------------------------------------------------------------------

def test_answer_paragraph_is_coloured():
    assert marked(Md("Bitte ultracode nutzen.")) == "ultracode"


def test_answer_list_item_quote_heading_and_table_cell_are_coloured():
    for source in ("- ein ultracode Punkt", "1. ein ultracode Punkt", "> ein ultracode Zitat", "# Titel ultracode",
                   "| a | b |\n|---|---|\n| ultracode | x |"):
        assert marked(Md(source)) == "ultracode", source
    header = "| ultracode | b |\n|---|---|\n| x | y |"                     # Rich paints header cells in its own bold magenta afterwards
    assert marked(Md(header)) == "" and "ultracode" in text_of(Md(header))


def test_answer_inline_code_and_code_blocks_are_not_coloured():
    assert marked(Md("so: `ultracode` einschalten")) == ""
    assert marked(Md("```\nultracode\n```")) == ""
    assert marked(Md("```py\nultracode = 1\n```")) == ""
    assert marked(Md("ultracode und `ultracode` und ultracode")) == "ultracodeultracode"     # the plain ones around it still are


def test_answer_bold_and_italic_words_keep_their_look():
    for source in ("**ultracode** jetzt", "*ultracode* jetzt", "~~ultracode~~ jetzt"):
        assert marked(Md(source)) == "ultracode", source
    c = Console(width=40, color_system="truecolor", force_terminal=True)
    line = c.render_lines(Md("**ultracode** jetzt"), c.options.update(width=40), pad=False)[0]
    assert text_of(Md("**ultracode** jetzt")).strip() == "ultracode jetzt"
    assert any(seg.text == "ultracode" and seg.style.bold for seg in line)


def test_answer_paths_and_files_are_not_coloured():
    assert marked(Md("siehe skills/ultracode/SKILL.md und ultracode.py")) == ""


def test_answer_text_is_the_same_with_or_without_colour():
    source = "# Titel ultracode\n\nBitte ultracode nutzen, `ultracode` nicht.\n\n- ultracode eins\n\n> ultracode Zitat"
    assert text_of(Md(source)) == text_of(Md(source, mark=False))   # the colour adds styles, never characters
    assert marked(Md(source, mark=False)) == ""


def test_answer_block_in_the_transcript_is_coloured_and_selection_text_is_unchanged():
    msg = Msg("agent", "lm", "Bitte ultracode nutzen.")
    assert marked(msg.build()) == "ultracode"
    assert msg.plain == "Bitte ultracode nutzen."                    # what copy and selection work from


def test_streaming_a_growing_answer_colours_the_word_once_it_is_whole():
    msg = Msg("agent", "lm", "Bitte ultra")
    assert marked(msg.build()) == ""
    msg.text = "Bitte ultracode"
    assert marked(msg.build()) == "ultracode"
    msg.text = "Bitte ultracoder"
    assert marked(msg.build()) == ""


def test_long_paragraph_searches_each_chunk_once_not_the_whole_buffer(monkeypatch):
    """One paragraph without a blank line and with many hits: the search work must stay linear in the text."""
    seen = []
    real = ultracode.spans
    monkeypatch.setattr(ultracode, "spans", lambda t: seen.append(len(t)) or real(t))
    text = "\n".join(f"line {i} ultracode here" for i in range(1000))        # about 25 KB, 1000 hits
    assert marked(Md(text), width=80) == "ultracode" * 1000
    assert sum(seen) < 5 * len(text)                                          # the whole-buffer search gave ~500 x len(text)


def test_word_across_chunk_edges_is_coloured_and_a_path_before_it_is_not():
    """Gegenprobe to the short search window: the look-behind char still decides, at the start of a chunk too."""
    assert marked(Md("a" "\n" "ultracode b" "\n" "/ultracode c" "\n" "ultracode"), width=80) == "ultracode" * 2
    assert marked(Md("ultracode" "\n" "ultracode.py" "\n" "ultracode ."), width=80) == "ultracode" * 2


def test_agent_grid_without_mark_has_no_colour():
    assert marked(agent_grid("ultracode", mark=False)) == ""
    assert marked(agent_grid("ultracode")) == "ultracode"


# -- the input line --------------------------------------------------------------------------------------------------

@pytest.mark.tui
async def test_input_colours_the_word_while_typing(home):
    async with Bare(home).run_test(size=(60, 10)) as pilot:
        box = pilot.app.query_one(ChatInput)
        box.load_text("bitte ultracode nutzen")
        await pilot.pause()
        assert input_marked(box) == "ultracode"
        assert "".join(seg.text for seg in box.render_line(0)).strip() == "bitte ultracode nutzen"     # same text on screen
        box.load_text("bitte nichts nutzen, skills/ultracode/ und ultracode.py")
        await pilot.pause()
        assert input_marked(box) == ""                                                  # counter-check: no word, no colour


@pytest.mark.tui
async def test_input_colours_the_word_on_every_line_and_in_wrapped_lines(home):
    async with Bare(home).run_test(size=(14, 12)) as pilot:
        box = pilot.app.query_one(ChatInput)
        box.load_text("eins\nultracode zwei\ndrei ultracode vier")
        await pilot.pause()
        assert input_marked(box) == "ultracodeultracode"                                # two lines
        box.load_text("eine lange Zeile bitte ultracode nutzen und noch mehr")           # wraps over several rows
        await pilot.pause()
        assert box.wrapped_document.height > 2
        assert input_marked(box) == "ultracode"


@pytest.mark.tui
async def test_input_typing_keeps_cursor_and_text(home):
    async with Bare(home).run_test(size=(60, 10)) as pilot:
        box = pilot.app.query_one(ChatInput)
        box.focus()
        await pilot.press(*"ultracode")
        await pilot.pause()
        assert box.value == "ultracode" and box.cursor_position == 9
        assert input_marked(box) == "ultracode"


# -- the switch: workflow.keyword ------------------------------------------------------------------------------------

@pytest.mark.tui
async def test_keyword_off_means_no_colour_anywhere_and_on_brings_it_back(home):
    home.settings = KEYWORD_OFF
    async with Bare(home).run_test(size=(60, 10)) as pilot:
        box = pilot.app.query_one(ChatInput)
        user, agent = Msg("user", "You", "bitte ultracode nutzen"), Msg("agent", "lm", "bitte ultracode nutzen")
        await pilot.app.mount(user, agent)
        box.load_text("bitte ultracode nutzen")
        await pilot.pause()
        assert (input_marked(box), marked(user.build()), marked(agent.build())) == ("", "", "")

        home.settings = {"workflow": {"keyword": True}}                  # switched on in /config: no restart needed
        box.load_text("bitte ultracode nutzen")
        assert (input_marked(box), marked(user.build()), marked(agent.build())) == ("ultracode",) * 3

        home.settings = None                                             # the key is absent: the default, on
        assert marked(user.build()) == "ultracode"


# -- proof: what a terminal gets -------------------------------------------------------------------------------------

def test_proof_ansi_codes_around_the_word(capsys):
    user, agent = ansi(Msg("user", "You", "bitte ultracode nutzen").build()), ansi(Md("Bitte ultracode nutzen, `ultracode` nicht."))
    with capsys.disabled():
        print("\nPROOF user :", repr(user))
        print("PROOF agent:", repr(agent))
    magenta = "\x1b[1;38;2;255;79;163m"                              # bold + the truecolor of MAG #ff4fa3
    assert magenta + "ultracode" in user
    assert agent.count(magenta + "ultracode") == 1                    # the inline code one stays out
