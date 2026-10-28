import shutil
from pathlib import Path

import pytest

from lightmind import rules as R
from lightmind import secrets as S
from lightmind.files import Home
from lightmind.model import Check, RuleContext

FAKE = "fake-value-1234567"
STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"


@pytest.fixture
def home(tree) -> Home:
    """Tree with the strict fixture permissions (service_token, modes) instead of the template's."""
    shutil.copy(STRICT, tree / "permissions.md")
    return Home(tree)


def make(home, ref, **vars_):
    name, acc = ref.split("/")
    d = home.path("secrets") / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "info.md").write_text("---\nservers: []\n---\nTest secret.\n", encoding="utf-8")
    body = "# header line\n# --- END HEADER ---\n" + "".join(f"{k}={v}\n" for k, v in vars_.items())
    (d / f"{acc}.env").write_text(body, encoding="utf-8")


def test_parse_env_header_list_info(home):
    make(home, "stripe/test", STRIPE_KEY=FAKE, OTHER='"quoted value"')
    p = home.path("secrets") / "stripe" / "test.env"
    assert S.parse_env(p) == {"STRIPE_KEY": FAKE, "OTHER": "quoted value"}
    assert S.env_header(p) == "# header line"
    assert "example" not in S.list_secrets(home) and "stripe/test" in S.list_secrets(home)
    info = S.secret_info(home, "stripe")
    assert "Test secret." in info and "STRIPE_KEY, OTHER" in info and FAKE not in info
    with pytest.raises(ValueError):
        S.secret_info(home, "../x")


def test_inject_only_when_allowed(home):
    make(home, "stripe/test", STRIPE_KEY=FAKE)
    make(home, "mail/main", MAIL_PASS="another-fake-99")
    inj = S.build_tool_env(home, ["stripe/test", "mail/main", "nope/x"], lambda r: r == "stripe/test", {"PATH": "p"})
    assert inj.env == {"PATH": "p", "STRIPE_KEY": FAKE}
    assert inj.uses == [{"secret": "stripe/test", "vars": ["STRIPE_KEY"], "tool": ""}]
    assert inj.values == [FAKE] and inj.named == [("stripe/test", "STRIPE_KEY", FAKE)]
    assert S.build_tool_env(home, ["stripe/test"], lambda r: False).env == {}


def test_provider_and_service_credentials_never_in_tool_env(home):
    make(home, "claude-abo/abo-1", CLAUDE_CODE_OAUTH_TOKEN="fake-oauth-token-1")
    make(home, "lm-services/main", LM_SERVICE_TOKEN="fake-service-token")
    inj = S.build_tool_env(home, ["claude-abo/abo-1", "lm-services/main"], lambda r: True)
    assert inj.env == {} and inj.uses == []
    assert {"claude-abo/abo-1", "lm-services/main"} <= S.reserved_refs(home)
    assert S.provider_env(home, "claude-abo/abo-1") == {"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth-token-1"}


def test_masker():
    m = S.Masker()
    m.add("a/b", "KEY", FAKE)
    m.add("a/b", "SHORT", "abc")
    out = m.mask(f"token {FAKE} and abc and {FAKE}")
    assert out == "token <secret:a/b:KEY> and abc and <secret:a/b:KEY>"


@pytest.mark.parametrize("value", ['pa"ss\\word-123', "tab\there-1234", "line\nbreak-1234", "grüße-straße-ß-123",
                                   "emoji-\U0001f511-key1", "back\\slash-\\u00e9"])
def test_masker_also_knows_the_json_forms_of_a_secret(value):
    """A hook or tool that prints JSON writes the value differently (escapes); the log must not hold it."""
    import json
    m = S.Masker()
    m.add("a/b", "KEY", value)
    for ensure_ascii in (True, False):
        text = json.dumps({"additionalContext": f"x {value} y"}, ensure_ascii=ensure_ascii)
        out = m.mask(text)
        assert "<secret:a/b:KEY>" in out
        assert json.loads(out)["additionalContext"] == "x <secret:a/b:KEY> y"       # nothing of the secret is left
    assert m.mask(f"plain {value}") == "plain <secret:a/b:KEY>"


def test_masker_json_forms_respect_the_minimum_length():
    import json
    m = S.Masker()
    m.add("a/b", "SHORT", '"a"')                                                      # 3 characters: never masked
    assert m.mask(json.dumps('"a"')) == json.dumps('"a"')


def test_service_token(home, tree):
    make(home, "lm-services/main", LM_SERVICE_TOKEN="fake-service-token")
    assert S.service_token(home) == "fake-service-token"
    assert S.check_service_token(home, "fake-service-token")
    assert not S.check_service_token(home, "wrong") and not S.check_service_token(home, None)
    perm = tree / "permissions.md"
    perm.write_text(perm.read_text(encoding="utf-8").replace("service_token: lm-services/main", "service_token: off"), encoding="utf-8")
    assert S.service_token(home) is None and S.check_service_token(home, None)


def test_service_token_missing_secret_fails_closed(home):
    assert not S.check_service_token(home, "anything")


def test_secrets_tool_new_set_rotate_describe(home):
    ph = S.secrets_tool(home, "new", "social-acme/insta", var="PASSWORD", generate=True, length=30)
    assert ph == "<secret:social-acme/insta:PASSWORD>"
    p = home.path("secrets") / "social-acme" / "insta.env"
    first = S.parse_env(p)["PASSWORD"]
    assert len(first) == 30 and first not in ph
    assert (home.path("secrets") / "social-acme" / "info.md").exists()
    with pytest.raises(ValueError):
        S.secrets_tool(home, "new", "social-acme/insta", var="PASSWORD")
    S.secrets_tool(home, "rotate", "social-acme/insta", var="PASSWORD")
    assert S.parse_env(p)["PASSWORD"] != first
    with pytest.raises(ValueError):
        S.secrets_tool(home, "rotate", "social-acme/insta", var="MISSING")
    S.secrets_tool(home, "set", "social-acme/insta", var="EMAIL", captured="me@example.org")
    S.secrets_tool(home, "set", "social-acme/copy", var="PW", from_ref="<secret:social-acme/insta:PASSWORD>")
    assert S.parse_env(home.path("secrets") / "social-acme" / "copy.env")["PW"] == S.parse_env(p)["PASSWORD"]
    S.secrets_tool(home, "describe", "social-acme", text="Instagram account, created 2026-10-20.")
    info = (home.path("secrets") / "social-acme" / "info.md").read_text(encoding="utf-8")
    assert info.startswith("---\nservers: []\n---\n") and "created 2026-10-20" in info
    with pytest.raises(ValueError):
        S.secrets_tool(home, "set", "a/b", var="X")
    with pytest.raises(ValueError):
        S.secrets_tool(home, "new", "../evil/x", var="X")


def test_agent_writes_own_secret_folder_only_when_rule_allows(home):
    from lightmind.model import Agent
    c = RuleContext(home=str(home.root))
    chk = R.checks_for_call("secrets", {"ref": "social-acme/insta", "var": "PASSWORD"})
    assert chk == [Check("secret-write", "social-acme/insta")]
    plain = R.build_layers(home, Agent("a"), "auto")
    granted = R.build_layers(home, Agent("a", permissions={"secrets_write": ["social-acme"]}), "auto")
    assert R.decide_all(plain, chk, c).effect == "deny"
    assert R.decide_all(granted, chk, c).effect == "allow"
    other = R.checks_for_call("secrets", {"ref": "stripe/live"})
    assert R.decide_all(granted, other, c).effect == "deny"
    assert R.decide_all(R.build_layers(home, Agent("a", permissions={"secrets_write": ["social-acme"]}), "plan"),
                        chk, c).effect == "allow"       # plan locks writes to files, not the secrets tool
