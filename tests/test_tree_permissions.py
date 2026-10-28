"""The shipped permissions.md, judged by the rule checker: normally open, secrets and grants protected."""
import os
import re
import warnings

import pytest

from lightmind import rules as R
from lightmind.files import ENTRY, Home
from lightmind.init import init_tree
from lightmind.model import Agent, Check, Rule, RuleContext, SessionState, Tool

MODES = ("auto", "plan", "bypass")
GRANT_FILES = ["permissions.md", "lightmind.md", "config.md", "rechte.md", "modes/auto.md", "modes/mine/x.md", "agents/x/agentMaster.md", "agents/x/tools.md",
               "workflows/w/agentMaster.md", "workflows/w/tools.md", "projects/p/tools.md", "providers/new.md",
               "tools/vault/status.py", "tools/vault/README.md", "tools/vault/lib/helper.py",
               "triggers/daily.md", "triggers/_example.md", "triggers/sub/x.md",
               "runs/20261020-101500-chat-ab12/events.jsonl", "runs/r1/meta.json", "runs/r1/session.md"]
PLAIN_FILES = ["agents/x/notes.md", "agents/README.md", "workflows/w/workflow.py", "projects/p/projectMemory.md",
               "projects/p/projectAssets.md", "skills/s/SKILL.md", "tools/t/run.py", "triggers-old/t.md",
               "servers/s/info.md", "vault/note.md", "runs/r1/log.md", "secrets/stripe/info.md", "README.md",
               "agents/x/deeper/tools.md", "tools/vaultish/run.py", "tools/_example/example.py",
               "vault/tools/vault/x.md", "projects/p/tools/vault/a.py", "systems/x.md", "agents/system/x.md",
               "runs/r1/task.md", "runs/r1/tool-out/1-bash.txt", "runs/r1/out/events.jsonl", "runs/events.jsonl",
               "runs/r1/events.jsonl.bak", "runs/r1/Session.md.txt"]
ENGINE_FILES = ["system/state/ledger.jsonl", "system/state/templates.json", "system/state/tools-approved.json",
                "system/claude/claude-abo/.credentials.json", "system/bin/uv", "system/python/x", "system/cache/x",
                "system/browsers/x", "system/runtime-windows-x86_64/Scripts/lm.exe", "system/lm", "system/lm.cmd",
                "system/README.md"]
GRANT_PATTERNS = 19                 # the lines of `grant_files` in the shipped permissions.md


@pytest.fixture
def home(tmp_path) -> Home:
    init_tree(tmp_path / "lm")
    return Home(tmp_path / "lm")


def ctx(home):
    return RuleContext(home=str(home.root), run_dir=str(home.root / "runs" / "r1"),
                       project_dir=str(home.root / "projects" / "p"))


def decide(home, mode, name, args, agent=None, session=None, tool=None):
    layers = R.build_layers(home, agent, mode, session)
    return R.decide_all(layers, R.checks_for_call(name, args, tool_meta=tool), ctx(home))


def secret(home, mode, ref, kind="secret", agent=None, session=None):
    return R.decide(R.build_layers(home, agent, mode, session), Check(kind, ref), ctx(home))


# ------------------------------------------------------------ normally open
@pytest.mark.parametrize("mode", ("auto", "bypass"))
def test_everything_else_is_allowed(home, mode):
    for name, args in (("read", {"path": "README.md"}), ("read", {"path": "C:/Windows/win.ini"}),
                       ("write", {"path": "projects/p/a.md"}), ("write", {"path": "C:/Users/x/Desktop/a.txt"}),
                       ("edit", {"path": "skills/s/SKILL.md"}), ("bash", {"command": "git push origin main"}),
                       ("bash", {"command": "rm -rf build"}), ("web", {"url": "https://example.com/x"}),
                       ("ssh", {"server": "gpubox", "command": "ls"}), ("spawn", {"agent": "webdev"}),
                       ("skill_load", {"name": "anything"})):
        assert decide(home, mode, name, args).effect == "allow", (name, args)


def test_plan_mode_reads_asks_for_bash_and_denies_writes(home):
    assert decide(home, "plan", "read", {"path": "README.md"}).effect == "allow"
    assert decide(home, "plan", "web", {"url": "https://example.com"}).effect == "allow"
    assert decide(home, "plan", "bash", {"command": "ls"}).effect == "ask"
    for tool in ("write", "edit"):
        for path in ("projects/p/a.md", "C:/Users/x/a.txt", *GRANT_FILES):
            assert decide(home, "plan", tool, {"path": path}).effect == "deny", (tool, path)


def test_agent_grant_lifts_the_plan_bash_ask_but_not_the_write_deny(home):
    a = Agent("a", permissions={"allow": ["bash(ls*)", "write(projects/**)"]})
    assert decide(home, "plan", "bash", {"command": "ls -la"}, agent=a).effect == "allow"
    assert decide(home, "plan", "write", {"path": "projects/p/a.md"}, agent=a).effect == "deny"


# ------------------------------------------------------------ secrets
@pytest.mark.parametrize("mode", MODES)
def test_secret_values_and_the_engine_are_locked_in_every_mode(home, mode):
    for tool in ("read", "write", "edit"):
        assert decide(home, mode, tool, {"path": "secrets/stripe/test.env"}).effect == "deny", tool
        assert decide(home, mode, tool, {"path": "system/state/ledger.jsonl"}).effect == "deny", tool
    assert decide(home, mode, "read", {"path": "secrets/stripe/info.md"}).effect == "allow"
    assert decide(home, mode, "bash", {"command": "cat secrets/stripe/test.env"}).effect == "deny"
    assert decide(home, mode, "read", {"path": "secrets/a/../stripe/test.env"}).effect == "deny"


@pytest.mark.parametrize("mode", MODES)
def test_the_whole_engine_folder_is_locked(home, mode):
    """runtime, logins, tokens, ledger, launchers: one folder, one lock, nothing of it moves out of reach."""
    for path in ENGINE_FILES:
        for tool in ("read", "write", "edit"):
            d = decide(home, mode, tool, {"path": path})
            assert d.effect == "deny" and d.rule.locked, (tool, path)
    for command in ("cat system/claude/claude-abo/.credentials.json", "cp system/claude/x /tmp/y",
                    "grep -r token system/claude"):
        assert decide(home, mode, "bash", {"command": command}).effect == "deny", command
    assert decide(home, mode, "bash", {"command": "ls system"}).effect == ("ask" if mode == "plan" else "allow")


def test_the_engine_lock_cannot_be_lifted(home):
    a = Agent("a", permissions={"allow": ["read(**)", "write(**)", "read(system/**)"]})
    s = SessionState(rules=[Rule("read", "**", "allow"), Rule("write", "system/**", "allow")])
    for path in ("system/claude/claude-abo/.credentials.json", "system/state/ledger.jsonl", "system/bin/uv"):
        for tool in ("read", "write"):
            assert decide(home, "bypass", tool, {"path": path}, agent=a, session=s).effect == "deny", (tool, path)


def test_engine_paths_follow_the_system_key(home):
    entry = home.root / ENTRY
    entry.write_text(entry.read_text(encoding="utf-8").replace("system: system/", "system: engine/"), encoding="utf-8")
    h = Home(home.root)
    assert decide(h, "bypass", "read", {"path": "engine/state/ledger.jsonl"}).effect == "deny"
    assert decide(h, "bypass", "read", {"path": "system/state/ledger.jsonl"}).effect == "allow"      # old name: nothing special


def test_secret_values_stay_locked_whatever_the_agent_or_session_says(home):
    a = Agent("a", permissions={"allow": ["read(**)", "write(**)"], "secrets": ["stripe/test"]})
    s = SessionState(rules=[Rule("read", "**", "allow"), Rule("bash", "*", "allow")])
    assert decide(home, "bypass", "read", {"path": "secrets/stripe/test.env"}, agent=a, session=s).effect == "deny"
    assert decide(home, "bypass", "bash", {"command": "cat secrets/stripe/test.env"}, agent=a, session=s).effect == "deny"


@pytest.mark.parametrize("mode", MODES)
def test_using_a_secret_needs_a_grant(home, mode):
    assert secret(home, mode, "stripe/test").effect == "deny"
    assert secret(home, mode, "stripe/test", "secret-write").effect == "deny"
    agent = Agent("a", permissions={"secrets": ["stripe/test"], "secrets_write": ["social"]})
    assert secret(home, mode, "stripe/test", agent=agent).effect == "allow"
    assert secret(home, mode, "stripe/other", agent=agent).effect == "deny"
    assert secret(home, mode, "social/ig", "secret-write", agent=agent).effect == "allow"
    sess = SessionState(rules=[Rule("secret", "mail/*", "allow")])
    assert secret(home, mode, "mail/main", session=sess).effect == "allow"
    assert secret(home, mode, "stripe/test", session=sess).effect == "deny"


def test_a_loaded_agent_gets_its_grant_from_tools_md(home):
    d = home.path("agents") / "mailer"
    d.mkdir(parents=True)
    (d / "agentMaster.md").write_text("---\nname: mailer\ndescription: d\n---\nbody\n", encoding="utf-8")
    (d / "tools.md").write_text("# Secrets\n- mailhost/main\n# Servers\n- buildbox\n", encoding="utf-8")
    a = home.load_agent("mailer")
    assert secret(home, "auto", "mailhost/main", agent=a).effect == "allow"
    assert secret(home, "auto", "mailhost/other", agent=a).effect == "deny"
    assert secret(home, "auto", "mailhost/main").effect == "deny"          # nobody else has it
    assert R.decide(R.build_layers(home, a, "auto"), Check("server", "buildbox"), ctx(home)).effect == "allow"
    assert R.decide(R.build_layers(home, a, "auto"), Check("server", "gpubox"), ctx(home)).effect == "allow"   # open


def test_a_bare_service_in_tools_md_grants_all_its_accounts(home):
    d = home.path("agents") / "m"
    d.mkdir(parents=True)
    (d / "agentMaster.md").write_text("---\nname: m\ndescription: d\n---\n", encoding="utf-8")
    (d / "tools.md").write_text("# Secrets\n- stripe\n", encoding="utf-8")
    a = home.load_agent("m")
    assert secret(home, "auto", "stripe/test", agent=a).effect == "allow"
    assert secret(home, "auto", "stripe/live", agent=a).effect == "allow"
    assert secret(home, "auto", "mail/x", agent=a).effect == "deny"


@pytest.mark.parametrize("row", ["mailhost: in fo", "mailhost: ../x", "mailhost: *", "mailhost: a, b",
                                 "*", "../x", "a/b/c", "mailhost/*", "ionos*", "mailhost/ma in", "/etc",
                                 "mailhost//main", "a/..", "mailhost: a/b"])
def test_a_malformed_secret_row_grants_nothing_not_even_the_folder(home, row):
    d = home.path("agents") / "bad"
    d.mkdir(parents=True)
    (d / "agentMaster.md").write_text("---\nname: bad\ndescription: d\n---\n", encoding="utf-8")
    (d / "tools.md").write_text(f"# Secrets\n- {row}\n- stripe/test\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="skipped"):
        a = home.load_agent("bad")
    assert a.secrets == ["stripe/test"] and a.permissions["secrets"] == ["stripe/test"]
    name = row.split(":")[0].split("/")[0].strip()
    for ref in ("mailhost/main", "mailhost/x", f"{name}/main", "other/y"):
        assert secret(home, "bypass", ref, agent=a).effect == "deny", ref


@pytest.mark.parametrize("row", ["*", "power*", "../x", "a/b", "pow erhouse", "buildbox; ls"])
def test_a_malformed_server_row_grants_nothing(home, row):
    d = home.path("agents") / "bad"
    d.mkdir(parents=True)
    (d / "agentMaster.md").write_text("---\nname: bad\ndescription: d\n---\n", encoding="utf-8")
    (d / "tools.md").write_text(f"# Servers\n- {row}\n- gpubox\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="skipped"):
        a = home.load_agent("bad")
    assert a.servers == ["gpubox"] and a.permissions["servers"] == ["gpubox"]


def test_well_formed_rows_do_not_warn(home):
    d = home.path("projects") / "p"
    d.mkdir(parents=True)
    (d / "tools.md").write_text("# Secrets\n- mailhost/main\n- mailhost: info\n- stripe\n- a.b_c-d/e.f\n"
                                "# Servers\n- buildbox\n- otherapp-server\n", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        p = home.load_project("p")
    assert p.secrets == ["mailhost/main", "mailhost/info", "stripe", "a.b_c-d/e.f"]
    assert p.servers == ["buildbox", "otherapp-server"]


# ------------------------------------------------------------ grant-bearing files
@pytest.mark.parametrize("path", GRANT_FILES)
def test_writing_a_grant_file_asks_in_auto(home, path):
    for tool in ("write", "edit"):
        d = decide(home, "auto", tool, {"path": path})
        assert d.effect == "ask", (tool, path)
        assert d.rule and d.rule.locked and d.rule.tool == "write"


@pytest.mark.parametrize("path", GRANT_FILES)
def test_bypass_never_asks_about_a_grant_file(home, path):
    """Bypass asks nothing: every grant file is a locked deny there (writing one unasked could lift every locked deny)."""
    for tool in ("write", "edit"):
        d = decide(home, "bypass", tool, {"path": path})
        assert d.effect == "deny" and d.rule.locked, (tool, path)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", PLAIN_FILES)
def test_other_files_are_not_asked_about(home, mode, path):
    expect = "deny" if mode == "plan" else "allow"
    assert decide(home, mode, "write", {"path": path}).effect == expect, path


def test_the_ask_is_a_locked_ask_nothing_can_lift(home):
    a = Agent("a", permissions={"allow": ["write(**)", "edit(**)", "write(lightmind.md)",
                                          "write(agents/x/tools.md)"]})
    s = SessionState(rules=[Rule("write", "**", "allow"), Rule("edit", "lightmind.md", "allow"),
                            Rule("write", "agents/x/tools.md", "allow")])
    for path in ("lightmind.md", "permissions.md", "agents/x/tools.md", "agents/x/agentMaster.md"):
        assert decide(home, "auto", "write", {"path": path}, agent=a, session=s).effect == "ask", path
        assert decide(home, "bypass", "write", {"path": path}, agent=a, session=s).effect == "deny", path


def test_the_asks_reach_a_path_however_it_is_written(home):
    for path in ("./lightmind.md", "agents/../lightmind.md", "agents/x/../x/tools.md", "AGENTS/X/TOOLS.MD"):
        eff = decide(home, "auto", "write", {"path": path}).effect
        if path.startswith("AGENTS") and os.name != "nt":
            continue                                       # case-insensitive match is a Windows thing
        assert eff == "ask", path
    absolute = str(home.root / "agents" / "x" / "tools.md")
    assert decide(home, "auto", "write", {"path": absolute}).effect == "ask"


def test_the_asks_follow_renamed_folders_and_files(home):
    p = home.root / ENTRY
    p.write_text(p.read_text(encoding="utf-8").replace("agents: agents/", "agents: bots/")
                 .replace("agent_tools: tools.md", "agent_tools: kit.md"), encoding="utf-8")
    h = Home(home.root)
    assert decide(h, "auto", "write", {"path": "bots/x/kit.md"}).effect == "ask"
    assert decide(h, "auto", "write", {"path": "bots/x/agentMaster.md"}).effect == "ask"
    assert decide(h, "auto", "write", {"path": "agents/x/tools.md"}).effect == "allow"      # old name: nothing special


def test_a_plan_mode_deny_beats_the_ask(home):
    d = decide(home, "plan", "write", {"path": "agents/x/tools.md"})
    assert d.effect == "deny" and d.layer == "mode:plan"


def test_ask_needs_an_asker_and_a_decline_denies(home):
    import asyncio
    layers = R.build_layers(home, None, "auto")
    checks = R.checks_for_call("write", {"path": "agents/x/tools.md"})
    assert asyncio.run(R.enforce(layers, checks, ctx(home), None, SessionState())).effect == "deny"

    async def no(q, c):
        return "no"

    async def always(q, c):
        return "always"

    sess = SessionState()
    assert asyncio.run(R.enforce(layers, checks, ctx(home), no, sess)).effect == "deny"
    assert asyncio.run(R.enforce(layers, checks, ctx(home), always, sess)).effect == "allow"      # this write only
    assert decide(home, "auto", "write", {"path": "agents/x/tools.md"}, session=sess).effect == "ask"


def test_template_list_is_one_shared_anchor(home):
    """The grant files are listed once (grant_files) and the mode files get them from there: asked in auto, refused
    in bypass, untouched in plan (whose own deny covers every write)."""
    meta = home.key_doc("permissions").meta
    assert "modes" not in meta and "&grant_files" not in home.path("permissions").read_text(encoding="utf-8")
    assert len(meta["grant_files"]) == GRANT_PATTERNS and all(r.startswith("write(") for r in meta["grant_files"])
    asked = {p for p in GRANT_FILES if decide(home, "auto", "write", {"path": p}).effect == "ask"}
    assert asked == set(GRANT_FILES)                                          # every listed file asks in auto ...
    assert all(decide(home, "bypass", "write", {"path": p}).effect == "deny" for p in GRANT_FILES)   # ... and is refused in bypass
    assert all(decide(home, "plan", "write", {"path": p}).effect == "deny" for p in GRANT_FILES)
    assert not any(r.locked and r.effect == "ask" and r.tool == "write" for r in R.defaults_layers(home, "bypass")[1].rules)


def test_the_script_of_the_shipped_on_start_hook_is_a_grant_file(home):
    """Hook commands run without a rule check, so the script they run is as trusted as lightmind.md."""
    command = home.fill(home.settings["on_start"][0]["run"])
    script = re.search(r"\S+/status\.py", command).group(0)
    assert script == "tools/vault/status.py"
    for tool in ("write", "edit"):
        d = decide(home, "auto", tool, {"path": script})
        assert d.effect == "ask" and d.rule.locked, tool
        assert decide(home, "bypass", tool, {"path": script}).effect == "deny", tool
    assert decide(home, "plan", "write", {"path": script}).effect == "deny"
    text = (home.root / "permissions.md").read_text(encoding="utf-8")
    assert "hook scripts run without a rule check" in text                   # the WHY, in the file itself


def test_project_memory_cannot_grant_a_secret_but_tools_md_can(home):
    """projectMemory.md is writable by agents; only the protected tools.md may grant."""
    d = home.path("projects") / "p"
    d.mkdir(parents=True)
    (d / "projectMemory.md").write_text("---\npermissions:\n  secrets: [stripe/test]\n  allow: [secret(*)]\n---\nx\n",
                                        encoding="utf-8")
    with pytest.warns(UserWarning, match="ignored"):
        p = home.load_project("p")
    layers = R.build_layers(home, None, "auto", None, p.permissions)
    assert R.decide(layers, Check("secret", "stripe/test"), ctx(home)).effect == "deny"
    (d / "tools.md").write_text("# Secrets\n- stripe/test\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="ignored"):
        p = home.load_project("p")
    layers = R.build_layers(home, None, "auto", None, p.permissions)
    assert R.decide(layers, Check("secret", "stripe/test"), ctx(home)).effect == "allow"
    assert R.decide(layers, Check("secret", "stripe/live"), ctx(home)).effect == "deny"
    # and writing that tools.md is the confirmed step
    assert decide(home, "auto", "write", {"path": "projects/p/tools.md"}).effect == "ask"
    assert decide(home, "auto", "write", {"path": "projects/p/projectMemory.md"}).effect == "allow"


# ------------------------------------------------------------ money, send, modes
def test_auto_asks_before_money_and_customers_only(home):
    mail = Tool("send-mail", category="send")
    pay = Tool("charge", category="money")
    rm = Tool("cleanup", category="delete")
    assert decide(home, "auto", "send-mail", {}, tool=mail).effect == "ask"
    assert decide(home, "auto", "charge", {}, tool=pay).effect == "ask"
    assert decide(home, "auto", "cleanup", {}, tool=rm).effect == "allow"
    assert decide(home, "bypass", "send-mail", {}, tool=mail).effect == "allow"
    assert decide(home, "plan", "send-mail", {}, tool=mail).effect == "deny"
    assert decide(home, "plan", "cleanup", {}, tool=rm).effect == "deny"


def test_every_rule_in_the_file_parses_and_every_mode_builds(home):
    for mode in MODES:
        layers = R.defaults_layers(home, mode)
        assert layers[0].fallback == "allow" and [L.name for L in layers] == ["defaults", f"mode:{mode}"]
        assert all(r.origin.startswith(("permissions.md", f"modes/{mode}.md")) for L in layers for r in L.rules)
    with pytest.raises(ValueError, match="unknown mode"):
        R.defaults_layers(home, "nothing")


def test_mode_texts_and_labels(home):
    modes = home.modes()
    assert set(modes) == set(MODES) and home.defaults["mode"] in modes
    assert "Ask the user before you spend money or send anything to customers" in modes["auto"]["text"]
    assert "Change nothing" in modes["plan"]["text"] and "Do not ask questions" in modes["bypass"]["text"]
    assert [modes[m]["color"] for m in MODES] == ["green", "blue", "red"]


def test_settings_other_modules_read(home):
    meta = home.key_doc("permissions").meta
    assert meta["money_limit"] == "20 USD/day" and meta["service_token"] == "lm-services/main"
    assert meta["audit"]["secret_use"] in (True, "on")
    assert R.Ledger(home).reserve(1.0) and not R.Ledger(home).reserve(25.0)
