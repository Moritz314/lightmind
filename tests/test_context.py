from lightmind import context as C
from lightmind.model import Agent, Skill, SessionState, Task, ToolRef


def build(home, **kw):
    kw.setdefault("agent", None)
    kw.setdefault("mode", "auto")
    kw.setdefault("task", None)
    kw.setdefault("session", SessionState())
    return C.build_context(home, **kw)


def test_default_blocks_and_order(home):
    ctx = build(home, rules_text="RULE-X", builtin_schemas=[{"name": "b"}, {"name": "a"}])
    names = [n for n, _ in ctx.blocks]
    assert names == ["lightmind", "mode", "skill_index"]           # no rules block, no required skills
    assert "RULE-X" not in ctx.prefix and "Work autonomously" in ctx.prefix
    assert [s["name"] for s in ctx.tool_schemas] == ["a", "b"]
    assert "- run-tracking:" in ctx.prefix and "(skills/run-tracking/SKILL.md)" in ctx.prefix


def test_order_from_lightmind_md(home):
    p = home.root / "lightmind.md"
    p.write_text(p.read_text("utf-8").replace("order: [lightmind, mode,", "order: [mode, lightmind,"), "utf-8")
    assert [n for n, _ in build(home).blocks][:2] == ["mode", "lightmind"]


def test_prefix_byte_stable_and_state_volatile(home):
    a = build(home, state_info={"time": "10:00"})
    b = build(home, state_info={"time": "11:00"})
    assert a.prefix == b.prefix and a.state != b.state
    assert build(home).prefix == build(home).prefix


def test_agent_project_and_memory_data(home):
    pd = home.path("projects") / "p1"
    pd.mkdir(parents=True)
    (pd / home.filename("project_memory")).write_text("PROJ-MEM", "utf-8")
    ag = Agent(name="a", body="AGENT BODY", tools=[ToolRef("skill", "run-tracking")])
    ctx = build(home, agent=ag, project="p1")
    t = dict(ctx.blocks)
    assert t["agent"] == "AGENT BODY" and "PROJ-MEM" in t["project"]
    assert {"example-skill", "run-tracking"} <= {s.name for s in ctx.skills}   # skills are always all on


def test_skill_index_sorted_and_off():
    sk = [Skill("b", "second\nline"), Skill("a", "first")]
    assert C.skill_index(sk) == "- a: first\n- b: second line"
    assert C.skill_index(sk, "off") == ""


def test_preload_regex_budget():
    sk = [Skill("b", preload_when="pdf", body="x" * 400), Skill("a", preload_when="PDF|doc", body="y" * 400),
          Skill("c", preload_when="never", body="z")]
    assert [s.name for s in C.preload_skills(sk, "Read a Pdf", 1000)] == ["a", "b"]
    assert [s.name for s in C.preload_skills(sk, "Read a Pdf", 150)] == ["a"]


def test_preload_in_no_tool_build(home):
    d = home.path("skills") / "pdfhelp"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: pdfhelp\ndescription: d\npreload_when: pdf\n---\nPDFBODY", "utf-8")
    assert "PDFBODY" in build(home, task=Task(goal="split a PDF"), builtin_schemas=[]).prefix
    assert "PDFBODY" not in build(home, task=Task(goal="split a PDF"), builtin_schemas=[{"name": "x"}]).prefix


def test_handover(home):
    assert C.handover_needed(70, 100, 0.7) and not C.handover_needed(69, 100, 0.7)
    assert "## Handover" in C.handover_instruction()
    orig = Task(goal="build it", limits={"usd": 2}, check="pytest")
    t = C.handover_task("junk\n## Handover\ndone: a\nnext: b\n<!-- lm:end status=handover -->\n", orig)
    assert "build it" in t.goal and "next: b" in t.goal and "junk" not in t.goal
    assert "lm:end" not in t.goal and t.limits == {"usd": 2} and t.check == "pytest"


def test_initial_messages(home):
    ctx = build(home)
    m = C.initial_messages(ctx, Task(goal="G", sources=["s.md"]), "hello")
    assert len(m) == 1 and m[0].role == "user" and "G" in m[0].text and "hello" in m[0].text
    assert C.initial_messages(ctx, None, "") == []
