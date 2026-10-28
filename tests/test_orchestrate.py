"""The workflow runtime (orchestrate.py), the `workflow` tool and the effort level ultracode, with a fake provider."""
import asyncio
import json
import re
import subprocess

import pytest

from lightmind import loop as L
from lightmind import orchestrate as O
from lightmind import protocol
from lightmind import spawn as SP
from lightmind import ultracode
from lightmind.model import Text, ToolCall, Usage
from lightmind.providers.base import Notice, SubagentStart, ToolDone, WorkflowEvent
from lightmind.subagents import SubLog, from_events
from lightmind.tui.subs import bar_items

from test_loop_support import SessionHome, collect, default_settings, write
from test_loop_session import PERMISSIONS
from test_spawn import Router, _own, say


@pytest.fixture
def make(tree, monkeypatch):
    """make(scripts, workflow={...}, **open_kw) -> (session, router, home).  Chat and workflow agents share one Router;
    the RAM always looks plenty unless a test says otherwise, and the machine has 32 CPUs."""
    monkeypatch.setattr(O, "available_ram_mb", lambda: 100_000)
    monkeypatch.setattr(O.os, "cpu_count", lambda: 32)

    def build(scripts, workflow=None, run=None, **open_kw):
        home = SessionHome(tree, default_settings(run=run or {"max_depth": 2, "max_parallel": 16}, spawn={},
                                                  workflow=workflow or {}))
        router = Router(scripts)
        monkeypatch.setattr(L, "make_provider", lambda h, a, d=None: setattr(router, "dispatcher", d) or router)
        monkeypatch.setattr(SP, "make_provider", lambda h, a, d=None: _own(router, d))
        return L.open_session(home, **open_kw), router, home
    return build


def wf(script, id="w1", **kw):
    """One chat turn that calls the workflow tool and waits for it (no UI: nothing goes to the background)."""
    return [ToolCall(id, "workflow", {"script": script, "background": False, **kw}), Usage(10, 5)]


def script(body, name="t", phases=("A",)):
    """A workflow script: meta + `async def main(args)` with `body` (indented by the caller's lines)."""
    lines = "\n".join("    " + ln for ln in body.strip("\n").splitlines())
    return (f"meta = {{'name': {name!r}, 'description': 'a test', 'phases': {[{'title': p} for p in phases]!r}}}\n"
            f"async def main(args):\n{lines}\n")


async def send(s, text="MAIN go"):
    return await collect(s.send(text))


def tool_text(s):
    """The full text of the chat's (first) workflow tool result."""
    return [m.text for m in s.messages if m.role == "tool"][0]


def journal(s, wid="wf-1"):
    return [json.loads(x) for x in (s.run.path / "workflow" / wid / "journal.jsonl").read_text("utf-8").splitlines()]


def result(s, wid="wf-1"):
    return json.loads((s.run.path / "workflow" / wid / "result.json").read_text("utf-8"))


def who(r):
    return [w for w, _ in r.seen]


def logs(evs):
    return [e.text for e in evs if isinstance(e, WorkflowEvent) and e.kind == "log"]


# ------------------------------------------------------------------ agent, parallel, pipeline
async def test_agents_run_and_the_return_value_is_the_result(make):
    s, r, _ = make({"MAIN": [wf(script("phase('A')\na = await agent('AG-A look', label='looker')\nreturn {'a': a}")), say("ok")],
                    "AG-A": [say("found it")]})
    evs = await send(s)
    assert result(s) == {"a": "found it"}
    assert "workflow wf-1 done" in tool_text(s) and '"a": "found it"' in tool_text(s)
    start = next(e for e in evs if isinstance(e, SubagentStart))
    assert (start.workflow, start.phase, start.name) == ("wf-1", "A", "looker")
    kinds = [e.kind for e in evs if isinstance(e, WorkflowEvent)]
    assert kinds[0] == "start" and "phase" in kinds and kinds[-1] == "end"
    sub_req = next(q for w, q in r.seen if w == "AG-A")
    assert "IS the value the workflow script receives" in sub_req.messages[0].text        # data for a script, no address
    assert "workflow" not in {t["name"] for t in sub_req.tools} and "spawn" not in {t["name"] for t in sub_req.tools}


async def test_parallel_is_a_barrier_and_a_failure_becomes_none(make):
    order = []

    async def slow(req):
        await asyncio.sleep(0.05)
        order.append("slow end")
        return say("s")

    def after(req):
        order.append("after start")
        return say("x")

    def boom(req):
        raise RuntimeError("provider exploded")
    body = ("xs = await parallel([lambda: agent('P-SLOW'), lambda: agent('P-FAST'), lambda: agent('P-BOOM')])\n"
            "y = await agent('P-AFTER')\nreturn [xs, y]")
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], "P-SLOW": [slow], "P-FAST": [say("f")],
                    "P-BOOM": [boom], "P-AFTER": [after]})
    await send(s)
    assert result(s) == [["s", "f", None], "x"]
    assert order == ["slow end", "after start"]                   # the agent after parallel() waits for all of it


async def test_pipeline_has_no_barrier_between_stages(make):
    order = []

    async def slow(req):
        await asyncio.sleep(0.1)
        order.append("S1-1 end")
        return say("one")

    def second(req):
        order.append("S2 start: " + req.messages[0].text.split("Task:\n")[-1])
        return say("done " + req.messages[0].text.split("Task:\n")[-1][-3:])
    body = ("return await pipeline([1, 2], lambda x: agent(f'S1-{x}'), lambda v, item: agent(f'S2 {item} {v}'))")
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], "S1-1": [slow], "S1-2": [say("two")], "S2": [second, second]})
    await send(s)
    assert order[0] == "S2 start: S2 2 two" and order[1] == "S1-1 end"     # item 2 went on while item 1 still worked
    assert result(s) == ["done one", "done two"]                           # results stay in item order


# ------------------------------------------------------------------ limits
async def test_the_limits_cut_with_a_log_line_never_silently(make, monkeypatch):
    monkeypatch.setattr(O, "MAX_ITEMS", 2)
    monkeypatch.setattr(O, "MAX_AGENTS", 3)
    body = ("xs = await parallel([lambda: agent('L-1'), lambda: agent('L-2'), lambda: agent('L-3')])\n"
            "y = await agent('L-4')\nz = await agent('L-5')\nreturn [xs, y, z]")
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **{f"L-{i}": [say(f"r{i}")] for i in range(1, 6)}})
    evs = await send(s)
    assert result(s) == [["r1", "r2", None], "r4", None]
    lines = logs(evs)
    assert any("only the first 2 run" in x for x in lines) and any("limit of 3 agents" in x for x in lines)
    assert "L-3" not in who(r) and "L-5" not in who(r)
    assert any(e.get("skipped") == "agent limit" for e in journal(s))


async def test_agents_at_once_are_bounded_on_a_laptop(make):
    peak = {"now": 0, "max": 0}

    async def work(req):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.05)
        peak["now"] -= 1
        return say("ok")
    body = "return await parallel([lambda i=i: agent(f'W-{i}') for i in range(6)])"
    agents = {f"W-{i}": [work] for i in range(6)}
    s, r, home = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"laptop_parallel": 2})
    assert O.is_laptop(home) and O.max_parallel(home) == 2
    await send(s)
    assert peak["max"] == 2 and result(s) == ["ok"] * 6
    peak["max"] = 0                                                    # the check: a higher limit lets more run
    s, r, home = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"laptop_parallel": 4})
    await send(s)
    assert peak["max"] == 4


def test_max_parallel_never_passes_16_or_cpus_minus_2(tree, monkeypatch):
    monkeypatch.setattr(O, "is_laptop", lambda home: False)
    home = SessionHome(tree, default_settings(workflow={"max_parallel": 64}))
    monkeypatch.setattr(O.os, "cpu_count", lambda: 64)
    assert O.max_parallel(home) == 16
    monkeypatch.setattr(O.os, "cpu_count", lambda: 6)
    assert O.max_parallel(home) == 4


async def test_short_ram_lets_agents_start_one_by_one(make, monkeypatch):
    peak = {"now": 0, "max": 0}

    async def work(req):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.03)
        peak["now"] -= 1
        return say("ok")
    monkeypatch.setattr(O, "RAM_POLL", 0.01)
    body = "return await parallel([lambda i=i: agent(f'R-{i}') for i in range(3)])"
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **{f"R-{i}": [work] for i in range(3)}})
    monkeypatch.setattr(O, "available_ram_mb", lambda: 500)               # less than one agent plus the reserve
    evs = await send(s)
    assert peak["max"] == 1 and result(s) == ["ok"] * 3
    assert any("waiting for free memory" in x for x in logs(evs))


# ------------------------------------------------------------------ determinism and the sandbox
@pytest.mark.parametrize("bad", ["import time", "import random", "from datetime import datetime", "import uuid",
                                 "import secrets"])
def test_clock_and_chance_are_refused_with_a_clear_message(bad):
    with pytest.raises(O.WorkflowError, match="clock or chance"):
        O.load(bad + "\n" + script("return 1"))
    assert O.load("import json\n" + script("return 1")).name == "t"           # the check: a pure helper is fine


@pytest.mark.parametrize("bad, why", [
    ("return open('x').read()", "`open` is not allowed"),
    ("return ().__class__", "`.__class__`"),
    ("return '{}'.format(1)", "f-string"),
    ("c = agent('x')\nreturn c.cr_frame", "`.cr_frame`"),
    ("import os\nreturn 1", "import of `os`"),
    ("budget.used = 0\nreturn 1", "setting an attribute"),
    ("c = agent('x')\nreturn c.send(None)", "`.send`"),
])
def test_the_script_cannot_reach_around_its_agents(bad, why):
    with pytest.raises(O.WorkflowError, match=re.escape(why)):
        O.load(script(bad))


def test_meta_must_be_a_literal_and_main_must_exist():
    with pytest.raises(O.WorkflowError, match="pure literal"):
        O.load("meta = {'name': str(1)}\nasync def main(args):\n    return 1\n")
    with pytest.raises(O.WorkflowError, match="async def main"):
        O.load("meta = {'name': 'x'}\n")
    assert O.load("meta = {'name': 'x'}\nasync def main(args):\n    id = 3\n    return id\n").name == "x"   # its own `id`


async def test_an_endless_loop_of_the_script_ends_with_a_message(make, monkeypatch):
    monkeypatch.setattr(O, "STEP_LIMIT", 1000)
    s, r, _ = make({"MAIN": [wf(script("while True:\n    pass")), say("ok")]})
    await send(s)
    assert "workflow wf-1 failed" in tool_text(s) and "loop steps" in tool_text(s)


async def test_a_refused_script_starts_nothing(make):
    s, r, _ = make({"MAIN": [wf("import random\n" + script("return await agent('X-1')")), say("ok")], "X-1": [say("x")]})
    await send(s)
    assert tool_text(s).startswith("workflow not started") and "X-1" not in who(r)
    assert not (s.run.path / "workflow").exists()


# ------------------------------------------------------------------ journal and resume
SEQ = "a = await agent('J-A')\nb = await agent('J-B')\nc = await agent('J-C')\nreturn [a, b, c]"


async def test_resume_takes_the_unchanged_start_from_the_journal(make):
    s, r, _ = make({"MAIN": [wf(script(SEQ)), say("1"), wf(script(SEQ), id="w2", resume_run_id="wf-1"), say("2"),
                             wf(script(SEQ.replace("J-B", "J-B changed")), id="w3", resume_run_id="wf-1"), say("3")],
                    "J-A": [say("a")] * 3, "J-B": [say("b")] * 3, "J-C": [say("c")] * 3})
    await send(s)
    agents = [e for e in journal(s) if e["type"] == "agent"]
    assert [(e["seq"], e["ok"], e["value"]) for e in agents] == [(1, True, "a"), (2, True, "b"), (3, True, "c")]
    assert all(re.fullmatch(r"[0-9a-f]{24}#0", e["key"]) for e in agents)
    calls = len(r.seen)
    await send(s, "MAIN again")
    assert who(r)[calls:] == ["MAIN", "MAIN"]                             # only the chat's own turns: every agent from the journal
    assert result(s, "wf-2") == ["a", "b", "c"] and all(e.get("cached") for e in journal(s, "wf-2") if e["type"] == "agent")
    seen = len(r.seen)
    await send(s, "MAIN changed")                                         # the check: a changed prompt runs live from there on
    live = [w for w, _ in r.seen[seen:] if w != "MAIN"]
    assert live == ["J-B", "J-C"]                                         # J-A cached; J-C unchanged but after the change: live
    assert [e.get("cached", False) for e in journal(s, "wf-3") if e["type"] == "agent"] == [True, False, False]


async def test_an_agent_that_failed_runs_again_on_resume(make):
    def boom(req):
        raise RuntimeError("down")
    s, r, _ = make({"MAIN": [wf(script(SEQ)), say("1"), wf(script(SEQ), id="w2", resume_run_id="wf-1"), say("2")],
                    "J-A": [say("a")], "J-B": [boom, say("b")], "J-C": [say("c")] * 2})
    await send(s)
    assert result(s) == ["a", None, "c"]
    r.scripts["J-B"] = [say("b")]
    await send(s, "MAIN again")
    assert result(s, "wf-2") == ["a", "b", "c"] and who(r).count("J-C") == 2


async def test_resume_of_an_unknown_run_is_refused(make):
    s, r, _ = make({"MAIN": [wf(script(SEQ), resume_run_id="../x"), say("1")]})
    await send(s)
    assert "no workflow '../x' in this chat" in tool_text(s)


# ------------------------------------------------------------------ schema
SCHEMA = "{'type': 'object', 'properties': {'n': {'type': 'integer'}}, 'required': ['n']}"


async def test_schema_answer_is_checked_and_corrected_in_the_same_agent(make):
    s, r, _ = make({"MAIN": [wf(script(f"return await agent('SCH-1', schema={SCHEMA})")), say("ok")],
                    "SCH-1": [[ToolCall("s1", "StructuredOutput", {"n": "three"}), Usage(3, 1)],
                              [ToolCall("s2", "StructuredOutput", {"n": 3}), Usage(3, 1)], say("done")]})
    evs = await send(s)
    assert result(s) == {"n": 3}
    inner = [e for e in evs if isinstance(e, ToolDone) and e.parent and e.name == "StructuredOutput"]
    assert [e.ok for e in inner] == [False, True] and "does not fit the schema" in inner[0].text
    sub_tools = {t["name"] for t in next(q for w, q in r.seen if w == "SCH-1").tools}
    assert "StructuredOutput" in sub_tools
    assert "StructuredOutput" not in {t["name"] for t in next(q for w, q in r.seen if w == "MAIN").tools}


async def test_schema_gives_up_after_three_more_tries(make):
    s, r, _ = make({"MAIN": [wf(script(f"return await agent('SCH-BAD', schema={SCHEMA})")), say("ok")],
                    "SCH-BAD": [[ToolCall("s1", "StructuredOutput", {"n": "x"}), Usage(3, 1)], say("no")]})
    evs = await send(s)
    assert result(s) is None
    starts = [e.id for e in evs if isinstance(e, SubagentStart)]
    assert starts == ["wf-1.a1", "wf-1.a1.2", "wf-1.a1.3", "wf-1.a1.4"]        # 1 + 3 fresh tries, each its own id
    assert sum(1 for x in logs(evs) if "did not fit the schema" in x) == 4


async def test_schema_also_takes_json_written_as_text_and_wraps_a_non_object(make):
    s, r, _ = make({"MAIN": [wf(script("return await agent('SCH-T', schema={'type': 'array', 'items': {'type': 'string'}})")),
                             say("ok")],
                    "SCH-T": [say('```json\n["a", "b"]\n```')]})
    await send(s)
    assert result(s) == ["a", "b"]
    tool = next(t for t in next(q for w, q in r.seen if w == "SCH-T").tools if t["name"] == "StructuredOutput")
    assert tool["input_schema"]["required"] == ["value"]


# ------------------------------------------------------------------ rights
async def test_workflow_agents_have_the_chats_rights_never_more(make, tree):
    s0, _, home = make({"MAIN": [say("m")]})
    write(home.path("permissions"), PERMISSIONS.replace("skill(*),", "skill(*), spawn(),"))     # copies only
    write(home.path("agents") / "loose" / home.filename("agent"),
          "---\nname: loose\npermissions:\n  allow: [\"bash(*)\"]\n---\nBody.\n")
    bash = [[ToolCall("b1", "bash", {"command": "ls /"}), Usage()], say("tried")]
    body = "return [await agent('RI-PLAIN'), await agent('RI-TYPED', agent_type='loose')]"
    s, r, _ = make({"MAIN": [wf(script(body)), say("m")], "RI-PLAIN": bash, "RI-TYPED": bash})
    evs = await send(s)
    assert result(s) == ["tried", None]                                    # agent_type needs spawn(loose): fallback deny
    assert any("refused by rule" in x for x in logs(evs)) and "RI-TYPED" not in who(r)
    inner = [e for e in evs if isinstance(e, ToolDone) and e.parent and e.name == "bash"]
    assert inner and not inner[0].ok and "denied" in inner[0].text          # the chat may not run `ls`, so neither may it
    write(home.path("permissions"), PERMISSIONS.replace("skill(*),", "skill(*), spawn(*), \"bash(ls*)\","))
    s, r, _ = make({"MAIN": [wf(script(body)), say("m")], "RI-PLAIN": bash, "RI-TYPED": bash})
    evs = await send(s)                                                    # the check: with the chat's rights it works
    inner = [e for e in evs if isinstance(e, ToolDone) and e.parent and e.name == "bash"]
    assert result(s) == ["tried", "tried"] and inner and all(e.ok for e in inner)


async def test_the_agent_types_own_wider_rules_do_not_count(make, tree):
    s0, _, home = make({"MAIN": [say("m")]})
    write(home.path("permissions"), PERMISSIONS.replace("skill(*),", "skill(*), spawn(*),"))
    write(home.path("agents") / "loose" / home.filename("agent"),
          "---\nname: loose\npermissions:\n  allow: [\"bash(*)\"]\n---\nBody.\n")
    s, r, _ = make({"MAIN": [wf(script("return await agent('RI-T2', agent_type='loose')")), say("m")],
                    "RI-T2": [[ToolCall("b1", "bash", {"command": "ls /"}), Usage()], say("tried")]})
    evs = await send(s)
    inner = [e for e in evs if isinstance(e, ToolDone) and e.parent and e.name == "bash"]
    assert result(s) == "tried" and inner and not inner[0].ok


async def test_a_workflow_needs_the_right_to_start_copies(make, tree):
    s0, _, home = make({"MAIN": [say("m")]})
    write(home.path("permissions"), PERMISSIONS)                         # no spawn rule at all
    s, r, _ = make({"MAIN": [wf(script("return await agent('NS-1')")), say("m")], "NS-1": [say("x")]})
    await send(s)
    assert "denied" in tool_text(s) and "NS-1" not in who(r)


async def test_no_workflow_in_mode_plan(make, tree):
    s0, _, home = make({"MAIN": [say("m")]})
    write(home.path("permissions"), PERMISSIONS.replace("skill(*),", "skill(*), spawn(),"))
    s, r, _ = make({"MAIN": [wf(script("return await agent('PL-1')")), say("m")], "PL-1": [say("x")]}, mode="plan")
    await send(s)
    assert tool_text(s).startswith("refused: no workflows in mode plan") and "PL-1" not in who(r)


async def test_a_sub_agent_gets_no_workflow_tool(make):
    s, r, _ = make({"MAIN": [[ToolCall("c1", "spawn", {"task": "SUB-X look", "background": False}), Usage()], say("m")],
                    "SUB-X": [say("x")]})
    await send(s)
    assert "workflow" in {t["name"] for t in next(q for w, q in r.seen if w == "MAIN").tools}
    assert "workflow" not in {t["name"] for t in next(q for w, q in r.seen if w == "SUB-X").tools}


# ------------------------------------------------------------------ budget
async def test_budget_is_a_hard_limit(make):
    body = "a = await agent('B-1')\nb = await agent('B-2')\nc = await agent('B-3')\nreturn [a, b, c, budget.spent()]"
    s, r, _ = make({"MAIN": [wf(script(body), budget="+20"), say("ok")], "B-1": [say("a")], "B-2": [say("b")],
                    "B-3": [say("c")]})
    evs = await send(s)
    assert result(s) == ["a", "b", None, 20]                               # 10 tokens per agent: the third is not started
    assert "workflow wf-1 budget" in tool_text(s) and "B-3" not in who(r)
    assert any("budget of 20 tokens used up" in x for x in logs(evs))
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], "B-1": [say("a")], "B-2": [say("b")], "B-3": [say("c")]})
    await send(s)                                                          # the check: without a budget all three run
    assert result(s) == ["a", "b", "c", 30]


async def test_the_budget_stops_agents_that_are_still_working(make):
    gate = asyncio.Event()

    async def slow(req):
        await gate.wait()
        return say("late")
    body = "return await parallel([lambda: agent('BS-FAST'), lambda: agent('BS-SLOW')])"
    s, r, _ = make({"MAIN": [wf(script(body), budget="15"), say("ok")],
                    "BS-FAST": [[Text("x"), Usage(20, 0)]], "BS-SLOW": [slow]})
    await asyncio.wait_for(send(s), 5)
    assert result(s) == ["x", None] and "workflow wf-1 budget" in tool_text(s)    # the reporting one ends its turn
    assert any(e.get("stopped") == "budget" for e in journal(s) if e["type"] == "agent")
    assert O.parse_budget("+500k") == 500_000 and O.parse_budget("1.5m") == 1_500_000 and O.parse_budget("lots") is None


# ------------------------------------------------------------------ nesting, background, display
async def test_a_nested_workflow_runs_one_level_deep(make, tree):
    write(tree / "wf" / "child.py", script("phase('C')\nreturn await agent('NE-' + args['x'])", name="child"))
    write(tree / "wf" / "deeper.py", script("return await workflow('wf/child.py', {'x': 'A'})", name="deeper"))
    s, r, _ = make({"MAIN": [wf(script("return await workflow('wf/child.py', {'x': 'A'})")), say("1"),
                             wf(script("return await workflow('wf/deeper.py')"), id="w2"), say("2")],
                    "NE-A": [say("nested")]})
    evs = await send(s)
    assert result(s) == "nested"
    assert next(e for e in evs if isinstance(e, SubagentStart)).phase == "child: C"
    await send(s, "MAIN deeper")                                           # the check: a nested one cannot nest again
    assert result(s, "wf-2") is None and "one level deep" in [m.text for m in s.messages if m.role == "tool"][1]


async def test_a_background_workflow_returns_at_once_and_reports_as_a_message(make):
    gate = asyncio.Event()

    async def slow(req):
        await gate.wait()
        return say("bg answer")
    s, r, _ = make({"MAIN": [[ToolCall("w1", "workflow", {"script": script("return await agent('BG-1')")}), Usage()],
                             say("I go on")], "BG-1": [slow]})
    msgs, seen = [], []
    s.bg.on_message = msgs.append
    s.bg.on_event = seen.append                                            # what a UI is told while the chat is free
    await asyncio.wait_for(send(s), 5)
    assert "started in the background as wf-1" in tool_text(s) and s.bg.running() == ["workflow t"]
    first = next(e for e in seen if isinstance(e, WorkflowEvent))
    assert (first.kind, first.phases) == ("start", ["A"])                  # the plan shows before it runs
    gate.set()
    for _ in range(300):
        if msgs:
            break
        await asyncio.sleep(0.01)
    assert msgs[0].startswith("[subagent workflow t finished]") and "untrusted" in msgs[0] and "bg answer" in msgs[0]
    log = from_events([json.loads(x) for x in (s.run.path / "events.jsonl").read_text("utf-8").splitlines()])
    assert log.flows["wf-1"].done and log.subs["wf-1.a1"].workflow == "wf-1"              # resume rebuilds it


def test_phases_group_the_line_under_the_input_and_the_protocol_names_them():
    log = SubLog()
    log.feed(WorkflowEvent("wf-1", "start", "audit", "check all", ["Find", "Check"]))
    for i in range(5):
        log.feed(SubagentStart(f"wf-1.a{i}", f"f{i}", "t", "wf-1", "Find"))
    log.feed(SubagentStart("c9", "helper", "t"))
    text, spans, targets = bar_items(log.current(), log.current_flows())
    assert text.plain == "◆ helper running · ▸ audit  Find: 5 running"
    assert len(spans) == len(targets) == 2 and targets[1].id == "wf-1.a0"
    tr = protocol.workflow_event("s1", "t1", WorkflowEvent("wf-1", "start", "audit", "check all", ["Find"]))
    assert tr == {"type": "workflow.start", "session": "s1", "turn": "t1", "id": "wf-1", "name": "audit",
                  "description": "check all", "phases": ["Find"]}
    assert protocol.flow_of(SubagentStart("x", "n", "t", "wf-1", "Find")) == {"workflow": "wf-1", "phase": "Find"}
    assert protocol.flow_of(SubagentStart("x", "n", "t")) == {}


# ------------------------------------------------------------------ worktrees
def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


async def test_isolation_worktree_gives_each_agent_its_own_copy(make, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "start")

    def writer(req):
        path = re.search(r"git worktree (\S+) \(its own branch\)", req.messages[0].text).group(1)
        return [ToolCall("w1", "write", {"path": path + "/new.txt", "content": "x"}), Usage()]
    body = "return await parallel([lambda: agent('WT-W', isolation='worktree'), lambda: agent('WT-R', isolation='worktree')])"
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], "WT-W": [writer, say("wrote")], "WT-R": [say("read")]},
                   workdir=str(repo))
    await send(s)
    w, rd = result(s)
    assert w["value"] == "wrote" and w["branch"].startswith("lm/") and (tmp_path / "repo" / ".git" / "lm-worktrees").is_dir()
    assert (O.Path(w["worktree"]) / "new.txt").read_text() == "x" and not (repo / "new.txt").exists()
    assert w["worktree"] != rd["worktree"]
    branches = git(repo, "branch", "--list", "lm/*")
    assert w["branch"] in branches and rd["branch"] not in branches          # the unchanged copy is removed, the other kept
    assert "kept" in tool_text(s) and "no changes, removed" in tool_text(s)


async def test_a_worktree_needs_the_chats_write_right(make, tree, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "start")
    s0, _, home = make({"MAIN": [say("m")]})
    write(home.path("permissions"), PERMISSIONS.replace("skill(*),", "skill(*), spawn(),"))     # writes only in the run folder
    s, r, _ = make({"MAIN": [wf(script("return await agent('WT-D', isolation='worktree')")), say("ok")],
                    "WT-D": [say("x")]}, workdir=str(repo))
    evs = await send(s)
    assert result(s) is None and any("no worktree at" in x and "refused by rule" in x for x in logs(evs))
    assert not (repo / ".git" / "lm-worktrees").exists() and "WT-D" not in who(r)


async def test_isolation_worktree_outside_a_repo_gives_none_and_says_why(make, tmp_path):
    s, r, _ = make({"MAIN": [wf(script("return await agent('WT-X', isolation='worktree')")), say("ok")],
                    "WT-X": [say("x")]}, workdir=str(tmp_path))
    evs = await send(s)
    assert result(s) is None and any("needs a git repo" in x for x in logs(evs)) and "WT-X" not in who(r)


# ------------------------------------------------------------------ ultracode
async def test_the_word_ultracode_switches_the_level_on_for_user_only(make):
    s, r, _ = make({"MAIN": [say("a"), say("b")]})
    s.personal = True
    evs = await send(s, "MAIN ultracode: check everything")
    assert s.effort == "ultracode" and any(isinstance(e, Notice) and "ultracode is on" in e.text for e in evs)
    req = r.seen[-1][1]
    assert req.effort == "max" and "Effort: ultracode" in req.system and "`workflow` tool" in req.system
    await send(s, "MAIN go on")
    assert r.seen[-1][1].effort == "max"                                  # it stays on for the chat


@pytest.mark.parametrize("case", ["subagent", "not personal", "keyword off", "a path"])
async def test_the_word_does_nothing_elsewhere(make, case):
    s, r, _ = make({"MAIN": [say("a")]}, workflow={"keyword": False} if case == "keyword off" else None)
    s.personal = case != "not personal"
    text = "MAIN read skills/ultracode/SKILL.md" if case == "a path" else "MAIN ultracode please"
    await collect(s.send(text, origin="subagent") if case == "subagent" else s.send(text))
    assert s.effort != "ultracode" and r.seen[-1][1].effort != "max"
    assert "Effort: ultracode" not in r.seen[-1][1].system


async def test_effort_ultracode_from_the_settings_is_max_for_the_provider_and_for_sub_agents(make):
    s, r, _ = make({"MAIN": [[ToolCall("c1", "spawn", {"task": "SUB-U x", "background": False}), Usage()], say("m")],
                    "SUB-U": [say("u")]}, effort="ultracode")
    await send(s)
    main = next(q for w, q in r.seen if w == "MAIN")
    sub = next(q for w, q in r.seen if w == "SUB-U")
    assert main.effort == "max" and sub.effort == "max" and "Effort: ultracode" not in sub.system


def test_ultracode_words():
    assert ultracode.mentioned("Ultracode: bau das") and ultracode.mentioned("bitte mit ultracode.")
    assert not ultracode.mentioned("skills/ultracode/SKILL.md") and not ultracode.mentioned("ultracode.py")
    assert ultracode.model_effort("ultracode") == "max" and ultracode.model_effort("high") == "high"
    from lightmind import core, slash, settings
    assert "ultracode" in core.efforts() and "ultracode" in slash.efforts() and settings.EFFORTS[-1] == "ultracode"
