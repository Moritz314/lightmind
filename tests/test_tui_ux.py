"""Orbit TUI operation: tool calls folded into one block, a status line that takes keys, the mode key, the key overview."""
import asyncio

import pytest
from textual.binding import Binding
from textual.widgets import TextArea

from lightmind import model as m
from lightmind.providers.base import ToolDone
from lightmind.tui import toolgroup
from lightmind.tui.chat import ChatScreen, Msg, ToolHead
from lightmind.tui.chatinput import NEWLINE, ChatInput
from lightmind.tui.keys import KeysScreen
from lightmind.tui.screens import Pick
from lightmind.tui.statusbar import StatusBar

from test_tui_support import FakeProv, edit, idle, install, open_chat, plain, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def calls(*specs):
    """Events of one turn: (id, tool, args, ok) -> call and result, then the closing text."""
    out = [m.Text("Let me look.")]
    for i, (tool, args, ok) in enumerate(specs):
        out += [m.ToolCall(f"t{i}", tool, args), ToolDone(f"t{i}", tool, ok, "fine" if ok else "boom")]
    return out + [m.Text("Done."), m.Usage(10, 5)]


FIVE = (("Read", {"file_path": "/a/one.py"}, True), ("Read", {"file_path": "/a/two.py"}, True), ("Read", {"file_path": "/a/three.py"}, True),
        ("Bash", {"command": "rm -rf /secret/raw-command"}, True), ("Edit", {"file_path": "/a/one.py"}, True))


def shown(app):
    return [w for w in app.screen.query(Msg) if w.display]


# ------------------------------------------------------------------ 1. tool calls in one block
def test_summary_counts_in_plain_words():
    assert toolgroup.summary(["Read", "Read", "Bash", "Read", "Edit"]) == "5 tool calls: 3 files read, 1 command, 1 edit"
    assert toolgroup.summary(["Grep"]) == "1 tool call: 1 search"
    assert toolgroup.summary(["mcp__x__y", "Task", "◆ reviewer"]) == "3 tool calls: 1 other call, 2 sub-agents"
    assert toolgroup.doing("Bash") == "running a command"
    assert toolgroup.doing("Read", "x.py") == "reading x.py"


async def test_calls_of_a_turn_are_one_closed_block(home, prov):
    prov.turns = [calls(*FIVE)]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await idle(pilot, app)
        heads = list(app.screen.query(ToolHead))
        assert len(heads) == 1
        assert "5 tool calls: 3 files read, 1 command, 1 edit" in heads[0].plain
        assert not heads[0].open
        tools = [w for w in app.screen.query(Msg) if w.kind == "tool"]
        assert len(tools) == 5 and not any(w.display for w in tools)           # none of them is on the screen
        assert "raw-command" not in "".join(w.plain for w in shown(app))        # the raw command is not shown either
        assert any(w.kind == "agent" and w.text == "Done." for w in shown(app))       # the model's words stay


async def test_enter_and_click_open_the_block_and_ctrl_t_all(home, prov):
    prov.turns = [calls(*FIVE), calls(("Grep", {"pattern": "x"}, True))]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "one")
        await idle(pilot, app)
        await say(pilot, "two")
        await idle(pilot, app)
        h1, h2 = app.screen.query(ToolHead)
        h1.focus()
        await pilot.press("enter")
        assert h1.open and all(w.display for w in h1.members) and not h2.open
        await pilot.press("enter")
        assert not h1.open
        await pilot.click(ToolHead, offset=(3, 0))
        assert h1.open and isinstance(app.focused, ChatInput)                   # a click opens, the input keeps the keys
        await pilot.press("ctrl+t")
        assert h1.open and h2.open and app.screen.tools_open                    # all open (h1 already was)
        await pilot.press("ctrl+t")
        assert not h1.open and not h2.open and not any(w.display for w in h1.members + h2.members)


async def test_a_running_call_shows_its_step_and_a_failure_stays_visible(home, prov):
    gate = asyncio.Event()

    async def turn(self, req):
        yield m.ToolCall("a", "Read", {"file_path": "/x/plan.md"})
        yield m.ToolCall("b", "Bash", {"command": "false"})
        yield ToolDone("b", "Bash", False, "exit 1")
        await gate.wait()
        yield ToolDone("a", "Read", True, "ok")
        yield m.Usage()
    prov.turns = [turn]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await until(pilot, lambda: bool(list(app.screen.query(ToolHead))) and "reading plan.md" in app.screen.query_one(ToolHead).build_text())
        head = app.screen.query_one(ToolHead)
        assert "1 failed" in head.build_text()
        bad = [w for w in head.members if w.result and not w.result[0]]
        assert bad and bad[0].display                                           # the failed call is not hidden
        assert not head.members[0].display
        gate.set()
        await idle(pilot, app)
        assert "reading" not in head.build_text()


async def test_a_text_between_calls_starts_a_new_block(home, prov):
    prov.turns = [[m.ToolCall("a", "Read", {"file_path": "x"}), ToolDone("a", "Read", True, "ok"), m.Text("hmm"),
                   m.ToolCall("b", "Bash", {"command": "ls"}), ToolDone("b", "Bash", True, "ok"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await idle(pilot, app)
        assert [h.plain for h in app.screen.query(ToolHead)] == ["1 tool call: 1 file read", "1 tool call: 1 command"]


# ------------------------------------------------------------------ 2. status line
async def test_status_line_takes_keys_and_enter_opens_the_choice(home, prov):
    edit(home, 'statusline: "{server} · {model}', 'statusline: "{server} · {effort} · {model}')     # (effort is a segment where the line shows it)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        bar = app.screen.query_one(StatusBar)
        assert {"model", "mode", "account", "effort", "context"} <= set(bar.keys())
        await pilot.press("f2")
        assert app.focused is bar
        for _ in range(8):
            if bar.key == "model":
                break
            await pilot.press("right")
        assert bar.key == "model"
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        pick = app.screen
        assert pick.title_ == "model"
        want = pick.opts[(pick.c + 1) % len(pick.opts)][0]
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))
        await pilot.pause(0.2)
        chosen = app.chat.s.model
        assert chosen == want
        assert chosen in plain(app, "#status")                                  # the status line shows it at once
        assert isinstance(app.focused, ChatInput)


async def test_status_line_click_on_a_segment_and_esc(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        bar = app.screen.query_one(StatusBar)
        a, b = next((a, b) for k, a, b in bar.spans if k == "mode")
        await pilot.click(StatusBar, offset=(a + 1, 1))         # (row 0 is the border above the text)
        await until(pilot, lambda: isinstance(app.screen, Pick))
        assert app.screen.title_ == "mode"
        await pilot.press("escape")                                             # given up: the keys are back in the input
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))
        await pilot.pause(0.1)
        assert isinstance(app.focused, ChatInput)
        await pilot.press("h", "i")
        assert app.screen.query_one(ChatInput).value == "hi"
        bar.focus()
        await pilot.press("escape")                                             # Esc only gives the keys back, the chat stays
        assert isinstance(app.focused, ChatInput) and app.chat is not None


# ------------------------------------------------------------------ 3. mode key
async def test_shift_tab_cycles_the_modes(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        modes = list(__import__("lightmind.tui.data", fromlist=["x"]).modes(home))
        assert modes[:3] == ["auto", "plan", "bypass"]
        s = app.chat.s
        s.set(mode="auto")
        steps = []
        for _ in range(5):
            await pilot.press("shift+tab")
            steps.append(s.mode)
        # auto -> plan; the way into bypass asks for a second press; then bypass -> auto -> plan
        assert steps == ["plan", "plan", "bypass", "auto", "plan"]
        assert "Plan" in plain(app, "#status")                                  # status line and colour follow at once
        assert isinstance(app.focused, ChatInput)


# ------------------------------------------------------------------ 4. key overview
async def test_f1_lists_the_live_bindings(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("f1")
        await until(pilot, lambda: isinstance(app.screen, KeysScreen))
        text = " ".join(f"{h} " + " ".join(f"{k} {d}" for k, d in rows) for h, rows in app.screen.sections).lower()
        for expected in ("ctrl+n", "ctrl+t", "shift+tab", "ctrl+k", "f1", "enter", "ctrl+j", "esc"):
            assert expected in text, expected
        assert "notes & to-dos" in text
        await pilot.press("f1")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))


async def test_f1_works_on_the_landing_screen(home, prov):
    async with running(home) as (app, pilot):
        await pilot.press("f1")
        await until(pilot, lambda: isinstance(app.screen, KeysScreen))
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, KeysScreen)


def test_new_keys_collide_with_no_editing_key_of_the_input():
    taken = {k for b in TextArea.BINDINGS if isinstance(b, Binding) for k in b.key.split(",")} | set(NEWLINE) | {"enter"}
    mine = {k for b in ChatScreen.BINDINGS if isinstance(b, Binding) for k in b.key.split(",")}
    mine |= {"f1", "ctrl+underscore"}
    assert mine >= {"ctrl+t", "shift+tab", "f2", "ctrl+k"}
    new = {"ctrl+t", "shift+tab", "f2", "f1", "ctrl+underscore"}
    assert not (new & taken), new & taken
    plain_keys = [b[0] if isinstance(b, tuple) else b.key for b in ChatScreen.BINDINGS]
    assert len(plain_keys) == len(set(plain_keys))


async def test_tool_blocks_are_reachable_by_keyboard(home, prov):
    prov.turns = [calls(*FIVE), calls(("Grep", {"pattern": "x"}, True))]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "one")
        await idle(pilot, app)
        await say(pilot, "two")
        await idle(pilot, app)
        h1, h2 = app.screen.query(ToolHead)
        await pilot.press("f3")
        assert app.focused is h2
        await pilot.press("up")
        assert app.focused is h1
        await pilot.press("enter")
        assert h1.open and not h2.open
        await pilot.press("escape")
        assert isinstance(app.focused, ChatInput) and app.chat is not None


async def test_calls_of_another_speaker_start_a_block_of_their_own(home, prov):
    from dataclasses import dataclass

    @dataclass
    class FromCall(m.ToolCall):
        agent: str = ""
    prov.turns = [[m.ToolCall("a", "Read", {"file_path": "x"}), ToolDone("a", "Read", True, "ok"),
                   FromCall("b", "Read", {"file_path": "y"}, agent="reviewer"), ToolDone("b", "Read", True, "ok"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await idle(pilot, app)
        assert [h.speaker for h in app.screen.query(ToolHead)] == ["lm", "reviewer"]


async def test_f1_does_not_cover_a_permission_question(home, prov):
    from lightmind.tui.screens import AskModal
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        app.push_screen(AskModal("Run this?", "ls"))
        await pilot.pause(0.1)
        await pilot.press("f1")
        await pilot.pause(0.1)
        assert isinstance(app.screen, AskModal)
