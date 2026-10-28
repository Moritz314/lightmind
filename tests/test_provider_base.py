import pytest

from lightmind.providers import base
from lightmind.providers.fake import FakeProvider

from test_loop_support import SessionHome, default_providers, default_settings


@pytest.fixture
def shome(tree):
    return SessionHome(tree)


# ---------------------------------------------------------------- resolve_account
def test_account_first_hit_wins(shome):
    assert base.resolve_account(shome) == "abo"                                   # providers default
    assert base.resolve_account(shome, project_provider="api") == "api"            # project beats default
    assert base.resolve_account(shome, agent_provider="abo2", project_provider="api") == "abo2"
    assert base.resolve_account(shome, "api", "abo2", "abo") == "api"              # chat switch beats all


def test_account_aliases_resolved(shome):
    assert base.resolve_account(shome, "main") == "abo"
    shome._providers.aliases["x"] = "main"                                          # alias of an alias
    assert base.resolve_account(shome, "x") == "abo"


def test_account_alias_cycle_is_an_error_not_a_hang(shome):
    shome._providers.aliases.update({"a": "b", "b": "a"})
    with pytest.raises(ValueError, match="unknown account or alias 'a'"):
        base.resolve_account(shome, "a")


def test_account_unknown_names_fail_loudly(shome):
    with pytest.raises(ValueError, match=r"unknown account or alias 'nope' \(chat switch\)"):
        base.resolve_account(shome, "nope")
    with pytest.raises(ValueError, match=r"\(agent\)"):                              # not skipped silently
        base.resolve_account(shome, "", "nope", "abo")
    shome._providers.default = ""
    with pytest.raises(ValueError, match="no provider account"):
        base.resolve_account(shome)


# ----------------------------------------------------------------- resolve_model
def test_model_fixed_name_passes_through(shome):
    assert base.resolve_model(shome, "abo", "claude-sonnet-5-5") == "claude-sonnet-5-5"
    assert base.resolve_model(shome, "abo", "opus") == "opus"


def test_model_tier_by_family_and_account(shome):
    assert base.resolve_model(shome, "abo", "strong") == "opus"
    assert base.resolve_model(shome, "api", "cheap") == "haiku"           # anthropic-api shares the claude row
    assert base.resolve_model(shome, "local", "cheap") == "qwen3-32b"     # account name wins over family


def test_model_default_from_settings(shome):
    assert base.resolve_model(shome, "abo") == "haiku"
    shome.settings = default_settings(defaults={"model": "medium"})
    assert base.resolve_model(shome, "abo") == "sonnet"                    # the default may be a tier
    shome.settings = {}
    with pytest.raises(ValueError, match="no default model"):
        base.resolve_model(shome, "abo")


def test_model_tier_without_entry_for_family(shome):
    with pytest.raises(ValueError, match="tier 'strong' has no model for account 'local'"):
        base.resolve_model(shome, "local", "strong")
    with pytest.raises(ValueError, match="unknown account"):
        base.resolve_model(shome, "ghost", "haiku")


# ------------------------------------------------------------------ make_provider
def test_make_provider_claude_subscription(shome):
    from lightmind.providers.claude_sdk import ClaudeSdkProvider
    p = base.make_provider(shome, "abo")
    assert isinstance(p, ClaudeSdkProvider) and p.account == "abo" and p.dispatcher is None
    assert isinstance(p, base.Provider) and p.owns_loop is True


def test_make_provider_passes_the_dispatcher(shome):
    sentinel = object()
    assert base.make_provider(shome, "abo2", sentinel).dispatcher is sentinel


def test_make_provider_api_types_have_adapters(shome):
    assert type(base.make_provider(shome, "api")).__name__ == "AnthropicApiProvider"
    assert type(base.make_provider(shome, "local")).__name__ == "OpenAICompatProvider"
    with pytest.raises(ValueError, match="unknown account"):
        base.make_provider(shome, "ghost")


def test_fake_provider_satisfies_the_protocol():
    assert isinstance(FakeProvider([]), base.Provider)


def test_settings_helpers_tolerate_a_tree_without_settings(tree):
    from lightmind.files import Home
    h = Home(tree)
    if hasattr(h, "settings"):                      # the new Home has them: then they are dicts
        assert isinstance(base.settings(h), dict)
    else:
        assert base.settings(h) == {} and base.settings_defaults(h) == {}


def test_providers_config_fixture_is_consistent():
    cfg = default_providers()
    assert cfg.fallback["abo"] == ["abo2"] and cfg.switch_at == 0.9
