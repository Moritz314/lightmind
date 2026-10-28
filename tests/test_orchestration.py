"""Phase 2 core: runs that start runs (detach, wait, limits, schema, depth, slots, ask channel) and
templates/workflows/lib.py.  No model is called: scripted fake providers, or a provider that cannot connect."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lightmind import cli, runs
from lightmind import loop as L
from lightmind.model import Error, Run, Text, ToolCall, Usage
from lightmind.providers.fake import FakeProvider
from lightmind.schema import check_answer, validate

from test_cli import lm, script, set_front        # noqa: F401  (the `lm` fixture)
from test_loop_session import ToolCallingFake


def last(lm):
    return json.loads(lm("runs", "list", "-n", "1", "--json")[1])[0]


# ------------------------------------------------------------------------------ schema
SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"},
          "tags": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}


def test_validator_checks_required_types_items_enum():
    assert validate({"a": 1, "tags": ["x"]}, SCHEMA) == []
    assert validate({}, SCHEMA) == ["$.a: required"]
    assert "expected integer" in validate({"a": "1"}, SCHEMA)[0]
    assert validate({"a": True}, SCHEMA)                       # a bool is no integer
    assert "$.tags[1]" in validate({"a": 1, "tags": ["x", 2]}, SCHEMA)[0]
    assert validate({"a": 1, "b": 2}, SCHEMA) == ["$.b: not allowed"]
    assert validate("x", {"enum": ["y"]}) and validate(None, {"type": ["null", "string"]}) == []


def test_extract_json_from_fence_or_prose():
    assert check_answer('```json\n{"a": 1}\n```', SCHEMA) == ({"a": 1}, [])
    assert check_answer('Here: {"a": 2} done', SCHEMA)[0] == {"a": 2}
    assert check_answer("no json", SCHEMA)[0] is None


def schema_file(lm, schema=SCHEMA):
    p = lm.tree / "s.json"
    p.write_text(json.dumps(schema), "utf-8")
    return str(p)


def test_schema_reprompts_then_passes(lm):
    script(lm, [Text("nope"), Usage()], [Text('{"a": "x"}'), Usage()], [Text('{"a": 1}'), Usage()])
    code, out, _ = lm("run", "give json", "--schema", schema_file(lm), "--json")
    d = json.loads(out)
    assert code == 0 and d["result"] == {"a": 1} and len(lm.fake.requests) == 3
    assert "valid against this JSON schema" in lm.fake.requests[0].messages[-1].text
    assert "$.a: expected integer" in lm.fake.requests[2].messages[-1].text


def test_schema_fails_after_three_corrections(lm):
    script(lm, *[[Text("bad"), Usage()]] * 6)
    code, out, err = lm("run", "x", "--schema", schema_file(lm), "--json")
    d = json.loads(out)
    assert code == 1 and (d["status"], d["exit_reason"]) == ("failed", "schema") and d["result"] is None
    assert len(lm.fake.requests) == 4 and "schema" in err


def test_schema_file_errors_are_usage_errors(lm):
    assert lm("run", "x", "--schema", str(lm.tree / "missing.json"))[0] == 2


# ------------------------------------------------------------------------------- limits
def api_fake(monkeypatch, turns):
    fake = FakeProvider(turns)
    fake.dispatcher = None
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])
    return fake


def busy(n=6, tokens=1_000_000):
    return [[ToolCall(f"c{i}", "bash", {"command": "echo hi"}), Usage(tokens, 0)] for i in range(n)]


def test_max_usd_stops_between_turns(lm, monkeypatch):
    fake = api_fake(monkeypatch, busy())
    code, out, _ = lm("run", "x", "--max-usd", "0.0001", "--json")
    d = json.loads(out)
    assert code == 3 and d["status"] == "budget" and d["exit_reason"] == "$0.0001" and len(fake.requests) == 1


def test_without_max_usd_the_same_run_goes_on(lm, monkeypatch):
    fake = api_fake(monkeypatch, busy(3))
    lm("run", "x", "--json")
    assert len(fake.requests) == 4                              # three tool turns, then the empty answer


def test_max_turns_pauses(lm, monkeypatch):
    fake = api_fake(monkeypatch, busy(8, 1))
    code, out, _ = lm("run", "x", "--max-turns", "2", "--json")
    assert code == 3 and json.loads(out)["exit_reason"] == "max_turns" and len(fake.requests) == 2


def test_an_owning_backend_gets_the_money_limit_and_its_budget_error_is_a_budget(lm):
    script(lm, [Error("budget", "cap"), Usage()])
    code, out, _ = lm("run", "x", "--max-usd", "1.5", "--json")
    assert code == 3 and json.loads(out)["status"] == "budget" and lm.fake.requests[0].max_usd == 1.5


# ------------------------------------------------------------------------- depth, parent
def test_depth_is_refused_beyond_max_depth_and_recorded(lm, monkeypatch):
    script(lm, [Text("ok"), Usage()])
    monkeypatch.setenv("LM_DEPTH", "4")
    code, _, err = lm("run", "x")
    assert code == 2 and "max_depth" in err and lm.fake.requests == []
    monkeypatch.setenv("LM_DEPTH", "3")
    monkeypatch.setenv("LM_PARENT", "20260101-000000-lead-abcd")
    assert lm("run", "x")[0] == 0
    meta = last(lm)
    assert (meta["depth"], meta["parent"]) == (3, "20260101-000000-lead-abcd") and meta["pid"] == os.getpid()


def test_tools_of_a_run_get_depth_plus_one_and_the_run_as_parent(lm, monkeypatch):
    fake = ToolCallingFake([("bash", {"command": "echo $LM_DEPTH:$LM_PARENT"})])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])
    monkeypatch.setenv("LM_DEPTH", "1")
    assert lm("run", "x")[0] == 0
    assert fake.results[0].text.strip() == f"2:{last(lm)['id']}"


# ------------------------------------------------------------------------------- slots
def test_slots_are_counted_across_processes_and_dead_ones_are_freed(home):
    assert runs.claim_slot(home, "a", 2) and runs.claim_slot(home, "b", 2)
    assert not runs.claim_slot(home, "c", 2)
    runs.release_slot(home, "a")
    assert runs.claim_slot(home, "c", 2)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (home.path("state") / "slots" / "b.slot").write_text(str(dead.pid))      # a process that died holding a slot
    assert runs.claim_slot(home, "d", 2) and not (home.path("state") / "slots" / "b.slot").exists()


def test_a_run_without_a_free_slot_ends_paused(lm, monkeypatch):
    set_front(lm.tree, on_start=[], run={"max_parallel": 1, "slot_timeout": "1s", "max_depth": 3})
    monkeypatch.setattr(cli, "SLOT_POLL", 0.05)
    from lightmind.files import Home
    assert runs.claim_slot(Home(lm.tree), "other", 1)
    script(lm, [Text("ok"), Usage()])
    code, _, err = lm("run", "x")
    assert code == 3 and "no free slot" in err and lm.fake.requests == []
    runs.release_slot(Home(lm.tree), "other")
    assert lm("run", "x")[0] == 0 and not list((lm.tree / "system" / "state" / "slots").glob("*.slot"))


# --------------------------------------------------------------------------- ask channel
ASKER = """import json, sys
d = json.load(sys.stdin)
open(sys.argv[1], 'w').write(json.dumps(d))
print('some noise')
print(sys.argv[2])
"""


def asked(lm, monkeypatch, tmp_path, answer, **cfg):
    py = tmp_path / "ask.py"
    py.write_text(ASKER, "utf-8")
    seen = tmp_path / "seen.json"
    set_front(lm.tree, on_start=[], run={"max_parallel": 6, "max_depth": 3, "ask_timeout": "30s", **cfg,
              "ask_channel": f'"{Path(sys.executable).as_posix()}" "{py.as_posix()}" "{seen.as_posix()}" {answer}'})
    fake = ToolCallingFake([("bash", {"command": "echo hello"})])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])
    lm("run", "x", "-m", "plan")                                  # plan asks before every bash call
    return fake.results[0], seen


@pytest.mark.parametrize("answer,ok", [("yes", True), ("always", True), ("no", False), ("maybe", False)])
def test_a_run_without_a_screen_asks_through_the_channel(lm, monkeypatch, tmp_path, answer, ok):
    res, seen = asked(lm, monkeypatch, tmp_path, answer)
    d = json.loads(seen.read_text())
    assert res.ok is ok and d["tool"] == "bash" and "echo hello" in d["arg"] and d["run"]


def test_no_answer_in_time_is_no(lm, monkeypatch, tmp_path):
    set_front(lm.tree, on_start=[], run={"max_parallel": 6, "max_depth": 3, "ask_timeout": "1s",
              "ask_channel": f'"{Path(sys.executable).as_posix()}" -c "import time; time.sleep(30)"'})
    fake = ToolCallingFake([("bash", {"command": "echo hello"})])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])
    lm("run", "x", "-m", "plan")
    assert not fake.results[0].ok


def test_channel_names_are_not_commands(lm, monkeypatch):
    fake = ToolCallingFake([("bash", {"command": "echo hello"})])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: (setattr(fake, "dispatcher", dispatcher), fake)[1])
    lm("run", "x", "-m", "plan")                                  # shipped `ask_channel: tui`: an ask is a deny
    assert not fake.results[0].ok and fake.dispatcher.asker is None


# -------------------------------------------------------------------- wait and results
def fake_run(home, rid, status="running", pid=0, text="", **meta):
    from lightmind.runs import RunDir
    rd = RunDir(home, Run(id=rid, status=status, pid=pid, **meta))
    if status != "running":
        rd.write_result({"run_id": rid, "status": status, "exit_reason": "", "text": text, "cost_usd": 0.0, "cost_est": False})
    return rd


def test_wait_prints_results_and_exit_codes(lm):
    from lightmind.files import Home
    h = Home(lm.tree)
    fake_run(h, "20261020-100000-a-0001", "done", text="the text", cost_usd=0.5)
    fake_run(h, "20261020-100000-b-0002", "failed")
    code, out, _ = lm("wait", "20261020-100000-a", "--json")
    d = json.loads(out)
    assert code == 0 and d[0]["text"] == "the text" and d[0]["cost_usd"] == 0.5 and d[0]["status"] == "done"
    assert lm("wait", "20261020-100000-a-0001", "20261020-100000-b-0002")[0] == 1
    assert lm("wait", "nope")[0] == 2


def test_wait_times_out_on_a_running_run_and_marks_a_dead_one_failed(lm):
    from lightmind.files import Home
    h = Home(lm.tree)
    fake_run(h, "20261020-100000-live-0001", pid=os.getpid())
    code, out, _ = lm("wait", "20261020-100000-live", "--timeout", "0.2", "--json")
    assert code == 124 and json.loads(out)[0]["status"] == "running"
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    fake_run(h, "20261020-100000-dead-0002", pid=p.pid)
    code, out, _ = lm("wait", "20261020-100000-dead", "--json")
    d = json.loads(out)[0]
    assert code == 1 and d["status"] == "failed" and d["exit_reason"] == "process died"
    assert json.loads(lm("runs", "show", "20261020-100000-dead", "--json")[1])["run"]["status"] == "failed"


def test_run_json_and_result_file_agree(lm):
    script(lm, [Text("hello"), Usage(100, 20)])
    out = json.loads(lm("run", "x", "--json")[1])
    assert runs.run_result(__import__("lightmind.files", fromlist=["Home"]).Home(lm.tree), out["run_id"])["text"] == "hello"


# ------------------------------------------------------- detach: a real second process
def local_account(tree):
    (tree / "providers" / "local.md").write_text(
        "---\ntype: openai-compatible\nbase_url: http://127.0.0.1:9/v1\ndefault_model: m\nmodels: {m: {input: 1, output: 1, cache_write: 1, cache_read: 1}}\n---\n", "utf-8")


def test_detach_starts_a_process_of_its_own_and_wait_collects_it(lm, monkeypatch):
    set_front(lm.tree, on_start=[])
    local_account(lm.tree)
    seen = {}
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (seen.update(k), real(*a, **k))[1])
    code, out, _ = lm("run", "--detach", "--account", "local", "--max-usd", "1", "do it", "--json")
    rid = json.loads(out)["run_id"]
    assert code == 0 and json.loads(out)["status"] == "running" and (lm.tree / "runs" / rid / "brief.md").read_text() == "do it"
    if os.name == "nt":
        assert seen["creationflags"] & subprocess.DETACHED_PROCESS and seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["start_new_session"] is True
    code, out, _ = lm("wait", rid, "--timeout", "120", "--json")      # the child cannot reach the "server": failed, but ended
    d = json.loads(out)[0]
    assert code == 1 and d["run_id"] == rid and d["status"] == "failed"
    meta = json.loads((lm.tree / "runs" / rid / "meta.json").read_text())
    assert meta["pid"] not in (0, os.getpid())
    assert (lm.tree / "runs" / rid / "result.json").is_file()


def test_a_detached_run_that_cannot_open_its_session_ends_failed(lm):
    set_front(lm.tree, on_start=[])
    rid = json.loads(lm("run", "--detach", "-a", "no-such-agent", "x", "--json")[1])["run_id"]
    code, out, _ = lm("wait", rid, "--timeout", "120", "--json")
    d = json.loads(out)[0]
    assert code == 1 and d["status"] == "failed" and "no-such-agent" in d["exit_reason"]


# ------------------------------------------------------------------------------ lib.py
FAKE_LM = """import json, os, sys, time
a = sys.argv[1:]
assert a[0] == '--json'
a = a[1:]
log = os.environ['FAKE_LOG']
task = sys.stdin.read() if a[0] == 'run' else ''
open(log, 'a').write(json.dumps({'args': a, 'task': task}) + '\\n')
n = sum(1 for l in open(log) if json.loads(l)['task'] == task)
if a[0] == 'run' and '--detach' in a:
    print(json.dumps({'run_id': 'r-1', 'status': 'running'}))
elif a[0] == 'run':
    if task.startswith('flaky') and n == 1 or task.startswith('broken'):
        print(json.dumps({'run_id': 'r-f', 'status': 'failed', 'exit_reason': 'x', 'text': '', 'cost_usd': 0.1}))
    else:
        time.sleep(0.2)
        r = {'run_id': 'r-' + task, 'status': 'done', 'exit_reason': '', 'text': 'T:' + task, 'cost_usd': 0.25}
        if '--schema' in a:
            r['result'] = json.load(open(a[a.index('--schema') + 1]))
        print(json.dumps(r))
elif a[0] == 'wait':
    print(json.dumps([{'run_id': i, 'status': 'done', 'exit_reason': '', 'text': 't', 'cost_usd': 0.5} for i in a[1:] if not i.startswith('--') and not i.isdigit()]))
elif a[0] == 'runs':
    print(json.dumps({'path': '/x/' + a[2]}))
"""


@pytest.fixture
def wf(tmp_path, monkeypatch):
    fake = tmp_path / "fakelm.py"
    fake.write_text(FAKE_LM, "utf-8")
    monkeypatch.setenv("LM_CMD", f'"{Path(sys.executable).as_posix()}" "{fake.as_posix()}"')
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.delenv("LM_RUN", raising=False)
    monkeypatch.chdir(tmp_path)
    src = Path(L.__file__).parent / "templates" / "workflows" / "lib.py"
    spec = importlib.util.spec_from_file_location("wf_lib", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.calls = lambda: [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines()]
    return mod


def test_lib_run_returns_the_result_and_sums_the_cost(wf):
    r = wf.run("a", agent="w", max_usd=1, model="haiku", max_turns=5)
    assert r["ok"] and r["text"] == "T:a" and wf.spent() == 0.25
    args = wf.calls()[0]["args"]
    assert args[:2] == ["run", "-"] and "-a" in args and ["--max-usd", "1"] == args[args.index("--max-usd"):][:2]
    assert "--model" in args and "--max-turns" in args and wf.calls()[0]["task"] == "a"


def test_lib_retries_a_failed_run_once(wf):
    assert wf.run("flaky one")["ok"] and len(wf.calls()) == 2
    assert not wf.run("broken one")["ok"] and len(wf.calls()) == 4
    assert wf.spent() == pytest.approx(0.55)


def test_lib_parallel_keeps_order_and_accepts_three_kinds(wf):
    res = wf.parallel(["p1", {"task": "p2", "agent": "w"}, lambda: wf.run("p3")], limit=2)
    assert [r["text"] for r in res] == ["T:p1", "T:p2", "T:p3"]


def test_lib_schema_dict_goes_to_a_file(wf):
    r = wf.run("s", schema={"type": "object"})
    assert r["result"] == {"type": "object"} and wf.work_dir().is_dir()


def test_lib_budget_stops_new_runs(wf):
    wf.budget(0.3)
    wf.run("b1")
    wf.run("b2")
    with pytest.raises(wf.BudgetExceeded):
        wf.run("b3")


def test_lib_resume_cache_skips_finished_steps(wf, tmp_path):
    wf.resume(tmp_path / "steps.json")
    assert wf.run("c", step="s1")["ok"]
    wf._state["data"] = {}
    wf.resume(tmp_path / "steps.json")                              # a new start of the script reads the file
    n = len(wf.calls())
    assert wf.run("c", step="s1")["text"] == "T:c" and len(wf.calls()) == n
    wf.run("c2", step="s1")
    assert len(wf.calls()) == n + 1                                 # other input: not cached


def test_lib_detached_and_wait(wf):
    rid = wf.run_detached("d", agent="w")
    assert rid == "r-1" and "--detach" in wf.calls()[0]["args"]
    res = wf.wait([rid, "r-2"], timeout=5)
    assert [r["ok"] for r in res] == [True, True] and wf.spent() == 1.0
    assert wf.run_out({"run_id": "r-1"}).name == "out"


def test_the_shipped_example_workflow_and_lead_agent_load(tree):
    import shutil
    from lightmind.files import Home
    shutil.copytree(tree / "agents/_example-lead", tree / "agents/project-lead")
    shutil.copytree(tree / "workflows/_example", tree / "workflows/my-workflow")
    h = Home(tree)
    a = h.load_agent("project-lead")
    assert a.budget_usd == 5 and a.may_spawn == ["my-agent"]
    assert (tree / "workflows" / "lib.py").is_file()
    subprocess.run([sys.executable, "-c", "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())",
                    str(tree / "workflows/my-workflow/workflow.py")], check=True)
