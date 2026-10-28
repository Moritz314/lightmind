"""Security hardening of the rule checker (phase 1): the checker must be right, whatever rules
the Markdown files hold.  Rules come from tests/fixtures/permissions_strict.md, never from the
shipped template, so these tests do not move when the template's rules change."""
import asyncio
import math
import os
import shutil
import subprocess
import sys
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
    return RuleContext(home=str(home.root), run_dir=str(home.root / "runs" / "r1"),
                       project_dir=str(home.root / "projects" / "crm"))


def agent(**perm):
    return Agent("a", permissions=perm)


def eff(home, mode, name, args, ag=None, session=None, tool=None):
    layers = R.build_layers(home, ag, mode, session)
    return R.decide_all(layers, R.checks_for_call(name, args, tool_meta=tool), ctx(home)).effect


def link_dir(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":                       # a junction needs no privilege
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0
    return False


# ------------------------------------------------- 1: placeholders in the rule lines
def test_placeholder_rule_line_denies_the_secret_file(home):
    deny = home.key_doc("permissions").meta["locked"]["deny"]
    assert "read({secrets}/**/*.env)" in deny
    assert eff(home, "bypass", "read", {"path": "secrets/a/b.env"}) == "deny"
    assert eff(home, "auto", "read", {"path": "secrets/a/info.md"}) == "allow"


# ------------------------------------------------- 2: money ledger fails closed
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, "1", None, 10 ** 400, -1])
def test_reserve_refuses_non_numbers(home, bad):
    led = R.Ledger(home)
    assert led.reserve(bad) is False
    assert led.booked() == 0 and not led.file.exists()


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_book_refuses_non_finite(home, bad):
    led = R.Ledger(home)
    with pytest.raises(ValueError, match="non-finite"):
        led._book(bad)
    assert not led.file.exists()


def test_settle_clamps_and_unknown_spend_counts_in_full(home):
    led = R.Ledger(home)
    assert led.reserve(10)
    led.settle(10, math.nan)                         # unreadable -> the full reservation stays booked
    assert led.booked() == pytest.approx(10)
    assert led.reserve(5)
    led.settle(5, 99)                                # more than reserved is clamped to the reservation
    assert led.booked() == pytest.approx(15)
    led.settle(5, -3)                                # negative counts as nothing spent
    assert led.booked() == pytest.approx(10)
    assert led.reserve(2)
    led.settle(2, math.inf)
    assert led.booked() == pytest.approx(12)
    assert not led.reserve(9)                        # 12 + 9 > 20: the limit still bites


@pytest.mark.parametrize("row", ['{"t": "2000-01-01T00:00:00", "usd": NaN}',
                                 '{"t": "2000-01-01T00:00:00", "usd": Infinity}',
                                 '{"t": "2000-01-01T00:00:00", "usd": -Infinity}',
                                 '{"t": "2000-01-01T00:00:00", "usd": "5"}'])
def test_corrupt_ledger_row_fails_closed(home, row):
    led = R.Ledger(home)
    led.file.write_text(row + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        led.booked()
    with pytest.raises(ValueError):
        led.reserve(1)                               # never "no limit reached" on unreadable books


def test_infinite_money_limit_is_rejected(home, tree):
    p = tree / "permissions.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "money_limit: 20 USD/day", "money_limit: " + "9" * 400 + " USD/day"), encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        R.Ledger(home).reserve(1)


# ------------------------------------------------- 6: 'always' stores an exact rule
def grant_always(home, ag, check):
    s = SessionState()

    async def always(q, c):
        return "always"
    d = asyncio.run(R.enforce(R.build_layers(home, ag, "auto", s), [check], ctx(home), always, s))
    assert d.effect == "allow" and len(s.rules) == 1
    return s


@pytest.mark.parametrize("cmd,others", [
    ("rm -rf *.tmp", ["rm -rf /", "rm -rf a.tmp", "rm -rf *.tmp x"]),
    ("echo {secrets}/x", ["echo secrets/x", "echo {secrets}/y"]),
    ("cat $RUN/x", ["cat runs/r1/x", "cat $RUN/y"]),
    ("ls ?", ["ls a", "ls ??"]),
    ("echo [a]", ["echo a", "echo [b]"]),
    ("x ** y", ["x ab y", "x  y"]),
])
def test_always_for_bash_is_the_exact_command(home, cmd, others):
    ag = agent(ask=["bash(*)"])
    s = grant_always(home, ag, Check("bash", cmd))
    c = ctx(home)
    assert R.match_rule(s.rules[0], Check("bash", cmd), c)
    for other in others + [cmd + " more", "z; " + cmd]:
        assert eff(home, "auto", "bash", {"command": other}, ag, s) != "allow", other
    assert eff(home, "auto", "bash", {"command": cmd}, ag, s) == "allow"


def test_always_for_paths_is_exact_and_stored_canonical(home):
    ag = agent(ask=["write(**)"])
    s = grant_always(home, ag, Check("write", "notes/./sub/../data[1].txt"))
    assert s.rules[0].pattern == "notes/data[[]1].txt"
    for ok in ("notes/data[1].txt", "notes/./x/../data[1].txt"):
        assert eff(home, "auto", "write", {"path": ok}, ag, s) == "allow", ok
    for no in ("notes/data1.txt", "notes/*.txt", "notes/data[1].txt/x", "notes/**"):
        assert eff(home, "auto", "write", {"path": no}, ag, s) != "allow", no
    assert eff(home, "auto", "edit", {"path": "notes/data[1].txt"}, ag, s) != "deny"


def test_always_for_urls_is_the_exact_url(home):
    ag = agent(ask=["web(*)"])
    s = grant_always(home, ag, Check("web", "https://a.com/x?y=*"))
    assert eff(home, "auto", "web", {"url": "https://a.com/x?y=*"}, ag, s) == "allow"
    assert eff(home, "auto", "web", {"url": "https://a.com/x?y=1"}, ag, s) != "allow"


def test_literal_escapes_round_trip():
    assert R.literal("a*b?{c}$RUN[x]") == "a[*]b[?][{]c}[$]RUN[[]x]"
    c = RuleContext(home="/h", run_dir="/h/runs/r1")
    for text in ("a*b?{c}$RUN[x]", "**", "{run}", "[*]", "plain"):
        assert R.match_rule(Rule("bash", R.literal(text), "allow"), Check("bash", text), c)


# ------------------------------------------------- 7: Windows paths
TRAPS = ["a.txt:stream", "a.txt::$DATA", "C:/x/a.txt:s", "secrets/PROGRA~1/x", "doc~1.txt", "a./b",
         "a /b", "b.env.", "b.env ", "//srv/share/x", "\\\\?\\C:\\x", "x\0y", ".../x"]
PLAIN = ["a/b.txt", "C:/Users/x/a.txt", "C:\\Users\\x\\a.txt", "./a/../b", "dir/.hidden", "../x",
         "name with space/x y.txt", "~notes/a~b.txt"]


@pytest.mark.parametrize("p", TRAPS)
def test_windows_traps_are_flagged(monkeypatch, p):
    monkeypatch.setattr(R, "_WIN", True)
    assert R.unsafe_path(p)
    monkeypatch.setattr(R, "_WIN", False)
    assert not R.unsafe_path(p)                      # only Windows has these aliases


@pytest.mark.parametrize("p", PLAIN)
def test_plain_paths_are_not_flagged(monkeypatch, p):
    monkeypatch.setattr(R, "_WIN", True)
    assert not R.unsafe_path(p)


@pytest.mark.parametrize("p", TRAPS)
def test_unsafe_path_is_denied_whatever_the_rules_say(home, monkeypatch, p):
    monkeypatch.setattr(R, "_WIN", True)
    layers = R.build_layers(home, agent(allow=["read(**)", "write(**)", "edit(**)"]), "bypass")
    for tool in ("read", "write", "edit"):
        d = R.decide(layers, Check(tool, p), ctx(home))
        assert d.effect == "deny" and "unsafe" in d.reason
        assert not R.match_rule(Rule(tool, "**", "allow"), Check(tool, p), ctx(home))


@pytest.mark.parametrize("p", ["secrets/a/b.env.", "secrets/a/b.env ", "secrets/a/b.env::$DATA",
                               "secrets/a/B~1.ENV", "secrets/a/b.env:stream"])
def test_secret_file_is_not_reachable_through_windows_aliases(home, monkeypatch, p):
    monkeypatch.setattr(R, "_WIN", True)
    assert eff(home, "bypass", "read", {"path": p}) == "deny"


def test_every_spelling_of_one_place_is_judged_alike(home):
    for p in ("secrets/a/b.env", "./secrets/a/b.env", "x/../secrets/a/b.env", "secrets//a/b.env",
              str(home.root / "secrets" / "a" / "b.env"), "secrets\\a\\b.env"):
        assert eff(home, "bypass", "read", {"path": p}) == "deny", p


def test_link_into_secrets_is_judged_by_its_target(home, tree):
    (tree / "secrets" / "a").mkdir(parents=True, exist_ok=True)
    (tree / "secrets" / "a" / "b.env").write_text("K=fake-value\n", encoding="utf-8")
    if not link_dir(tree / "innocent", tree / "secrets"):
        pytest.skip("cannot create a link here")
    assert eff(home, "bypass", "read", {"path": "innocent/a/b.env"}) == "deny"
    assert eff(home, "auto", "read", {"path": "innocent/a/b.env"}) == "deny"


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows paths ignore case")
def test_case_does_not_dodge_a_deny(home):
    assert eff(home, "bypass", "read", {"path": "SECRETS/A/B.ENV"}) == "deny"


# ------------------------------------------------- 8: web domains
def mw(rule, arg):
    return R.match_rule(R.parse_rule(rule), Check("web", arg), RuleContext(home="/h"))


@pytest.mark.parametrize("rule,url", [
    ("web(domain:example.com)", "https://EXAMPLE.Com/x"),
    ("web(domain:example.com)", "https://example.com./x"),
    ("web(domain:EXAMPLE.com)", "https://example.com:8443/x"),
    ("web(domain:*.example.com)", "https://A.Example.Com./x"),
    ("web(domain:bücher.de)", "https://xn--bcher-kva.de/"),
    ("web(domain:xn--bcher-kva.de)", "https://BÜCHER.DE/"),
    ("web(domain:example.com)", "https://ｅｘａｍｐｌｅ.com/"),       # full-width letters fold to ASCII
    ("web(domain:example.com)", "https://good.com#@example.com/"),   # fragment, not userinfo: host is good.com
])
def test_domain_matching_normalises_case_dot_and_idna(rule, url):
    expected = "good.com" not in url
    assert mw(rule, url) is expected


@pytest.mark.parametrize("url", [
    "https://example.com\\@evil.com/",
    "https://evil.com\\.example.com/",
    "https://example.com@evil.com/",
    "https://user:pw@example.com/",
    "https://example.com\t.evil.com/",
    "https://example.com/\nx",
    "https://example..com/",
    "https:///example.com",
    "example.com/path",
    "",
])
def test_ambiguous_urls_are_refused(home, url):
    assert not mw("web(domain:*)", url) and not mw("web(domain:*.example.com)", url)
    layers = R.build_layers(home, agent(allow=["web(*)", "web(domain:*)"]), "bypass")
    d = R.decide(layers, Check("web", url), ctx(home))
    assert d.effect == "deny" and "unsafe" in d.reason


def test_domain_rules_do_not_overreach():
    assert not mw("web(domain:example.com)", "https://notexample.com/")
    assert not mw("web(domain:*.example.com)", "https://evilexample.com/")
    assert not mw("web(domain:*.example.com)", "https://example.com/")
    assert not mw("web(domain:example.com)", "https://evil.com/example.com")
    assert mw("web(domain:*.example.com)", "https://a.b.example.com/")


def test_domain_deny_beats_userinfo_trick(home):
    ag = agent(allow=["web(*)"], deny=["web(domain:facebook.com)"])
    assert eff(home, "auto", "web", {"url": "https://facebook.com/x"}, ag) == "deny"
    assert eff(home, "auto", "web", {"url": "https://FACEBOOK.com./x"}, ag) == "deny"
    assert eff(home, "auto", "web", {"url": "https://example.org@facebook.com/x"}, ag) == "deny"
    assert eff(home, "auto", "web", {"url": "https://example.org/x"}, ag) == "allow"


# ------------------------------------------------- 9: bash is judged per simple command
@pytest.mark.parametrize("cmd,subs", [
    ("git status; rm -rf x", ["git status", "rm -rf x"]),
    ("a && b || c | d", ["a", "b", "c", "d"]),
    ("a\nb\r\nc", ["a", "b", "c"]),
    ("a & b", ["a", "b"]),
    ("echo $(rm x) hi", ["rm x", "echo $() hi"]),
    ("echo `id`", ["id", "echo $()"]),
    ('echo "$(id)"', ["id", 'echo "$()"']),
    ("echo '$(id)'", ["echo '$(id)'"]),
    ('git commit -m "a; b | c"', ['git commit -m "a; b | c"']),
    ("npm test 2>&1", ["npm test 2>&1"]),
    ("cmd &> out", ["cmd &> out"]),
    ("(cd x && rm y)", ["cd x", "rm y"]),
    ("ls $(echo $(whoami))", ["whoami", "echo $()", "ls $()"]),
    ("x \\; y", ["x \\; y"]),
    ("echo \"open; rm", ['echo "open; rm']),
    ("", []),
    ("  ;; ", []),
])
def test_split_bash(cmd, subs):
    assert R.split_bash(cmd) == subs


CHAINS = ["git status; rm -rf x", "git status && rm x", "git status || rm x", "git status | sh",
          "git status\nrm x", "git status $(rm x)", "git status `rm x`", "git status & rm x",
          "(git status; rm x)", "git status;rm x", "git status\r\nrm x", "git status <(rm x)"]


@pytest.mark.parametrize("cmd", CHAINS)
def test_allow_pattern_does_not_cover_a_chained_command(home, cmd):
    ag = agent(allow=["bash(git status*)"])
    assert eff(home, "auto", "bash", {"command": "git status"}, ag) == "allow"
    assert eff(home, "auto", "bash", {"command": cmd}, ag) != "allow"
    assert not R.match_rule(Rule("bash", "git status*", "allow"), Check("bash", cmd), ctx(home))


def test_quoted_operators_do_not_split(home):
    ag = agent(allow=["bash(git commit*)", "bash(echo *)"])
    for cmd in ('git commit -m "fix a; b && c"', "echo '$(rm x) `id` ; |'", 'echo "a|b"'):
        assert len(R.checks_for_call("bash", {"command": cmd})) == 1
        assert eff(home, "auto", "bash", {"command": cmd}, ag) == "allow", cmd
    assert eff(home, "auto", "bash", {"command": 'echo "$(rm x)"'}, ag) != "allow"   # expands inside ""


def test_deny_and_ask_fire_on_any_subcommand(home):
    ag = agent(allow=["bash(npm *)"], ask=["bash(git push*)"], deny=["bash(rm -rf*)"])
    assert eff(home, "auto", "bash", {"command": "npm test && npm run build"}, ag) == "allow"
    assert eff(home, "auto", "bash", {"command": "npm test && git push origin main"}, ag) == "ask"
    assert eff(home, "auto", "bash", {"command": "npm test || git push -f"}, ag) == "ask"
    assert eff(home, "auto", "bash", {"command": "npm test; rm -rf build"}, ag) == "deny"
    assert eff(home, "auto", "bash", {"command": "npm test\nrm -rf /"}, ag) == "deny"
    assert eff(home, "auto", "bash", {"command": "npm $(rm -rf build)"}, ag) == "deny"
    assert eff(home, "auto", "bash", {"command": "npm test | git push"}, ag) == "ask"
    c = ctx(home)
    chain = Check("bash", "git status; rm -rf x")
    assert R.match_rule(Rule("bash", "rm -rf*", "deny"), chain, c)
    assert R.match_rule(Rule("bash", "rm -rf*", "ask"), chain, c)
    assert not R.match_rule(Rule("bash", "rm -rf*", "allow"), chain, c)
    assert R.match_rule(Rule("bash", "*", "allow"), chain, c)         # every command is covered


def test_checks_for_bash_and_ssh_are_per_command():
    assert R.checks_for_call("bash", {"command": "a && b | c"}) == [Check("bash", "a"), Check("bash", "b"),
                                                                   Check("bash", "c")]
    assert R.checks_for_call("bash", {"command": "echo $(id)"}) == [Check("bash", "id"),
                                                                    Check("bash", "echo $()")]
    assert R.checks_for_call("bash", {"command": ""}) == [Check("bash", "")]
    assert R.checks_for_call("ssh", {"server": "k", "command": "ls; rm -rf /"}) == [
        Check("ssh", "k:ls"), Check("ssh", "k:rm -rf /"), Check("server", "k")]


def test_ssh_remote_command_chain(home):
    ag = agent(allow=["ssh(gpubox:ls*)", "server(gpubox)"])
    assert eff(home, "auto", "ssh", {"server": "gpubox", "command": "ls -la"}, ag) == "allow"
    for cmd in ("ls; rm -rf /", "ls && reboot", "ls | sh", "ls $(reboot)"):
        assert eff(home, "auto", "ssh", {"server": "gpubox", "command": cmd}, ag) != "allow", cmd
    whole = agent(servers=["gpubox"])                                   # `ssh(gpubox)` covers any command
    assert eff(home, "auto", "ssh", {"server": "gpubox", "command": "ls; reboot"}, whole) == "allow"
    assert eff(home, "auto", "ssh", {"server": "other", "command": "ls"}, whole) != "allow"


# ------------------------------------------------- 10: tool match values
MAIL = Tool("send-mail", match="to")


def test_match_values_are_checked_element_by_element():
    two = [Check("tool", "send-mail:a@example.com"), Check("tool", "send-mail:b@evil.de")]
    for v in (["a@example.com", "b@evil.de"], "a@example.com,b@evil.de", "a@example.com, b@evil.de",
              "a@example.com\nb@evil.de", "a@example.com\tb@evil.de", "a@example.com\rb@evil.de",
              ("a@example.com", ["b@evil.de"]), ["a@example.com b@evil.de"]):
        assert R.checks_for_call("send-mail", {"to": v}, tool_meta=MAIL) == two, v
    one = [Check("tool", "send-mail:a@example.com")]
    assert R.checks_for_call("send-mail", {"to": "  a@example.com \x00"}, tool_meta=MAIL) == one
    assert R.checks_for_call("send-mail", {"to": 5}, tool_meta=MAIL) == [Check("tool", "send-mail:5")]


@pytest.mark.parametrize("v", [["a@example.com", "b@evil.de"], "a@example.com,b@evil.de",
                               "b@evil.de a@example.com", "b@evil.de\na@example.com",
                               "b@evil.de\x00@example.com", {"a": "b"}, "", [], None, "   "])
def test_scoped_tool_rule_needs_every_value(home, v):
    layers = [R.Layer("scoped", [R.parse_rule("tool(send-mail:*@example.com)")], "deny")]

    def decide(to):
        return R.decide_all(layers, R.checks_for_call("send-mail", {"to": to}, tool_meta=MAIL), ctx(home)).effect
    assert decide("a@example.com") == "allow"
    assert decide(v) != "allow"


def test_star_in_plain_patterns_never_spans_separators(home):
    c = ctx(home)
    rule = Rule("tool", "send-mail:*@example.com", "allow")
    for arg in ("send-mail:b@evil.de,a@example.com", "send-mail:b@evil.de a@example.com",
                "send-mail:b@evil.de\na@example.com", "send-mail:b@evil.de\ra@example.com"):
        assert not R.match_rule(rule, Check("tool", arg), c), repr(arg)
    assert R.match_rule(rule, Check("tool", "send-mail:x@example.com"), c)
    sec = Rule("secret", "stripe/*", "allow")
    assert R.match_rule(sec, Check("secret", "stripe/acme-test"), c)
    assert not R.match_rule(sec, Check("secret", "stripe/a/b"), c)
    assert not R.match_rule(sec, Check("secret", "stripe/a b"), c)


def test_a_linked_secrets_folder_is_still_protected(home, tree, tmp_path):
    target = tmp_path / "elsewhere"
    (target / "a").mkdir(parents=True)
    (target / "a" / "b.env").write_text("K=fake-value\n", encoding="utf-8")
    shutil.rmtree(tree / "secrets")
    if not link_dir(tree / "secrets", target):
        pytest.skip("cannot create a link here")
    assert eff(home, "bypass", "read", {"path": "secrets/a/b.env"}) == "deny"          # the rule follows the link
    assert eff(home, "bypass", "read", {"path": str(target / "a" / "b.env")}) == "deny"
    assert eff(home, "auto", "read", {"path": "projects/example/projectMemory.md"}) == "allow"
