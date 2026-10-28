"""Context from the new tree (master prompt, mode instruction, skill index, state block) and the
on_start / on_end hooks."""
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

from lightmind import context as C
from lightmind.files import ENTRY, Home
from lightmind.init import init_tree
from lightmind.model import Agent, SessionState, Task, ToolRef

NOW = datetime(2026, 10, 20, 15, 5)               # a Tuesday
PY = Path(sys.executable).as_posix()


@pytest.fixture
def fresh(tmp_path) -> Home:
    init_tree(tmp_path / "lm")
    return Home(tmp_path / "lm")


def build(home, **kw):
    kw.setdefault("agent", None)
    kw.setdefault("mode", "auto")
    kw.setdefault("task", None)
    kw.setdefault("session", SessionState())
    kw.setdefault("now", NOW)
    return C.build_context(home, **kw)


def edit(home: Home, old: str, new: str) -> None:
    p = home.root / ENTRY
    p.write_text(p.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    t = p.stat().st_mtime + 10
    os.utime(p, (t, t))


def add_skill(home: Home, name: str, desc: str = "does it", body: str = "BODY", extra: str = "") -> None:
    d = home.path("skills") / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n{extra}---\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------- prefix
def no_skills(home: Home) -> None:
    shutil.rmtree(home.path("skills"))
    home.path("skills").mkdir()


def test_prefix_is_master_prompt_then_mode_and_nothing_else_in_an_empty_tree(fresh):
    no_skills(fresh)
    ctx = build(fresh)
    assert [n for n, _ in ctx.blocks] == ["lightmind", "mode"]
    assert ctx.blocks[0][1] == fresh.master_prompt()
    assert ctx.blocks[1][1] == ("Mode: auto\nWork autonomously on the task. Ask the user before you spend money "
                                "or send anything to customers.")
    assert ctx.prefix == fresh.master_prompt() + "\n\n" + ctx.blocks[1][1]


def test_permission_rules_are_not_sent(fresh):
    text = build(fresh, rules_text="RULE-X").prefix
    assert "RULE-X" not in text
    # the rules are in permissions.md; the catalog of numbered rights (rechte.md) holds the category rules
    perm = (fresh.root / "permissions.md").read_text(encoding="utf-8") + (fresh.root / "rechte.md").read_text(encoding="utf-8")
    for line in ("secret(*)", "read({secrets}/**/*.env)", "bash(*{secrets}/*.env*)", "fallback: allow",
                 "category(money)", "Rule tools"):
        assert line in perm and line not in text


def test_mode_instruction_follows_the_mode(fresh):
    assert "Research and plan. Change nothing." in build(fresh, mode="plan").prefix
    assert "Do not ask questions." in build(fresh, mode="bypass").prefix
    assert "Mode:" not in build(fresh, mode="").prefix


def test_default_order_of_blocks(fresh):
    add_skill(fresh, "alpha")
    (fresh.path("projects") / "p1").mkdir(parents=True)
    (fresh.path("projects") / "p1" / "projectMemory.md").write_text("PROJ-MEM", encoding="utf-8")
    ag = Agent(name="a", body="AGENT BODY")
    ctx = build(fresh, agent=ag, project="p1")
    assert [n for n, _ in ctx.blocks] == ["lightmind", "mode", "agent", "project", "skill_index"]
    assert ctx.blocks[2][1] == "AGENT BODY" and "PROJ-MEM" in ctx.blocks[3][1]


def test_order_comes_from_lightmind_md(fresh):
    add_skill(fresh, "alpha")
    edit(fresh, "order: [lightmind, mode,", "order: [mode, lightmind,")
    assert [n for n, _ in build(fresh).blocks] == ["mode", "lightmind", "skill_index"]
    edit(fresh, "skill_index, required_skills,", "required_skills,")
    assert [n for n, _ in build(fresh).blocks] == ["mode", "lightmind"]


def test_rules_block_only_when_asked_for_in_the_order(fresh):
    edit(fresh, "order: [lightmind,", "order: [lightmind, rules,")
    assert [n for n, _ in build(fresh, rules_text="RULE-X").blocks][:2] == ["lightmind", "rules"]


def test_skill_index_lines_have_name_description_path(fresh):
    no_skills(fresh)
    add_skill(fresh, "zeta", "last   one on two lines")
    add_skill(fresh, "alpha", "first one")
    text = dict(build(fresh).blocks)["skill_index"]
    assert text == ("Skills (read the SKILL.md at the path before you use one):\n"
                    "- alpha: first one (skills/alpha/SKILL.md)\n"
                    "- zeta: last one on two lines (skills/zeta/SKILL.md)")
    edit(fresh, "skill_index: oneline", "skill_index: off")
    assert "skill_index" not in dict(build(fresh).blocks)


def test_skill_index_paths_follow_a_renamed_folder(fresh):
    add_skill(fresh, "alpha")
    (fresh.root / "skills").rename(fresh.root / "wissen")
    edit(fresh, "skills: skills/", "skills: wissen/")
    assert "(wissen/alpha/SKILL.md)" in build(fresh).prefix


def test_skill_index_helper_without_root_or_path():
    from lightmind.model import Skill
    sk = [Skill("b", "second\nline"), Skill("a", "first")]
    assert C.skill_index(sk) == "- a: first\n- b: second line"
    assert C.skill_index(sk, "off") == ""
    assert C.skill_index([Skill("c", "d", path="/x/y/SKILL.md")], root=Path("/x")) == "- c: d (y/SKILL.md)"


def test_agent_loadout_and_tools_off_never_narrow_the_skill_index(fresh):
    no_skills(fresh)
    add_skill(fresh, "alpha")
    add_skill(fresh, "beta")
    ag = Agent(name="a", tools=[ToolRef("skill", "beta")])
    ctx = build(fresh, agent=ag)
    assert [s.name for s in ctx.skills] == ["alpha", "beta"] and "- alpha:" in ctx.prefix
    off = build(fresh, session=SessionState(tools_removed=["alpha", "beta"]))
    assert [s.name for s in off.skills] == ["alpha", "beta"] and "- beta:" in off.prefix


def test_required_skills_are_inlined(fresh):
    add_skill(fresh, "alpha", body="ALPHA FULL TEXT")
    edit(fresh, "required_skills: []", "required_skills: [alpha]")
    t = dict(build(fresh).blocks)
    assert "## Skill: alpha\nALPHA FULL TEXT" in t["required_skills"] and "alpha:" in t["skill_index"]


def test_preload_budget_comes_from_lightmind_md(fresh):
    add_skill(fresh, "pdfhelp", extra="preload_when: pdf\n", body="P" * 800)
    task = Task(goal="split a PDF")
    assert "P" * 800 in build(fresh, task=task, builtin_schemas=[]).prefix
    edit(fresh, "preload_max_tokens: 8000", "preload_max_tokens: 10")
    assert "P" * 800 not in build(fresh, task=task, builtin_schemas=[]).prefix
    assert "P" * 800 not in build(fresh, task=task, builtin_schemas=[{"name": "x"}]).prefix


def test_prefix_is_byte_stable_and_state_is_volatile(fresh):
    a = build(fresh, now=datetime(2026, 10, 20, 10, 0), state_info={"start": "one"})
    b = build(fresh, now=datetime(2026, 10, 20, 11, 0), state_info={"start": "two"})
    assert a.prefix == b.prefix and a.state != b.state
    assert build(fresh).prefix == build(fresh).prefix


def test_settings_helpers(fresh):
    assert C.handover_at(fresh) == 0.7 and C.context_settings(fresh)["tool_output_inline"] == 2000
    edit(fresh, "handover_at: 0.7", "handover_at: 0.5")
    assert C.handover_at(fresh) == 0.5


# ---------------------------------------------------------------- state block
def test_state_block_has_time_server_secrets_and_the_start_output(fresh):
    ctx = build(fresh, granted_secrets=["stripe/test", "mailhost/main"],
                state_info={"start": "Vault connected · 368 notes · main · clean"})
    assert ctx.state == ("State\ntime: 2026-10-20 15:05 Tue\nserver: laptop\n"
                         "secrets granted: mailhost/main, stripe/test\n"
                         "start: Vault connected · 368 notes · main · clean")


def test_state_block_defaults_and_overrides(fresh):
    assert build(fresh).state == "State\ntime: 2026-10-20 15:05 Tue\nserver: laptop"       # unknown grants: no line
    assert "secrets granted: none" in build(fresh, granted_secrets=[]).state
    s = build(fresh, server="buildbox", state_info={"time": "noon", "run": "runs/r1", "empty": ""}).state
    assert s == "State\ntime: noon\nserver: buildbox\nrun: runs/r1"


def test_state_block_prints_the_facts_the_session_passes(fresh):
    """loop.py hands over date, server, mode, model, account, run and the on_start outputs as `start`."""
    s = build(fresh, state_info={"date": "2026-10-20", "server": "buildbox", "mode": "auto", "model": "sonnet",
                                 "account": "claude-abo", "run": "runs/r1",
                                 "start": "Vault connected · 3 notes · main · clean"}).state
    assert s == ("State\ntime: 2026-10-20 15:05 Tue\nserver: buildbox\ndate: 2026-10-20\nmode: auto\n"
                 "model: sonnet\naccount: claude-abo\nrun: runs/r1\n"
                 "start: Vault connected · 3 notes · main · clean")


def test_state_block_is_after_the_breakpoint_only(fresh):
    edit(fresh, '"--breakpoint--", state, task]', '"--breakpoint--", task]')
    assert build(fresh).state == ""
    assert "State" not in build(fresh).prefix


def test_state_never_leaks_into_the_prefix(fresh):
    ctx = build(fresh, state_info={"start": "Vault connected"}, granted_secrets=["a/b"])
    assert "Vault connected" not in ctx.prefix and "a/b" not in ctx.prefix


def test_handover_helpers_and_initial_messages(fresh):
    assert C.handover_needed(70, 100, 0.7) and not C.handover_needed(69, 100, 0.7)
    orig = Task(goal="build it", limits={"usd": 2}, check="pytest")
    t = C.handover_task("junk\n## Handover\ndone: a\nnext: b\n<!-- lm:end status=handover -->\n", orig)
    assert "next: b" in t.goal and "junk" not in t.goal and t.limits == {"usd": 2}
    m = C.initial_messages(build(fresh), Task(goal="G"), "hello")
    assert len(m) == 1 and "G" in m[0].text and "hello" in m[0].text
