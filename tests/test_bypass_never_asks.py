"""Bypass never asks: every way a question can come up is turned into allow; a deny (locked or not) stays a deny."""
import asyncio

import pytest

from lightmind import rules as R
from lightmind.files import Home
from lightmind.init import init_tree
from lightmind.model import Agent, Rule, RuleContext, SessionState, Tool
from lightmind.tools import Dispatcher

from conftest import legacy_modes


@pytest.fixture
def home(tmp_path) -> Home:
    init_tree(tmp_path / "lm")
    return Home(tmp_path / "lm")


def ctx(home):
    return RuleContext(home=str(home.root), run_dir=str(home.root / "runs" / "r1"), project_dir=str(home.root / "projects" / "p"))


def judge(home, mode, name, args, *, agent=None, session=None, tool=None, approved=True):
    layers = R.build_layers(home, agent, mode, session)
    return R.decide_all(layers, R.checks_for_call(name, args, tool_meta=tool, hash_approved=approved), ctx(home))


def asks_in_auto_only(home, name, args, **kw):
    assert judge(home, "auto", name, args, **kw).effect == "ask"       # the same call does ask in auto ...
    assert judge(home, "bypass", name, args, **kw).effect == "allow"   # ... and never in bypass


def test_an_ask_rule_of_the_agent(home):
    agent = Agent("a", permissions={"ask": ["bash(*)", "write(**)"]})
    asks_in_auto_only(home, "bash", {"command": "ls"}, agent=agent)
    asks_in_auto_only(home, "write", {"path": "notes.md"}, agent=agent)


def test_an_ask_rule_of_the_session(home):
    asks_in_auto_only(home, "web", {"url": "https://example.com"}, session=SessionState(rules=[Rule("web", "*", "ask")]))


def test_the_grant_files_are_a_locked_deny_in_bypass(home):
    # writing them unasked could lift the locked denies (secret values): bypass refuses instead of asking
    for path in ("permissions.md", "lightmind.md", "config.md", "agents/x/tools.md", "tools/vault/status.py", "triggers/t.md", "runs/r1/meta.json"):
        for name, args in (("write", {"path": path}), ("edit", {"path": path, "old": "a", "new": "b"})):
            assert judge(home, "auto", name, args).effect == "ask", (name, path)
            assert judge(home, "bypass", name, args).effect == "deny", (name, path)



def test_the_categories_of_a_tool(home):
    for cat in ("send", "money", "delete", "deploy", "account"):
        t = Tool("x", category=cat)
        assert judge(home, "bypass", "x", {}, tool=t).effect == "allow", cat
    assert judge(home, "auto", "x", {}, tool=Tool("x", category="money")).effect == "ask"
    asks_in_auto_only(home, "x", {}, tool=Tool("x", category="send"))
    for cat in ("send", "money", "delete", "deploy", "account"):                  # even when a rule of the agent asks for it
        agent = Agent("a", permissions={"ask": [f"category({cat})"]})
        assert judge(home, "bypass", "x", {}, tool=Tool("x", category=cat), agent=agent).effect == "allow", cat


def test_a_tool_that_is_not_approved_yet(home):
    agent = Agent("a", permissions={"ask": ["new-tool(*)"]})
    asks_in_auto_only(home, "x", {}, tool=Tool("x"), approved=False, agent=agent)


def test_the_fallback_ask(home):
    p = home.root / "permissions.md"
    p.write_text(p.read_text(encoding="utf-8").replace("fallback: allow", "fallback: ask"), encoding="utf-8")
    h = Home(home.root)
    asks_in_auto_only(h, "bash", {"command": "ls"})
    asks_in_auto_only(h, "read", {"path": "/etc/hostname"})                        # a path outside the tree


@pytest.mark.parametrize("cmd", ['echo "abc', "echo $(ls", "echo `ls", "echo a\x0b b"])
def test_a_shell_line_that_cannot_be_read_safely(home, cmd):
    asks_in_auto_only(home, "bash", {"command": cmd})
    assert judge(home, "bypass", "ssh", {"server": "k", "command": cmd}).effect == "allow"


def test_enforce_never_calls_the_asker_and_needs_none(home):
    agent = Agent("a", permissions={"ask": ["bash(*)"]})
    layers = R.build_layers(home, agent, "bypass")
    checks = R.checks_for_call("bash", {"command": "rm -rf build"}) + R.checks_for_call("write", {"path": "notes.md"})
    called = []

    async def asker(q, c):
        called.append(q)
        return "no"
    assert asyncio.run(R.enforce(layers, checks, ctx(home), None, SessionState())).effect == "allow"     # no asker: not a deny
    assert asyncio.run(R.enforce(layers, checks, ctx(home), asker, SessionState())).effect == "allow"
    assert not called


def test_what_stays_refused_without_a_question(home):
    """Locked denies (secret values, the engine, config.md) and a plain deny rule are no question: they stay."""
    for tool, args in (("read", {"path": "secrets/stripe/main.env"}), ("write", {"path": "secrets/stripe/main.env"}),
                       ("bash", {"command": "cat secrets/stripe/main.env"}), ("write", {"path": "system/state/ledger.jsonl"}),
                       ("write", {"path": "config.md"}), ("edit", {"path": "config.md", "old": "a", "new": "b"})):
        assert judge(home, "bypass", tool, args).effect == "deny", (tool, args)
    assert judge(home, "bypass", "secrets", {"ref": "stripe/main", "from_ref": "<secret:stripe/main:KEY>"}).effect == "deny"   # not granted
    assert judge(home, "bypass", "bash", {"command": "ls"}, agent=Agent("a", permissions={"deny": ["bash(ls)"]})).effect == "deny"


def test_config_md_stays_guarded_so_the_core_keeps_using_it(home):
    assert home.unguarded_reason() == ""


def test_a_mode_not_called_bypass_still_asks(home):
    assert R.is_bypass(R.build_layers(home, None, "bypass")) and not R.is_bypass(R.build_layers(home, None, "auto"))
    # a mode file that asks for the shell (catalog 21) but does not carry `fragt-nie`
    (home.root / "modes" / "yolo.md").write_text("---\nlabel: Yolo\nregel: darf-alles-ausser\nrechte: []\nfragt: [21]\n---\nx\n", encoding="utf-8")
    assert judge(Home(home.root), "yolo", "bash", {"command": "ls"}).effect == "ask"


def test_an_older_bypass_block_with_the_locked_ask_keeps_config_md_guarded(home):
    """A tree made before this change: bypass still has `locked: ask: *grant_files`; the grant files are refused, config.md stays in use."""
    p = legacy_modes(home)     # the `modes:` block of permissions.md is the path of such an older tree
    t = p.read_text(encoding="utf-8")
    old = "    locked:\n      deny: *grant_files\n"
    assert old in t
    p.write_text(t.replace(old, "    locked:\n      ask: *grant_files\n"), encoding="utf-8")
    h = Home(home.root)
    assert judge(h, "bypass", "write", {"path": "config.md"}).effect == "deny"
    assert judge(h, "bypass", "edit", {"path": "config.md", "old": "a", "new": "b"}).effect == "deny"
    assert judge(h, "bypass", "write", {"path": "lightmind.md"}).effect == "deny"
    assert judge(h, "bypass", "write", {"path": "notes.md"}).effect == "allow"
    assert h.unguarded_reason() == ""


@pytest.mark.parametrize("cmd", ["cat secrets/x/y.e`#`nv", "cat secrets/x/y.e$(#\n)nv", "echo \"a ~/.claude/x"])
def test_an_unreadable_line_that_names_secrets_is_refused_in_bypass(home, cmd):
    assert judge(home, "bypass", "bash", {"command": cmd}).effect == "deny"


async def test_the_ask_tool_does_not_ask_in_bypass(home):
    seen = []

    async def questioner(q):
        seen.append(q)
        return "yes"
    d = Dispatcher.__new__(Dispatcher)
    d.mode, d.questioner = "bypass", questioner
    res = await d._b_ask("May I?")
    assert res.ok and "asks nothing" in res.text and not seen
    d.mode = "auto"
    assert (await d._b_ask("May I?")).text == "yes" and seen == ["May I?"]
