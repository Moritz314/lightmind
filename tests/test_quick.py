"""`lm q` (the quick chat), `-n` (the session name) and `lm resume`: the arguments, the settings a quick chat opens with, how a
chat is found again, the launchers `lmq` and `lmr`.  The Orbit screen is replaced by a recorder (test_open.py), except in the
one test that opens a real quick chat in a pilot."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from lightmind import cli, launch, quick, resume, runs, settings
from lightmind import loop as L
from lightmind import model as m
from lightmind.files import CONFIG_KEYS, Home
from lightmind.init import init_tree, shipped_paths, update_tree, TRACK
from lightmind.tui import app as tuiapp
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen

from test_cli import lm, script          # noqa: F401  (the `lm` fixture)
from test_open import tty                # noqa: F401  (a terminal, an Orbit that only records)
from test_session_lock import locks
from test_tui_support import FakeProv, install, quiet, say, texts, until, idle


def set_quick(home, **values):
    cfg = home.root / "config.md"
    cfg.write_bytes(settings.edit_text(cfg.read_bytes().decode("utf-8"),
                                       {("quick", k): v for k, v in values.items()}).encode("utf-8"))
    home.forget(cfg)


def drop_quick(home):
    """A config.md of an older tree: no `quick:` section at all."""
    cfg = home.root / "config.md"
    text = cfg.read_bytes().decode("utf-8")
    new = settings.drop_keys(text, [("quick", k) for k in CONFIG_KEYS["quick"]], "")
    cfg.write_bytes(new.encode("utf-8"))
    home.forget(cfg)
    assert "quick" not in home.config.values


def chat(home, name="", age=0, **kw):
    """A closed chat with no model call; `age` = seconds back from now for its last activity."""
    s = L.open_session(home, **kw)
    s.close()
    if name:
        runs.claim_name(home, name, s.id)
    f = home.path("runs") / s.id / "events.jsonl"
    t = datetime.now().timestamp() - age
    os.utime(f, (t, t))
    return s.id


# ------------------------------------------------------------------ the settings of a quick chat
def test_without_a_quick_section_the_built_in_values_apply(home):
    drop_quick(home)
    assert quick.quick_settings(home) == {"model": "opus", "effort": "high", "context": "500k", "mode": "bypass"}


def test_the_shipped_config_holds_the_built_in_values(home):
    assert home.config.values["quick"] == quick.BUILTIN
    assert quick.quick_settings(home)["mode"] == "bypass"


def test_a_partial_section_fills_the_rest_from_the_built_in_values(home):
    set_quick(home, model="sonnet", effort="")             # empty = not set
    got = quick.quick_settings(home)
    assert got == {"model": "sonnet", "effort": "high", "context": "500k", "mode": "bypass"}


def test_a_full_section_wins_over_the_built_in_values(home):
    set_quick(home, model="haiku", effort="low", context="200k", mode="plan")
    assert quick.quick_settings(home) == {"model": "haiku", "effort": "low", "context": "200k", "mode": "plan"}


def test_permission_mode_is_an_alias_of_mode(home):
    drop_quick(home)
    set_quick(home, permission_mode="auto")
    assert quick.quick_settings(home)["mode"] == "auto"
    set_quick(home, mode="plan")                             # the canonical key wins
    assert quick.quick_settings(home)["mode"] == "plan"
    assert home.config.state == "ok" and not [k for k in home.config.ignored if k.startswith("quick.")]


def test_with_an_agent_only_the_flags_apply(home):
    set_quick(home, model="haiku")
    assert quick.quick_settings(home, agent="webdev") == {"model": None, "effort": None, "context": None, "mode": None}
    got = quick.quick_settings(home, agent="webdev", effort="low", context="1m")
    assert got == {"model": None, "effort": "low", "context": "1m", "mode": None}


def test_a_number_as_context_is_taken(home):
    set_quick(home, context=300000)
    assert quick.quick_settings(home)["context"] == 300000


def test_an_argument_wins_over_the_section(home):
    set_quick(home, model="haiku")
    got = quick.quick_settings(home, model="sonnet", context="1m", mode="auto", effort="max")
    assert got == {"model": "sonnet", "effort": "max", "context": "1m", "mode": "auto"}


def test_a_context_that_is_no_size_is_an_error(home):
    with pytest.raises(ValueError):
        quick.quick_settings(home, context="lots")


# ------------------------------------------------------------------ the arguments
def parse(*argv):
    return cli.build_parser().parse_args(cli.expand_flag_commands(list(argv)))


def test_q_takes_a_name_and_the_chat_flags():
    a = parse("q", "-n", "x", "--model", "sonnet", "--effort", "low", "--context", "200k", "-m", "plan", "--account", "a2",
              "-a", "ag", "-p", "pr")
    assert (a.cmd, a.name, a.model, a.effort, a.context, a.mode, a.account, a.agent, a.project) == (
        "q", "x", "sonnet", "low", "200k", "plan", "a2", "ag", "pr")
    assert parse("q").name is None and parse("q").dry_run is False


def test_the_flag_spellings_are_the_commands():
    assert parse("-q").cmd == "q" and parse("--quick", "-n", "x").name == "x"
    assert parse("--resume", "--last").cmd == "resume" and parse("-resume", "abc").ref == "abc"
    assert cli.expand_flag_commands(["--home", "/t", "--json", "-q", "-n", "x"]) == ["--home", "/t", "--json", "q", "-n", "x"]
    assert cli.expand_flag_commands(["--home=/t", "--resume"]) == ["--home=/t", "resume"]
    assert cli.expand_flag_commands(["chat", "-q"]) == ["chat", "-q"]            # only where the command stands
    assert cli.expand_flag_commands([]) == []


def test_resume_takes_a_ref_or_last():
    a = parse("resume")
    assert (a.cmd, a.ref, a.last) == ("resume", None, False)
    assert parse("resume", "mychat").ref == "mychat" and parse("resume", "--last").last is True


def test_n_names_the_session_of_chat_open_and_run():
    assert parse("chat", "-n", "x").name == "x" and parse("chat", "--name", "y").name == "y"
    assert parse("open", "new", "-n", "x").name == "x"
    assert parse("run", "task", "--name", "y").name == "y"
    assert parse("chat").name is None and parse("chat", "--context", "1m").context == "1m"


# ------------------------------------------------------------------ lm q
def test_q_opens_a_new_chat_here_with_the_quick_values(lm, tty, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert lm("q")[0] == 0
    (asked,) = tty.asked
    new = asked["open"]["new"]
    assert asked["boot"] is False and "id" not in asked["open"]
    assert (new["model"], new["effort"], new["context"], new["mode"]) == ("opus", "high", "500k", "bypass")
    assert Path(new["workdir"]) == tmp_path and "name" not in new         # named from its content later, like every chat
    assert new["account"] == "" and new["agent"] == "" and new["project"] == ""        # the usual choices stay


def test_q_reads_the_section_and_the_flags_win(lm, tty):
    set_quick(Home(lm.tree), model="sonnet", effort="medium")
    assert lm("q", "-n", "mine", "--effort", "low", "--context", "1m")[0] == 0
    new = tty.asked[0]["open"]["new"]
    assert (new["model"], new["effort"], new["context"], new["name"]) == ("sonnet", "low", "1m", "mine")


def test_q_on_an_old_tree_without_the_section_uses_the_built_in_values(lm, tty):
    drop_quick(Home(lm.tree))
    assert lm("q")[0] == 0
    assert tty.asked[0]["open"]["new"]["model"] == "opus"


def test_q_dry_run_prints_the_resolved_chat_and_opens_nothing(lm, tty):
    code, out, _ = lm("q", "--dry-run", "-n", "d1", "--model", "haiku")
    data = json.loads(out)
    assert code == 0 and tty.asked == [] and data["model"] == "haiku" and data["name"] == "d1" and data["mode"] == "bypass"


def test_q_refuses_a_bad_name_a_taken_name_and_a_bad_context(lm, tty):
    for argv, word in ((("q", "-n", "a b"), "no session name"), (("q", "--context", "lots"), "bad size"),
                       (("q", "-a", "x;y"), "only letters, digits")):
        code, _, err = lm(*argv)
        assert code == 2 and word in err, argv
    runs.claim_name(Home(lm.tree), "taken", chat(Home(lm.tree)))
    code, _, err = lm("q", "-n", "Taken")
    assert code == 2 and "exists" in err and tty.asked == []


def test_q_needs_a_terminal_and_never_runs_inside_an_agent(lm, tty, monkeypatch):
    monkeypatch.setenv("LM_RUN", "r1")
    code, _, err = lm("q")
    assert code == 2 and "not for a run an agent started" in err and tty.asked == []
    monkeypatch.delenv("LM_RUN")
    monkeypatch.setattr(cli, "_tty", lambda stream: False)
    code, _, err = lm("q")
    assert code == 2 and "needs a terminal" in err


def test_open_new_with_a_name(lm, tty):
    assert lm("open", "new", "-n", "named")[0] == 0
    assert tty.asked[0]["open"] == {"new": {"agent": "", "project": "", "name": "named", "name_fixed": True}}
    code, _, err = lm("open", "newer", "-n", "x")
    assert code == 2


def test_chat_with_a_name_names_the_session(lm):
    script(lm, [m.Text("hi"), m.Usage(1, 1)])
    code, out, err = lm("chat", "-n", "terminal-one", stdin="hello\n")
    assert code == 0, err
    assert runs.find_name(Home(lm.tree), "terminal-one")
    code, _, err = lm("chat", "-n", "terminal-one", stdin="")
    assert code == 2 and "exists" in err


def test_run_keeps_only_long_name_so_a_task_may_start_with_n(lm):
    script(lm, [m.Text("done"), m.Usage(1, 1)])
    assert lm("run", "--name", "job-1", "do it")[0] == 0 and runs.find_name(Home(lm.tree), "job-1")
    script(lm, [m.Text("done"), m.Usage(1, 1)])
    assert lm("run", "-n is the first word")[0] == 0                       # a task, not an option


# ------------------------------------------------------------------ lm resume
def test_resume_by_name_id_prefix_and_last(lm, tty):
    h = Home(lm.tree)
    old = chat(h, name="old", age=500)
    new = chat(h, name="new", age=5)
    assert lm("resume", "old")[0] == 0 and lm("resume", new)[0] == 0 and lm("resume", new[:-1])[0] == 0
    assert lm("resume", "--last")[0] == 0
    assert [a["open"]["id"] for a in tty.asked] == [old, new, new, new]
    assert all(a["boot"] is False for a in tty.asked)


def test_last_means_the_newest_activity_not_the_newest_start(lm, tty):
    h = Home(lm.tree)
    started_later = chat(h, age=900)
    started_first = chat(h, age=10)
    os.utime(h.path("runs") / started_later / "meta.json", None)
    assert resume.latest(h) == started_first
    assert [r.id for r in resume.rows(h)] == [started_first, started_later]


def test_resume_says_what_is_wrong(lm, tty):
    assert lm("resume", "--last")[2].strip().endswith("there is no chat to resume yet")
    h = Home(lm.tree)
    chat(h, name="one")
    assert "no chat is called" in lm("resume", "nothing-like-it")[2]
    assert "give a chat or --last" in lm("resume", "one", "--last")[2]
    d = h.path("runs") / "20261001-000000-job-0001"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"id": "20261001-000000-job-0001", "status": "done"}')
    (d / "events.jsonl").write_text('{"kind": "message", "data": {"role": "user", "text": "x"}}\n')
    assert "is a run, not a chat" in lm("resume", "20261001-000000-job-0001")[2]
    assert tty.asked == []


def test_resume_without_an_argument_opens_the_resume_menu_of_the_start_menu(lm, tty):
    chat(Home(lm.tree), name="first-chat", age=900)
    code, out, _ = lm("resume")
    assert code == 0 and out == ""                                            # no numbered list on the terminal any more
    assert [(a["boot"], a["open"]) for a in tty.asked] == [(False, {"screen": "resume", "label": "lm resume"})]


def test_the_list_shows_name_time_folder_and_device(home):
    wd = str(home.root / "work")
    s = L.open_session(home, workdir=wd)
    s.close()
    runs.claim_name(home, "with-folder", s.id)
    (row,) = [r for r in resume.rows(home) if r.id == s.id]
    assert (row.name, row.folder, row.server) == ("with-folder", wd, s.run.run.server)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", row.when)
    line = resume.table([row])
    assert "with-folder" in line and wd in line and row.server in line


def test_resume_dry_run_shows_the_settings_the_chat_comes_back_with(lm, tty):
    h = Home(lm.tree)
    sid = chat(h, name="cfg", model="sonnet", effort="low", mode="plan", workdir="/somewhere")
    code, out, _ = lm("resume", "cfg", "--dry-run")
    data = json.loads(out)
    assert code == 0 and tty.asked == [] and data["id"] == sid
    assert (data["model_req"], data["effort"], data["mode"], data["workdir"]) == ("sonnet", "low", "plan", "/somewhere")
    assert "sdk_session_id" in data


def test_the_list_and_last_leave_out_sub_runs_and_lm_run_runs(lm, tty):
    h = Home(lm.tree)
    mine = chat(h, name="mine", age=500)
    child = chat(h, age=1)
    deep = chat(h, age=2)
    batch = chat(h, age=3)
    for rid, patch in ((child, {"parent": "20261022-000000-chat-0000"}), (deep, {"depth": 1})):
        f = h.path("runs") / rid / "meta.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        d.update(patch)
        f.write_text(json.dumps(d), encoding="utf-8")
    (h.path("runs") / batch / "result.json").write_text("{}", encoding="utf-8")
    assert [r.id for r in resume.rows(h)] == [mine] and resume.latest(h) == mine
    assert lm("resume", "--last")[0] == 0 and tty.asked[-1]["open"]["id"] == mine
    assert lm("resume", child)[0] == 0                                      # named on purpose: it opens


def test_q_with_an_agent_leaves_model_effort_and_mode_to_the_agent(lm, tty):
    name = sorted(Home(lm.tree).list_agents())[0]
    assert lm("q", "-a", name)[0] == 0
    new = tty.asked[-1]["open"]["new"]
    assert (new["agent"], new["model"], new["effort"], new["context"], new["mode"]) == (name, "", "", "", "")
    assert lm("q", "-a", name, "--model", "haiku")[0] == 0 and tty.asked[-1]["open"]["new"]["model"] == "haiku"


# ------------------------------------------------------------------ the screen: a quick chat and a resumed one
@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


async def test_a_direct_quick_chat_takes_settings_name_and_folder_and_resume_brings_them_back(home, prov, tmp_path):
    spec = {"new": {"agent": "", "project": "", "account": "", "model": "sonnet", "effort": "low", "context": "300k",
                    "mode": "plan", "name": "quick-0001", "workdir": str(tmp_path)}, "label": "lm q"}
    app = Orbit(home, anim=False, direct=spec)
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        s = app.chat.s
        assert (s.mode, s.effort, s.context_limit, s.workdir) == ("plan", "low", 300_000, str(tmp_path))
        assert s.info.model.startswith("sonnet") or "sonnet" in s.info.model
        assert runs.find_name(home, "quick-0001") == s.id and len(app.screen_stack) == 2
        prov.turns.append([m.Text("quick answer"), m.Usage()])
        await say(pilot, "hi")
        await until(pilot, lambda: "quick answer" in " ".join(texts(app)))
        await idle(pilot, app)
        sid = s.id
        await pilot.press("escape")
        await until(pilot, lambda: app.return_code is not None)
    assert app.return_code == 0 and locks(home) == []
    app = Orbit(home, anim=False, direct={"id": runs.resolve_session(home, "quick-0001"), "label": "lm resume"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        s = app.chat.s
        assert s.id == sid and s.resumed and (s.mode, s.effort, s.context_limit, s.workdir) == ("plan", "low", 300_000, str(tmp_path))
        assert "quick answer" in " ".join(texts(app))
        await pilot.press("escape")
        await until(pilot, lambda: app.return_code is not None)


async def test_a_quick_chat_whose_name_was_taken_meanwhile_exits_with_the_reason(home, prov):
    runs.claim_name(home, "late", chat(home))
    app = Orbit(home, anim=False, direct={"new": {"name": "late"}, "label": "lm q"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: app.return_code is not None)
    assert app.return_code == 1 and any(l.startswith("lm q:") and "taken" in l for l in app.exit_lines) and locks(home) == []


# ------------------------------------------------------------------ the launchers
SYSTEM = Path(launch.__file__).parent / "templates" / "system"


@pytest.mark.parametrize("name,command", [("lmq", "q"), ("lmr", "resume")])
def test_the_launcher_templates_hand_over_to_lm(name, command):
    sh, cmd = (SYSTEM / name).read_bytes(), (SYSTEM / f"{name}.cmd").read_bytes()
    assert sh.startswith(b"#!/bin/sh\n") and b"\r" not in sh and f'/lm" {command} "$@"'.encode() in sh
    assert b"\r\n" in cmd and cmd.replace(b"\r\n", b"").count(b"\n") == 0 and cmd.isascii()    # ASCII, CRLF all through
    assert f'call "%~dp0lm.cmd" {command} %*'.encode() in cmd and cmd.rstrip().endswith(b"exit /b %ERRORLEVEL%")


def test_the_gitattributes_and_gitignore_cover_the_new_launchers():
    repo = Path(__file__).resolve().parent.parent
    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    for n, eol in (("lmq", "lf"), ("lmq.cmd", "crlf"), ("lmr", "lf"), ("lmr.cmd", "crlf")):
        assert f"src/lightmind/templates/system/{n} text eol={eol}" in attrs.splitlines()
    ignore = (SYSTEM.parent / "dot-gitignore").read_text(encoding="utf-8").splitlines()
    assert all(f"!system/{n}" in ignore for n in ("lmq", "lmq.cmd", "lmr", "lmr.cmd"))


def test_a_fresh_tree_has_the_launchers_and_update_brings_them_to_an_older_tree(tmp_path):
    fresh = tmp_path / "fresh"
    init_tree(fresh)
    names = ("lmq", "lmq.cmd", "lmr", "lmr.cmd")
    assert all((fresh / "system" / n).is_file() for n in names)
    assert all(f"system/{n}" in shipped_paths() for n in names)
    if os.name != "nt":
        assert os.access(fresh / "system" / "lmq", os.X_OK)
    # a tree made before: no lmr, lmq written by hand (it stays), the record knows neither of them
    old = tmp_path / "old"
    init_tree(old)
    for n in names:
        (old / "system" / n).unlink()
    (old / "system" / "lmq.cmd").write_bytes(b'@echo off\r\ncall "%~dp0lm.cmd" q %*\r\n')
    rec = old / "system" / "state" / TRACK
    data = json.loads(rec.read_text(encoding="utf-8"))
    data["shipped"] = [p for p in data["shipped"] if p.rsplit("/", 1)[-1] not in names]
    rec.write_text(json.dumps(data), encoding="utf-8")
    created = update_tree(old)
    assert sorted(created) == ["system/lmq", "system/lmr", "system/lmr.cmd"]
    assert (old / "system" / "lmq.cmd").read_bytes() == b'@echo off\r\ncall "%~dp0lm.cmd" q %*\r\n'      # his own file is left alone
    assert (old / "system" / "lmr.cmd").read_bytes() == (SYSTEM / "lmr.cmd").read_bytes()
    assert set(json.loads(rec.read_text(encoding="utf-8"))["shipped"]) >= {f"system/{n}" for n in names}


def test_resume_dry_run_without_an_argument_names_the_menu_and_opens_nothing(lm, tty):
    code, out, _ = lm("resume", "--dry-run")
    assert code == 0 and json.loads(out) == {"screen": "resume"} and tty.asked == []
