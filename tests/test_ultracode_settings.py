"""`/ultracode on | off | status`, the effort ladder and the `workflow:` settings: each one is read from config.md and changes
what the runtime does (every test has its counter-case with the setting left out).  The runtime itself: test_orchestrate.py."""
import re
from types import SimpleNamespace

import pytest

from lightmind import core, settings as S, slash, ultracode, watch
from lightmind import orchestrate as O
from lightmind.files import CONFIG_KEYS, Home
from lightmind.model import Text, ToolCall, Usage
from lightmind.providers.base import SubagentStart
from lightmind.tui import data

from test_loop_support import SessionHome, default_settings, write
from test_orchestrate import SCHEMA, git, journal, logs, make, result, say, script, send, tool_text, who, wf  # noqa: F401 - `make` is a fixture
from test_tui_slash import has, popup, prov, typed  # noqa: F401 - `prov` is a fixture
from test_tui_support import idle, open_chat, running, say as say_in_chat, until

BIG = [Text("big"), Usage(12_000, 5)]            # more tokens than the lowest budget the settings allow


# ------------------------------------------------------------------ the settings as a set
def test_the_template_lists_every_workflow_key_once_with_its_default_and_the_bounds_match_the_page(home):
    text = (home.config_file).read_text("utf-8")
    block = text.split("\nworkflow:\n", 1)[1].split("\n\n", 1)[0]
    keys = re.findall(r"^  (\w+):", block, re.M)
    assert keys == list(CONFIG_KEYS["workflow"]) and all("#" in ln for ln in block.splitlines() if ln.startswith("  "))
    fields = {f.path[1]: f for f in S.GENERAL if f.path[0] == "workflow"}
    assert set(fields) == set(keys)
    for key, (default, low, high) in ultracode.BOUNDS.items():                    # one list for the page and the runtime
        assert (fields[key].low, fields[key].high) == (low, high), key
    assert ultracode.read(home) == ultracode.Config(                              # the template holds exactly the built-in defaults
        max_agents=1000, max_items=4096, budget=None, model="", effort="max", schema_retries=3, skeptics=3,
        dry_rounds=2, tidy_worktrees=True, notify="off", notify_after=10)


def test_the_workflow_fields_are_the_page_ultracode_and_nothing_else_is(home):
    page = S.read_general(home)
    on = {r.field.path for r in page.rows if S.area_of(r.field) == "ultracode"}
    assert on == {f.path for f in S.GENERAL if f.path[0] == "workflow"} and len(on) == len(CONFIG_KEYS["workflow"])
    assert all(S.area_of(r.field) == "general" for r in page.rows if r.field.path[0] != "workflow")      # the counter-case


def test_every_new_field_offers_values_the_check_accepts(home):
    page = S.read_general(home)
    values = page.values()
    for f in (f for f in S.GENERAL if f.path[0] == "workflow"):
        offered = [o for o in S.options(home, f, values) if o[0] is not None]
        assert offered, f.path
        assert all(S.check_value(home, f, o[0], values) == "" for o in offered), f.path
    budget = next(f for f in S.GENERAL if f.path == ("workflow", "budget"))
    assert S.check_value(home, budget, 5) != "" and S.check_value(home, budget, 2_000_000) == ""
    assert [o[1] for o in S.options(home, budget) if o[0] == 2_000_000] == ["2M"]


def test_a_changed_workflow_setting_is_saved_in_its_own_line_of_config_md(home):
    page = S.save_page(home, S.read_general(home), {("workflow", "skeptics"): 5, ("workflow", "budget"): 2_000_000,
                                                    ("workflow", "notify"): "ntfy"})
    assert {r.field.path[1]: r.value for r in page.rows if r.field.path[0] == "workflow"}["skeptics"] == 5
    cfg = ultracode.read(Home(home.root))
    assert (cfg.skeptics, cfg.budget, cfg.notify) == (5, 2_000_000, "ntfy")


def test_wrong_values_in_the_file_are_clamped_or_fall_back(tree):
    cfg = ultracode.read(SessionHome(tree, default_settings(workflow={
        "max_agents": 10 ** 9, "max_items": 0, "schema_retries": "x", "skeptics": -4, "dry_rounds": 99, "notify_after": 10 ** 6,
        "effort": "ultracode", "notify": "carrier pigeon", "model": "not a model!", "budget": 5, "tidy_worktrees": "no"})))
    assert (cfg.max_agents, cfg.max_items, cfg.schema_retries, cfg.skeptics, cfg.dry_rounds, cfg.notify_after) == (10000, 1, 3, 1, 10, 1440)
    assert (cfg.effort, cfg.notify, cfg.model, cfg.budget, cfg.tidy_worktrees) == ("max", "off", "", 10_000, True)
    assert ultracode.read(SessionHome(tree, default_settings(workflow={"budget": "2m", "effort": "LOW"}))).budget == 2_000_000
    assert ultracode.read(SessionHome(tree, default_settings(workflow={"effort": "LOW"}))).effort == "low"


# ------------------------------------------------------------------ the effort ladder
def test_ultracode_stands_right_after_max_everywhere_a_level_is_chosen(home):
    assert core.efforts()[-2:] == ("max", "ultracode") and slash.efforts()[-2:] == ("max", "ultracode")
    assert S.EFFORTS[-2:] == ("max", "ultracode") and data.efforts()[-2:] == ("max", "ultracode")
    page = S.read_general(home)
    for path in (("defaults", "effort"), ("vscode", "effort"), ("quick", "effort"), ("remote", "effort")):
        f = next(f for f in S.GENERAL if f.path == path)
        names = [o[0] for o in S.options(home, f, page.values()) if o[0] is not None]
        assert names[-2:] == ["max", "ultracode"], path
    agent = next(f for f in S.AGENT if f.path == ("effort",))
    assert [o[0] for o in S.options(home, agent) if o[0]][-2:] == ["max", "ultracode"]
    work = next(f for f in S.GENERAL if f.path == ("workflow", "effort"))            # the counter-case: an agent has no such level
    assert [o[0] for o in S.options(home, work)] == ["low", "medium", "high", "xhigh", "max"]
    assert S.check_value(home, work, "ultracode") != ""


# ------------------------------------------------------------------ /ultracode
async def test_ultracode_on_off_goes_back_to_the_effort_before(make):
    s, r, home = make({"MAIN": [say("a")]}, effort="high")
    on = core.command(home, s, "ultracode", "")
    assert on.ok and s.effort == "ultracode" and s.effort_before == "high" and "ultracode is on" in on.text and "goes back to high" in on.text
    assert core.command(home, s, "ultracode", "on").text == "ultracode is on already" and s.effort_before == "high"
    off = core.command(home, s, "ultracode", "off")
    assert off.ok and s.effort == "high" and "effort high" in off.text                  # back to what it was, not to a fixed level
    assert core.command(home, s, "ultracode", "off").text == "ultracode is off already (effort high)" and s.effort == "high"
    core.command(home, s, "effort", "xhigh")
    assert core.command(home, s, "effort", "ultracode").text.startswith("ultracode is on") and s.effort_before == "xhigh"
    core.command(home, s, "ultracode", "off")
    assert s.effort == "xhigh"


async def test_ultracode_off_without_a_remembered_effort_is_max(make):
    s, _, home = make({"MAIN": [say("a")]}, effort="ultracode")
    assert s.effort_before == ""
    assert core.command(home, s, "ultracode", "off").text == "ultracode is off: effort max" and s.effort == "max"


async def test_the_earlier_effort_survives_a_resume(make):
    from lightmind import loop as L
    s, _, home = make({"MAIN": [say("a")]}, effort="low")
    core.command(home, s, "ultracode", "on")
    assert L.read_trusted(home, s.id)["effort_before"] == "low"
    s.close()
    r = L.resume_session(home, s.id)
    assert r.effort == "ultracode" and r.effort_before == "low"
    assert core.command(home, r, "ultracode", "off").text == "ultracode is off: effort low"        # the way back survives the resume


async def test_ultracode_status_lists_the_state_and_the_effective_settings(make):
    s, _, home = make({"MAIN": [say("a")]}, workflow={"max_agents": 77, "skeptics": 5, "model": "haiku", "notify": "both", "notify_after": 3},
                      effort="high")
    off = core.command(home, s, "ultracode", "status").text
    assert off.startswith("ultracode: off  (effort high)") and "at most 77 per run" in off
    core.command(home, s, "ultracode", "on")
    on = core.command(home, s, "ultracode", "status").text
    assert on.startswith("ultracode: on  (effort ultracode, off goes back to high)")
    for want in ("sceptics: 5", "agent model: haiku", "agent effort: max", "both after 3 min", "budget: none", "ask before a workflow: no",
                 "the word ultracode switches it on: yes", "RAM per agent / kept free: 1024 / 2048 MB"):
        assert want in on, want
    assert core.command(home, s, "ultracode", "sideways").ok is False


async def test_the_command_comes_through_the_slash_parser_of_every_surface_and_a_skill_does_not_hide_it(make):
    s, _, home = make({"MAIN": [say("a")]}, effort="high")
    write(home.path("skills") / "ultracode" / "SKILL.md", "---\nname: ultracode\ndescription: the long manual\n---\nread me\n")
    cmds = core.skill_commands(home, core.SERVE_SLASH)
    assert "ultracode" not in cmds and "skill:ultracode" in cmds                    # the skill is still there, as `/skill ultracode`
    assert "ultracode" in dict(core.BUILTIN_SLASH) and "ultracode" in dict(data.slash_commands(home))
    assert data.slash_commands(home)[[n for n, _ in data.slash_commands(home)].index("ultracode")][1].startswith("ultracode on | off | status")
    p = slash.parse("/ultracode off then explain X", core.catalog(home, s))
    assert p.error == "" and [(a.name, a.arg) for a in p.acts] == [("ultracode", "off")] and p.skills == [] and p.text == "then explain X"
    assert slash.parse("/ultracode", core.catalog(home, s)).acts[0].name == "ultracode"
    bad = slash.parse("/ultracode sideways", core.catalog(home, s))
    assert bad.error == "ultracode: unknown 'sideways'; use on, off, status"
    prep = core.prepare(home, s, "/ultracode")                                      # the path of the plugin chat and of the phone
    assert s.effort == "ultracode" and prep.message is None and prep.notes[0][1].startswith("ultracode is on")
    prep = core.prepare(home, s, "/ultracode status")
    assert prep.notes[0][1].startswith("ultracode: on")
    old = slash.Catalog(("mode", "model"), {"ultracode": cmds["skill:ultracode"]})            # the counter-case: without the command ...
    assert slash.parse("/ultracode", old).skills == ["ultracode"]                   # ... the skill of that name answers


def test_the_phone_lists_the_command_and_may_use_it(home):
    assert "ultracode" in dict(core.BUILTIN_SLASH) and "ultracode" not in core.RC_DENY


async def test_the_chat_screen_runs_the_command_and_completes_its_words(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await typed(pilot, "/ultra")
        assert "/ultracode" in popup(app) and "on | off | status" in popup(app)
        await typed(pilot, "/ultracode ")
        assert "on" in popup(app) and "off" in popup(app) and "status" in popup(app)
        await typed(pilot, "/ultracode s")
        assert "status" in popup(app) and " off " not in popup(app)
        await say_in_chat(pilot, "/ultracode")
        await until(pilot, lambda: app.chat.s.info.effort == "ultracode")
        assert has(app, "ultracode is on")
        await say_in_chat(pilot, "/ultracode off")
        await until(pilot, lambda: app.chat.s.info.effort != "ultracode")
        assert has(app, "ultracode is off")
        await say_in_chat(pilot, "/ultracode status")
        await until(pilot, lambda: has(app, "ultracode: off"))
        await idle(pilot, app)


# ------------------------------------------------------------------ each setting changes what the runtime does
async def test_max_agents_limits_the_agents_of_a_run(make):
    body = "return [await agent('MA-1'), await agent('MA-2'), await agent('MA-3')]"
    agents = {f"MA-{i}": [say(f"r{i}")] for i in (1, 2, 3)}
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"max_agents": 2})
    evs = await send(s)
    assert result(s) == ["r1", "r2", None] and any("limit of 2 agents per run reached (workflow.max_agents)" in x for x in logs(evs))
    assert "limits: " in tool_text(s) and "2 per run" in tool_text(s)
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents})
    await send(s)
    assert result(s) == ["r1", "r2", "r3"] and "1000 per run" in tool_text(s)


async def test_max_items_limits_one_parallel(make):
    body = "return await parallel([lambda: agent('MI-1'), lambda: agent('MI-2'), lambda: agent('MI-3')])"
    agents = {f"MI-{i}": [say(f"r{i}")] for i in (1, 2, 3)}
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"max_items": 2})
    evs = await send(s)
    assert result(s) == ["r1", "r2", None] and any("only the first 2 run" in x for x in logs(evs))
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents})
    await send(s)
    assert result(s) == ["r1", "r2", "r3"]


async def test_the_default_budget_stops_a_workflow_and_a_budget_of_the_call_wins(make):
    body = "return [await agent('BU-1'), await agent('BU-2')]"
    agents = {"BU-1": [BIG], "BU-2": [BIG]}
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"budget": 10_000})
    evs = await send(s)
    assert result(s) == ["big", None] and "BU-2" not in who(r) and "budget 10000 tokens" in tool_text(s)
    s, r, _ = make({"MAIN": [wf(script(body), budget="+50k"), say("ok")], **agents}, workflow={"budget": 10_000})
    await send(s)
    assert result(s) == ["big", "big"] and "budget 50000 tokens" in tool_text(s)      # the call's own budget wins
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents})
    await send(s)
    assert result(s) == ["big", "big"] and "budget none tokens" in tool_text(s)


async def test_the_agent_model_is_the_workflows_own_until_the_call_names_one(make):
    body = "return [await agent('MO-1'), await agent('MO-2', model='opus')]"
    agents = {"MO-1": [say("a")], "MO-2": [say("b")]}
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"model": "zeta-1"})
    await send(s)
    models = {w: q.model for w, q in r.seen}
    assert models["MO-1"] == "zeta-1" and models["MO-2"] == "opus"                   # the call's model beats the setting
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents})
    await send(s)
    models = {w: q.model for w, q in r.seen}
    assert models["MO-1"] == models["MAIN"] != "zeta-1"                              # the counter-case: not set, the chat's model


async def test_the_agent_effort_is_max_by_default_the_setting_changes_it_and_the_call_wins(make):
    body = "return [await agent('EF-1'), await agent('EF-2', effort='high')]"
    agents = {"EF-1": [say("a")], "EF-2": [say("b")]}
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents}, effort="medium")
    await send(s)
    assert {w: q.effort for w, q in r.seen if w != "MAIN"} == {"EF-1": "max", "EF-2": "high"}     # not the chat's `medium`
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **agents}, workflow={"effort": "low"}, effort="medium")
    await send(s)
    assert {w: q.effort for w, q in r.seen if w != "MAIN"} == {"EF-1": "low", "EF-2": "high"}


async def test_schema_retries_are_the_settings_number(make):
    body = f"return await agent('SR-BAD', schema={SCHEMA})"
    bad = {"SR-BAD": [[ToolCall("s1", "StructuredOutput", {"n": "x"}), Usage(3, 1)], say("no")]}
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **bad}, workflow={"schema_retries": 1})
    evs = await send(s)
    assert result(s) is None and [e.id for e in evs if isinstance(e, SubagentStart)] == ["wf-1.a1", "wf-1.a1.2"]    # 1 + 1 tries
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **bad}, workflow={"schema_retries": 0})
    evs = await send(s)
    assert [e.id for e in evs if isinstance(e, SubagentStart)] == ["wf-1.a1"]
    s, r, _ = make({"MAIN": [wf(script(body)), say("ok")], **bad})
    evs = await send(s)
    assert len([e for e in evs if isinstance(e, SubagentStart)]) == 4                                 # the default: 1 + 3


async def test_sceptics_and_dry_rounds_go_into_the_ultracode_text(make):
    s, r, _ = make({"MAIN": [say("a")]}, workflow={"skeptics": 5, "dry_rounds": 4}, effort="ultracode")
    await send(s, "MAIN go")
    system = r.seen[-1][1].system
    assert "5 independent sceptics" in system and "until 4 rounds in a row bring nothing new" in system
    s, r, _ = make({"MAIN": [say("a")]}, effort="ultracode")
    await send(s, "MAIN go")
    assert "3 independent sceptics" in r.seen[-1][1].system and "until 2 rounds in a row" in r.seen[-1][1].system


@pytest.mark.parametrize("tidy", [True, False])
async def test_an_unchanged_worktree_is_removed_only_when_asked_to(make, tmp_path, tidy):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "start")
    s, r, _ = make({"MAIN": [wf(script("return await agent('TW-1', isolation='worktree')")), say("ok")], "TW-1": [say("looked")]},
                   workflow={"tidy_worktrees": tidy}, workdir=str(repo))
    await send(s)
    branches = git(repo, "branch", "--list", "lm/*")
    assert bool(branches) is (not tidy)
    assert ("kept" in tool_text(s)) is (not tidy) and ("no changes, removed" in tool_text(s)) is tidy


async def test_a_long_workflow_reports_its_end_a_short_one_or_a_switched_off_one_does_not(make, monkeypatch):
    sent = []
    monkeypatch.setattr(watch, "notify", lambda home, text, key="", where="watch.notify", default="both": sent.append((text, key, where, default)) or True)
    body = "return await agent('NO-1')"

    async def run(workflow, minutes):
        ticks = iter([0.0, minutes * 60.0])
        monkeypatch.setattr(O, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        s, _, _ = make({"MAIN": [wf(script(body)), say("ok")], "NO-1": [say("x")]}, workflow=workflow)
        await send(s)
        return s
    s = await run({"notify": "ntfy", "notify_after": 5}, 7)
    assert len(sent) == 1 and sent[0][2:] == ("workflow.notify", "off") and "wf-1" in sent[0][0] and "done after 7 min" in sent[0][0]
    assert sent[0][1] == f"workflow:{s.id}:wf-1"
    await run({"notify": "ntfy", "notify_after": 5}, 3)                      # ended sooner than notify_after
    await run({"notify": "off", "notify_after": 5}, 7)                       # switched off
    await run({"notify_after": 5}, 7)                                        # not set: off
    assert len(sent) == 1


def test_the_workflow_push_goes_where_workflow_notify_says_and_nowhere_by_default(tree):
    (tree / "tools" / "ntfy").mkdir(parents=True, exist_ok=True)
    (tree / "tools" / "ntfy" / "notify.py").write_text("", "utf-8")
    (tree / "tools" / "telegram").mkdir(parents=True, exist_ok=True)
    home = SessionHome(tree, default_settings(workflow={"notify": "ntfy"}, watch={"notify": "both"}))
    assert watch.channels_of(home, "workflow.notify", "off") == ["ntfy"]
    assert watch._channels(home) == ["telegram", "ntfy"]                      # the watch's own setting is unchanged
    quiet = SessionHome(tree, default_settings(workflow={}, watch={"notify": "both"}))
    assert watch.channels_of(quiet, "workflow.notify", "off") == []
    both = SessionHome(tree, default_settings(workflow={"notify": "both"}))
    assert watch.channels_of(both, "workflow.notify", "off") == ["telegram", "ntfy"]
