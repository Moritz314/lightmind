"""The `lm` command line on the Session API, with a scripted provider instead of a model."""
import io
import json
import sys

import pytest
import yaml

from lightmind import cli
from lightmind import loop as L
from lightmind import settings
from lightmind.files import CONFIG_KEYS, parse_frontmatter
from lightmind.model import Error, Text, ToolCall, Usage
from lightmind.providers.base import Notice
from lightmind.providers.claude_sdk import NotLoggedIn

from test_loop_support import SdkFake


def set_front(tree, **over):
    """Rewrite keys of the lightmind.md frontmatter (comments are lost, fine for a test).  The keys that config.md owns
    (`CONFIG_KEYS`, e.g. run.max_parallel) go there instead, line by line: config.md wins over lightmind.md."""
    cfg = {}
    for sec, val in list(over.items()):
        if sec in CONFIG_KEYS and isinstance(val, dict):
            cfg.update({(sec, k): v for k, v in val.items() if k in CONFIG_KEYS[sec]})
            over[sec] = {k: v for k, v in val.items() if k not in CONFIG_KEYS[sec]}
    p = tree / "lightmind.md"
    meta, body = parse_frontmatter(p.read_text("utf-8"))
    meta.update(over)
    p.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body, "utf-8")
    if cfg:
        c = tree / "config.md"
        c.write_bytes(settings.edit_text(c.read_bytes().decode("utf-8"), cfg).encode("utf-8"))


@pytest.fixture
def lm(tree, monkeypatch, capsys):
    """lm(*args, stdin="") -> (exit code, stdout, stderr); `lm.fake` is the one provider all accounts get."""
    set_front(tree, on_start=[])                                   # the shipped hook runs uv: not in these tests
    fake = SdkFake([])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])

    def run(*args, stdin=""):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        capsys.readouterr()
        code = cli.main(["--home", str(tree), *args])
        out, err = capsys.readouterr()
        return code, out, err
    run.fake, run.tree = fake, tree
    return run


def script(lm, *turns):
    lm.fake.script, lm.fake._i = list(turns), 0


# ---------------------------------------------------------------------------- run
def test_run_prints_only_the_final_text_on_stdout(lm):
    script(lm, [Text("let me look"), ToolCall("t1", "read", {"path": "x"}), Text("the answer"), Usage(10, 5)])
    code, out, err = lm("run", "what is it?")
    assert (code, out) == (0, "the answer\n") and "let me look" not in out
    assert lm.fake.requests[0].messages[-1].text == "what is it?"


def test_run_takes_the_task_from_stdin_and_a_dash(lm):
    script(lm, [Text("ok"), Usage()], [Text("ok2"), Usage()])
    assert lm("run", stdin="from stdin\n")[:2] == (0, "ok\n")
    assert lm("run", "-", stdin="dash")[:2] == (0, "ok2\n")
    assert [r.messages[-1].text for r in lm.fake.requests] == ["from stdin", "dash"]


def test_run_sends_notices_and_errors_to_stderr(lm):
    script(lm, [Notice("warning", "careful"), Text("fine"), Error("overloaded", "busy"), Usage()])
    code, out, err = lm("run", "x")
    assert code == 1 and out == "fine\n" and "[warning] careful" in err and "error (overloaded): busy" in err


def test_run_json_is_one_object(lm):
    script(lm, [Text("done it"), Usage(100, 20)])
    code, out, _ = lm("run", "x", "--json")
    d = json.loads(out)
    assert code == 0 and d["status"] == "done" and d["text"] == "done it" and d["exit_reason"] == ""
    assert d["run_id"] and d["cost_usd"] > 0 and d["cost_est"] is True
    assert (lm.tree / "runs" / d["run_id"] / "meta.json").is_file(), [str(p) for p in lm.tree.rglob("meta.json")]


@pytest.mark.parametrize("turn,code,status", [
    ([Error("overloaded", "busy"), Usage()], 1, "failed"),
    ([Error("rate_limit", "slow down"), Usage()], 3, "paused"),
    ([Error("max_turns", "enough"), Usage()], 3, "paused"),
])
def test_run_exit_codes(lm, turn, code, status):
    script(lm, turn)
    got, out, err = lm("run", "x", "--json")
    assert got == code and json.loads(out)["status"] == status
    assert json.loads((lm.tree / "runs" / json.loads(out)["run_id"] / "meta.json").read_text())["status"] == status


def test_run_not_logged_in(lm):
    class Out(SdkFake):
        async def turn(self, req):
            raise NotLoggedIn("claude-abo")
            yield
    lm.fake.__class__ = Out
    code, out, err = lm("run", "x")
    assert code == 1 and out == "" and "not logged in: run `lm login claude-abo`" in err
    code, out, _ = lm("run", "x", "--json")
    d = json.loads(out)
    assert code == 1 and d["status"] == "failed" and "lm login claude-abo" in d["exit_reason"]


def test_run_usage_errors_exit_2(lm):
    assert lm("run", "x", "-a", "ghost")[0] == 2
    assert lm("run", "x", "-m", "wild")[0] == 2
    assert lm("run", "x", "--allow", "((")[0] == 2
    code, out, err = lm("run", stdin="   ")
    assert code == 2 and "empty" in err and lm.fake.requests == []


def test_run_allow_and_deny_become_session_rules_and_asks_are_denied(lm):
    script(lm, [Text("ok"), Usage()])
    code, out, _ = lm("run", "x", "--allow", "bash(git status*)", "--deny", "web(*)", "--json")
    rid = json.loads(out)["run_id"]
    rec = json.loads((lm.tree / "system" / "state" / "sessions" / f"{rid}.json").read_text())
    assert [(r["tool"], r["pattern"], r["effect"], r["origin"]) for r in rec["state"]["rules"]] == [
        ("bash", "git status*", "allow", "session:cli"), ("web", "*", "deny", "session:cli")]
    assert lm.fake.dispatcher.asker is None


def test_run_with_an_agent_and_options(lm):
    d = lm.tree / "agents" / "webdev"
    d.mkdir()
    (d / "agentMaster.md").write_text("---\nname: webdev\n---\nWeb.\n", "utf-8")
    script(lm, [Text("ok"), Usage()])
    assert lm("run", "x", "-a", "webdev", "-m", "plan", "--model", "haiku", "--effort", "high")[0] == 0
    req = lm.fake.requests[0]
    assert (req.model, req.effort) == ("haiku", "high") and "Mode: plan" in req.system and "Web." in req.system


def test_run_shows_the_on_start_lines_on_stderr(lm):
    set_front(lm.tree, on_start=[{"run": "echo vault ready", "show": True, "context": True}])
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("run", "x")
    assert out == "ok\n" and "vault ready" in err and "vault ready" in lm.fake.requests[0].state


# ---------------------------------------------------------------------------- chat
def test_chat_runs_a_scripted_conversation(lm):
    script(lm, [Text("hello the user"), Usage()],
           [ToolCall("t1", "bash", {"command": "ls -la"}), Text("listed"), Usage()])
    code, out, err = lm("chat", stdin="hi\n\nlist it\n/quit\n")
    assert code == 0
    assert "hello the user" in out and "  . bash ls -la" in out and "listed" in out
    assert [m.text for m in lm.fake.requests[1].messages if m.role == "user"] == ["hi", "list it"]
    assert "lm chat " in out


def test_chat_ends_on_ctrl_d_and_closes_the_run(lm):
    set_front(lm.tree, on_end=[{"run": "echo goodbye", "show": True}])
    code, out, _ = lm("chat", stdin="")
    assert code == 0 and "goodbye" in out
    runs = list((lm.tree / "runs").glob("*/meta.json"))
    assert json.loads(runs[0].read_text())["status"] == "done"


def test_chat_slash_commands(lm):
    code, out, err = lm("chat", stdin="/mode\n/mode plan\n/mode wild\n/effort high\n/model opus\n/account abo-1\n"
                                      "/tools\n/nonsense\n/quit\n")
    assert "mode: auto" in out and "mode: plan" in out and "effort: high" in out and "model: opus" in out
    assert "tools: " in out and "bash" in out and "unknown command /nonsense" in out
    assert "error: unknown mode 'wild'" in err


def test_chat_not_logged_in_keeps_the_loop_alive(lm):
    state = {"fail": True}

    class Flaky(SdkFake):
        async def turn(self, req):
            if state["fail"]:
                state["fail"] = False
                raise NotLoggedIn("claude-abo")
            async for ev in super().turn(req):
                yield ev
    lm.fake.__class__ = Flaky
    script(lm, [Text("back again"), Usage()])
    code, out, err = lm("chat", stdin="one\ntwo\n")
    assert code == 0 and "not logged in: run `lm login claude-abo`" in err and "back again" in out


def test_chat_ctrl_c_stops_the_turn_and_keeps_the_session(lm):
    state = {"n": 0}

    class Interrupted(SdkFake):
        async def turn(self, req):
            state["n"] += 1
            if state["n"] == 1:
                raise KeyboardInterrupt
            async for ev in super().turn(req):
                yield ev
    lm.fake.__class__ = Interrupted
    script(lm, [Text("second works"), Usage()])
    code, out, err = lm("chat", stdin="first\nsecond\n")
    assert code == 0 and "(stopped)" in err and "second works" in out


def asked_chat(lm, monkeypatch, command, stdin, mode="plan"):
    from test_loop_session import ToolCallingFake
    fake = ToolCallingFake([("bash", {"command": command})])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])
    return fake, lm("chat", "-m", mode, stdin=stdin)       # plan asks before every bash call


def test_chat_console_asker_wants_an_explicit_answer(lm, monkeypatch):
    fake, (code, out, err) = asked_chat(lm, monkeypatch, "echo hi", "go\nmaybe\n\ny\n/quit\n")
    assert code == 0 and fake.results[0].ok and "hi" in fake.results[0].text
    assert out.count("[y]es / [a]lways") == 3                         # "maybe" and the empty line asked again


def test_chat_console_asker_no_and_end_of_input_deny(lm, monkeypatch):
    fake, (code, out, err) = asked_chat(lm, monkeypatch, "echo hi", "go\nn\n")
    assert not fake.results[0].ok and "declined" in fake.results[0].denied_by.reason
    fake, (code, out, err) = asked_chat(lm, monkeypatch, "echo hi", "go\n")          # Ctrl+D at the question
    assert not fake.results[0].ok


def test_chat_console_asker_shows_the_whole_command_escaped(lm, monkeypatch):
    cmd = "echo 'one\necho \x1b[2Jtwo\nrm -rf x"                    # an open quote: the whole line is asked about
    fake, (code, out, err) = asked_chat(lm, monkeypatch, cmd, "go\nn\n", mode="auto")
    assert "shell-unparsed | echo 'one" in out and "               | echo \\x1b[2Jtwo" in out
    assert "               | rm -rf x" in out
    assert not [l for l in out.splitlines() if (l.startswith("?") or " | " in l) and "\x1b" in l]   # the question is escaped

def test_the_asker_escapes_bidi_and_zero_width_characters(lm, monkeypatch):
    cmd = "echo 'a‮b​c d"                      # open quote: the whole line is asked about
    fake, (code, out, err) = asked_chat(lm, monkeypatch, cmd, "go\nn\n", mode="auto")
    shown = [l for l in out.splitlines() if "shell-unparsed" in l or l.startswith("?")]
    assert any("a\\u{202e}b\\u{200b}c\\u{2028}d\\u{e000}" in l for l in shown)
    assert not [c for l in out.splitlines() if l.startswith("?") or " | " in l for c in l if c in "‮​ "]
    assert cli._esc("tool‮") == "tool\\u{202e}" and cli._esc("tab\there\x1b") == "tab\there\\x1b"


def test_terminal_output_loses_control_characters_but_a_pipe_keeps_the_text(lm, monkeypatch):
    evil = "ok\x1b[2J\x07\x9b31m\r done\tnow\nnext"
    script(lm, [Text(evil), Usage()])
    assert lm("run", "x")[1] == evil + "\n"                           # captured, not a terminal: as it is
    monkeypatch.setattr(cli, "_tty", lambda stream: True)
    script(lm, [Text(evil), Notice("warning", "w\x1b[31m"), Usage()])
    code, out, err = lm("run", "x")
    assert out == "ok[2J31m done\tnow\nnext\n" and "\x1b" not in out and "\x07" not in out
    assert "[warning] w[31m" in err and "\x1b" not in err
    script(lm, [Text(evil), ToolCall("t", "bash", {"command": "echo \x1b[1mhi"}), Text(evil), Usage()])
    code, out, err = lm("chat", stdin="x\n/quit\n")
    assert "\x1b" not in out and "\x9b" not in out and "\r" not in out


# ------------------------------------------------------------------------ init, update, login
def test_init_creates_a_tree_and_refuses_a_second_time(tmp_path, capsys):
    target = tmp_path / "new"
    assert cli.main(["init", str(target)]) == 0
    out = capsys.readouterr().out
    assert (target / "lightmind.md").is_file() and "created" in out and "next: lm login" in out
    assert cli.main(["init", str(target)]) == 1
    assert "already a Lightmind tree" in capsys.readouterr().err


def test_init_json_and_home_flag(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path / "t"), "init", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["root"].endswith("t") and "lightmind.md" in d["created"]


def test_init_refuses_a_dangerous_vault_repo(tmp_path, capsys):
    assert cli.main(["init", str(tmp_path / "t"), "--vault-repo=--upload-pack=x"]) == 2


def test_update_adds_what_is_new(lm):
    (lm.tree / "providers" / "README.md").unlink()
    code, out, _ = lm("update")
    assert code == 0 and "added 0 new template files" in out
    code, out, _ = lm("update", "--json")
    assert json.loads(out) == {"created": [], "build": {"status": "skipped", "detail": "LM_NO_BUILD is set"}}   # the tests never build


def test_update_without_a_tree_fails(tmp_path, capsys):
    assert cli.main(["update", str(tmp_path / "nothing")]) == 1


def test_login_error_paths(lm):
    p = lm.tree / "providers" / "claude-abo.md"
    p.write_text("\n".join(l for l in p.read_text("utf-8").splitlines() if not l.startswith("config_dir")), "utf-8")
    code, out, err = lm("login", "claude-abo")
    assert code == 1 and "no config_dir" in err and "system/claude/claude-abo" in err
    code, out, err = lm("login", "ghost")
    assert code == 2 and "unknown account" in err


# ---------------------------------------------------------------------------- runs, rules, misc
def test_runs_list_show_cost(lm):
    script(lm, [Text("ok"), Usage(1000, 100)])
    rid = json.loads(lm("run", "x", "--json")[1])["run_id"]
    code, out, _ = lm("runs")
    assert code == 0 and rid in out and "done" in out
    rows = json.loads(lm("runs", "list", "--json")[1])
    assert rows[0]["id"] == rid
    code, out, _ = lm("runs", "show", rid[:20])
    assert code == 0 and rid in out and "events" in out
    assert json.loads(lm("runs", "show", rid, "--json")[1])["run"]["id"] == rid
    code, out, _ = lm("runs", "cost", "--by", "model")
    assert code == 0 and "total" in out
    assert json.loads(lm("runs", "cost", "--json")[1])
    assert lm("runs", "show")[0] == 2 and lm("runs", "show", "nope")[0] == 1


def test_rules_lists_every_layer_with_origin(lm):
    code, out, _ = lm("rules", "-m", "plan")
    assert code == 0 and "locked" in out and "permissions.md" in out
    assert json.loads(lm("rules", "--json")[1])[0]["layer"]


def test_sync_approve_reaches_sync_approve_with_its_arguments(lm, monkeypatch):
    from lightmind import sync
    calls = []
    monkeypatch.setattr(sync, "approve", lambda home, name, given=None: (calls.append((name, given)), [f"device {name}"])[1])
    code, out, err = lm("sync", "approve", "server")                        # no fingerprint: it only shows the device
    assert code == 0 and calls == [("server", None)] and "device server" in out
    code, out, err = lm("sync", "approve", "server", "ab12-cd34-ef56-7890-ab12")
    assert code == 0 and calls[-1] == ("server", "ab12-cd34-ef56-7890-ab12")
    for args in ((), ("a", "b", "c")):
        code, out, err = lm("sync", "approve", *args)
        assert code == 2 and "lm sync approve <name> [fingerprint]" in err
    assert len(calls) == 2                                                   # the wrong ones never got there


@pytest.mark.parametrize("status, code", [("ok", 0), ("offline", 0), ("skip", 0), ("conflict", 1), ("warn", 1), ("auth", 1)])
def test_lm_sync_exit_code_follows_the_status(lm, monkeypatch, status, code):
    from lightmind import sync
    monkeypatch.setattr(sync, "run", lambda home, **kw: sync.Result(status, ["x"], []))
    assert lm("sync")[0] == code


def test_sync_approve_on_a_tree_that_is_not_a_sync_repo_says_so(lm):
    code, out, err = lm("sync", "approve", "server")
    assert code == 2 and "not a sync repo" in err
    code, out, err = lm("sync", "add-device", "server")                      # the other commands keep their exact argument counts
    assert code == 2 and "two arguments" in err


def test_a_missing_tree_is_a_usage_error(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path / "none"), "run", "x"]) == 2
    assert "lm init" in capsys.readouterr().err


def test_no_command_starts_the_tui(lm, monkeypatch):
    import types
    seen = []
    mod = types.ModuleType("lightmind.tui.app")
    mod.main = lambda home, boot=None: seen.append(home.root) or 0
    monkeypatch.setitem(sys.modules, "lightmind.tui.app", mod)
    assert lm()[0] == 0 and seen == [lm.tree.resolve()]


def test_a_missing_tui_extra_is_one_line_and_exit_2(lm, monkeypatch):
    import builtins
    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name.endswith("tui.app"):
            raise ModuleNotFoundError("No module named 'textual'", name="textual")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    code, out, err = lm()
    assert code == 2 and len(err.strip().splitlines()) == 1 and "lm chat" in err and "tui" in err


def test_the_global_flags_work_before_and_after_the_command(lm):
    script(lm, [Text("ok"), Usage()])
    assert json.loads(lm("--json", "run", "x")[1])["text"] == "ok"
    script(lm, [Text("ok"), Usage()])
    assert json.loads(lm("run", "x", "--json")[1])["text"] == "ok"


def test_chat_streams_small_text_deltas_on_one_line(lm):
    script(lm, [Text("Hel"), Text("lo "), Text("Wor"), Text("ld"), ToolCall("t1", "bash", {"command": "ls"}),
                Text("done"), Text("!"), Usage()])
    code, out, err = lm("chat", stdin="hi\n/quit\n")
    assert code == 0
    assert "Hello World\n  . bash ls\ndone!\n" in out
