"""protocol.py: the events of protocol v1 made in one place.  `Translator` and `history` were `Backend._event` and
`Backend._history` of serve.py; these tests pin that the output is the same as it was, and that serve still sends it."""
import json
import os
import queue
from types import SimpleNamespace

import pytest

from lightmind import model as m
from lightmind import protocol, serve, spawn
from lightmind import interject as ij
from lightmind.files import Home
from lightmind.providers.base import Interjected, Notice, SubagentEnd, SubagentStart, ToolDone
from lightmind.secrets import Masker
from lightmind.subagents import SubLog

SECRET = "sk-live-0123456789abcdefXYZ"


@pytest.fixture
def masker():
    mk = Masker()
    mk.add("elevenlabs/default", "ELEVENLABS_API_KEY", SECRET)
    return mk


def fake_session(home, masker, owns_loop=False, **extra):
    return SimpleNamespace(id="s1", home=home, provider=SimpleNamespace(owns_loop=owns_loop),
                           dispatcher=SimpleNamespace(masker=masker), **extra)


def a_turn(home):
    """A fake turn: text, a read, a write that works, a write that fails, a sub-agent, a notice, a user word fed in, an error."""
    return [m.Text("Hello "), m.Text(""),
            m.ToolCall("c1", "read", {"path": "lightmind.md"}), ToolDone("c1", "read", True, "line one\nline two"),
            m.ToolCall("c2", "write", {"path": "a/../b.md", "content": "x"}), ToolDone("c2", "write", True, "wrote b.md"),
            m.ToolCall("c3", "edit", {"path": "c.md"}), ToolDone("c3", "edit", False, "refused: no"),
            m.ToolCall("c4", "bash", {"command": "ls   -la"}), ToolDone("c4", "bash", True, ""),
            SubagentStart("tk", "reviewer", "check it"), m.Text("step", "tk", "reviewer"),
            m.ToolCall("r", "read", {"path": "plan.md"}, "tk", "reviewer"), ToolDone("r", "read", True, "ok", "tk", "reviewer"),
            SubagentEnd("tk", False, "broken"),
            Notice("limit", "five hour limit"), Notice("hook", "hook says hi"),
            Interjected(7, "also this"), Interjected(8, "ignored", "tk", "reviewer"),
            m.Error("rate_limit", "slow down")]


def legacy_event(emit, home, s, turn, ev, calls) -> bool:
    """`Backend._event` as it was before the move (the Backend method of serve.py at 1e8730e), emitting through `emit`."""
    if isinstance(ev, m.Text):
        if ev.text:
            whole = bool(getattr(s.provider, "owns_loop", False))
            emit("text", s.id, turn=turn, text=ev.text, delta=not whole, **protocol.who(ev))
    elif isinstance(ev, m.ToolCall):
        files = protocol.paths(home, ev.name, ev.args)
        calls[ev.id] = (ev.name, files)
        emit("tool.start", s.id, turn=turn, call=ev.id, name=ev.name, summary=protocol.summary(ev.name, ev.args), files=files, **protocol.who(ev))
    elif isinstance(ev, ToolDone):
        name, files = calls.pop(ev.id, (ev.name, []))
        emit("tool.end", s.id, turn=turn, call=ev.id, ok=ev.ok, summary=protocol.first_line(ev.text),
             files=files if ev.ok and name in protocol.WRITES else [], **protocol.who(ev))
    elif isinstance(ev, Interjected):
        if ev.parent:
            emit("interject", s.id, id=f"i{ev.id}", state="delivered", text="", **protocol.who(ev))
        else:
            emit("interject", s.id, id=f"i{ev.id}", state="delivered", text=ev.text)
    elif isinstance(ev, SubagentStart):
        emit("subagent.start", s.id, turn=turn, id=ev.id, name=ev.name, task=ev.task[:protocol.SUB_TASK_MAX])
    elif isinstance(ev, SubagentEnd):
        emit("subagent.end", s.id, turn=turn, id=ev.id, ok=ev.ok, summary=ev.summary)
    elif isinstance(ev, Notice):
        emit("notice", s.id, level="warn" if ev.kind in ("warning", "limit") else "info", text=ev.text)
    elif isinstance(ev, m.Error):
        emit("error", s.id, text=f"{ev.kind}: {ev.message}")
        return True
    return False


# ------------------------------------------------------------------------ the translator
def test_a_fake_turn_gives_the_events_the_serve_backend_gave_before(tree, masker):
    home = Home(tree)
    s = fake_session(home, masker)
    legacy, calls = [], {}
    emit = lambda type_, session, **f: legacy.append({"type": type_, "session": session, **f})
    for ev in a_turn(home):
        legacy_event(emit, home, s, "t1", ev, calls)
    tr = protocol.Translator(s, calls={})
    got = [e for ev in a_turn(home) for e in tr.event(ev, "t1")]
    assert got == legacy and len(got) == 19
    assert [e["type"] for e in got][:6] == ["text", "tool.start", "tool.end", "tool.start", "tool.end", "tool.start"]


def test_the_events_have_exactly_the_protocol_fields(tree, masker):
    home = Home(tree)
    tr = protocol.Translator(fake_session(home, masker), calls={})
    first = lambda ev: tr.event(ev, "t1")[0]
    assert first(m.Text("hi")) == {"type": "text", "session": "s1", "turn": "t1", "text": "hi", "delta": True}
    assert first(m.Text("hi", "tk", "reviewer")) == {"type": "text", "session": "s1", "turn": "t1", "text": "hi", "delta": True,
                                                     "parent": "tk", "agent": "reviewer"}
    read = first(m.ToolCall("c1", "read", {"path": "lightmind.md"}))
    assert read == {"type": "tool.start", "session": "s1", "turn": "t1", "call": "c1", "name": "read",
                    "summary": "Read lightmind.md", "files": [os.path.normpath(tree / "lightmind.md")]}
    assert first(ToolDone("c1", "read", True, "ok")) == {"type": "tool.end", "session": "s1", "turn": "t1", "call": "c1",
                                                         "ok": True, "summary": "ok", "files": []}      # a read names no file at its end
    tr.event(m.ToolCall("c2", "write", {"path": "b.md"}), "t1")
    assert first(ToolDone("c2", "write", True, "done"))["files"] == [os.path.normpath(tree / "b.md")]
    tr.event(m.ToolCall("c3", "write", {"path": "b.md"}), "t1")
    assert first(ToolDone("c3", "write", False, "refused: x"))["files"] == []       # a failed write changed nothing
    assert first(Notice("warning", "w")) == {"type": "notice", "session": "s1", "level": "warn", "text": "w"}
    assert first(Notice("hook", "h"))["level"] == "info"
    assert first(Interjected(3, "words")) == {"type": "interject", "session": "s1", "id": "i3", "state": "delivered", "text": "words"}
    assert first(m.Error("auth", "login")) == {"type": "error", "session": "s1", "text": "auth: login"}
    assert tr.event(m.Text(""), "t1") == [] and tr.event(m.Usage(1, 2), "t1") == []     # nothing to say


def test_a_provider_that_owns_the_loop_sends_whole_blocks(tree, masker):
    home = Home(tree)
    whole = protocol.Translator(fake_session(home, masker, owns_loop=True)).event(m.Text("block"), "t1")[0]
    delta = protocol.Translator(fake_session(home, masker, owns_loop=False)).event(m.Text("block"), "t1")[0]
    assert whole["delta"] is False and delta["delta"] is True


def test_the_translator_masks_secrets_and_the_gegenprobe_shows_the_secret_is_really_there(tree, masker):
    home = Home(tree)
    s = fake_session(home, masker)
    events = [m.Text(f"key {SECRET}"), m.ToolCall("c1", "bash", {"command": f"echo {SECRET}"}), ToolDone("c1", "bash", True, f"out {SECRET}"),
              Notice("hook", f"n {SECRET}"), m.Error("other", f"e {SECRET}"), SubagentEnd("tk", True, f"s {SECRET}")]
    masked = [e for ev in events for e in protocol.Translator(s).event(ev, "t1")]
    assert SECRET not in json.dumps(masked) and len(masked) == len(events)
    raw = [e for ev in events for e in protocol.Translator(s, mask=False).event(ev, "t1")]
    assert SECRET in json.dumps(raw)                                      # unmasked on request: the check above is not vacuous


def test_the_open_calls_table_is_shared_when_given_and_the_turn_can_be_set_once(tree, masker):
    home = Home(tree)
    calls: dict = {}
    s = fake_session(home, masker)
    protocol.Translator(s, calls=calls).event(m.ToolCall("c1", "write", {"path": "b.md"}), "t1")
    assert "c1" in calls                                                  # a second translator of the same turn sees it
    end = protocol.Translator(s, calls=calls).event(ToolDone("c1", "write", True, "ok"), "t1")[0]
    assert end["files"] == [os.path.normpath(tree / "b.md")] and calls == {}
    tr = protocol.Translator(s)
    tr.turn = "t9"
    assert tr.event(m.Text("x"))[0]["turn"] == "t9" and tr.event(m.Text("x"), "t2")[0]["turn"] == "t2"


def test_is_error_sees_only_error_events(tree, masker):
    tr = protocol.Translator(fake_session(Home(tree), masker))
    assert protocol.is_error(tr.event(m.Error("other", "x"), "t1")) is True
    assert protocol.is_error(tr.event(m.Text("x"), "t1")) is False and protocol.is_error([]) is False


# ------------------------------------------------------------------------ serve still sends the same bytes
def test_the_serve_backend_sends_what_the_translator_makes(tree, masker):
    home = Home(tree)
    out: queue.SimpleQueue = queue.SimpleQueue()
    b = serve.Backend(home, None, out)
    s = SimpleNamespace(id="s1", provider=SimpleNamespace(owns_loop=False))
    errored = [b._event(s, "t1", ev, {}) for ev in a_turn(home)]
    sent = []
    while not out.empty():
        sent.append(json.loads(out.get_nowait()))
    expect = [e for ev in a_turn(home) for e in protocol.Translator(fake_session(home, b.base_masker), mask=False, calls={}).event(ev, "t1")]
    assert sent == expect
    assert errored == [False] * 19 + [True]
    assert not any(e["type"] == "text" and e["text"] == "" for e in sent)       # an empty text says nothing


def test_serve_masks_a_secret_in_a_translated_event_at_its_own_door(tree):
    home = Home(tree)
    out: queue.SimpleQueue = queue.SimpleQueue()
    b = serve.Backend(home, None, out)
    b.base_masker.add("x/y", "KEY", SECRET)
    b._event(SimpleNamespace(id="s1", provider=SimpleNamespace(owns_loop=False)), "t1", m.Text(f"see {SECRET}"), {})
    assert SECRET not in out.get_nowait().decode()


# ------------------------------------------------------------------------ the history
def test_history_of_a_chat_with_tools_notes_and_sub_agents(tree, masker):
    home = Home(tree)
    log = SubLog()
    log.new_turn()
    for e in [SubagentStart("tk", "reviewer", "check"), m.Text("step", "tk", "reviewer"),
              m.ToolCall("r", "read", {"path": "plan.md"}, "tk", "reviewer"), ToolDone("r", "read", True, "ok\nmore", "tk", "reviewer"),
              SubagentEnd("tk", True, "fine")]:
        log.feed(e)
    framed = spawn.frame("bg job", True, "result <b>", "out/bg.md")
    msgs = [m.Message("user", f"first\n<context source=\"on_prompt\">hidden {SECRET}</context>"),
            m.Message("user", "note to the model", origin=ij.HARNESS),
            m.Message("assistant", f"I read it {SECRET}", tool_calls=[m.ToolCall("c1", "read", {"path": "lightmind.md"}),
                                                                       m.ToolCall("c2", "bash", {"command": "ls"})]),
            m.Message("tool", "line one\nline two", tool_call_id="c1"),
            m.Message("tool", "refused: not allowed", tool_call_id="c2"),
            m.Message("tool", "stray", tool_call_id="unknown"),
            m.Message("user", framed, origin="subagent"),
            m.Message("user", "second")]
    s = fake_session(home, masker, messages=msgs, subs=log)
    got = protocol.history(s)
    assert [(e["type"], e.get("turn")) for e in got] == [
        ("text", "h1"), ("text", "h1"), ("tool.start", "h1"), ("tool.start", "h1"), ("tool.end", "h1"), ("tool.end", "h1"),
        ("subagent.message", "h1"), ("text", "h2"),
        ("subagent.start", "h1"), ("text", "h1"), ("tool.start", "h1"), ("tool.end", "h1"), ("subagent.end", "h1")]
    assert got[0] == {"type": "text", "session": "s1", "turn": "h1", "text": "first", "delta": False, "role": "user"}      # the context is cut
    assert got[1]["text"] == "I read it " + masker.mask(SECRET) and "role" not in got[1]
    assert got[2]["summary"] == "Read lightmind.md" and got[2]["files"] == [os.path.normpath(tree / "lightmind.md")]
    assert got[4]["ok"] is True and got[4]["summary"] == "line one" and got[5]["ok"] is False
    assert got[6]["name"] == "bg job" and got[6]["file"] == "out/bg.md" and got[6]["summary"].startswith("result <b>")
    assert got[7] == {"type": "text", "session": "s1", "turn": "h2", "text": "second", "delta": False, "role": "user"}
    assert got[9]["parent"] == "tk" and got[11]["summary"] == "ok" and got[12]["summary"] == "fine"
    assert SECRET not in json.dumps(got)


def test_history_keeps_only_the_last_messages(tree, masker):
    msgs = [m.Message("user", f"word {i}") for i in range(protocol.HISTORY_MESSAGES + 30)]
    got = protocol.history(fake_session(Home(tree), masker, messages=msgs, subs=SubLog()))
    assert len(got) == protocol.HISTORY_MESSAGES and got[0]["text"] == "word 30" and got[-1]["text"] == f"word {len(msgs) - 1}"


def test_the_backend_history_is_the_protocol_history(tree, masker):
    b = serve.Backend(Home(tree), None, queue.SimpleQueue())
    s = fake_session(Home(tree), b.base_masker, messages=[m.Message("user", "hi")], subs=SubLog())
    assert b._history(s) == protocol.history(s) == [{"type": "text", "session": "s1", "turn": "h1", "text": "hi",
                                                       "delta": False, "role": "user"}]


# ------------------------------------------------------------------------ the small helpers
def test_mask_reaches_every_string_at_any_depth_and_leaves_the_rest(masker):
    obj = {"a": f"x {SECRET}", "n": 3, "ok": True, "none": None, "l": [f"{SECRET}", {"d": [f"y {SECRET}"]}]}
    got = protocol.mask(obj, masker)
    assert SECRET not in json.dumps(got) and got["n"] == 3 and got["ok"] is True and got["none"] is None
    assert obj["a"].endswith(SECRET)                                      # the input is not changed


def test_summary_first_line_paths_and_who(tree):
    home = Home(tree)
    assert protocol.summary("bash", {"command": "ls   -la"}) == "Bash ls -la" and protocol.summary("todo", {}) == "Todo"
    assert protocol.first_line("\n  \n  hello  \nworld") == "hello" and protocol.first_line("") == "" and len(protocol.first_line("x" * 500)) == 200
    assert protocol.paths(home, "read", {"path": "lightmind.md"}) == [os.path.normpath(tree / "lightmind.md")]
    assert protocol.paths(home, "edit", {"path": str(tree / "a" / ".." / "b.md")}) == [os.path.normpath(tree / "b.md")]
    assert protocol.paths(home, "bash", {"command": "cat lightmind.md"}) == [] and protocol.paths(home, "read", {}) == []
    assert protocol.paths(home, "read", {"path": "  "}) == [] and protocol.paths(home, "read", {"path": 5}) == []
    assert protocol.who(m.Text("x")) == {} and protocol.who(m.Text("x", "tk", "rev")) == {"parent": "tk", "agent": "rev"}


def test_sub_messages_parse_frames_or_fall_back_to_a_notice():
    framed = spawn.frame("job one", False, "went wrong", "out/one.md") + "\n" + spawn.frame("job two", True, "fine", "out/two.md")
    got = protocol.sub_messages("s1", "t4", framed)
    assert [(g["type"], g["name"], g["ok"], g["file"]) for g in got] == [("subagent.message", "job one", False, "out/one.md"),
                                                                       ("subagent.message", "job two", True, "out/two.md")]
    assert all(g["session"] == "s1" and g["turn"] == "t4" for g in got)
    (notice,) = protocol.sub_messages("s1", "t4", "<x untrusted>\nplain text\nmore")
    assert notice == {"type": "notice", "session": "s1", "turn": "t4", "level": "info", "text": "sub-agent result: plain text"}
