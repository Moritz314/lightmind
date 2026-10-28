"""Account switch and account order (AN-760, AN-766): lists of accounts, the weekly start switch, a fresh conversation on a
switched account, and the guard that keeps `lm update` away from a runtime that lm processes still use.  No network."""
import json
import os
import time
from pathlib import Path

import pytest

from lightmind import accountpick as P
from lightmind import cli
from lightmind import loop as L
from lightmind import runtime
from lightmind.files import Home
from lightmind.model import Text, Usage
from lightmind.providers.base import resolve_account
from lightmind.runs import read_trusted

from test_accounts import two  # noqa: F401 - four accounts: abo -> abo-2 -> api-1 -> api-2
from test_loop_session import fakes, script, shome  # noqa: F401 - fake providers per account
from test_loop_support import collect

FUTURE, PAST = time.time() + 3600, time.time() - 3600
A1, A2 = "claude-abo", "claude-abo-2"


def login(home: Home, *names: str) -> None:
    """The credentials file marks a subscription as logged in (it is never opened)."""
    for name in names:
        d = home.root / "system" / "claude" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text("{}", encoding="utf-8")


def limits(home: Home, name: str, **windows: tuple[float, float]) -> None:
    """The limits file the chats keep: window -> (utilization, reset time)."""
    state = home.path("state")
    state.mkdir(parents=True, exist_ok=True)
    rows = {k: {"utilization": u, "resets_at": r} for k, (u, r) in windows.items()}
    (state / f"limits-{name}.json").write_text(json.dumps({"seen": "now", "limits": rows}), encoding="utf-8")


# ------------------------------------------------------------------------------------ provider lists
@pytest.mark.parametrize("value, names", [("a", ["a"]), ("a, b", ["a", "b"]), (["a", "b"], ["a", "b"]), ("[a, b]", ["a", "b"]),
                                           ("", []), (None, []), ('"a", \'b\'', ["a", "b"])])
def test_names_of_reads_every_spelling_of_a_provider(value, names):
    assert P.names_of(value) == names
    assert P.names_of(P.as_text(value)) == names                       # the text a file stores reads back the same


def test_a_list_takes_the_first_account_that_is_free(two):
    login(two, A1, A2)
    assert resolve_account(two, agent_provider=f"{A1}, {A2}") == A1
    limits(two, A1, five_hour=(0.95, FUTURE))
    assert resolve_account(two, agent_provider=[A1, A2]) == A2          # the counter-probe: the first one is full now
    assert resolve_account(two, requested=f"{A1},{A2}") == A2           # `--account a,b` and `/account a,b`


def test_when_no_account_of_the_list_is_free_the_first_one_runs_into_its_limit(two):
    login(two, A1, A2)
    limits(two, A1, seven_day=(0.97, FUTURE))
    limits(two, A2, five_hour=(0.99, FUTURE))
    assert resolve_account(two, agent_provider=f"{A1}, {A2}") == A1


def test_a_window_that_has_reset_does_not_count(two):
    login(two, A1, A2)
    limits(two, A1, five_hour=(0.99, PAST))
    assert resolve_account(two, agent_provider=f"{A1}, {A2}") == A1


def test_an_account_that_is_not_logged_in_is_skipped(two):
    login(two, A2)
    assert resolve_account(two, agent_provider=f"{A1}, {A2}") == A2


def test_an_unknown_name_in_a_list_is_an_error_not_skipped(two):
    login(two, A1)
    with pytest.raises(ValueError, match="ghost"):
        resolve_account(two, agent_provider=f"{A1}, ghost")


def test_one_name_is_taken_as_it_is_even_when_it_is_full(two):
    login(two, A1, A2)
    limits(two, A1, five_hour=(0.99, FUTURE))
    assert resolve_account(two, agent_provider=A1) == A1
    assert resolve_account(two, agent_provider=[A1]) == A1


def test_agent_project_and_defaults_may_hold_a_list(two):
    login(two, A1, A2)
    limits(two, A1, five_hour=(0.99, FUTURE))
    agent = two.root / "agents" / "pair" / two.filename("agent")
    agent.parent.mkdir(parents=True)
    agent.write_text(f"---\nname: pair\nprovider: [{A1}, {A2}]\n---\nbody\n", encoding="utf-8")
    assert two.load_agent("pair").provider == f"{A1}, {A2}"
    proj = two.root / "projects" / "duo" / two.filename("project_memory")
    proj.parent.mkdir(parents=True)
    proj.write_text(f"---\nprovider: [{A1}, {A2}]\n---\nmemory\n", encoding="utf-8")
    assert two.load_project("duo").provider == f"{A1}, {A2}"
    assert L.open_session(two, agent="pair").info.account == A2        # the first account is full: the second one
    assert L.open_session(two, project="duo").info.account == A2
    cfg = two.root / "config.md"
    cfg.write_text(cfg.read_text("utf-8").replace("provider: claude-abo ", f"provider: [{A1}, {A2}]"), encoding="utf-8")
    assert L.open_session(Home(two.root)).info.account == A2


# ------------------------------------------------------------------------------------ the weekly start switch
def test_a_new_chat_starts_on_the_fallback_when_the_week_is_over_95_percent(two):
    login(two, A1, A2)
    limits(two, A1, seven_day=(0.96, FUTURE))
    assert L.open_session(two).info.account == A2
    limits(two, A1, seven_day=(0.94, FUTURE))
    assert L.open_session(two).info.account == A1                      # the counter-probe: under 95 % nothing moves
    limits(two, A1, seven_day=(0.97, PAST))
    assert L.open_session(two).info.account == A1                      # a week that has reset does not count


def test_an_account_asked_for_by_name_or_pinned_by_an_agent_stays(two):
    login(two, A1, A2)
    limits(two, A1, seven_day=(0.99, FUTURE))
    assert L.open_session(two, account=A1).info.account == A1
    agent = two.root / "agents" / "pinned" / two.filename("agent")
    agent.parent.mkdir(parents=True)
    agent.write_text(f"---\nname: pinned\nprovider: {A1}\n---\nbody\n", encoding="utf-8")
    assert L.open_session(two, agent="pinned").info.account == A1


def test_the_start_switch_skips_a_fallback_that_is_over_too_or_not_logged_in(two):
    login(two, A1)
    limits(two, A1, seven_day=(0.99, FUTURE))
    assert P.start_account(two, A1, [A2]) == A1                         # abo-2 is not logged in
    login(two, A2)
    limits(two, A2, seven_day=(0.98, FUTURE))
    assert P.start_account(two, A1, [A2]) == A1                         # abo-2 is over as well: nothing better
    limits(two, A2, seven_day=(0.5, FUTURE))
    assert P.start_account(two, A1, [A2]) == A2


# ------------------------------------------------------------------------------------ fresh conversation on the new account
async def test_a_switch_leaves_no_conversation_id_of_the_old_account_on_disk(shome, fakes):
    s = L.open_session(shome)
    fakes["abo"].session_id = "sdk-of-abo"
    script(fakes["abo"], [Text("a1"), Usage()])
    await collect(s.send("q1"))
    rec = read_trusted(shome, s.id)
    assert (rec["account"], rec["sdk_session_id"]) == ("abo", "sdk-of-abo")        # no switch: the id stays
    s.set(account="abo2")
    rec = read_trusted(shome, s.id)
    assert (rec["account"], rec["sdk_session_id"], rec["sdk_account"]) == ("abo2", "", "abo2")
    s.close()
    again = L.resume_session(shome, s.id)                                          # `lm run --continue` / the bot opens it again
    assert again.account == "abo2" and again.provider.session_id == ""


@pytest.mark.parametrize("rec, account, want", [
    ({"sdk_session_id": "x", "sdk_account": "abo"}, "abo", "x"),
    ({"sdk_session_id": "x", "sdk_account": "abo"}, "abo2", ""),
    ({"sdk_session_id": "x"}, "abo2", "x"),                                        # written before the field existed: trusted
    ({"sdk_session_id": "", "sdk_account": "abo"}, "abo", "")])
def test_a_conversation_id_is_only_used_by_the_account_that_made_it(rec, account, want):
    assert L._own_sdk_session(rec, account) == want


async def test_the_switched_account_gets_the_history_in_its_first_request(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a1"), Usage()])
    await collect(s.send("q1"))
    s.set(account="abo2")
    script(fakes["abo2"], [Text("a2"), Usage()])
    await collect(s.send("q2"))
    assert [(m.role, m.text) for m in fakes["abo2"].requests[0].messages] == [("user", "q1"), ("assistant", "a1"), ("user", "q2")]


# ------------------------------------------------------------------------------------ an agent with a list moves on within it
async def test_an_agent_with_a_list_moves_to_the_next_free_account_of_the_list(two):
    login(two, A1, A2)
    agent = two.root / "agents" / "pair" / two.filename("agent")
    agent.parent.mkdir(parents=True)
    agent.write_text(f"---\nname: pair\nprovider: [{A1}, {A2}]\n---\nbody\n", encoding="utf-8")
    s = L.open_session(two, agent="pair")
    assert L._fallback(s.dispatcher, s.run) == A2
    limits(two, A2, five_hour=(0.99, FUTURE))
    assert L._fallback(s.dispatcher, s.run) == ""                                   # the counter-probe: the other one is full
    pinned = two.root / "agents" / "one" / two.filename("agent")
    pinned.parent.mkdir(parents=True)
    pinned.write_text(f"---\nname: one\nprovider: {A1}\n---\nbody\n", encoding="utf-8")
    t = L.open_session(two, agent="one")
    assert L._fallback(t.dispatcher, t.run) == ""                                   # a single fixed account still pauses


# ------------------------------------------------------------------------------------ lm update and the runtime in use
def old_tree(tmp_path: Path, launcher: bytes) -> Home:
    system = tmp_path / "system"
    (system / f"runtime-{runtime._tag()}").mkdir(parents=True)
    (system / "lm.cmd").write_bytes(launcher)
    (system / "lm").write_bytes(launcher)
    (tmp_path / "lightmind.md").write_text("---\npaths: {system: system/, state: system/state/}\n---\n", encoding="utf-8")
    return Home(tmp_path)


def test_an_old_launcher_and_a_process_in_the_legacy_runtime_block_the_rebuild(tmp_path, monkeypatch):
    home = old_tree(tmp_path, b"@echo off\r\nrem builds system\\runtime-windows in place\r\n")
    monkeypatch.setattr(runtime, "_process_table", lambda: [(os.getpid(), 1, "me"), (4711, 1, str(runtime.legacy_dir(home)) + os.sep + "lm")])
    assert runtime.rebuild_blockers(home) == [4711]
    assert runtime.other_users(runtime.legacy_dir(home)) == [4711]                  # this process is not counted
    monkeypatch.setattr(runtime, "_process_table", lambda: [(os.getpid(), 1, "me")])
    assert runtime.rebuild_blockers(home) == []                                     # nobody uses it: go ahead


def test_a_new_launcher_never_blocks_because_it_keeps_every_build(tmp_path, monkeypatch):
    home = old_tree(tmp_path, b"echo runtimes side by side\n")
    monkeypatch.setattr(runtime, "_process_table", lambda: [(4711, 1, str(runtime.legacy_dir(home)) + os.sep + "lm")])
    assert runtime.rebuild_blockers(home) == []


def test_lm_update_refuses_with_a_clear_message_before_it_changes_anything(tmp_path, monkeypatch, capsys):
    home = old_tree(tmp_path, b"old launcher without the folder name\n")
    monkeypatch.setattr(runtime, "_process_table", lambda: [(4711, 1, str(runtime.legacy_dir(home)) + os.sep + "lm")])
    monkeypatch.setattr(runtime, "build_now", lambda h: pytest.fail("the build must not start"))
    code = cli.main(["--home", str(tmp_path), "update"])
    assert code == 2 and "4711" in capsys.readouterr().err
    assert cli.main(["--home", str(tmp_path), "update", "--no-build"]) != 2         # without a build nothing is deleted
