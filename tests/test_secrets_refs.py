"""One spelling per secret (phase 1, last hardening pass): refs are canonical before they are
compared with the reserved ones, and aliases (trailing dot or space, separators) never parse."""
import shutil
from pathlib import Path

import pytest

from lightmind import secrets as S
from lightmind.files import Home

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


@pytest.mark.parametrize("ref", ["a./b", "a/b.", "a /b", "a/b ", " a/b", "a\\b", "a/b/c", ".a/b", "a/.b", "A../b",
                                 "a/b..", "a//b", "a..b/c", "/b", "a/", ""])
def test_aliased_refs_do_not_parse(home, ref):
    with pytest.raises(ValueError):
        S._split(ref)
    with pytest.raises(ValueError):
        S._env_path(home, ref)
    with pytest.raises(ValueError):
        S.secrets_tool(home, "describe", ref, text="t")


def test_honest_refs_parse():
    assert S._split("a") == ("a", "")
    assert S._split("a/b") == ("a", "b")
    assert S._split("A1.x-y/b_2") == ("A1.x-y", "b_2")


def test_canon_folds_case_on_every_system():
    """Windows and macOS read Stripe/Live and stripe/live as one folder: one spelling everywhere."""
    assert S.canon_ref("Stripe/Live") == "stripe/live" and S.canon_ref("stripe/live") == "stripe/live"
    assert S.canon_ref("STRASSE/Straße") == "strasse/strasse"            # casefold, not lower


@pytest.mark.parametrize("ref", ["CLAUDE-ABO/ABO-1", "Claude-Abo/abo-1", "claude-abo/ABO-1", "LM-SERVICES/Main"])
def test_reserved_compare_folds_case(home, ref):
    for r in RESERVED:
        make(home, r, CLAUDE_CODE_OAUTH_TOKEN="fake-oauth-token-1", LM_SERVICE_TOKEN="fake-service-token")
    before = sorted(p.name for p in home.path("secrets").rglob("*.env"))
    with pytest.raises(ValueError, match="reserved"):
        S.secrets_tool(home, "set", ref, var="V", captured="x")
    with pytest.raises(ValueError, match="reserved"):
        S.secrets_tool(home, "set", "social-acme/copy", var="V", from_ref=f"<secret:{ref}:CLAUDE_CODE_OAUTH_TOKEN>")
    assert sorted(p.name for p in home.path("secrets").rglob("*.env")) == before


def test_tool_env_never_gets_a_reserved_secret_in_any_case(home):
    make(home, RESERVED[0], CLAUDE_CODE_OAUTH_TOKEN="fake-oauth-token-1")
    make(home, RESERVED[1], LM_SERVICE_TOKEN="fake-service-token")
    inj = S.build_tool_env(home, ["CLAUDE-ABO/ABO-1", "Lm-Services/Main", "claude-abo/abo-1", "lm-services/main"],
                           lambda r: True)
    assert inj.env == {} and inj.uses == []


def test_tool_env_skips_aliased_refs_instead_of_injecting(home):
    make(home, "stripe/test", KEY="fake-value-1234567")
    inj = S.build_tool_env(home, ["stripe./test", "stripe/test.", "stripe /test", "stripe/test "], lambda r: True)
    assert inj.env == {} and inj.uses == []
    assert S.build_tool_env(home, ["stripe/test"], lambda r: True).env == {"KEY": "fake-value-1234567"}


def test_other_accounts_and_spellings_of_unreserved_secrets_stay_usable(home):
    make(home, "stripe/test", KEY="fake-value-1234567")
    assert S.secrets_tool(home, "set", "stripe/copy", var="KEY", from_ref="<secret:stripe/test:KEY>") == "<secret:stripe/copy:KEY>"
