"""Several accounts of one type side by side, `lm accounts`, `lm secret set|list`: no network, fake keys."""
import asyncio
import getpass
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from lightmind import accounts as A
from lightmind import cli
from lightmind import loop as L
from lightmind import secrets as S
from lightmind.context import Context
from lightmind.files import Home
from lightmind.model import Message, RateLimit
from lightmind.providers import base
from lightmind.providers import claude_sdk as C
from lightmind.tui import data as tui_data

from test_loop_support import FakeQuery, collect, init, make_dispatcher, request, result
from test_provider_api import KW, Server, a_text, drain, req

KEY1, KEY2 = "sk-ant-ONE-0123456789", "sk-ant-TWO-9876543210"


@pytest.fixture
def two(tree) -> Home:
    """Two subscriptions and two API keys, as in the user's tree, chained abo -> abo-2 -> api-1 -> api-2."""
    prov = tree / "providers"
    base_abo = (prov / "claude-abo.md").read_text("utf-8")
    (prov / "claude-abo-2.md").write_text(base_abo.replace("claude-abo", "claude-abo-2")
                                          .replace("fallback: []", "fallback: [anthropic-api-1, anthropic-api-2]"), "utf-8")
    (prov / "claude-abo.md").write_text(base_abo.replace("fallback: []", "fallback: [claude-abo-2, anthropic-api-1]"), "utf-8")
    api = (prov / "_example-anthropic-api.md").read_text("utf-8")
    (prov / "anthropic-api-1.md").write_text(api.replace("fallback: []", "fallback: [anthropic-api-2]"), "utf-8")
    (prov / "anthropic-api-2.md").write_text(api.replace("anthropic/api-1", "anthropic/api-2"), "utf-8")
    for n, k in (("api-1", KEY1), ("api-2", KEY2)):
        (tree / "secrets" / "anthropic").mkdir(parents=True, exist_ok=True)
        (tree / "secrets" / "anthropic" / f"{n}.env").write_text(f"# t\n# --- END HEADER ---\nANTHROPIC_API_KEY={k}\n", "utf-8")
    return Home(tree)


# ------------------------------------------------------------- two subscriptions
def test_two_subscriptions_get_separate_config_dirs(two):
    p1, p2 = (C.ClaudeSdkProvider(two, n) for n in ("claude-abo", "claude-abo-2"))
    e1, e2 = p1._env(), p2._env()
    assert e1["CLAUDE_CONFIG_DIR"] != e2["CLAUDE_CONFIG_DIR"]
    assert Path(e1["CLAUDE_CONFIG_DIR"]) == two.root / "system" / "claude" / "claude-abo"
    assert Path(e2["CLAUDE_CONFIG_DIR"]) == two.root / "system" / "claude" / "claude-abo-2"
    assert e1["CLAUDE_CODE_OAUTH_TOKEN"] == e2["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert e1["ANTHROPIC_API_KEY"] == "" and e2["ANTHROPIC_API_KEY"] == ""


def test_the_query_of_each_subscription_carries_its_own_config_dir(two, monkeypatch):
    q = FakeQuery([init(), result()], [init("s2"), result()])
    monkeypatch.setattr(C, "_query", q)
    for n in ("claude-abo", "claude-abo-2"):
        d, _ = make_dispatcher(two)
        asyncio.run(collect(C.ClaudeSdkProvider(two, n, d).turn(request())))
    dirs = [c["options"].env["CLAUDE_CONFIG_DIR"] for c in q.calls]
    assert dirs[0].endswith("claude-abo") and dirs[1].endswith("claude-abo-2")


async def test_limits_files_are_per_account(two):
    p1, p2 = (C.ClaudeSdkProvider(two, n) for n in ("claude-abo", "claude-abo-2"))
    p1.limits, p1._seen = {"five_hour": RateLimit("five_hour", 0.8, 1.0)}, "now"
    p1._save_limits()
    state = two.path("state")
    assert (state / "limits-claude-abo.json").is_file() and not (state / "limits-claude-abo-2.json").exists()
    assert (await C.ClaudeSdkProvider(two, "claude-abo").usage()).five_hour == 0.8
    assert (await p2.usage()).five_hour is None


# -------------------------------------------------------------------- two API keys
async def test_each_api_account_uses_its_own_key(two):
    for name, key in (("anthropic-api-1", KEY1), ("anthropic-api-2", KEY2)):
        srv = Server(a_text(["hi"]))
        p = base.make_provider(two, name)
        p.transport = srv.transport
        await drain(p, req())
        assert srv.requests[0].headers["x-api-key"] == key


async def test_one_key_missing_does_not_touch_the_other(two):
    (two.root / "secrets" / "anthropic" / "api-1.env").unlink()
    srv = Server(a_text(["hi"]))
    p = base.make_provider(two, "anthropic-api-2")
    p.transport = srv.transport
    await drain(p, req())
    assert srv.requests[0].headers["x-api-key"] == KEY2
    with pytest.raises(base.ProviderError, match="anthropic/api-1"):
        await drain(base.make_provider(two, "anthropic-api-1"), req())


async def test_limit_on_key_one_names_key_two_as_fallback(two):
    srv = Server(httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "limit"}}))
    d, run = make_dispatcher(two)
    run.run.account, run.run.model = "anthropic-api-1", "claude-sonnet-5-5"
    p = base.make_provider(two, "anthropic-api-1")
    p.transport = srv.transport
    res = await L.run_loop(p, Context(blocks=[("lightmind", "P")], state="S"), d, run, [Message("user", "hi")], **KW)
    assert res.switch_to == "anthropic-api-2"


def test_fallback_skips_an_account_that_cannot_run(two):
    d, run = make_dispatcher(two)
    run.run.account = "claude-abo"
    assert L._fallback(d, run) == "anthropic-api-1"          # abo-2 is not logged in: skipped
    cfg = two.root / "system" / "claude" / "claude-abo-2"
    cfg.mkdir(parents=True)
    (cfg / A.CREDENTIALS_FILE).write_text("{}", "utf-8")
    assert L._fallback(d, run) == "claude-abo-2"
    (two.root / "secrets" / "anthropic" / "api-2.env").unlink()
    run.run.account = "anthropic-api-1"
    assert L._fallback(d, run) == ""                         # api-2 has no key: nothing to switch to


# ------------------------------------------------------------------ lm accounts
@pytest.fixture
def lm(two, capsys, monkeypatch):
    def run(*args, stdin=""):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        capsys.readouterr()
        code = cli.main(["--home", str(two.root), *args])
        out, err = capsys.readouterr()
        return code, out, err
    return run


def test_accounts_json_states_and_hints(two, lm):
    code, out, _ = lm("accounts", "--json")
    rows = {r["name"]: r for r in json.loads(out)}
    assert code == 0 and {"claude-abo", "claude-abo-2", "anthropic-api-1", "anthropic-api-2"} <= set(rows)
    assert rows["claude-abo"]["hint"] == "not logged in: lm login claude-abo" and rows["claude-abo"]["state"] == "login-missing"
    assert rows["anthropic-api-1"]["key_present"] is True and rows["anthropic-api-1"]["hint"] == ""
    assert rows["claude-abo"]["fallback"] == ["claude-abo-2", "anthropic-api-1"]
    assert KEY1 not in out and KEY2 not in out
    (two.root / "secrets" / "anthropic" / "api-2.env").unlink()
    cfg = two.root / "system" / "claude" / "claude-abo"
    cfg.mkdir(parents=True)
    (cfg / A.CREDENTIALS_FILE).write_text("SECRET-TOKEN-CONTENT", "utf-8")
    code, out, _ = lm("accounts", "--json")
    rows = {r["name"]: r for r in json.loads(out)}
    assert "SECRET-TOKEN-CONTENT" not in out
    assert rows["claude-abo"]["state"] == "ready" and rows["claude-abo"]["logged_in"] is True
    assert rows["anthropic-api-2"]["hint"] == "key missing: lm secret set anthropic/api-2"


def test_accounts_shows_last_known_limits(two, lm):
    two.path("state").mkdir(parents=True, exist_ok=True)
    (two.path("state") / "limits-claude-abo.json").write_text(json.dumps(
        {"seen": "t", "limits": {"five_hour": {"utilization": 0.42, "resets_at": 5},
                                 "seven_day": {"utilization": 0.1, "resets_at": 6}}}), "utf-8")
    rows = {r["name"]: r for r in json.loads(lm("accounts", "--json")[1])}
    assert (rows["claude-abo"]["five_hour"], rows["claude-abo"]["seven_day"]) == (0.42, 0.1)
    assert rows["claude-abo-2"]["five_hour"] is None
    text = lm("accounts")[1]
    assert "5h 42%" in text and "lm login claude-abo-2" in text and "anthropic-api-1" in text


def test_tui_hint_matches_the_cli(two):
    a = two.load_providers().accounts["claude-abo"]
    assert tui_data.account_note(two, "claude-abo", a).endswith("not logged in: lm login claude-abo")
    api = two.load_providers().accounts["anthropic-api-1"]
    assert tui_data.account_note(two, "anthropic-api-1", api) == "anthropic-api"
    (two.root / "secrets" / "anthropic" / "api-1.env").unlink()
    assert tui_data.account_note(two, "anthropic-api-1", api).endswith("key missing: lm secret set anthropic/api-1")


# ------------------------------------------------------------------ lm secret
def test_secret_set_from_a_pipe_writes_the_documented_format(two, lm):
    value = "sk-ant-NEW-VALUE-424242"
    code, out, err = lm("secret", "set", "anthropic/api-3", stdin=value + "\n")
    assert code == 0 and value not in out + err and "ANTHROPIC_API_KEY" in out
    f = two.root / "secrets" / "anthropic" / "api-3.env"
    lines = f.read_text("utf-8").splitlines()
    assert lines[0].startswith("#") and lines[1] == S.END_HEADER and lines[2] == f"ANTHROPIC_API_KEY={value}"
    assert S.parse_env(f) == {"ANTHROPIC_API_KEY": value}
    code, out, err = lm("secret", "set", "anthropic/api-3", stdin="sk-ant-OTHER-111111\n")
    assert code == 0 and "replaced" in out and S.parse_env(f)["ANTHROPIC_API_KEY"] == "sk-ant-OTHER-111111"


def test_secret_set_default_vars_and_custom_var(two, lm):
    lm("secret", "set", "openrouter/main", stdin="sk-or-1234567\n")
    lm("secret", "set", "mail/work", "MAIL_PASS", stdin="pw-1234567\n")
    assert list(S.parse_env(two.root / "secrets" / "openrouter" / "main.env")) == ["OPENROUTER_API_KEY"]
    assert list(S.parse_env(two.root / "secrets" / "mail" / "work.env")) == ["MAIL_PASS"]


@pytest.mark.parametrize("ref", ["anthropic", "../x/y", "a/b/c", "a/../b", "x/y.", "x\\y", "/abs/p", "a b/c"])
def test_secret_set_refuses_odd_refs(two, lm, ref):
    code, out, err = lm("secret", "set", ref, stdin="value-123456\n")
    assert code == 2 and "value-123456" not in out + err
    assert all("value-123456" not in f.read_text("utf-8") for f in two.root.rglob("*.env"))
    assert not list(two.root.parent.rglob("y.env")) and not (two.root / "x").exists()


def test_secret_set_refuses_empty_value_bad_var_and_service_token(two, lm):
    assert lm("secret", "set", "anthropic/api-1", stdin="\n")[0] == 2
    assert lm("secret", "set", "anthropic/api-1", "BAD-VAR", stdin="v-123456\n")[0] == 2
    assert S.parse_env(two.root / "secrets" / "anthropic" / "api-1.env")["ANTHROPIC_API_KEY"] == KEY1   # untouched
    code, _, err = lm("secret", "set", "LM-Services/main", "LM_SERVICE_TOKEN", stdin="tok-123456\n")
    assert code == 2 and "service token" in err


def test_secret_list_shows_refs_and_variable_names_only(two, lm):
    code, out, _ = lm("secret", "list")
    assert code == 0 and KEY1 not in out and KEY2 not in out
    assert "anthropic/api-1" in out and "ANTHROPIC_API_KEY" in out and "anthropic-api-1" in out
    rows = json.loads(lm("secret", "list", "--json")[1])
    assert {"ref": "anthropic/api-2", "vars": ["ANTHROPIC_API_KEY"], "used_by": ["anthropic-api-2"]} in rows


def test_secret_set_uses_a_hidden_prompt_on_a_terminal(two, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty())
    answers = iter(["sk-ant-TYPED-5555555", "sk-ant-TYPED-5555555"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["--home", str(two.root), "secret", "set", "anthropic/api-4"]) == 0
    assert S.parse_env(two.root / "secrets" / "anthropic" / "api-4.env") == {"ANTHROPIC_API_KEY": "sk-ant-TYPED-5555555"}
    answers = iter(["aaaaaaaa1", "bbbbbbbb2"])
    assert cli.main(["--home", str(two.root), "secret", "set", "anthropic/api-5"]) == 2
    assert not (two.root / "secrets" / "anthropic" / "api-5.env").exists()
