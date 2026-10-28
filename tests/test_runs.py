import json
from datetime import datetime, timedelta

import pytest

from lightmind import runs as R
from lightmind.model import Rule, Run, SessionState, Task, Text, Tokens, ToolRef


def mk(home, agent="a", model="claude-sonnet-5-5", rid=None, **kw):
    run = Run(id=rid or R.new_run_id(agent), agent=agent, model=model, **kw)
    return R.RunDir(home, run, Task(goal="g", limits={"usd": 1}))


def test_run_id_format():
    i = R.new_run_id("web", datetime(2026, 10, 20, 12, 30, 5))
    assert i.startswith("20261020-123005-web-") and len(i.split("-")[-1]) == 4


def test_creates_files(home):
    rd = mk(home)
    assert json.loads((rd.path / "meta.json").read_text())["agent"] == "a"
    assert "goal: g" in (rd.path / "task.md").read_text()
    assert rd.path.parent == home.path("runs")


def test_events_and_tool_out(home):
    rd = mk(home)
    rd.event("tool", {"a": 1})
    rd.event("model", Text("hi"))
    lines = [json.loads(l) for l in (rd.path / "events.jsonl").read_text().splitlines()]
    assert lines[0]["kind"] == "tool" and lines[1]["data"] == {"type": "Text", "text": "hi"}
    assert rd.tool_out(3, "bash", "x").read_text() == "x"
    assert rd.path / "tool-out" / "3-bash.txt" == rd.tool_out(3, "bash", "x")


def test_cost_from_prices_file(home):
    p = R.load_prices(home)
    assert p["sonnet"] == (3.0, 15.0, 3.75, 0.30)
    usd, est = R.compute_cost(p, "claude-sonnet-5-5", Tokens(1_000_000, 1_000_000, 1_000_000, 0))
    assert usd == pytest.approx(18.3) and not est
    assert R.compute_cost(p, "mystery", Tokens(5)) == (0.0, True)


def test_usage_accumulates_and_finish(home):
    rd = mk(home)
    rd.add_usage(Tokens(1000, 500))
    rd.add_usage(Tokens(1000, 500, 10, 20))
    m = json.loads((rd.path / "meta.json").read_text())
    assert m["tokens"]["input"] == 2000 and m["cost_usd"] > 0
    run = rd.finish("done", "ok")
    assert run.status == "done" and run.end
    log = (rd.path / "log.md").read_text()
    assert log.strip().endswith("<!-- lm:end status=done -->")


def test_finish_keeps_agent_marker_and_handover(home):
    rd = mk(home)
    (rd.path / "log.md").write_text("notes\n## Handover\ndone: x\n<!-- lm:end status=handover -->\n")
    rd.finish("handover")
    assert (rd.path / "log.md").read_text().count("lm:end") == 1


def test_write_session(home):
    rd = mk(home)
    rd.write_session(SessionState(mode="plan", tools_added=[ToolRef("tool", "pdf")],
                                  rules=[Rule("bash", "ls*", "allow")]))
    t = (rd.path / "session.md").read_text()
    assert "mode: plan" in t and "bash(ls*)" in t and "pdf" in t


def test_list_show_cost(home):
    a = mk(home, "web", rid="20261020-100000-web-aaaa")
    b = mk(home, "doc", rid="20261020-110000-doc-bbbb")
    a.add_usage(Tokens(1_000_000))
    b.add_usage(Tokens(0, 1_000_000))
    a.finish("done")
    ids = [r.id for r in R.list_runs(home)]
    assert ids == ["20261020-110000-doc-bbbb", "20261020-100000-web-aaaa"]
    assert len(R.list_runs(home, limit=1)) == 1
    s = R.show_run(home, "20261020-1000")
    assert s["run"]["status"] == "done" and "lm:end" in s["log"]
    assert R.cost_summary(home) == {"web": 3.0, "doc": 15.0}
    assert R.cost_summary(home, by="model") == {"claude-sonnet-5-5": 18.0}
    old = (datetime.now().astimezone() - timedelta(days=3)).isoformat()
    a.run.start = old
    a._meta()
    assert R.cost_summary(home, since="24h") == {"doc": 15.0}
    with pytest.raises(FileNotFoundError):
        R.show_run(home, "nope")
