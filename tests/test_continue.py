"""Fixed sessions: `lm run --name`, `lm run --continue <id|name> [--create]`, names in `lm runs`, and the trigger field
`session:`.  A scripted provider instead of a model; the "other process" that has a session open is a real Python process."""
import json
import threading

import pytest

from lightmind import cli, runs
from lightmind.files import Home
from lightmind.model import Text, Usage

from test_cli import lm, script                                  # noqa: F401  (the `lm` fixture)
from test_session_lock import CHILD, other_process


def say(lm, text, *turns):
    script(lm, *(turns or ([Text(text), Usage()],)))


def run_id(out):
    return json.loads(out)["run_id"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(cli, "CONTINUE_POLL", 0.05)
    monkeypatch.setattr(cli, "CONTINUE_WAIT", 0.5)


# -------------------------------------------------------------------------- --name
def test_name_gives_the_first_run_a_name_that_runs_shows(lm):
    say(lm, "one")
    code, out, _ = lm("run", "hello", "--name", "wacht", "--json")
    rid = run_id(out)
    assert code == 0 and runs.find_name(Home(lm.tree), "WACHT") == rid
    rows = json.loads(lm("runs", "list", "--json")[1])
    assert rows[0]["name"] == "wacht"
    assert lm("runs")[1].split()[-1] == "wacht"
    assert json.loads(lm("runs", "show", "wacht", "--json")[1])["name"] == "wacht"


@pytest.mark.parametrize("bad", ["", "a b", "a/b", "ä", "x" * 65, "a;b", "a\nb"])
def test_a_bad_name_is_a_usage_error_before_anything_is_made(lm, bad):
    code, _, err = lm("run", "hello", "--name", bad)
    assert code == 2 and ("no session name" in err or "no task" in err or err) and lm.fake.requests == []
    assert json.loads(lm("runs", "list", "--json")[1]) == []


def test_the_longest_name_is_fine(lm):
    say(lm, "one")
    assert lm("run", "hello", "--name", "x" * 64)[0] == 0


def test_a_name_is_unique_also_without_regard_to_case(lm):
    say(lm, "one", [Text("one"), Usage()], [Text("two"), Usage()])
    assert lm("run", "hello", "--name", "wacht")[0] == 0
    code, _, err = lm("run", "again", "--name", "Wacht")
    assert code == 2 and "exists" in err and "--continue" in err
    assert len(json.loads(lm("runs", "list", "--json")[1])) == 1


def test_the_name_is_kept_in_the_state_folder_not_in_the_run_folder(lm):
    say(lm, "one")
    rid = run_id(lm("run", "hello", "--name", "wacht", "--json")[1])
    names = json.loads((lm.tree / "system" / "state" / "sessions" / "names.json").read_text())
    assert names == {"wacht": rid} and "wacht" not in [v for k, v in json.loads((lm.tree / "runs" / rid / "meta.json").read_text()).items() if k != "title"]    # only as the title -n sets


# -------------------------------------------------------------------------- --continue
def test_continue_goes_on_in_the_same_session_with_its_history(lm):
    d = lm.tree / "agents" / "webdev"
    d.mkdir()
    (d / "agentMaster.md").write_text("---\nname: webdev\n---\nWeb.\n", "utf-8")
    say(lm, "", [Text("first answer"), Usage()], [Text("second answer"), Usage()])
    rid = run_id(lm("run", "question one", "-a", "webdev", "--model", "haiku", "--name", "wacht", "--json")[1])
    code, out, _ = lm("run", "--continue", "wacht", "question two", "--json")
    assert code == 0 and run_id(out) == rid and json.loads(out)["text"].strip() == "second answer"
    texts = [m.text for m in lm.fake.requests[-1].messages]
    assert "question one" in texts[0] and "first answer" in " ".join(texts) and "question two" in texts[-1]
    assert lm.fake.requests[-1].model == lm.fake.requests[0].model == "haiku"
    assert "Web." in lm.fake.requests[-1].system
    assert len(json.loads(lm("runs", "list", "--json")[1])) == 1


def test_continue_takes_a_run_id_or_its_start_too(lm):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()], [Text("c"), Usage()])
    rid = run_id(lm("run", "one", "--json")[1])
    assert run_id(lm("run", "--continue", rid, "two", "--json")[1]) == rid
    assert run_id(lm("run", "--continue", rid[:22], "three", "--json")[1]) == rid


def test_continue_does_not_make_a_session_without_create(lm):
    say(lm, "x")
    code, _, err = lm("run", "--continue", "wacht", "hello")
    assert code == 2 and "no session" in err and "--create" in err and lm.fake.requests == []
    assert json.loads(lm("runs", "list", "--json")[1]) == []


def test_continue_with_create_makes_it_once_and_continues_it_after(lm):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()])
    code, out, _ = lm("run", "--continue", "wacht", "--create", "one", "--json")
    rid = run_id(out)
    assert code == 0 and runs.resolve_session(Home(lm.tree), "wacht") == rid
    assert run_id(lm("run", "--continue", "wacht", "--create", "two", "--json")[1]) == rid
    assert len(json.loads(lm("runs", "list", "--json")[1])) == 1 and len(lm.fake.requests[-1].messages) > 1


def test_create_with_a_bad_name_is_refused(lm):
    say(lm, "x")
    code, _, err = lm("run", "--continue", "a b", "--create", "hello")
    assert code == 2 and "no session name" in err and lm.fake.requests == []


def test_a_name_wins_over_a_run_id_prefix(lm):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()], [Text("c"), Usage()])
    first = run_id(lm("run", "one", "--json")[1])
    second = run_id(lm("run", "two", "--name", first[:8], "--json")[1])
    assert run_id(lm("run", "--continue", first[:8], "three", "--json")[1]) == second


def test_an_ambiguous_prefix_is_a_usage_error(lm):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()])
    lm("run", "one")
    lm("run", "two")
    code, _, err = lm("run", "--continue", "20", "three")
    assert code == 2 and "matches 2 runs" in err


def test_flags_the_session_decides_for_itself_are_named_in_a_note(lm):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()])
    lm("run", "one", "--name", "wacht", "--effort", "low")
    code, _, err = lm("run", "--continue", "wacht", "two", "--effort", "high", "--model", "opus", "--allow", "bash(x)")
    assert code == 0 and "note: --effort, --model, --allow not used" in err


def test_a_session_whose_agent_is_gone_is_a_usage_error(lm):
    d = lm.tree / "agents" / "webdev"
    d.mkdir()
    (d / "agentMaster.md").write_text("---\nname: webdev\n---\nWeb.\n", "utf-8")
    say(lm, "", [Text("a"), Usage()])
    lm("run", "one", "-a", "webdev", "--name", "wacht")
    (d / "agentMaster.md").unlink()
    d.rmdir()
    code, _, err = lm("run", "--continue", "wacht", "two")
    assert code == 2 and "does not exist" in err


# -------------------------------------------------------------------------- the lock
def test_continue_waits_for_a_session_that_is_open_elsewhere_and_then_goes_on(lm, monkeypatch):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()])
    rid = run_id(lm("run", "one", "--name", "wacht", "--json")[1])
    monkeypatch.setattr(cli, "CONTINUE_WAIT", 30)
    with other_process(CHILD, str(lm.tree), rid) as (other, first):
        assert first == "0"
        # reap it too: on Linux an ended child stays a zombie (its pid alive) until waited for
        timer = threading.Timer(0.6, lambda: (other.proc.stdin.close(), other.proc.wait(timeout=30)))
        timer.start()
        code, out, _ = lm("run", "--continue", "wacht", "two", "--json")
        timer.join()
    assert code == 0 and run_id(out) == rid and json.loads(out)["text"].strip() == "b"


def test_continue_ends_as_an_error_when_the_session_stays_open(lm):
    say(lm, "", [Text("a"), Usage()], [Text("b"), Usage()])
    rid = run_id(lm("run", "one", "--name", "wacht", "--json")[1])
    n = len(lm.fake.requests)
    with other_process(CHILD, str(lm.tree), rid):
        code, _, err = lm("run", "--continue", "wacht", "two")
    assert code == 3 and "stayed open in another process" in err and len(lm.fake.requests) == n
    assert runs.lock_holder(Home(lm.tree), rid) == 0        # we hold nothing afterwards


# -------------------------------------------------------------------------- combinations
@pytest.mark.parametrize("args", [
    ["--create"], ["--name", "n", "--continue", "m"], ["--continue", "m", "--detach"], ["--create", "--name", "n"]])
def test_senseless_combinations_are_usage_errors(lm, args):
    say(lm, "x")
    assert lm("run", "hello", *args)[0] == 2 and lm.fake.requests == []


def test_detach_with_a_name_hands_the_name_to_the_child(lm, monkeypatch):
    seen = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    code, out, _ = lm("run", "hello", "--detach", "--name", "wacht", "--json")
    assert code == 0 and seen and seen[0][seen[0].index("--name") + 1] == "wacht" and "--run-id" in seen[0]
    code, _, err = lm("run", "hello", "--detach", "--name", "a b")
    assert code == 2


# -------------------------------------------------------------------------- triggers: session:
def trigger_tool():
    import importlib.util
    from conftest import templates_dir
    spec = importlib.util.spec_from_file_location("apply_session_t", templates_dir() / "tools" / "triggers" / "apply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_trigger(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def test_a_trigger_with_session_continues_a_named_session(tmp_path):
    pytest.importorskip("yaml")
    apply = trigger_tool()
    write_trigger(tmp_path, "watch.md", '---\nschedule: "*/5 * * * *"\nagent: wacht\ntask: look\nsession: telegram-1\n---\n')
    (t,) = apply.load_triggers(tmp_path)
    assert not t["error"] and t["session"] == "telegram-1"
    assert apply.lm_command(tmp_path, t)[1:] == ["run", "--continue", "telegram-1", "--create", "-a", "wacht", "look"]
    assert "--detach" not in apply.lm_command(tmp_path, t)
    plain = {**t, "session": ""}
    assert "--detach" in apply.lm_command(tmp_path, plain) and "--continue" not in apply.lm_command(tmp_path, plain)
    block = apply.render_block(tmp_path, [t])
    assert "run --continue telegram-1 --create -a wacht look" in block
    assert "--continue telegram-1" in " ".join(apply.schtasks_args(tmp_path, t))


@pytest.mark.parametrize("session", ["a b", "a;b", "x" * 65, "$(id)", "a/b", "ä"])
def test_the_session_of_a_trigger_is_validated_like_the_other_fields(tmp_path, session):
    pytest.importorskip("yaml")
    apply = trigger_tool()
    write_trigger(tmp_path, "t.md", f'---\nschedule: "0 7 * * *"\nagent: ag\ntask: t\nsession: "{session}"\n---\n')
    (t,) = apply.load_triggers(tmp_path)
    assert "session" in t["error"]


def test_session_needs_agent_and_task_and_not_run(tmp_path):
    pytest.importorskip("yaml")
    apply = trigger_tool()
    write_trigger(tmp_path, "a.md", '---\nschedule: "0 7 * * *"\nrun: sync\nsession: s\n---\n')
    write_trigger(tmp_path, "b.md", '---\nschedule: "0 7 * * *"\nsession: s\n---\n')
    assert all(t["error"] for t in apply.load_triggers(tmp_path))


def test_the_trigger_loader_of_the_core_knows_session(lm):
    (lm.tree / "triggers").mkdir(exist_ok=True)
    write_trigger(lm.tree / "triggers", "w.md", '---\nwhen: telegram\nagent: ag\ntask: t\nsession: s1\n---\n')
    t = Home(lm.tree).load_trigger("w")
    assert t.session == "s1" and "session" not in t.extra
