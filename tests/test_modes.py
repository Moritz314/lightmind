"""Modes from numbered rights: the catalog rechte.md, the mode files modes/*.md, the three states, the fixed limits,
and that the shipped and the user's modes decide exactly as their `modes:` blocks in permissions.md did before."""
import shutil
from pathlib import Path

import pytest

from lightmind import checks, modes as M, rules as R
from lightmind.files import Home
from lightmind.init import init_tree, update_tree
from lightmind.model import Agent, Rule, RuleContext, SessionState, Tool

FIX = Path(__file__).parent / "fixtures" / "rechte"


@pytest.fixture
def home(tmp_path) -> Home:
    init_tree(tmp_path / "lm")
    return Home(tmp_path / "lm")


def ctx(home):
    return RuleContext(home=str(home.root), run_dir=str(home.root / "runs" / "r1"), project_dir=str(home.root / "projects" / "p"))


def judge(home, mode, name, args, *, agent=None, session=None, tool=None, approved=True):
    layers = R.build_layers(home, agent, mode, session)
    return R.decide_all(layers, R.checks_for_call(name, args, tool_meta=tool, hash_approved=approved), ctx(home))


def effect(home, mode, name, args=None, **kw):
    return judge(home, mode, name, args or {}, **kw).effect


def mode_file(home, key, front, text="Work."):
    p = home.root / "modes" / f"{key}.md"
    p.write_text(f"---\n{front}\n---\n{text}\n", encoding="utf-8")
    return p


# ------------------------------------------------------------------ catalog
def test_the_shipped_catalog_parses_with_unique_numbers_and_valid_rules(home):
    cat = M.load_catalog(home)
    assert {11, 12, 13, 14, 21, 22, 31, 41, 51, 52, 61, 71, 72}.issubset(cat.rights)
    assert all(r.rules for r in cat.rights.values())
    for r in cat.rights.values():
        for t in r.rules:
            R.parse_rule(t)
    assert cat.rights[13].rules == tuple(home.key_doc("permissions").meta["grant_files"])      # @grant_files expanded
    assert cat.rights[13].parent == 12 and cat.rights[14].parent == 13 and cat.rights[72].parent == 41
    assert [f[0] for f in cat.fixed][:3] == ["F1", "F2", "F3"]
    assert cat.rights[11].group.startswith("1 ")


@pytest.mark.parametrize("row, why", [
    ("| 11 | a | `read(**)` | offen | ja |\n| 11 | b | `bash(*)` | offen | ja |", "twice"),
    ("| 11 | a | `read(**)` | weit | ja |", "Grundsatz"),
    ("| 11 | a | `read(**)` | offen | vielleicht |", "Fragen"),
    ("| 11 | a | `read((` | offen | ja |", "bad rule"),
    ("| 11 | a | `@nothing` | offen | ja |", "unknown reference"),
    ("| 11 | a | `read(**)` | offen | ja | 99 |", "part of 99"),
    ("| 11 | a | `read(**)` | offen | ja | 12 |\n| 12 | b | `bash(*)` | offen | ja | 11 |", "goes round"),
    ("| 1x | a | `read(**)` | offen | ja |", "needs Nr"),
    ("just text", "no numbered right"),
])
def test_a_bad_catalog_row_names_the_problem(row, why):
    with pytest.raises(ValueError, match=why):
        M.parse_catalog(f"# Rechte\n\n| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |\n|---|---|---|---|---|---|\n{row}\n",
                        "rechte.md")


def test_every_kind_of_check_is_covered_by_a_number_or_a_fixed_limit(home):
    """A new kind of check without a catalog number would be a right no mode can switch: this test turns red then."""
    cat = M.load_catalog(home)
    kinds = {R.parse_rule(t).tool for r in cat.rights.values() for t in r.rules}
    calls = [("bash", {"command": "ls"}), ("bash", {"command": "echo 'x"}), ("ssh", {"server": "s", "command": "ls"}),
             ("read", {"path": "a"}), ("write", {"path": "a"}), ("edit", {"path": "a"}), ("web", {"url": "https://a.b"}),
             ("spawn", {"agent": "x"}), ("spawn", {}), ("workflow", {"script": "x"}), ("skill_load", {"name": "x"}),
             ("secrets", {"ref": "a/b", "from_ref": "<secret:c/d:KEY>"}), ("browser.open", {"account": "a"}),
             ("pc-control.click", {}), ("ask", {}), ("todo", {}), ("timer", {}), ("job", {}), ("relay", {})]
    seen = {c.tool for n, a in calls for c in R.checks_for_call(n, a)}
    seen |= {c.tool for c in R.checks_for_call("x", {}, tool_meta=Tool("x", category="send", secrets=["a/b"], server="s"),
                                                hash_approved=False)}
    in_code = {"secret-malformed"}                         # fixed limit F4, refused in code
    assert seen - kinds - in_code == set()


# --------------------------------------------------------------- the toggle
def test_a_mode_that_allows_sub_agents_allows_workflows(home):
    mode_file(home, "x", "regel: darf-nur\nrechte: [11, 43]")
    assert effect(home, "x", "workflow", {"script": "x"}) == "allow"


def test_darf_alles_ausser_blocks_only_the_listed_numbers(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [24, 53]")
    assert effect(home, "x", "web", {"url": "https://example.com"}) == "deny"
    assert effect(home, "x", "y", tool=Tool("y", category="delete")) == "deny"
    assert effect(home, "x", "bash", {"command": "ls"}) == "allow"
    assert effect(home, "x", "write", {"path": "notes.md"}) == "allow"


def test_darf_nur_allows_only_the_listed_numbers(home):
    mode_file(home, "x", "regel: darf-nur\nrechte: [11, 24]")
    assert effect(home, "x", "read", {"path": "notes.md"}) == "allow"
    assert effect(home, "x", "web", {"url": "https://example.com"}) == "allow"
    assert effect(home, "x", "bash", {"command": "ls"}) == "deny"
    assert effect(home, "x", "write", {"path": "notes.md"}) == "deny"
    assert effect(home, "x", "spawn", {}) == "deny"                                 # not even a copy of itself
    assert effect(home, "x", "workflow", {"script": "x"}) == "deny"                # it starts copies of itself


def test_the_toggle_accepts_the_spellings_user_writes(home):
    for v in ("darf-nur", "Darf nur:", "darf_nur"):
        mode_file(home, "x", f"regel: '{v}'\nrechte: [11]")
        assert effect(home, "x", "bash", {"command": "ls"}) == "deny", v
    for v in ("darf-alles-ausser", "Darf alles außer:", "darf alles ausser"):
        mode_file(home, "x", f"regel: '{v}'\nrechte: [21]")
        assert effect(home, "x", "bash", {"command": "ls"}) == "deny", v
        assert effect(home, "x", "read", {"path": "a"}) == "allow", v


def test_numbers_may_carry_names_and_ranges(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: ['51-55', '24 Webseiten abrufen']")
    for cat in ("money", "send", "delete", "deploy", "account"):
        assert effect(home, "x", "y", tool=Tool("y", category=cat)) == "deny", cat
    assert effect(home, "x", "web", {"url": "https://a.example"}) == "deny"


def test_a_blocked_right_cannot_be_lifted_by_an_agent_or_the_session(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [21]")
    agent = Agent("a", permissions={"allow": ["bash(*)"]})
    session = SessionState(rules=[Rule("bash", "*", "allow")])
    assert effect(home, "x", "bash", {"command": "ls"}, agent=agent, session=session) == "deny"


# ---------------------------------------------------------- the third state
def test_fragt_asks_and_a_plain_ask_can_be_lifted_by_the_agent(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: []\nfragt: [21]")
    assert effect(home, "x", "bash", {"command": "ls"}) == "ask"
    assert effect(home, "x", "bash", {"command": "git status"}, agent=Agent("a", permissions={"allow": ["bash(git *)"]})) == "allow"


def test_a_fixed_ask_cannot_be_lifted(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: []\nfragt: [13]")
    agent = Agent("a", permissions={"allow": ["write(**)"]})
    assert effect(home, "x", "write", {"path": "permissions.md"}, agent=agent) == "ask"
    assert effect(home, "x", "write", {"path": "notes.md"}, agent=agent) == "allow"


def test_fragt_in_darf_nur_means_allowed_after_asking(home):
    mode_file(home, "x", "regel: darf-nur\nrechte: [11]\nfragt: [21]")
    assert effect(home, "x", "bash", {"command": "ls"}) == "ask"
    assert effect(home, "x", "write", {"path": "a.md"}) == "deny"


def test_an_unreadable_line_asks_by_default_and_follows_its_number(home):
    line = {"command": "echo 'it is open"}
    mode_file(home, "asks", "regel: darf-alles-ausser\nrechte: []\nfragt: [22]")
    mode_file(home, "unnamed", "regel: darf-alles-ausser\nrechte: []")
    mode_file(home, "open", "regel: darf-nur\nrechte: [21, 22]")
    mode_file(home, "shut", "regel: darf-alles-ausser\nrechte: [22]")
    assert effect(home, "asks", "bash", line) == "ask"
    assert effect(home, "unnamed", "bash", line) == "ask"                            # `frage`: asks unless named under darf-nur
    assert effect(home, "open", "bash", line) == "allow"
    assert effect(home, "shut", "bash", line) == "deny"
    secretish = {"command": "cat secrets/x 'oops"}
    assert effect(home, "open", "bash", secretish) == "ask"                          # F5: never unasked
    # an unreadable line is matched whole: a pattern sees what comes after a colon, an `always` covers no longer line
    mode_file(home, "pick", "regel: darf-alles-ausser\nrechte: []\nextra:\n  deny: ['shell-unparsed(*evil*)']")
    assert effect(home, "pick", "bash", {"command": "echo 'x: evil"}) == "deny"
    always = SessionState(rules=[Rule("shell-unparsed", R.literal("echo 'abc"), "allow")])
    assert effect(home, "unnamed", "bash", {"command": "echo 'abc"}, session=always) == "allow"
    assert effect(home, "unnamed", "bash", {"command": "echo 'abc: rm -rf x"}, session=always) == "ask"


def test_a_number_inside_another_follows_it_unless_it_is_named(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [12, 41]\nfragt: [13]")
    assert effect(home, "x", "write", {"path": "notes.md"}) == "deny"
    assert effect(home, "x", "write", {"path": "permissions.md"}) == "ask"           # 13 lies inside 12, named under fragt
    assert effect(home, "x", "write", {"path": "config.md"}) == "ask"               # 14 inside 13, not named: as 13
    assert effect(home, "x", "todo", {}) == "deny"                                   # 72 inside 41, not named: as 41
    mode_file(home, "y", "regel: darf-alles-ausser\nrechte: [12]")
    assert effect(home, "y", "write", {"path": "agents/a/tools.md"}) == "deny"      # blocking 12 blocks 13 and 14
    assert effect(home, "y", "write", {"path": "config.md"}) == "deny"
    mode_file(home, "z", "regel: darf-nur\nrechte: [11, 72]")
    assert effect(home, "z", "todo", {}) == "allow"                                  # named: allowed although 41 is not
    assert effect(home, "z", "y", tool=Tool("y")) == "deny"
    mode_file(home, "w", "regel: darf-alles-ausser\nrechte: []\nfragt: [41]")
    assert effect(home, "w", "y", tool=Tool("y")) == "ask"
    assert effect(home, "w", "ask", {"question": "?"}) == "allow"                   # 71 cannot ask: allowed, not asked


# ------------------------------------------------------------ never asking
def test_bypass_never_asks_whatever_is_listed(home):
    p = home.root / "modes" / "bypass.md"
    p.write_text(p.read_text(encoding="utf-8").replace("fragt: []", "fragt: [21, 51, 22]"), encoding="utf-8")
    assert any("never asks" in w for w in home.modes()["bypass"]["warnings"])
    agent = Agent("a", permissions={"ask": ["web(*)"]})
    session = SessionState(rules=[Rule("read", "**", "ask")])
    assert effect(home, "bypass", "bash", {"command": "ls"}) == "allow"
    assert effect(home, "bypass", "y", tool=Tool("y", category="money")) == "allow"
    assert effect(home, "bypass", "bash", {"command": "echo 'it is open"}) == "allow"
    assert effect(home, "bypass", "web", {"url": "https://a.example"}, agent=agent) == "allow"
    assert effect(home, "bypass", "read", {"path": "a.md"}, session=session) == "allow"
    assert effect(home, "bypass", "write", {"path": "permissions.md"}) == "deny"     # F6: grant files refused, never asked
    p.write_text(p.read_text(encoding="utf-8").replace("fragt: [21, 51, 22]", "fragt: [31, 32]"), encoding="utf-8")
    assert effect(home, "bypass", "o", tool=Tool("o", secrets=["other/x"])) == "deny"   # never asking opens no secret
    assert effect(home, "bypass", "secrets", {"ref": "other/x"}) == "deny"


def test_fragt_nie_makes_any_mode_never_ask(home):
    mode_file(home, "quiet", "regel: darf-alles-ausser\nrechte: [13, 14]\nfragt: [21]\nfragt-nie: ja")
    assert R.mode_never_asks(home, "quiet") and not R.mode_never_asks(home, "auto")
    assert effect(home, "quiet", "bash", {"command": "ls"}) == "allow"
    assert effect(home, "quiet", "bash", {"command": "cat secrets/a 'oops"}) == "deny"


# ------------------------------------------------------------ fixed limits
@pytest.mark.parametrize("mode", ["auto", "plan", "bypass", "free"])
def test_the_fixed_limits_hold_in_every_mode(home, mode):
    mode_file(home, "free", "regel: darf-alles-ausser\nrechte: []\ngeheimnisse: [k]\nextra:\n  allow: ['read(**)', 'bash(*)']\n"
                            "  locked:\n    allow: ['read({secrets}/**)']\nfragt-nie: ja")
    agent = Agent("a", permissions={"allow": ["read(**)", "bash(*)", "write(**)"]})
    session = SessionState(rules=[Rule("read", "**", "allow")])
    for name, args in (("read", {"path": "secrets/k/a.env"}), ("write", {"path": "secrets/k/a.env"}),
                       ("bash", {"command": "cat secrets/k/a.env"}), ("read", {"path": "system/state/x"}),
                       ("bash", {"command": "ls system/claude/x"})):
        assert effect(home, mode, name, args, agent=agent, session=session) == "deny", (mode, name, args)


def test_a_mode_file_cannot_name_a_fixed_limit_or_an_unknown_number(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [F1]")
    assert "no number" in home.modes()["x"]["error"]
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [99]")
    assert "99 is not in rechte.md" in home.modes()["x"]["error"]
    with pytest.raises(ValueError, match="cannot be used"):
        R.build_layers(home, None, "x")


# ----------------------------------------------------------- bad mode files
@pytest.mark.parametrize("front, why", [
    ("rechte: []", "regel"),
    ("regel: darf-alles-ausser\nrecht: [12]", "unknown key"),
    ("regel: darf-alles-ausser\nrechte: ['55-51']", "holds no number"),
    ("regel: darf-alles-ausser\nrechte: [12.5]", "whole number"),
    ("regel: darf-alles-ausser\nrechte: []\nfragt: [31]\ngeheimnisse: [k]", "together"),
    ("regel: darf-alles-ausser\nextra:\n  allwo: ['bash(*)']", "unknown key"),
    ("regel: vielleicht\nrechte: []", "regel"),
    ("regel: darf-nur\nrechte: [21]\nfragt: [21]", "at once"),
    ("regel: darf-nur\nrechte: []\nfragt: [71]", "cannot ask"),
    ("regel: darf-nur\nrechte: []\ngeheimnisse: [crm/api-1]", "needs number 31"),
    ("regel: darf-nur\nrechte: [31]\ngeheimnisse: ['../x']", "no secret name"),
    ("regel: darf-alles-ausser\nrechte: [12\n", "line"),
    ("regel: darf-alles-ausser\nextra: [x]", "extra"),
    ("regel: darf-alles-ausser\nextra:\n  allow: ['bash((']", "bad rule"),
])
def test_a_broken_mode_file_blocks_only_its_own_mode(home, front, why):
    mode_file(home, "x", front)
    assert why in home.modes()["x"]["error"]
    with pytest.raises(ValueError, match="cannot be used"):
        R.build_layers(home, None, "x")
    assert effect(home, "auto", "bash", {"command": "ls"}) == "allow"


def test_a_broken_catalog_names_itself_in_every_mode(home):
    (home.root / "rechte.md").write_text("| 11 | a | `read(**)` | offen | nie |\n", encoding="utf-8")
    assert all("Fragen" in m["error"] for m in home.modes().values())
    (home.root / "rechte.md").unlink()
    assert all("cannot be read" in m["error"] for m in home.modes().values())
    with pytest.raises(ValueError, match="cannot be used"):
        R.build_layers(home, None, "auto")


def test_secrets_open_only_through_geheimnisse(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: []")
    mode_file(home, "y", "regel: darf-alles-ausser\nrechte: []\ngeheimnisse: [crm/api-1]")
    t = Tool("k", secrets=["crm/api-1"])
    assert effect(home, "x", "k", tool=t) == "deny"                                # 31 allowed alone opens nothing
    assert effect(home, "y", "k", tool=t) == "allow"
    assert effect(home, "y", "o", tool=Tool("o", secrets=["other/a"])) == "deny"
    mode_file(home, "z", "regel: darf-nur\nrechte: [41]")
    assert effect(home, "z", "k", tool=t, agent=Agent("a", permissions={"secrets": ["crm/api-1"]})) == "deny"   # 31 blocked


# --------------------------------------------------------- the tree around it
def test_modes_are_ordered_by_order_and_carry_their_text(home):
    ms = home.modes()
    assert list(ms)[:3] == ["auto", "plan", "bypass"]
    assert ms["bypass"]["no_ask"] and not ms["auto"]["no_ask"]
    assert "Change nothing" in ms["plan"]["text"]
    mode_file(home, "late", "regel: darf-alles-ausser\nrechte: []")
    assert list(home.modes())[-1] == "late" and home.modes()["late"]["order"] is None


def test_an_edit_takes_effect_on_the_next_call(home):
    assert effect(home, "auto", "y", tool=Tool("y", category="delete")) == "allow"
    p = home.root / "modes" / "auto.md"
    p.write_text(p.read_text(encoding="utf-8").replace("fragt: [13, 22, 51, 52]", "fragt: [13, 22, 51, 52, 53]"),
                 encoding="utf-8")
    assert effect(home, "auto", "y", tool=Tool("y", category="delete")) == "ask"


def test_without_mode_files_the_permissions_block_counts(home):
    shutil.rmtree(home.root / "modes")
    (home.root / "rechte.md").unlink()
    perm = home.root / "permissions.md"
    perm.write_text(perm.read_text(encoding="utf-8").replace(
        "audit:", "modes:\n  solo:\n    label: Solo\n    ask: [bash(*)]\n    text: Old style.\naudit:", 1), encoding="utf-8")
    assert list(home.modes()) == ["solo"]
    assert effect(home, "solo", "bash", {"command": "ls"}) == "ask"


def test_writing_the_mode_files_and_the_catalog_asks_in_auto(home):
    for path in ("modes/auto.md", "modes/new.md", "rechte.md"):
        assert effect(home, "auto", "write", {"path": path}) == "ask", path
        assert effect(home, "plan", "write", {"path": path}) == "deny", path
        assert effect(home, "bypass", "write", {"path": path}) == "deny", path      # shipped bypass blocks 13


def test_config_md_stays_guarded_with_the_shipped_modes(home):
    assert home.unguarded_reason() == ""
    p = home.root / "modes" / "bypass.md"
    p.write_text(p.read_text(encoding="utf-8").replace("rechte: [13]", "rechte: []"), encoding="utf-8")
    assert "bypass" in home.unguarded_reason()


def test_update_keeps_the_modes_of_an_older_tree_in_permissions_md(home):
    shutil.rmtree(home.root / "modes")
    (home.root / "rechte.md").unlink()
    perm = home.root / "permissions.md"
    perm.write_text(perm.read_text(encoding="utf-8").replace(
        "audit:", "modes:\n  solo:\n    label: Solo\n    text: Old style.\naudit:", 1), encoding="utf-8")
    track = home.path("state") / "templates.json"
    track.write_text(track.read_text(encoding="utf-8").replace('"modes/auto.md",', "").replace('"rechte.md",', ""),
                     encoding="utf-8")
    created = update_tree(home.root)
    assert not (home.root / "modes" / "auto.md").exists() and not (home.root / "rechte.md").exists()
    assert any("not created, the modes of this tree stay in permissions.md" in n for n in created.notes)
    assert list(Home(home.root).modes()) == ["solo"]


def test_a_tree_that_switched_does_not_fall_back_when_its_mode_files_go(home):
    for p in (home.root / "modes").glob("*.md"):
        if p.name[0] != "_" and p.name != "README.md":
            p.unlink()
    with pytest.raises(ValueError, match="holds no mode file"):
        home.modes()
    with pytest.raises(ValueError):
        R.build_layers(home, None, "auto")


def test_edits_of_the_catalog_and_of_grant_files_count_at_once(home):
    assert effect(home, "auto", "write", {"path": "notes/x.md"}) == "allow"
    perm = home.root / "permissions.md"
    perm.write_text(perm.read_text(encoding="utf-8").replace("  - write({permissions})\n",
                                                             "  - write({permissions})\n  - write(notes/**)\n"), encoding="utf-8")
    assert effect(home, "auto", "write", {"path": "notes/x.md"}) == "ask"
    cat = home.root / "rechte.md"
    cat.write_text(cat.read_text(encoding="utf-8").replace("| 24 | Webseiten abrufen | `web(*)`",
                                                           "| 24 | Webseiten abrufen | `web(*)`, `bash(curl *)`"), encoding="utf-8")
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [24]")
    assert effect(home, "x", "bash", {"command": "curl https://a.example"}) == "deny"


def test_lm_check_reports_mode_files(home):
    mode_file(home, "x", "regel: darf-alles-ausser\nrechte: [99]")
    perm = home.root / "permissions.md"
    perm.write_text(perm.read_text(encoding="utf-8").replace("    - bash(*{claude}*)\n", ""), encoding="utf-8")
    lines = checks.check_config(home)
    assert any(l.status == checks.FAIL and "mode x" in l.detail for l in lines)
    assert any(l.status == checks.WARN and "F2" in l.detail for l in lines)


async def test_the_ask_tool_answers_nothing_in_a_mode_that_never_asks(home):
    from lightmind.tools import Dispatcher
    mode_file(home, "quiet", "regel: darf-alles-ausser\nrechte: [13, 14]\nfragt-nie: ja")
    seen = []

    async def questioner(q):
        seen.append(q)
        return "yes"
    d = Dispatcher.__new__(Dispatcher)
    d.home, d.mode, d.questioner = home, "quiet", questioner
    res = await d._b_ask("May I?")
    assert res.ok and "asks nothing" in res.text and not seen
    d.mode = "auto"
    assert (await d._b_ask("May I?")).text == "yes" and seen == ["May I?"]


def test_a_blocked_ask_tool_is_refused(home):
    mode_file(home, "mute", "regel: darf-alles-ausser\nrechte: [71]")
    assert effect(home, "mute", "ask", {"question": "x"}) == "deny"
    assert effect(home, "mute", "todo", {}) == "allow"


# --------------------------------------------- migration: the same decisions
AGENTS = [None, Agent("a", permissions={"ask": ["bash(*)"]}), Agent("b", permissions={"allow": ["bash(git *)", "write(**)"]}),
          Agent("c", permissions={"secrets": ["crm/api-1", "other/x"]})]
SESSIONS = [None, SessionState(rules=[Rule("web", "*", "allow"), Rule("category", "send", "allow")])]
PATHS = ["notes.md", "permissions.md", "lightmind.md", "config.md", "agents/x/agentMaster.md", "agents/x/tools.md",
         "workflows/w/tools.md", "projects/p/tools.md", "providers/a.md", "tools/vault/status.py", "tools/watch/w.py",
         "triggers/t.md", "runs/r1/events.jsonl", "runs/r1/meta.json", "runs/r1/session.md", "runs/r1/log.md",
         "secrets/k/a.env", "secrets/k/info.md", "system/state/x", "vault/n.md", "tools/x/run.py"]
CALLS = ([(n, {"path": p}) for p in PATHS for n in ("read", "write", "edit")]
         + [("bash", {"command": c}) for c in ("ls", "git push", "rm -rf x", "cat secrets/k/a.env", "echo 'x",
                                               "cat <<'EOF'\nx\nEOF", "cat <<'EOF'\nsecrets\nEOF", "ls system/claude")]
         + [("web", {"url": "https://example.com"}), ("ssh", {"server": "buildbox", "command": "ls"}),
            ("secrets", {"ref": "crm/api-1"}), ("spawn", {"agent": "webdev"}), ("spawn", {}), ("skill_load", {"name": "x"}),
            ("browser.open", {"account": "a"}), ("pc-control.click", {}), ("ask", {}), ("todo", {}), ("timer", {})])
TOOLS = [Tool("t", category=c) for c in ("none", "send", "money", "delete", "deploy", "account")] + \
        [Tool("k", secrets=["crm/api-1"]), Tool("o", secrets=["other/x"]), Tool("s", server="buildbox")]


def _decisions(home: Home) -> dict:
    out = {}
    for mode in ("auto", "plan", "bypass"):
        for ai, agent in enumerate(AGENTS):
            for si, session in enumerate(SESSIONS):
                s = SessionState(rules=list(session.rules)) if session else None
                for name, args in CALLS:
                    out[(mode, ai, si, name, str(args))] = effect(home, mode, name, args, agent=agent, session=s)
                for t in TOOLS:
                    for approved in (True, False):
                        out[(mode, ai, si, t.name + t.category, approved)] = effect(home, mode, t.name, {}, tool=t,
                                                                                   agent=agent, session=s, approved=approved)
    return out


def _legacy(tmp_path, before: Path) -> Home:
    init_tree(tmp_path / "old")
    old = Home(tmp_path / "old")
    shutil.rmtree(old.root / "modes")
    (old.root / "rechte.md").unlink()
    shutil.copy(before, old.root / "permissions.md")
    return old


def _compare(new: Home, old: Home, allowed_diff) -> None:
    a, b = _decisions(old), _decisions(new)
    diff = {k: (a[k], b[k]) for k in a if a[k] != b[k] and not allowed_diff(k, a[k], b[k])}
    assert not diff, f"{len(diff)} decisions changed, e.g. {list(diff.items())[:5]}"
    assert len(a) > 1000


def test_the_shipped_modes_decide_as_the_shipped_permissions_block_did(home, tmp_path):
    old = _legacy(tmp_path, FIX / "template-before-permissions.md")
    _compare(home, old, lambda k, was, now: False)
    for tree in (home, old):                                  # an older tree too: a new mode file would switch it
        assert effect(tree, "auto", "write", {"path": "modes/auto.md"}) == "ask"
        assert effect(tree, "auto", "write", {"path": "rechte.md"}) == "ask"
        assert effect(tree, "bypass", "write", {"path": "modes/auto.md"}) == "deny"


def _user(tmp_path) -> Home:
    init_tree(tmp_path / "user")
    h = Home(tmp_path / "user")
    shutil.rmtree(h.root / "modes")
    shutil.copytree(FIX / "tree" / "modes", h.root / "modes")
    for f in ("permissions.md", "rechte.md"):
        shutil.copy(FIX / "tree" / f, h.root / f)
    return h


def test_user_modes_decide_as_his_permissions_block_did(tmp_path):
    new, old = _user(tmp_path), _legacy(tmp_path, FIX / "tree-before-permissions.md")
    locked = lambda k: k[3] == "bash" and any(w in k[4] for w in ("vaultwarden", "schluesseldienst"))
    _compare(new, old, lambda k, was, now: locked(k))
    assert all(effect(new, m, "bash", {"command": "ls /srv/vaultwarden"}) == "deny" for m in ("auto", "plan", "bypass"))


def test_user_new_permissions_file_with_its_old_block_decides_the_same_on_an_old_style_reading(tmp_path):
    """While a machine still runs a core that reads only `modes:` of permissions.md, the new file must work there."""
    new = _user(tmp_path)
    shutil.rmtree(new.root / "modes")                    # what an older core sees: no mode files
    (new.root / "rechte.md").unlink()
    new_mode_files = _user(tmp_path / "b")
    a, b = _decisions(new), _decisions(new_mode_files)
    assert {k: (a[k], b[k]) for k in a if a[k] != b[k]} == {}


def test_user_mode_files_are_the_tree_fixture(tmp_path):
    h = _user(tmp_path)
    assert not [f for f in M.findings(h) if f[0] in ("fail", "warn")]
    assert any("number 13 allowed" in f[1] for f in M.findings(h) if f[0] == "info")   # the user's bypass, on purpose
    assert h.unguarded_reason() == ""
    ms = h.modes()
    assert ms["plan"]["summary"]["fragt"] == ["21 Shell-Befehle ausführen",
                                              "22 Unlesbare Shell-Zeilen ausführen (z. B. Heredoc, offene Anführungszeichen)"]
