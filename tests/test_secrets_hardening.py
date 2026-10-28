"""Security hardening of the secrets tool and of the checks built for it (phase 1)."""
import shutil
from pathlib import Path

import pytest

from lightmind import rules as R
from lightmind import secrets as S
from lightmind.files import Home
from lightmind.model import Agent, Check, RuleContext

STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"
RESERVED = ["claude-abo/abo-1", "lm-services/main"]       # provider account and service token of the template


@pytest.fixture
def home(tree) -> Home:
    shutil.copy(STRICT, tree / "permissions.md")
    return Home(tree)


def make(home, ref, **vars_):
    name, acc = ref.split("/")
    d = home.path("secrets") / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "info.md").write_text("---\nservers: []\n---\nTest secret.\n", encoding="utf-8")
    body = "# header\n# --- END HEADER ---\n" + "".join(f"{k}={v}\n" for k, v in vars_.items())
    (d / f"{acc}.env").write_text(body, encoding="utf-8")


def snapshot(home):
    root = home.path("secrets")
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------- 5: reserved refs, every action
@pytest.mark.parametrize("action", ["new", "set", "rotate", "describe"])
@pytest.mark.parametrize("ref", RESERVED)
def test_reserved_ref_is_rejected_for_every_action(home, ref, action):
    make(home, ref, CLAUDE_CODE_OAUTH_TOKEN="fake-oauth-token-1", LM_SERVICE_TOKEN="fake-service-token")
    before = snapshot(home)
    with pytest.raises(ValueError, match="reserved"):
        S.secrets_tool(home, action, ref, var="CLAUDE_CODE_OAUTH_TOKEN", generate=True,
                       captured="x", text="overwritten")
    assert snapshot(home) == before


@pytest.mark.parametrize("name", ["claude-abo", "lm-services"])
def test_describe_on_the_folder_of_a_reserved_secret_is_rejected(home, name):
    make(home, f"{name}/x", V="fake-value-1234567")
    make(home, RESERVED[0], V="fake-value-1234567")
    make(home, RESERVED[1], V="fake-value-1234567")
    before = snapshot(home)
    with pytest.raises(ValueError, match="reserved"):
        S.secrets_tool(home, "describe", name, text="overwritten")
    assert snapshot(home) == before


@pytest.mark.parametrize("src", RESERVED)
def test_reserved_from_ref_is_rejected(home, src):
    make(home, src, CLAUDE_CODE_OAUTH_TOKEN="fake-oauth-token-1", LM_SERVICE_TOKEN="fake-service-token")
    for action in ("new", "set", "rotate", "describe"):
        for extra in ({}, {"captured": "x"}):
            with pytest.raises(ValueError, match="reserved"):
                S.secrets_tool(home, action, "social-acme/copy", var="STOLEN",
                               from_ref=f"<secret:{src}:CLAUDE_CODE_OAUTH_TOKEN>", **extra)
    assert not (home.path("secrets") / "social-acme").exists()


def test_other_accounts_of_the_same_folder_stay_usable(home):
    make(home, "stripe/test", KEY="fake-value-1234567")
    ph = S.secrets_tool(home, "set", "stripe/copy", var="KEY", from_ref="<secret:stripe/test:KEY>")
    assert ph == "<secret:stripe/copy:KEY>"
    assert S.parse_env(home.path("secrets") / "stripe" / "copy.env")["KEY"] == "fake-value-1234567"


# ------------------------------------------------- 5: malformed placeholders reject the call
MALFORMED = ["garbage", "<secret:a/b>", "<secret:a/b:V", "secret:a/b:V", "<secret:../etc/passwd:V>",
             "<secret:a/b/c:V>", "<secret:a/b:V> trailing", "<secret:a/b:V><secret:c/d:W>",
             "<secret:a/b:V-W>", "<secret:/b:V>", "<secret:a/:V>", 12345, ["<secret:a/b:V>"], "<secret:a\\b:V>"]


@pytest.mark.parametrize("bad", MALFORMED)
def test_malformed_from_ref_rejects_the_call_for_every_action(home, bad):
    make(home, "a/b", V="fake-value-1234567")
    before = snapshot(home)
    for action in ("new", "set", "rotate", "describe"):
        with pytest.raises(ValueError):
            S.secrets_tool(home, action, "social-acme/copy", var="V", from_ref=bad, text="t")
    assert snapshot(home) == before and not (home.path("secrets") / "social-acme").exists()


def test_placeholder_with_surrounding_whitespace_is_still_parsed(home):
    make(home, "stripe/test", KEY="fake-value-1234567")
    assert S.secrets_tool(home, "set", "stripe/copy", var="KEY",
                          from_ref="  <secret:stripe/test:KEY>\n") == "<secret:stripe/copy:KEY>"


# ------------------------------------------------- 5: the checks come from the parsed placeholder
def test_from_ref_is_checked_as_the_parsed_name_account():
    chk = R.checks_for_call("secrets", {"ref": "social-acme/insta", "from_ref": "  <secret:stripe/acme-test:KEY> "})
    assert chk == [Check("secret-write", "social-acme/insta"), Check("secret", "stripe/acme-test")]


def test_scoped_secret_rules_apply_to_from_ref(home):
    c = RuleContext(home=str(home.root))
    a = Agent("a", permissions={"secrets_write": ["social-acme"], "secrets": ["stripe"]})
    layers = R.build_layers(home, a, "auto")

    def effect(from_ref):
        chk = R.checks_for_call("secrets", {"ref": "social-acme/insta", "from_ref": from_ref})
        return R.decide_all(layers, chk, c).effect
    assert effect("<secret:stripe/acme-test:KEY>") == "allow"
    assert effect("<secret:other/x:KEY>") == "deny"
    assert effect("<secret:stripe/../other/x:KEY>") == "deny"
    # the raw placeholder text used to reach the rules as a string no scoped pattern could match
    assert Check("secret", "<secret:stripe/acme-test:KEY>") not in R.checks_for_call(
        "secrets", {"ref": "a/b", "from_ref": "<secret:stripe/acme-test:KEY>"})


@pytest.mark.parametrize("bad", MALFORMED[:12])
def test_malformed_placeholder_is_denied_by_the_checker_in_every_mode(home, bad):
    c = RuleContext(home=str(home.root))
    chk = R.checks_for_call("secrets", {"ref": "social-acme/insta", "from_ref": bad})
    assert Check("secret-malformed", str(bad)) in chk
    wide = Agent("a", permissions={"allow": ["*"]})
    for mode in ("auto", "bypass"):
        assert R.decide_all(R.build_layers(home, wide, mode), chk, c).effect == "deny"


@pytest.mark.parametrize("ref", ["../x", "a/../b", "a/b/c", "a b/c", "/a", "a/", ""])
def test_malformed_ref_is_denied_by_the_checker(home, ref):
    c = RuleContext(home=str(home.root))
    chk = R.checks_for_call("secrets", {"ref": ref})
    wide = Agent("a", permissions={"allow": ["*"]})
    assert R.decide_all(R.build_layers(home, wide, "bypass"), chk, c).effect == "deny"


def test_valid_refs_are_not_flagged():
    for ref in ("social-acme/insta", "stripe", "a.b/c_d"):
        assert R.checks_for_call("secrets", {"ref": ref}) == [Check("secret-write", ref)]
