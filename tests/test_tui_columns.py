"""Orbit TUI, the fixed column layout of the transcript: column 1 holds only the speaker (`You`, `lm`, an agent's name,
`[lm]` on tool lines), column 2 the text; follow-up lines start exactly under the first character of their column."""
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from rich.console import Console
from textual.selection import Selection

from lightmind import model as m
from lightmind.providers.base import Notice, ToolDone
from lightmind.tui.chat import ChatScreen, Msg, speaker_name, turn_line

from test_tui_support import FakeProv, idle, install, open_chat, quiet, running, say, until

LONG = ("Okay, I have to use bash, but first I make this message artificially long to show the indentations "
        "that wrap onto the next line. Tools should be indented too")
CMD = "cd here is the bash command asdhbllllllllll llll llllllasfhhabf hbasghbshbdhB DHBFHBHBFWB FBHFBFBABF  BBAFBBA"


@dataclass
class FromText(m.Text):
    """What an event of a sub-agent would look like: the usual event plus the name of its origin."""
    agent: str = ""


@dataclass
class FromCall(m.ToolCall):
    agent: str = ""


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def lines(msg: Msg, width: int = 60) -> list[str]:
    """The block as it is drawn at a fixed width."""
    c = Console(width=width, record=True, color_system=None)
    c.print(msg.build())
    return [ln.rstrip() for ln in c.export_text().splitlines()]


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def test_you_and_lm_lines_start_in_column_2_on_every_follow_up_line():
    for kind, who in (("user", "You"), ("agent", "lm")):
        rows = lines(Msg(kind, who, LONG))
        assert len(rows) >= 3
        assert rows[0].startswith(f"{who:<3} > " + "Okay"), rows[0]       # `You > ` / `lm  > `
        assert all(indent(r) == 6 and r.strip() for r in rows[1:]), rows        # exactly under the O


def test_column_1_of_every_follow_up_line_is_empty():
    rows = lines(Msg("agent", "lm", LONG))
    assert all(r[:6] == " " * 6 for r in rows[1:])
    assert lines(Msg("user", "You", "short")) == ["You > short"]


def test_a_tool_line_has_the_marker_the_name_and_the_arguments_from_column_3():
    rows = lines(Msg("tool", "lm", "bash", CMD))
    assert rows[0].startswith("[lm]  bash > cd here"), rows[0]
    assert len(rows) >= 3
    assert all(indent(r) == len("[lm]  bash > ") and r.strip() for r in rows[1:]), rows       # under the c of `cd`
    assert "…" not in "".join(rows) and "".join(r.strip() for r in rows).count("FBHFBFBABF") == 1


def test_a_tool_result_stands_on_its_own_line_from_column_3():
    t = Msg("tool", "lm", "bash", "ls -la")
    t.done(False, "no such file")
    rows = lines(t)
    assert rows[0] == "[lm]  bash > ls -la"
    assert rows[1] == " " * 13 + "✗ no such file"
    assert t.plain == "bash > ls -la  ✗ no such file"


def test_notices_start_in_column_2_with_column_1_empty():
    rows = lines(Msg("warning", "", "this notice is quite long and has to wrap over more than one line of the screen"), 40)
    assert len(rows) >= 2
    assert rows[0].startswith(" " * 6 + "! this") and all(indent(r) >= 6 for r in rows)
    assert lines(Msg("note", "", "resumed: go on below")) == [" " * 6 + "· resumed: go on below"]


def test_column_1_is_as_wide_as_the_longest_speaker_but_at_most_12():
    assert lines(Msg("user", "You", "hi", width=10)) == ["You        > hi"]
    assert lines(Msg("agent", "researcher", "hi", width=10)) == ["researcher > hi"]
    assert lines(Msg("tool", "researcher", "bash", "ls", width=10))[0].startswith("[researcher] bash > ls")
    assert speaker_name("a-very-long-agent-name") == "a-very-long…" and len(speaker_name("a-very-long-agent-name")) == 12
    assert speaker_name("") == "lm" and speaker_name("x\ny") == "x y"


async def test_the_whole_transcript_widens_when_a_longer_speaker_name_appears(home, prov, monkeypatch):
    async with running(home, size=(80, 40)) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "hello")
        await until(pilot, lambda: any(w.kind == "agent" for w in app.chat.query(Msg)))
        await idle(pilot, app)
        assert app.chat.col == 3 and {w.width for w in app.chat.query(Msg)} == {3}
        monkeypatch.setattr(ChatScreen, "me", property(lambda self: "researcher"))       # an agent session
        await app.chat.add("agent", "long name now")
        assert app.chat.col == 10 and {w.width for w in app.chat.query(Msg)} == {10}
        await pilot.pause(0.1)
        rows = [r for w in app.chat.query(Msg) if w.kind == "user" for r in w._rows()]
        assert any(r.startswith("You        > hello") for r in rows), rows


async def test_what_is_copied_has_no_indent_filler(home, prov):
    prov.turns = [[m.Text(LONG), m.ToolCall("t1", "bash", {"command": CMD}), ToolDone("t1", "bash", True, "all fine\nsecond"),
                   Notice("warning", "w " * 60), m.Usage()]]
    async with running(home, size=(60, 40)) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await until(pilot, lambda: any(w.kind == "warning" for w in app.chat.query(Msg)))
        await idle(pilot, app)
        await pilot.press("ctrl+t")                                                  # the tool calls are folded by default
        await pilot.pause(0.2)
        by = {w.kind: w for w in app.chat.query(Msg)}
        for kind, head in (("agent", "lm  > Okay"), ("tool", "[lm]  bash > cd here"), ("warning", "! w w")):
            got, sep = by[kind].get_selection(Selection(None, None))
            rows = got.split(sep)
            assert len(rows) >= 2 and rows[0].startswith(head), rows
            assert all(r == r.lstrip() for r in rows[1:]), rows               # nothing but real text at the start of a line
        text = by["agent"].get_selection(Selection(None, None))[0]
        assert " ".join(text.split()) == "lm > " + LONG


def test_a_long_tool_result_wraps_under_column_3_and_stays_on_its_own_line():
    t = Msg("tool", "lm", "bash", "ls")
    t.done(True, "word " * 30)
    rows = lines(t, 50)
    assert rows[0] == "[lm]  bash > ls"
    assert len(rows) >= 3 and all(indent(r) == 13 for r in rows[1:]) and rows[1].strip().startswith("✓ word")


def test_the_closing_line_of_a_turn():
    assert turn_line(12, 0.04, False, 31) == "12 s · $0.04 · 31 %"
    assert turn_line(3.4, 0.0, False, 5) == "3 s · 5 %"                    # no cost (subscription): left out
    assert turn_line(65, 0.0042, True, 100) == "1 m 05 s · $0.004~ · 100 %"
    rows = lines(Msg("stats", "", "12 s · $0.04 · 31 %"), 40)
    assert len(rows) == 1 and len(rows[0]) == 40 and rows[0].endswith("31 %") and rows[0][:6] == " " * 6       # right-aligned, column 1 empty


async def test_blank_line_before_every_you_block_and_the_closing_line_after_every_turn(home, prov):
    async with running(home, size=(80, 40)) as (app, pilot):
        await open_chat(app, pilot)
        for text in ("one", "two"):
            await say(pilot, text)
            await idle(pilot, app)
            await pilot.pause(0.1)
        ms = list(app.chat.query(Msg))
        users = [w for w in ms if w.kind == "user"]
        assert len(users) == 2 and users[0].styles.margin.top == 0 and users[1].styles.margin.top == 1
        assert all(w.styles.margin.top == 0 for w in ms if w.kind != "user")      # none between lm text and tools
        stats = [w for w in ms if w.kind == "stats"]
        assert len(stats) == 2 and ms[ms.index(users[1]) - 1] is stats[0] and ms[-1] is stats[1]
        row = stats[0]._rows()[0]
        assert row.endswith(" %") and row.startswith(" " * 6) and row.split(" · ")[0].strip().endswith(" s"), row
        assert stats[0].get_selection(Selection(None, None))[0] == row.strip()          # the filler is not copied


async def test_sub_agent_events_show_their_name_in_column_1(home, prov):
    prov.turns = [[m.Text("I ask the reviewer."), m.ToolCall("t1", "agent", {"name": "reviewer"}),
                   FromText("this plan has a hole", agent="reviewer"), FromCall("t2", "read", {"path": "plan.md"}, agent="reviewer"),
                   ToolDone("t2", "read", True, "ok"), m.Text("Thanks."), m.Usage()]]
    async with running(home, size=(80, 40)) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await until(pilot, lambda: any(w.kind == "stats" for w in app.chat.query(Msg)))
        await pilot.press("ctrl+t")                                                  # the tool calls are folded by default
        await pilot.pause(0.1)
        ms = [w for w in app.chat.query(Msg) if w.kind in ("agent", "tool")]
        assert [(w.kind, w.speaker) for w in ms] == [("agent", "lm"), ("tool", "lm"), ("agent", "reviewer"), ("tool", "reviewer"), ("agent", "lm")]
        assert {w.width for w in app.chat.query(Msg)} == {8}                         # column 1 follows the longest name
        rows = [r for w in app.chat.query(Msg) for r in w._rows()]
        assert any(r.startswith("reviewer > this plan") for r in rows) and any(r.startswith("[reviewer] read > plan.md") for r in rows), rows
        assert any(r.startswith("lm       > Thanks.") for r in rows)


def picture(app) -> list[str]:
    """The transcript as drawn, with the blank line a margin makes."""
    out: list[str] = []
    for w in app.chat.query(Msg):
        out += [""] * w.styles.margin.top + w._rows()
    return out


async def test_text_picture_of_a_transcript_at_width_80(home, prov, monkeypatch):
    """The picture the user asked for; written to a file when LM_SPALTEN_ABZUG names one."""
    monkeypatch.setattr("lightmind.runs.compute_cost", lambda prices, model, tokens: (0.04 if tokens.input else 0.0, False))
    async def first(self, req):
        app.chat.turn_start -= 12                                        # a turn that took 12 s and cost $0.04
        self.context_tokens = 62_000
        for ev in [m.Text(LONG + "\n\n- a list\n- with two items\n\n```py\nprint('code stays in column 2')\n```"),
                   m.ToolCall("t1", "bash", {"command": CMD}), ToolDone("t1", "bash", True, "alles gut\nsecond line"),
                   Notice("warning", "a notice that is long enough to wrap in the second column, " * 2), m.Usage(1000, 10)]:
            yield ev

    prov.turns = [first, [m.Text("I ask the reviewer."), m.ToolCall("t2", "agent", {"name": "reviewer"}),
                          FromText("The plan has a gap in the costs.", agent="reviewer"),
                          FromCall("t3", "read", {"path": "plan.md"}, agent="reviewer"), ToolDone("t3", "read", True, "# Plan\nsecond line"),
                          m.Text("Thanks, I will add it."), m.Usage()]]
    async with running(home, size=(80, 60)) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "This here is a test")
        await until(pilot, lambda: any(w.kind == "stats" for w in app.chat.query(Msg)))
        await idle(pilot, app)
        await say(pilot, "and now the reviewer")
        await until(pilot, lambda: sum(w.kind == "stats" for w in app.chat.query(Msg)) == 2)
        await idle(pilot, app)
        await pilot.press("ctrl+t")                                                  # the tool calls are folded by default
        await pilot.pause(0.2)
        out = picture(app)
        assert out[0].startswith("You      > This here") and any(r.startswith("[lm]       bash > cd here") for r in out)
        assert [r.strip() for r in out if r.endswith(" %")] == ["12 s · $0.04~ · 31 %", "0 s · 31 %"]       # the second turn cost nothing
        assert all(r[:11].strip() in ("", "You      >", "lm       >", "[lm]", "reviewer >", "[reviewer]") for r in out if r), out      # column 1: only the speaker
        if target := os.environ.get("LM_SPALTEN_ABZUG"):
            Path(target).write_text("\n".join(out) + "\n", "utf-8")
