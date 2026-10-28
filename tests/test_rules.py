import asyncio
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from lightmind import rules as R
from lightmind.files import Home
from lightmind.model import Agent, Check, Rule, RuleContext, SessionState, Tool

STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"


@pytest.fixture
def home(tree) -> Home:
    """The tree with the strict fixture permissions: these tests exercise the checker against
    known rules and must not change when the shipped template's rules change."""
    shutil.copy(STRICT, tree / "permissions.md")
    shutil.rmtree(tree / "modes")      # the fixture holds its modes in its own `modes:` block, read when no mode file exists
    (tree / "rechte.md").unlink(missing_ok=True)
    return Home(tree)


def ctx(home, **kw):
    return RuleContext(home=str(home.root), run_dir=str(home.root / "runs" / "r1"),
                       project_dir=str(home.root / "projects" / "crm"), **kw)


def webdev(**perm):
    return Agent("webdev", permissions={"allow": ["bash(npm *)"], "ask": ["bash(git push*)"], **perm})


def call(home, mode, name, args, agent=None, session=None, tool=None, approved=True):
    layers = R.build_layers(home, agent, mode, session)
    return R.decide_all(layers, R.checks_for_call(name, args, tool_meta=tool, hash_approved=approved), ctx(home))


# ------------------------------------------------------------ worked examples 3.7
@pytest.mark.parametrize("mode,name,args,effect", [
    ("auto", "bash", {"command": "npm test"}, "allow"),
    ("auto", "bash", {"command": "git push origin main"}, "ask"),
    ("plan", "write", {"path": "projects/crm/app.py"}, "deny"),
    ("bypass", "read", {"path": "secrets/stripe/acme-test.env"}, "deny"),
    ("auto", "read", {"path": "secrets/stripe/info.md"}, "allow"),
    ("auto", "ssh", {"server": "gpubox", "command": "ls"}, "deny"),
])
def test_worked_examples(home, mode, name, args, effect):
    assert call(home, mode, name, args, webdev()).effect == effect


def test_worked_new_tool_and_category(home):
    t = Tool("slugify")
    assert call(home, "auto", "slugify", {}, webdev(), tool=t, approved=False).effect == "ask"
    assert call(home, "auto", "slugify", {}, webdev(), tool=t, approved=True).effect == "allow"
    mail = Tool("send-mail", category="send")
    d = call(home, "auto", "send-mail", {}, webdev(), tool=mail)
    assert d.effect == "ask" and "category(send)" in d.reason
    a = webdev(allow=["bash(npm *)", "new-tool(*)"])
    assert call(home, "auto", "slugify", {}, a, tool=t, approved=False).effect == "allow"


# ------------------------------------------------------------------- layers
def test_layer1_fallback_and_strictness(home):
    L = R.defaults_layers(home, "auto")
    c = ctx(home)
    assert R.decide(L, Check("read", "x/y.txt"), c).effect == "allow"
    assert R.decide(L, Check("bash", "git push -f"), c).effect == "ask"
    assert R.decide(L, Check("bash", "rm -rf /usr"), c).effect == "deny"
    assert R.decide(L, Check("bash", "ls"), c).rule is None            # fallback deny
    same = [R.Layer("l", [Rule("bash", "x*", "allow"), Rule("bash", "x*", "deny"), Rule("bash", "x*", "ask")])]
    assert R.decide(same, Check("bash", "xy"), c).effect == "deny"


def test_modes_over_defaults(home):
    assert call(home, "plan", "bash", {"command": "ls"}).effect == "ask"
    assert call(home, "plan", "read", {"path": "a.md"}).effect == "allow"
    assert call(home, "plan", "edit", {"path": "projects/a.md"}).effect == "deny"
    assert call(home, "bypass", "bash", {"command": "anything"}).effect == "allow"
    assert call(home, "bypass", "bash", {"command": "cat secrets/a/b.env"}).effect == "deny"
    with pytest.raises(ValueError):
        R.defaults_layers(home, "nope")


def test_agent_shorthands_and_overrides(home):
    a = Agent("x", permissions={"secrets": ["stripe"], "secrets_write": ["social-acme"],
                                "tools": ["browser", "pc-control", "mytool"], "servers": ["buildbox"],
                                "deny": ["web(domain:facebook.com)"]})
    got = {r.text() for r in R.agent_layer(a).rules}
    assert {"secret(stripe/*)", "secret-write(social-acme/*)", "browser(*)", "pc-control(*)",
            "tool(mytool)", "server(buildbox)", "ssh(buildbox)", "web(domain:facebook.com)"} <= got
    L = R.build_layers(home, a, "auto")
    c = ctx(home)
    assert R.decide(L, Check("secret", "stripe/acme-test"), c).effect == "allow"
    assert R.decide(L, Check("secret", "other/x"), c).effect == "deny"
    assert R.decide(L, Check("ssh", "buildbox:ls -la"), c).effect == "allow"
    assert R.decide(L, Check("web", "https://facebook.com/x"), c).effect == "deny"
    assert R.decide(L, Check("web", "https://example.org/x"), c).effect == "allow"
    pl = R.build_layers(home, Agent("y", permissions={"allow": ["bash(*)", "write(**)", "read(secrets/**/*.env)"]}), "plan")
    assert R.decide(pl, Check("bash", "ls"), c).effect == "allow"           # agent allow beats mode ask
    assert R.decide(pl, Check("write", "projects/a"), c).effect == "deny"   # locked mode deny stays
    assert R.decide(pl, Check("read", "secrets/a/b.env"), c).effect == "deny"


def test_project_permissions_join_agent_layer(home):
    L = R.build_layers(home, None, "auto", None, {"allow": ["bash(make*)"]})
    assert len(L) == 4 and R.decide(L, Check("bash", "make x"), ctx(home)).effect == "allow"


def test_session_layer_and_expiry(home):
    s = SessionState(rules=[Rule("bash", "make*", "allow")])
    assert call(home, "auto", "bash", {"command": "make all"}, session=s).effect == "allow"
    s2 = SessionState(rules=[Rule("bash", "npm *", "deny")])
    assert call(home, "auto", "bash", {"command": "npm test"}, webdev(), session=s2).effect == "deny"
    r = R.parse_rule("bash(make*) until 2026-12-01")
    assert r.until == date(2026, 12, 1)
    base = dict(home="/h", run_dir="", project_dir="")
    assert R.match_rule(r, Check("bash", "make"), RuleContext(**base, now=date(2026, 11, 30)))
    assert not R.match_rule(r, Check("bash", "make"), RuleContext(**base, now=date(2026, 12, 1)))
    for bad in ("(x)", "bash(x) until tomorrow", ""):
        with pytest.raises(ValueError):
            R.parse_rule(bad)


def test_enforce_ask_routing(home):
    layers = R.build_layers(home, webdev(), "auto")
    s = SessionState()
    chk = [Check("bash", "git push origin main")]
    run = lambda asker, lay=layers, ch=chk: asyncio.run(R.enforce(lay, ch, ctx(home), asker, s))

    async def yes(q, c): return "yes"
    async def no(q, c): return "no"
    async def always(q, c): return "always"
    async def late(q, c): raise asyncio.TimeoutError
    assert run(None).effect == "deny"
    assert run(no).effect == "deny" and run(late).effect == "deny"
    assert run(yes).effect == "allow" and not s.rules
    assert run(always).effect == "allow" and len(s.rules) == 1
    lay2 = R.build_layers(home, webdev(), "auto", s)
    assert run(None, lay2).effect == "allow"                      # session rule now allows the push
    send = [Check("category", "send")]
    assert run(None, lay2, send).effect == "deny"                 # locked ask still asks
    assert run(always, lay2, send).effect == "allow"
    assert run(None, R.build_layers(home, webdev(), "auto", s), send).effect == "deny"


# ----------------------------------------------------------------- matching
def test_matching_globs_and_vars(home):
    c = ctx(home)
    m = lambda rule, tool, arg: R.match_rule(R.parse_rule(rule), Check(tool, arg), c)
    assert m("read(**)", "read", "a/b/c")
    assert m("write(projects/**)", "write", "projects/k/a.py")
    assert not m("write(projects/*)", "write", "projects/k/a.py")
    assert m("write(projects/**)", "write", "projects\\k\\a.py")
    assert not m("write(projects/**)", "write", "projects/../vault/x")
    assert m("write(projects/**)", "write", str(home.root / "projects" / "k.md"))
    assert m("write({run}/**)", "write", "runs/r1/log.md") and m("write($RUN/**)", "write", "runs/r1/x")
    assert m("write($PROJECT/**)", "write", "projects/crm/a")
    assert not m("write($PROJECT/**)", "write", "projects/other/a")
    assert m("write({runs}/**)", "write", "runs/x/y")
    assert m("bash(git push*)", "bash", "git push origin main")
    assert m("bash(*)", "bash", "a b  c") and not m("bash(git push*)", "bash", "git status")
    assert m("web(domain:*.example.com)", "web", "https://a.example.com/x")
    assert m("web(domain:example.com)", "web", "domain:example.com")
    assert not m("web(domain:example.com)", "web", "https://evil.com/example.com")
    assert m("tool(send-mail)", "tool", "send-mail:a@b.de")
    assert m("tool(send-mail:*@example.com)", "tool", "send-mail:x@example.com")
    assert not m("tool(send-mail:*@example.com)", "tool", "send-mail:x@evil.de")
    assert m("secret(stripe)", "secret", "stripe/acme") and m("ssh(gpubox)", "ssh", "gpubox:ls")
    assert m("*", "anything", "x") and m("write(**)", "edit", "a") and not m("edit(**)", "write", "a")
    unset = RuleContext(home=str(home.root))
    assert not R.match_rule(R.parse_rule("write($RUN/**)"), Check("write", "x"), unset)


def test_placeholders_follow_renamed_folders(home, tree):
    entry = tree / "lightmind.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("secrets: secrets/", "secrets: geheim/"), encoding="utf-8")
    h = Home(tree)
    assert call(h, "bypass", "read", {"path": "geheim/a/b.env"}).effect == "deny"
    assert call(h, "bypass", "bash", {"command": "cat geheim/a/b.env"}).effect == "deny"
    assert call(h, "auto", "read", {"path": "secrets/a/b.env"}).effect == "allow"   # old name no longer special


@pytest.mark.parametrize("path", ["lightmind.md", "permissions.md", "agents/x/agentMaster.md",
                                  "system/state/ledger.jsonl", "runs/_audit/index.jsonl"])
def test_locked_files_cannot_be_written_even_in_bypass(home, path):
    for tool in ("write", "edit"):
        assert call(home, "bypass", tool, {"path": path}).effect == "deny"
    assert call(home, "bypass", "write", {"path": "x/../" + path}).effect == "deny"


def test_paths_outside_home_refused(home, tree):
    entry = tree / "lightmind.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("secrets: secrets/", "secrets: ../elsewhere/"), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the home"):
        call(Home(tree), "auto", "read", {"path": "a"})


def test_checks_table():
    t = Tool("x", category="money", match="to", secrets=["stripe/a"], server="buildbox")
    cs = R.checks_for_call("x", {"to": "a@b"}, tool_meta=t, hash_approved=False)
    assert cs == [Check("tool", "x:a@b"), Check("new-tool", "x"), Check("category", "money"),
                  Check("secret", "stripe/a"), Check("server", "buildbox")]
    assert R.checks_for_call("ssh", {"server": "k", "command": "ls"}) == [Check("ssh", "k:ls"), Check("server", "k")]
    assert R.checks_for_call("secrets", {"ref": "a/b", "from_ref": "<secret:c/d:V>"})[0] == Check("secret-write", "a/b")
    assert R.checks_for_call("skill_load", {"name": "s"}) == [Check("skill", "s")]


def test_resolved_rules_has_origin(home):
    rr = R.resolved_rules(R.build_layers(home, webdev(), "auto"))
    assert rr[0][1].locked and rr[0][1].origin.startswith("permissions.md:")
    assert any(layer == "agent:webdev" for layer, _ in rr)


# ------------------------------------------------------------ ledger / audit
def test_ledger_limit_and_settle(home):
    led = R.Ledger(home)
    assert led.reserve(15) and not led.reserve(6)
    led.settle(15, 2)
    assert led.reserve(6)
    led.settle(6, 0)
    assert led.booked() == pytest.approx(2)


def test_audit_switches(home):
    R.audit(home, "send", {"a": 1})
    R.audit(home, "nonexistent", {"a": 2})
    rows = [json.loads(x) for x in (home.path("audit") / "index.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "send" and rows[0]["a"] == 1
