"""Shell-line reading and secret-ref spelling in the rule checker (phase 1, last hardening pass).
The checker is a guard against mistakes, not a sandbox: these tests pin down what it reads the
same way the configured bash does, and that it asks when it cannot be sure."""
import asyncio
import shutil
from pathlib import Path

import pytest

from lightmind import rules as R
from lightmind.files import Home
from lightmind.model import Agent, Check, Rule, RuleContext, SessionState, Tool

STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"


@pytest.fixture
def home(tree) -> Home:
    shutil.copy(STRICT, tree / "permissions.md")
    return Home(tree)


def ctx(home):
    return RuleContext(home=str(home.root))


# ------------------------------------------------- `#` comments, read as bash reads them
@pytest.mark.parametrize("cmd,subs", [
    ("ls # rm -rf x", ["ls"]),
    ("ls\t#x", ["ls"]),
    ("ls #x\nrm y", ["ls", "rm y"]),
    ("a;#c\nb", ["a", "b"]),
    ("ls&&#c\nrm z", ["ls", "rm z"]),
    ("x=1 #c", ["x=1"]),
    ("(ls) #c", ["ls"]),
    ("echo \\\\ #b; rm x", ["echo \\\\"]),                    # an escaped backslash escapes nothing else
    ("echo a#b", ["echo a#b"]),                              # mid-word: no comment
    ("echo $# ${#v}", ["echo $# ${#v}"]),
    ("echo '#x' \"#y\"", ["echo '#x' \"#y\""]),
    ("echo $(x)#y; rm w", ["x", "echo $()#y", "rm w"]),
])
def test_hash_comments_only_at_the_start_of_a_word(cmd, subs):
    assert R.parse_bash(cmd) == (subs, True)


@pytest.mark.parametrize("cmd,subs", [
    ("echo $(ls # c\n)", ["ls", "echo $()"]),
    ("echo $(ls # c\n); rm x", ["ls", "echo $()", "rm x"]),
    ('echo "$(ls #c\n)"', ["ls", 'echo "$()"']),
    ("echo $(echo $(ls #c\n))", ["ls", "echo $()", "echo $()"]),
    ("echo `ls #c`", ["ls", "echo $()"]),
    ("echo `ls #c`; rm x", ["ls", "echo $()"]),               # the comment swallowed the closing backtick: rm x is hidden
    ("echo $(echo a#b)", ["echo a#b", "echo $()"]),           # no comment here, but nobody checks which shell thinks so
    ("echo `echo a#b`", ["echo a#b", "echo $()"]),
])
def test_a_hash_inside_a_substitution_asks(cmd, subs):
    """Shells disagree where a comment inside `$( )` or backticks ends, so the line is not read, it asks."""
    assert R.parse_bash(cmd) == (subs, False)
    assert Check("shell-unparsed", cmd) in R.checks_for_call("bash", {"command": cmd})


@pytest.mark.parametrize("cmd", ["echo $(ls) # c", "echo $(echo '#') x", 'echo "$(echo "#")"', "echo ${x#y} $(ls)",
                                 "echo $((1 + 2)) #x", "echo `ls` # c"])
def test_a_hash_outside_or_quoted_inside_a_substitution_stays_clean(cmd):
    assert R.parse_bash(cmd)[1] is True


@pytest.mark.parametrize("cmd,subs", [
    ("echo a\\ #b; rm x", ["echo a\\ #b", "rm x"]),            # `\ ` is part of the word: `#` is no comment
    ("echo a\\\n#b; rm x", ["echo a\\\n#b", "rm x"]),          # line continuation
    ("echo a \\\n#b; rm x", ["echo a \\\n#b", "rm x"]),
])
def test_hash_after_an_escape_is_not_a_comment_and_asks(cmd, subs):
    assert R.parse_bash(cmd) == (subs, False)                # the chained command stays visible


@pytest.mark.parametrize("blank", ["\x0b", "\x0c", "\r", " ", " ", "　", "\x01", "\x00", "\x1f"])
def test_only_space_and_tab_are_blanks(blank):
    subs, ok = R.parse_bash(f"echo a{blank}#b; rm x")
    assert ok is False and len(subs) == 2 and subs[1] == "rm x"      # `#` is mid-word: rm is not hidden
    assert R.parse_bash(f"ls{blank}")[1] is False


def test_odd_whitespace_inside_quotes_is_inert():
    assert R.parse_bash('echo "a\x0b#b"; rm x') == (['echo "a\x0b#b"', "rm x"], True)
    assert R.parse_bash('git commit -m "fix it"') == (['git commit -m "fix it"'], True)
    assert R.parse_bash("echo 'a b'") == (["echo 'a b'"], True)


@pytest.mark.parametrize("cmd", ['echo "abc', "echo 'abc", "echo abc\\", "echo $(ls", "echo `ls",
                                 "echo $(ls 'x)", 'echo "a\\" b', "echo a\\ #b; ls", "echo a\x0b b"])
def test_unclean_lines_are_reported(cmd):
    assert R.parse_bash(cmd)[1] is False


@pytest.mark.parametrize("cmd", ["ls", "echo 'a b' \"c d\"", "echo $(ls) `id`", "a && b | c; d &", "echo a\\ b",
                                 "ls # trailing comment", "echo \"a\\\"b\"", ""])
def test_clean_lines_are_clean(cmd):
    assert R.parse_bash(cmd)[1] is True
    assert all(c.tool != "shell-unparsed" for c in R.checks_for_call("bash", {"command": cmd}))


@pytest.mark.parametrize("cmd", ['echo "abc', "echo 'abc", "echo abc\\", "echo $(ls", "echo `ls",
                                 "echo a\\ #b; ls", "echo a\x0b b"])
def test_an_unparsed_line_asks_in_every_mode(home, cmd):
    chk = R.checks_for_call("bash", {"command": cmd})
    assert Check("shell-unparsed", cmd) in chk
    assert R.checks_for_call("ssh", {"server": "k", "command": cmd})[-2] == Check("shell-unparsed", cmd)
    wide = Agent("a", permissions={"allow": ["*"]})
    d = R.decide(R.build_layers(home, wide, "auto"), Check("shell-unparsed", cmd), ctx(home))
    assert d.effect == "ask" and "cannot be read safely" in d.reason
    d = R.decide(R.build_layers(home, wide, "bypass"), Check("shell-unparsed", cmd), ctx(home))
    assert d.effect == "allow" and "bypass asks nothing" in d.reason              # bypass never asks
    layers, s = R.build_layers(home, wide, "auto"), SessionState()

    async def yes(q, c):
        return "yes"
    assert asyncio.run(R.enforce(layers, chk, ctx(home), None, s)).effect == "deny"      # nobody to ask
    assert asyncio.run(R.enforce(layers, chk, ctx(home), yes, s)).effect == "allow"      # usable once approved


def test_a_comment_cannot_hide_a_chained_command_from_the_rules(home):
    ag = Agent("a", permissions={"allow": ["bash(echo *)"]})

    def effect(cmd):
        chk = R.checks_for_call("bash", {"command": cmd})
        return R.decide_all(R.build_layers(home, ag, "auto"), chk, ctx(home)).effect
    assert effect("echo hi # rm -rf x") == "allow"                    # really a comment
    for cmd in ("echo a\\ #b; rm x", "echo a\x0b#b; rm x", "echo a #b; rm x", "echo a\\\n#b; rm x"):
        assert effect(cmd) in ("ask", "deny"), cmd
        assert Check("bash", "rm x") in R.checks_for_call("bash", {"command": cmd})


# ------------------------------------------------- secret refs: one spelling per secret
@pytest.mark.parametrize("ref", ["a./b", "a/b.", "a /b", "a/b ", "a\\b", "a/b/c", ".a/b", "a/.b", "A../b", "a//b"])
def test_aliased_refs_are_malformed_for_the_checker(ref):
    assert Check("secret-malformed", ref) in R.checks_for_call("secrets", {"ref": ref})
    cs = R.checks_for_call("secrets", {"ref": "a/b", "from_ref": f"<secret:{ref}:V>"})
    assert any(c.tool == "secret-malformed" for c in cs)


def test_honest_refs_are_not_malformed():
    for ref in ("a", "a/b", "a.b/c-d_e", "A1/b2", "x-/y_"):
        assert all(c.tool != "secret-malformed" for c in R.checks_for_call("secrets", {"ref": ref})), ref


def test_secret_rules_fold_case_on_windows(monkeypatch):
    c = RuleContext(home="/h")
    read, write = Rule("secret", "stripe/*", "allow"), Rule("secret-write", "social-acme/*", "allow")
    monkeypatch.setattr(R, "_WIN", True)
    assert R.match_rule(read, Check("secret", "STRIPE/Live"), c)
    assert R.match_rule(write, Check("secret-write", "Social-Acme/Insta"), c)
    assert not R.match_rule(read, Check("secret", "stripe/a/b"), c)
    monkeypatch.setattr(R, "_WIN", False)
    assert not R.match_rule(read, Check("secret", "STRIPE/Live"), c)       # case-sensitive file systems differ


def test_scoped_secret_rule_applies_to_any_case_of_a_from_ref(home, monkeypatch):
    monkeypatch.setattr(R, "_WIN", True)
    a = Agent("a", permissions={"secrets_write": ["social-acme"], "secrets": ["stripe"]})
    layers = R.build_layers(home, a, "auto")

    def effect(from_ref):
        chk = R.checks_for_call("secrets", {"ref": "Social-Acme/insta", "from_ref": from_ref})
        return R.decide_all(layers, chk, ctx(home)).effect
    assert effect("<secret:STRIPE/Acme-Test:KEY>") == "allow"
    assert effect("<secret:Other/x:KEY>") == "deny"


# ------------------------------------------------- tool match values: more boundaries
@pytest.mark.parametrize("v", ["a@example.com;b@evil.de", "a@example.com<b@evil.de>", "a@example.com>b@evil.de",
                               "\"a@example.com\" 'b@evil.de'", "a@example.com\"b@evil.de", "a@example.com'b@evil.de",
                               ["a@example.com;b@evil.de"], "a@example.com ;b@evil.de;"])
def test_match_values_split_on_semicolons_angles_and_quotes(v):
    cs = R.checks_for_call("send-mail", {"to": v}, tool_meta=Tool("send-mail", match="to"))
    assert cs == [Check("tool", "send-mail:a@example.com"), Check("tool", "send-mail:b@evil.de")]


def test_a_tool_star_stops_at_every_value_boundary():
    rule = Rule("tool", "send-mail:*@example.com", "allow")
    c = RuleContext(home="/h")
    for sep in (";", "<", ">", '"', "'", ",", " ", "\n", "\t"):
        assert not R.match_rule(rule, Check("tool", f"send-mail:b@evil.de{sep}a@example.com"), c), repr(sep)
    assert R.match_rule(rule, Check("tool", "send-mail:a@example.com"), c)
