"""`lm open <chat id|new|agent>`: what it opens, what it refuses, and what it says.  The screen itself is replaced by a
recorder (the Orbit side is in test_tui_tabs.py)."""
import builtins

import pytest

from lightmind import cli, runs
from lightmind.tui import app as tuiapp

from test_cli import lm, script          # noqa: F401  (the `lm` fixture)


@pytest.fixture
def tty(monkeypatch):
    """A terminal on both ends, an Orbit that only records what it was asked to open, no agent run around."""
    monkeypatch.setattr(cli, "_tty", lambda stream: True)
    monkeypatch.delenv("LM_RUN", raising=False)
    monkeypatch.delenv("LM_DEPTH", raising=False)
    asked = []

    def fake_main(home, boot=None, open=None):
        asked.append({"boot": boot, "open": open})
        return fake_main.code
    fake_main.code, fake_main.asked = 0, asked
    monkeypatch.setattr(tuiapp, "main", fake_main)
    return fake_main


def make_run(home, rid, agent="chat"):
    d = home.path("runs") / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"id": "%s", "agent": "%s", "status": "done"}' % (rid, agent))
    return d


# ------------------------------------------------------------------ what it opens
def test_open_a_chat_by_its_id_or_a_unique_prefix(lm, tty):
    from lightmind.files import Home
    h = Home(lm.tree)
    make_run(h, "20261021-101010-chat-aaaa")
    make_run(h, "20261021-111111-chat-bbbb")
    assert lm("open", "20261021-101010-chat-aaaa")[0] == 0
    assert lm("open", "20261021-1111")[0] == 0
    assert [a["open"] for a in tty.asked] == [{"id": "20261021-101010-chat-aaaa"}, {"id": "20261021-111111-chat-bbbb"}]
    assert all(a["boot"] is False for a in tty.asked)                       # no start checklist in a tab


def test_open_new_with_agent_and_project_and_an_agent_name_alone(lm, tty):
    assert lm("open", "new")[0] == 0 and lm("open", "NEW", "-a", "webdev", "-p", "site")[0] == 0
    assert [a["open"] for a in tty.asked] == [{"new": {"agent": "", "project": ""}},
                                              {"new": {"agent": "webdev", "project": "site"}}]
    tty.asked.clear()
    from lightmind.files import Home
    agents = Home(lm.tree).list_agents()
    name = sorted(agents)[0]
    assert lm("open", name, "-p", "site")[0] == 0
    assert tty.asked[0]["open"] == {"new": {"agent": name, "project": "site"}}


def test_the_exit_code_of_the_screen_is_the_exit_code_of_the_command(lm, tty):
    tty.code = 1
    assert lm("open", "new")[0] == 1


# ------------------------------------------------------------------ what it refuses, with a clean message
@pytest.mark.parametrize("ref", ["x;y", "a b", "../x", ".h", "a|b", "$(id)", "ä"])
def test_an_id_or_name_with_other_characters_is_refused(lm, tty, ref):
    code, out, err = lm("open", ref)
    assert code == 2 and "only letters, digits" in err and tty.asked == []


def test_an_id_that_looks_like_an_option_is_refused_too(lm, tty):
    code, _, err = lm("open", "--", "-x")                                  # past `--` argparse hands it over; the check stops it
    assert code == 2 and "only letters, digits" in err and tty.asked == []


def test_agent_and_project_values_are_checked_the_same_way(lm, tty):
    for flag in ("-a", "-p"):
        code, _, err = lm("open", "new", flag, "x;rm")
        assert code == 2 and "only letters, digits" in err
    assert tty.asked == []


def test_an_unknown_id_is_a_clean_error(lm, tty):
    code, out, err = lm("open", "20300101-000000-chat-ffff")
    assert code == 2 and "no chat starts with '20300101-000000-chat-ffff'" in err and "Traceback" not in err
    assert tty.asked == []


def test_an_id_that_fits_several_chats_asks_for_more(lm, tty):
    from lightmind.files import Home
    h = Home(lm.tree)
    make_run(h, "20261021-101010-chat-aaaa")
    make_run(h, "20261021-101010-chat-bbbb")
    code, _, err = lm("open", "20261021-101010")
    assert code == 2 and "fits 2 chats" in err and tty.asked == []


def test_agent_and_project_only_go_with_new_or_an_agent(lm, tty):
    from lightmind.files import Home
    h = Home(lm.tree)
    make_run(h, "20261021-101010-chat-aaaa")
    code, _, err = lm("open", "20261021-101010-chat-aaaa", "-a", "webdev")
    assert code == 2 and "only go with `new`" in err and tty.asked == []


def test_it_needs_a_terminal(lm, tty, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda stream: False)
    code, _, err = lm("open", "new")
    assert code == 2 and "needs a terminal" in err and tty.asked == []


@pytest.mark.parametrize("var, value", [("LM_RUN", "20261021-000000-x-0001"), ("LM_DEPTH", "1")])
def test_a_run_an_agent_started_cannot_open_a_screen(lm, tty, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    code, _, err = lm("open", "new")
    assert code == 2 and "not for a run an agent started" in err and tty.asked == []


# ------------------------------------------------------------------ --hold: a window of lm's own waits after a failure
def test_hold_waits_for_enter_after_a_failure_only(lm, tty, monkeypatch):
    waited = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": waited.append(prompt) or "")
    assert lm("open", "new", "--hold")[0] == 0 and waited == []                # a good end closes at once
    code, _, err = lm("open", "bad;id", "--hold")
    assert code == 2 and len(waited) == 1 and "Enter" in waited[0]
    tty.code = 1
    assert lm("open", "new", "--hold")[0] == 1 and len(waited) == 2
    assert lm("open", "bad;id")[0] == 2 and len(waited) == 2                   # without --hold it never waits


def test_hold_does_not_wait_without_a_terminal(lm, tty, monkeypatch):
    monkeypatch.setattr(cli, "_tty", lambda stream: False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("waited for a terminal that is not there"))
    assert lm("open", "new", "--hold")[0] == 2


def test_the_tui_extra_missing_is_said_like_for_lm_itself(lm, tty, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "lightmind.tui.app", None)                # import fails with ModuleNotFoundError
    code, _, err = lm("open", "new")
    assert code == 2 and "needs the tui extra" in err


# ------------------------------------------------------------------ the runs helper
def test_find_runs_matches_a_prefix_and_never_the_empty_one(lm):
    from lightmind.files import Home
    h = Home(lm.tree)
    make_run(h, "20261021-101010-chat-aaaa")
    assert [p.name for p in runs.find_runs(h, "2026102")] == ["20261021-101010-chat-aaaa"]
    assert runs.find_runs(h, "") == [] and runs.find_runs(h, "nothing") == []
