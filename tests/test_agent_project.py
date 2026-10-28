"""`project:` in an agent's agentMaster.md: the agent's default project.  A chat or run with the agent loads that project
(memory and assets) when none is chosen, `-p` wins, it grants nothing, a missing one is an error before a run exists, and
`lm lint` says so."""
import json

import pytest

from lightmind import checks, cli
from lightmind import loop as L
from lightmind import rules as R
from lightmind.model import RuleContext, Text, Usage

from test_cli import lm, script          # noqa: F401  (the `lm` fixture)
from test_loop_session import fakes, shome            # noqa: F401  (fixtures)
from test_loop_support import collect
from test_loop_session import script as script_fake


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def make_agent(home, name="webdev", project=None, extra=""):
    front = f"description: builds sites\n{extra}" + (f"project: {project}\n" if project is not None else "")
    put(home.path("agents") / name / home.filename("agent"), f"---\n{front}---\nYou build sites.\n")


def make_project(home, name, memory="", tools=""):
    d = home.path("projects") / name
    put(d / home.filename("project_memory"), f"---\n---\n{memory}\n")
    if tools:
        put(d / home.filename("project_tools"), tools)


# ------------------------------------------------------------------ reading
def test_the_agent_carries_its_project(shome):
    make_agent(shome, "webdev", "site")
    make_agent(shome, "plain")
    make_agent(shome, "blank", extra="project:\n")
    assert shome.load_agent("webdev").project == "site"
    assert shome.load_agent("plain").project == "" and shome.load_agent("blank").project == ""
    assert "project" not in shome.load_agent("webdev").extra              # it is a field now, not an unknown key


def test_has_project_wants_a_valid_name_and_a_folder(shome):
    make_project(shome, "site")
    (shome.path("projects") / "_hidden").mkdir()
    assert shome.has_project("site")
    assert not shome.has_project("nope") and not shome.has_project("") and not shome.has_project("_hidden")
    assert not shome.has_project("../agents") and not shome.has_project("a/b") and not shome.has_project("Site")


# ------------------------------------------------------------------ a session with the agent
async def test_a_chat_with_the_agent_loads_its_project_and_an_explicit_one_wins(shome, fakes):
    make_agent(shome, "webdev", "site")
    make_project(shome, "site", "MEMORY-OF-SITE")
    make_project(shome, "other", "MEMORY-OF-OTHER")
    s = L.open_session(shome, agent="webdev")
    assert s.project == "site" and s.info.project == "site"
    assert s.rule_ctx.project_dir.endswith("projects/site") or s.rule_ctx.project_dir.endswith("projects\\site")
    script_fake(fakes["abo"], [Text("ok"), Usage()])
    await collect(s.send("hi"))
    assert "MEMORY-OF-SITE" in fakes["abo"].requests[0].system and "MEMORY-OF-OTHER" not in fakes["abo"].requests[0].system
    s.close()
    o = L.open_session(shome, agent="webdev", project="other")             # `-p` wins
    assert o.project == "other"
    script_fake(fakes["abo"], [Text("ok"), Usage()])
    await collect(o.send("hi"))
    assert "MEMORY-OF-OTHER" in fakes["abo"].requests[1].system and "MEMORY-OF-SITE" not in fakes["abo"].requests[1].system
    o.close()


def test_the_project_of_the_agent_goes_into_the_record_so_a_resume_keeps_it(shome, fakes):
    make_agent(shome, "webdev", "site")
    make_project(shome, "site")
    s = L.open_session(shome, agent="webdev")
    s.close()
    assert L.resume_session(shome, s.id).project == "site"


def test_without_a_project_nothing_changes(shome, fakes):
    make_agent(shome, "plain")
    assert L.open_session(shome, agent="plain").project == "" and L.open_session(shome).project == ""


def test_a_project_of_the_agent_that_is_gone_fails_before_a_run_exists(shome, fakes):
    make_agent(shome, "webdev", "gone")
    before = sorted(p.name for p in shome.path("runs").glob("*")) if shome.path("runs").exists() else []
    with pytest.raises(ValueError, match="agent 'webdev': its project 'gone' does not exist"):
        L.open_session(shome, agent="webdev")
    assert (sorted(p.name for p in shome.path("runs").glob("*")) if shome.path("runs").exists() else []) == before
    s = L.open_session(shome, agent="webdev", project="site") if shome.has_project("site") else None
    assert s is None                                                       # an explicit project is judged by itself
    make_project(shome, "site")
    assert L.open_session(shome, agent="webdev", project="site").project == "site"   # so `-p` rescues a broken default


@pytest.mark.parametrize("bad", ["../agents", "a/b", "..", "Site", "a b"])
def test_a_project_name_that_leaves_the_folder_is_refused(shome, fakes, bad):
    make_agent(shome, "webdev", f'"{bad}"')
    with pytest.raises(ValueError, match="does not exist"):
        L.open_session(shome, agent="webdev")


def test_the_project_of_an_agent_grants_nothing(shome, fakes):
    """The project's tools.md lists a secret, but a project is memory and assets only: the rules of a chat with and without
    it are the same, and the secret stays shut (layer 1 denies secret(*))."""
    make_agent(shome, "webdev", "site")
    make_agent(shome, "plain")
    make_project(shome, "site", "m", tools="# Secrets\n- stripe/main\n# Servers\n- buildbox\n")
    with_p, without = L.open_session(shome, agent="webdev"), L.open_session(shome, agent="plain")
    assert with_p.project == "site" and without.project == ""
    ctx = RuleContext(home=str(shome.root), run_dir=str(shome.root / "runs" / "x"))
    for s in (with_p, without):
        layers = R.build_layers(shome, s.agent, s.mode, s.state)
        for check in (R.Check("secret", "stripe/main"), R.Check("secret-write", "stripe/main")):
            assert R.decide(layers, check, ctx).effect == "deny", (s.project, check)
    texts = lambda s: [r.text() for _, r in R.resolved_rules(R.build_layers(shome, s.agent, s.mode, s.state))]
    assert texts(with_p) == texts(without)


# ------------------------------------------------------------------ lm run and lm lint
def test_lm_run_uses_the_project_of_the_agent_and_dash_p_wins(lm):
    from lightmind.files import Home
    h = Home(lm.tree)
    make_agent(h, "webdev", "site")
    make_project(h, "site")
    make_project(h, "other")
    from lightmind.model import Text, Usage
    script(lm, [Text("one"), Usage()], [Text("two"), Usage()])
    def projects_of_runs():
        return {p.parent.name: json.loads(p.read_text())["project"] for p in (lm.tree / "runs").glob("*/meta.json")}
    assert lm("run", "-a", "webdev", "x")[0] == 0
    (first,) = projects_of_runs().values()
    assert first == "site"                                                 # the agent's own default
    seen = set(projects_of_runs())
    assert lm("run", "-a", "webdev", "-p", "other", "x")[0] == 0
    (second,) = [v for k, v in projects_of_runs().items() if k not in seen]
    assert second == "other"                                               # `-p` wins


def test_lm_run_with_a_missing_project_of_the_agent_is_a_usage_error(lm):
    from lightmind.files import Home
    make_agent(Home(lm.tree), "webdev", "gone")
    code, out, err = lm("run", "-a", "webdev", "x")
    assert code == 2 and "its project 'gone' does not exist" in err and lm.fake.requests == []


def test_lm_lint_checks_the_agents_and_their_projects(lm, capsys):
    from lightmind.files import Home
    h = Home(lm.tree)
    code, out, _ = lm("lint")
    assert code == 0 and "agents" in out and "found" in out
    make_agent(h, "webdev", "site")
    assert lm("lint")[0] == 1                                              # the project is missing
    code, out, _ = lm("--json", "lint")
    rows = json.loads(out)
    assert code == 1 and any(r["status"] == "fail" and "webdev" in r["detail"] and "'site'" in r["detail"] for r in rows)
    make_project(h, "site")
    assert lm("lint")[0] == 0


def test_lm_lint_names_an_agent_file_that_does_not_read(lm):
    from lightmind.files import Home
    h = Home(lm.tree)
    put(h.path("agents") / "broken" / h.filename("agent"), "---\nbudget:\n  tokens: [oops\n---\nbody\n")   # nested and not YAML: the lenient reader refuses it too
    code, out, _ = lm("lint")
    assert code == 1 and "broken" in out


def test_the_agent_check_is_not_part_of_the_boot_checklist(shome):
    assert "agents" not in [n for n, _ in checks.groups(shome)]            # the checklist keeps its fixed groups


# ------------------------------------------------------------------ what is shipped
def test_the_example_agent_and_the_readme_name_the_field():
    from importlib import resources
    base = resources.files("lightmind") / "templates" / "agents"
    example = (base / "_example" / "agentMaster.md").read_bytes().decode("utf-8")
    readme = (base / "README.md").read_bytes().decode("utf-8")
    assert "# project:" in example and "`-p` wins" in example
    assert "| `project` |" in readme and "`lm lint`" in readme and "grants nothing" in readme
