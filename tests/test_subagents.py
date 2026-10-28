"""Sub-agents: SDK mapping (recorded messages), the log in events.jsonl and its resume, the TUI line and screen, serve events."""
import json
import queue
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, UserMessage

from lightmind import loop as L
from lightmind import model as m
from lightmind import serve, subagents
from lightmind.files import Home
from lightmind.providers import claude_sdk as C
from lightmind.providers.base import SubagentEnd, SubagentStart, ToolDone
from lightmind.tui.chat import ChatScreen, Msg
from lightmind.tui.subs import SubBar, SubScreen, bar_text

from test_loop_support import (FakeQuery, SessionHome, assistant, collect, init, make_dispatcher, request, result,
                               tool_result, tool_use, usage_dict)
from test_tui_support import FakeProv, idle, install, open_chat, quiet, running, say, until

TASK = "Check the plan for gaps in the costs."


def inner(parent: str, *blocks, usage=None) -> AssistantMessage:
    """What the CLI sends for a step of a sub-agent: an assistant message with parent_tool_use_id."""
    a = assistant(*blocks, usage=usage)
    a.parent_tool_use_id = parent
    return a


def inner_result(parent: str, id_: str, text: str) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=id_, content=[{"type": "text", "text": text}])],
                       parent_tool_use_id=parent)


def recorded():
    """A recorded stream: the main chat starts the Task tool, the sub-agent reads a file and answers."""
    return [
        init("sess-1"),
        assistant("I ask the reviewer.", tool_use("tk1", "Task", {"subagent_type": "reviewer", "description": "Check plan", "prompt": TASK}),
                  usage=usage_dict(2, 10, 100, 50)),
        inner("tk1", "I read the plan.", tool_use("r1", "Read", {"file_path": "plan.md"}), usage=usage_dict(900, 5, 0, 0)),
        inner_result("tk1", "r1", "# Plan"),
        inner("tk1", "An item is missing.", usage=usage_dict(950, 5, 0, 0)),
        tool_result("tk1", "The plan has a gap in the costs."),
        assistant("Thanks, I will add it.", usage=usage_dict(3, 8, 150, 10)),
        result("sess-1", usage_dict(5, 28, 250, 60), cost=0.01)]


@pytest.fixture
def shome(tree):
    return SessionHome(tree)


@pytest.fixture
def prov(shome):
    d, _ = make_dispatcher(shome)
    return C.ClaudeSdkProvider(shome, "abo", d)


async def test_provider_passes_sub_agent_messages_with_parent_and_agent(prov, monkeypatch):
    monkeypatch.setattr(C, "_query", FakeQuery(recorded()))
    evs = await collect(prov.turn(request("check")))
    assert [type(e).__name__ for e in evs] == ["Text", "ToolCall", "SubagentStart", "Text", "ToolCall", "ToolDone", "Text",
                                               "ToolDone", "SubagentEnd", "Text", "Usage"]
    assert evs[2] == SubagentStart("tk1", "reviewer", TASK)
    assert evs[3] == m.Text("I read the plan.", "tk1", "reviewer")
    assert evs[4] == m.ToolCall("r1", "Read", {"file_path": "plan.md"}, "tk1", "reviewer")
    assert evs[5] == ToolDone("r1", "Read", True, "# Plan", "tk1", "reviewer")
    assert evs[7] == ToolDone("tk1", "Task", True, "The plan has a gap in the costs.")        # the main chat's own result
    assert evs[8] == SubagentEnd("tk1", True, "The plan has a gap in the costs.")
    assert evs[9] == m.Text("Thanks, I will add it.") and evs[9].parent == ""
    assert prov.context_tokens == 3 + 150 + 10                       # a sub-agent's prompt size is not the chat's context


async def test_inner_steps_neither_change_the_context_nor_the_final_text(prov, monkeypatch):
    monkeypatch.setattr(C, "_query", FakeQuery(recorded()))
    got = [e for e in await collect(prov.turn(request("check"))) if isinstance(e, m.Usage)]
    assert len(got) == 1 and got[0].input == 5                       # cost and tokens once: the result's total


def feed(events):
    log = subagents.SubLog()
    log.new_turn()
    for e in events:
        log.feed(e)
    return log


def test_sublog_collects_task_steps_and_answer():
    log = feed([SubagentStart("a", "reviewer", TASK), m.Text("I ", "a", "reviewer"), m.Text("read.", "a", "reviewer"),
                m.ToolCall("r", "Read", {}, "a", "reviewer"), SubagentEnd("a", True, "fertig"), m.Text("main text")])
    s = log.subs["a"]
    assert s.task == TASK and s.done and s.summary == "fertig"
    assert [type(x).__name__ for x in s.steps] == ["Text", "ToolCall"] and s.steps[0].text == "I read."
    assert log.current() == [s]


def test_events_jsonl_rebuilds_the_log_for_resume(tmp_path):
    lines = [{"kind": "message", "data": {"role": "user", "text": "eins"}},
             {"kind": "subagentstart", "data": {"type": "SubagentStart", "id": "x", "name": "alt", "task": "t"}},
             {"kind": "subagentend", "data": {"type": "SubagentEnd", "id": "x", "ok": True, "summary": "s"}},
             {"kind": "message", "data": {"role": "user", "text": "zwei"}},
             {"kind": "subagentstart", "data": {"id": "y", "name": "neu", "task": TASK}},
             {"kind": "text", "data": {"text": "schritt", "parent": "y", "agent": "neu"}},
             {"kind": "toolcall", "data": {"id": "r", "name": "Read", "args": {"a": 1}, "parent": "y", "agent": "neu"}},
             {"kind": "tooldone", "data": {"id": "r", "name": "Read", "ok": False, "text": "boom", "parent": "y", "agent": "neu"}},
             {"kind": "text", "data": {"text": "Hauptchat"}}]
    log = subagents.from_events(lines)
    assert [s.name for s in log.subs.values()] == ["alt", "neu"]
    assert [s.name for s in log.current()] == ["neu"]                # only the last round
    assert not log.subs["y"].done and [type(x).__name__ for x in log.subs["y"].steps] == ["Text", "ToolCall", "ToolDone"]
    assert log.subs["y"].steps[2].ok is False


# --------------------------------------------------------------- session: save and resume
def sdk_turn(self, req):
    async def gen():
        for ev in [m.Text("I ask."), m.ToolCall("tk", "Task", {"subagent_type": "reviewer", "prompt": TASK}),
                   SubagentStart("tk", "reviewer", TASK), m.Text("Loch gefunden.", "tk", "reviewer"),
                   m.ToolCall("r", "Read", {"file_path": "plan.md"}, "tk", "reviewer"),
                   ToolDone("r", "Read", True, "# Plan", "tk", "reviewer"),
                   ToolDone("tk", "Task", True, "Ein Loch."), SubagentEnd("tk", True, "Ein Loch."),
                   m.Text("Thanks."), m.Usage(10, 5)]:
            yield ev
    return gen()


@pytest.fixture
def sprov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv([sdk_turn]))


async def test_a_resumed_chat_knows_its_sub_agents_and_the_turn_text_stays_clean(home, sprov):
    s = L.open_session(home)
    async for _ in s.send("check"):
        pass
    assert s.last_result.final_text == "I ask.Thanks."                      # the sub-agent's text is not the answer of the turn
    assert [x.text for x in s.messages if x.role == "assistant"] == ["I ask.Thanks."]
    s.close()
    s2 = L.resume_session(home, s.id)
    sub = s2.subs.subs["tk"]
    assert sub.name == "reviewer" and sub.task == TASK and sub.done and sub.summary == "Ein Loch."
    assert [type(x).__name__ for x in sub.steps] == ["Text", "ToolCall", "ToolDone"]
    assert s2.subs.current() == [sub]
    s2.close()


# ------------------------------------------------------------------------------------ TUI
async def test_tui_shows_the_card_the_line_and_the_screen(home, sprov):
    async with running(home, size=(100, 40)) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "check")
        await until(pilot, lambda: any(w.kind == "stats" for w in app.chat.query(Msg)))
        await idle(pilot, app)
        await pilot.pause(0.1)
        rows = [r for w in app.chat.query(Msg) for r in w._rows()]
        assert any("◆ reviewer" in r and "Check the plan" in r for r in rows), rows        # the card: name + task
        assert any("Ein Loch." in r for r in rows)                                          # + the result
        assert not any("Loch gefunden" in r or "Read" in r for r in rows)                   # no inner steps in the main transcript
        bar = app.chat.query_one(SubBar)
        assert bar.display and bar_text(bar.subs)[0].plain == "◆ reviewer done"
        await pilot.press("ctrl+o")                                                          # one sub-agent: opens directly
        await until(pilot, lambda: isinstance(app.screen, SubScreen))
        await pilot.pause(0.2)
        ms = list(app.screen.query(Msg))
        assert (ms[0].speaker, ms[0].text) == ("lm", TASK)                                   # the main chat's task on top
        assert [(w.kind, w.speaker) for w in ms[1:4]] == [("agent", "reviewer"), ("tool", "reviewer"), ("note", "")]
        assert ms[-1].speaker == "reviewer" and ms[-1].text == "Ein Loch."                   # its answer at the bottom
        assert not app.screen.query("ChatInput")                                             # read only
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))
        await pilot.click(SubBar, offset=(4, 0))                                             # a click on the line opens it too
        await until(pilot, lambda: isinstance(app.screen, SubScreen))


def test_bar_text_marks_each_item():
    subs = [subagents.Sub("a", "reviewer"), subagents.Sub("b", "explore", done=True)]
    t, spans = bar_text(subs)
    assert t.plain == "◆ reviewer running · ◆ explore done" and spans == [(0, 18), (21, 35)]


# ---------------------------------------------------------------------------------- serve
def backend(tree):
    out = queue.SimpleQueue()
    return serve.Backend(Home(tree), None, out), out


def drain(out):
    lines = []
    while not out.empty():
        lines.append(json.loads(out.get_nowait()))
    return lines


def test_serve_events_carry_parent_and_agent(tree):
    b, out = backend(tree)
    s = SimpleNamespace(id="s1", provider=SimpleNamespace(owns_loop=True))
    for ev in [SubagentStart("tk", "reviewer", TASK), m.Text("schritt", "tk", "reviewer"),
               m.ToolCall("r", "Read", {"path": "plan.md"}, "tk", "reviewer"), ToolDone("r", "Read", True, "ok", "tk", "reviewer"),
               SubagentEnd("tk", False, "kaputt"), m.Text("Hauptchat")]:
        b._event(s, "t1", ev, {})
    got = drain(out)
    assert [g["type"] for g in got] == ["subagent.start", "text", "tool.start", "tool.end", "subagent.end", "text"]
    assert got[0]["id"] == "tk" and got[0]["name"] == "reviewer" and got[0]["task"] == TASK
    assert all(g["parent"] == "tk" and g["agent"] == "reviewer" for g in got[1:4])
    assert got[4] == {"type": "subagent.end", "session": "s1", "turn": "t1", "id": "tk", "ok": False, "summary": "kaputt"}
    assert "parent" not in got[5] and "agent" not in got[5]           # the chat's own events are unchanged


def test_serve_history_replays_the_sub_agents(tree):
    b, out = backend(tree)
    log = feed([SubagentStart("tk", "reviewer", TASK), m.Text("schritt", "tk", "reviewer"),
                SubagentEnd("tk", True, "fertig")])
    s = SimpleNamespace(id="s1", messages=[m.Message("user", "hi")], subs=log, dispatcher=SimpleNamespace(masker=b.base_masker))
    hist = b._history(s)
    assert [h["type"] for h in hist] == ["text", "subagent.start", "text", "subagent.end"]
    assert hist[2]["parent"] == "tk" and hist[3]["summary"] == "fertig"
