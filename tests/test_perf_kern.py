"""Changes of the opt-kern round: history cache breakpoint, own to-do changes not echoed."""
import asyncio

from lightmind import todos as T
from lightmind.model import Message
from lightmind.providers.anthropic_api import mark_history, to_api_messages

from test_loop_session import fakes, script, shome  # noqa: F401  (fixtures)
from test_loop_support import make_dispatcher


def test_history_gets_a_rolling_cache_breakpoint_on_the_second_to_last_message():
    msgs = to_api_messages([Message("user", "go"), Message("assistant", "ok"), Message("user", "next")])
    mark_history(msgs)
    assert msgs[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}      # second-to-last, last block
    assert msgs[0]["content"] == "go"
    assert msgs[-1]["content"] == "next"                                         # the newest message stays unmarked


def test_single_message_gets_no_breakpoint():
    msgs = to_api_messages([Message("user", "go")])
    mark_history(msgs)
    assert msgs == [{"role": "user", "content": "go"}]


def test_todo_tool_changes_by_the_agent_are_not_told_again(tree):
    from lightmind.files import Home
    d, run = make_dispatcher(Home(tree))
    s = T.Store(run.path)
    s.add_task("first")
    _, told = T.context_block(s)
    state = {"digest": told}

    def note(before, after):
        if before == state["digest"]:
            state["digest"] = after
    d.todo_told = note
    asyncio.run(d._b_todo("add_task", title="by agent", info="found"))
    assert T.context_block(T.Store(run.path), state["digest"])[0] == ""          # agent knows its own change
    T.Store(run.path).add_task("by user")
    assert "by user" in T.context_block(T.Store(run.path), state["digest"])[0]  # an outside change still comes


# ----------------------------------------------------------- B1: loaded once, reloaded on change
def test_permissions_are_cached_but_an_edit_takes_effect_at_once(tree):
    import os
    from lightmind import rules as R
    from lightmind.files import Home
    from lightmind.model import Check, RuleContext
    home = Home(tree)
    ctx = RuleContext(home=str(tree), run_dir=str(tree / "runs" / "r1"))
    first = R.defaults_layers(home, "auto")
    again = R.defaults_layers(home, "auto")
    assert [l.rules for l in first] == [l.rules for l in again]
    assert first[0].rules is not again[0].rules                       # callers may extend their copy
    assert R.decide(first, Check("bash", "echo-lm-edit-probe"), ctx).effect != "deny"
    path = tree / "permissions.md"
    text = path.read_text("utf-8")
    marker = "\ndeny:\n"
    assert marker in text
    path.write_text(text.replace(marker, marker + "  - bash(echo-lm-edit-probe*)\n", 1), "utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))      # a coarse clock must not hide the edit
    after = R.defaults_layers(Home(tree), "auto")
    assert R.decide(after, Check("bash", "echo-lm-edit-probe"), ctx).effect == "deny"
    assert R.decide(R.defaults_layers(home, "auto"), Check("bash", "echo-lm-edit-probe"), ctx).effect == "deny"


def test_secret_masker_follows_changed_secret_files(tree):
    from lightmind.files import Home
    from lightmind.tools import secret_masker
    home = Home(tree)
    d = tree / "secrets" / "probe"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "one.env"
    f.write_text("# h\n# --- END HEADER ---\nPROBE_TOKEN=aaaaaaaa-first-value\n", "utf-8")
    assert "aaaaaaaa-first-value" not in secret_masker(home).mask("x aaaaaaaa-first-value y")
    f.write_text("# h\n# --- END HEADER ---\nPROBE_TOKEN=bbbbbbbb-second-value-longer\n", "utf-8")
    m = secret_masker(home)
    assert "bbbbbbbb-second-value-longer" not in m.mask("x bbbbbbbb-second-value-longer y")
    f.unlink()
    assert "bbbbbbbb-second-value-longer" in secret_masker(home).mask("x bbbbbbbb-second-value-longer y")


# ----------------------------------------------------------------------------- B2
def test_read_pages_a_big_file_without_loading_it_and_edit_refuses_giants(tree, monkeypatch):
    from lightmind import tools as TL
    from lightmind.files import Home
    d, run = make_dispatcher(Home(tree))
    big = tree / "projects" / "big.log"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("".join(f"line {i}\n" for i in range(5000)), "utf-8")
    r = asyncio.run(d._b_read(str(big), offset=10, limit=2))
    assert "11\tline 10" in r.text and "12\tline 11" in r.text and "[5000 lines total; continue with offset=12]" in r.text
    monkeypatch.setattr(TL, "EDIT_MAX_BYTES", 100)
    r = asyncio.run(d._b_edit(str(big), "line 1\n", "x"))
    assert not r.ok and "larger than" in r.text


async def test_a_failing_save_after_a_turn_does_not_hide_it_and_is_told_next_time(shome, fakes, monkeypatch):
    from lightmind import loop as L
    from lightmind.model import Text, Usage
    from lightmind.providers.base import Notice
    from test_loop_support import collect
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), Usage()], [Text("b"), Usage()])
    real = L.Session._persist
    monkeypatch.setattr(L.Session, "_persist", lambda self: (_ for _ in ()).throw(OSError("disk full")))
    evs = await collect(s.send("one"))
    assert Text("a") in evs                                   # the turn's answer arrived, no exception
    monkeypatch.setattr(L.Session, "_persist", real)
    evs = await collect(s.send("two"))
    assert any(isinstance(e, Notice) and "could not save the session" in e.text for e in evs)
