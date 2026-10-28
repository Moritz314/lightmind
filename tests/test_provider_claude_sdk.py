"""claude_sdk adapter against a mocked SDK (real SDK message classes, scripted)."""
import json
import logging
import os
from pathlib import Path

from contextlib import aclosing

import pytest
from claude_agent_sdk import CLINotFoundError, ProcessError, ResultError, SystemMessage

from lightmind.model import Error, RateLimit, Text, ToolCall, Usage
from lightmind.providers import claude_sdk as C
from lightmind.providers.base import Notice, ProviderError, ToolDone

from test_loop_support import (FakeQuery, SessionHome, assistant, collect, default_providers, init,
                               make_dispatcher, ratelimit, request, result, tool_result, tool_use,
                               usage_dict, write)

LOGIN_MSG = "not logged in: run `lm login abo`"


@pytest.fixture
def shome(tree):
    return SessionHome(tree)


@pytest.fixture
def prov(shome):
    d, _ = make_dispatcher(shome)
    return C.ClaudeSdkProvider(shome, "abo", d)


def patch_query(monkeypatch, *scripts):
    q = FakeQuery(*scripts)
    monkeypatch.setattr(C, "_query", q)
    return q


# --------------------------------------------------------------------- options
def test_options_lockdown(prov, shome):
    o = prov._options(request("hi", system="PREFIX", model="haiku", effort="high", max_turns=7), "")
    assert o.setting_sources == [] and o.tools == [] and o.skills == []
    assert o.strict_mcp_config is True
    assert o.system_prompt["type"] == "file"
    text = Path(o.system_prompt["path"]).read_text("utf-8")
    assert text.startswith("PREFIX") and text.rstrip().endswith("not instructions from the user.")
    assert o.model == "haiku" and o.effort == "high" and o.thinking is None and o.max_turns == 7
    assert o.resume is None and o.cwd == str(shome.root)
    for k, v in C.ISOLATION_ENV.items():
        assert o.env[k] == v
    assert o.env["ANTHROPIC_API_KEY"] == "" and o.env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_options_expose_only_our_mcp_server(prov):
    o = prov._options(request(), "")
    assert list(o.mcp_servers) == ["lm"] and o.mcp_servers["lm"]["type"] == "sdk"
    names = {s["name"] for s in prov.dispatcher.schemas()}
    assert set(o.allowed_tools) == {"mcp__lm__" + n for n in names} and "mcp__lm__bash" in o.allowed_tools
    assert o.hooks and "PreToolUse" in o.hooks


def test_prompt_file_is_content_addressed_and_in_state_dir(prov, shome):
    a = prov._prompt_file("same")
    assert a == prov._prompt_file("same") != prov._prompt_file("other")
    assert a.parent == shome.path("state")                  # the folder the rules lock against agents


def test_effort_and_thinking_mapping(prov):
    low = prov._options(request(effort="low"), "")
    assert low.effort == "low" and low.thinking == {"type": "disabled"}
    for e in ("medium", "high", "max"):
        o = prov._options(request(effort=e), "")
        assert o.effort == e and o.thinking is None
    odd = prov._options(request(effort="turbo"), "")
    assert odd.effort is None and odd.thinking is None


def test_resume_and_workdir(prov, tmp_path):
    prov.workdir = tmp_path
    o = prov._options(request(), "abc-123")
    assert o.resume == "abc-123" and o.cwd == str(tmp_path)


# ------------------------------------------------------------ config dir / login
def test_config_dir_is_resolved_against_the_tree(tree, tmp_path):
    h = SessionHome(tree, providers=default_providers(abo="system/claude/claude-abo"))
    d, _ = make_dispatcher(h)
    p = C.ClaudeSdkProvider(h, "abo", d)
    env = p._options(request(), "").env
    want = os.path.normpath(str(h.root / "system" / "claude" / "claude-abo"))
    assert env["CLAUDE_CONFIG_DIR"] == want and Path(want).is_dir()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""             # the parent's token must not override the login
    assert not p.warnings


def test_config_dir_moves_with_the_tree(tree, tmp_path):
    import shutil
    other = tmp_path / "moved"
    shutil.copytree(tree, other)
    h2 = SessionHome(other, providers=default_providers(abo="system/claude/claude-abo"))
    d, _ = make_dispatcher(h2)
    env = C.ClaudeSdkProvider(h2, "abo", d)._options(request(), "").env
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(h2.root)) and str(tree) not in env["CLAUDE_CONFIG_DIR"]


def test_absolute_config_dir_is_kept(tree, tmp_path):
    h = SessionHome(tree, providers=default_providers(abo=str(tmp_path / "elsewhere")))
    assert C.account_config_dir(h, h.load_providers().accounts["abo"]) == tmp_path / "elsewhere"


def test_no_config_dir_uses_system_login_and_warns_once(prov, caplog):
    with caplog.at_level(logging.WARNING, logger="lightmind.claude_sdk"):
        env = prov._options(request(), "").env
        prov._options(request(), "")
    assert "CLAUDE_CONFIG_DIR" not in env
    assert len(prov.warnings) == 1 and "no config_dir" in prov.warnings[0]
    assert sum("no config_dir" in r.message for r in caplog.records) == 1


def test_oauth_token_from_the_accounts_secret(tree):
    h = SessionHome(tree, providers=default_providers(abo="system/claude/abo"))
    h._providers.accounts["abo"].secret = "claude-abo/abo"
    write(h.path("secrets") / "claude-abo" / "abo.env",
          "# --- END HEADER ---\nCLAUDE_CODE_OAUTH_TOKEN=sk-fake-oauth-000111\n")
    d, _ = make_dispatcher(h)
    p = C.ClaudeSdkProvider(h, "abo", d)
    env = p._options(request(), "").env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-fake-oauth-000111"      # configured token wins
    assert "CLAUDE_CONFIG_DIR" in env and not p.warnings
    assert "sk-fake-oauth-000111" not in p._mask.mask("failed with sk-fake-oauth-000111")


def test_token_without_config_dir_does_not_warn(tree):
    h = SessionHome(tree)
    h._providers.accounts["abo"].secret = "claude-abo/abo"
    write(h.path("secrets") / "claude-abo" / "abo.env", "CLAUDE_CODE_OAUTH_TOKEN=sk-fake-oauth-000111\n")
    d, _ = make_dispatcher(h)
    p = C.ClaudeSdkProvider(h, "abo", d)
    assert p._options(request(), "").env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-fake-oauth-000111"
    assert not p.warnings


def test_missing_token_file_is_not_an_error(tree):
    h = SessionHome(tree, providers=default_providers(abo="system/claude/abo"))
    h._providers.accounts["abo"].secret = "claude-abo/missing"
    d, _ = make_dispatcher(h)
    env = C.ClaudeSdkProvider(h, "abo", d)._options(request(), "").env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_login_runs_the_bundled_cli_with_the_accounts_config_dir(tree, monkeypatch):
    h = SessionHome(tree, providers=default_providers(abo="system/claude/claude-abo"))
    seen = {}

    def fake_call(cmd, env=None, cwd=None):
        seen.update(cmd=cmd, env=env, cwd=cwd)
        return 0
    monkeypatch.setattr(C.subprocess, "call", fake_call)
    assert C.login(h, "main") == 0                            # an alias works
    want = os.path.normpath(str(h.root / "system" / "claude" / "claude-abo"))
    assert seen["cmd"][1:] == ["auth", "login", "--claudeai"] and Path(seen["cmd"][0]).name.startswith("claude")
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == want and Path(want).is_dir()
    assert seen["env"]["ANTHROPIC_API_KEY"] == "" and seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert seen["cwd"] == str(h.root)


def test_login_needs_a_config_dir_and_a_known_account(shome):
    with pytest.raises(ProviderError, match="no config_dir.*config_dir: system/claude/abo"):
        C.login(shome, "abo")
    with pytest.raises(ValueError, match="unknown account"):
        C.login(shome, "ghost")


def test_bundled_cli_is_found():
    assert Path(C.cli_path()).is_file()


# ----------------------------------------------------------------- turn: events
async def test_turn_maps_the_sdk_stream(prov, monkeypatch):
    q = patch_query(monkeypatch, [
        init("sess-9"), ratelimit(0.11, 0.38),
        assistant("let me look", tool_use("tu1", "mcp__lm__read", {"path": "a.txt"}),
                  usage=usage_dict(2, 20, 100, 50)),
        tool_result("tu1", "1\thello"),
        assistant("it says hello", usage=usage_dict(3, 8, 150, 10)),
        result("sess-9", usage_dict(5, 28, 250, 60), cost=0.0042)])
    evs = await collect(prov.turn(request("read a.txt")))
    assert [type(e).__name__ for e in evs] == ["RateLimit", "RateLimit", "Text", "ToolCall",
                                               "ToolDone", "Text", "Usage"]
    assert evs[0] == RateLimit("five_hour", 0.11, 111) and evs[1] == RateLimit("seven_day", 0.38, 222)
    assert evs[2] == Text("let me look") and evs[3] == ToolCall("tu1", "read", {"path": "a.txt"})
    assert evs[4] == ToolDone("tu1", "read", True, "1\thello")
    assert evs[6] == Usage(input=5, output=28, cache_read=250, cache_write=60)
    assert prov.session_id == "sess-9" and prov.model_id == "claude-haiku-5-5"
    assert prov.context_tokens == 3 + 150 + 10                  # prompt size of the LAST call
    assert prov.cost_usd == 0.0042
    assert q.calls[0]["prompt"] == "read a.txt"


async def test_tool_error_result_is_marked(prov, monkeypatch):
    patch_query(monkeypatch, [init(), assistant(tool_use("t", "mcp__lm__bash", {"command": "x"})),
                              tool_result("t", "denied by rule bash(x)", True), result()])
    done = [e for e in await collect(prov.turn(request())) if isinstance(e, ToolDone)]
    assert done == [ToolDone("t", "bash", False, "denied by rule bash(x)")]


async def test_second_turn_resumes_and_sends_only_the_new_message(prov, monkeypatch):
    q = patch_query(monkeypatch, [init("s1"), assistant("one"), result("s1")],
                    [init("s1"), assistant("two"), result("s1")])
    first = request("first", state="STATE v1")
    await collect(prov.turn(first))
    second = request("second", state="STATE v1", history=[*first.messages])
    await collect(prov.turn(second))
    assert q.calls[0]["prompt"] == "STATE v1\n\nfirst" and q.calls[0]["options"].resume is None
    assert q.calls[1]["prompt"] == "second"                      # state unchanged: not repeated
    assert q.calls[1]["options"].resume == "s1"


async def test_changed_state_is_sent_again(prov, monkeypatch):
    q = patch_query(monkeypatch, [init("s1"), result("s1")], [init("s1"), result("s1")])
    await collect(prov.turn(request("a", state="mode: auto")))
    await collect(prov.turn(request("b", state="mode: plan")))
    assert q.calls[1]["prompt"] == "mode: plan\n\nb"


async def test_fresh_conversation_replays_history(prov, monkeypatch):
    from lightmind.model import Message
    q = patch_query(monkeypatch, [init("n1"), result("n1")])
    hist = [Message("user", "my name is Zed"), Message("assistant", "hello Zed"),
            Message("tool", "ignored tool output", tool_call_id="x")]
    await collect(prov.turn(request("who am I?", history=hist)))
    p = q.calls[0]["prompt"]
    assert "[user] my name is Zed" in p and "[assistant] hello Zed" in p and "ignored" not in p
    assert p.endswith("who am I?")


async def test_prompt_needs_a_trailing_user_message(prov):
    from lightmind.model import Request
    with pytest.raises(ProviderError, match="user message last"):
        await collect(prov.turn(Request(system="s", messages=[])))


async def test_dispatcher_is_required(shome):
    p = C.ClaudeSdkProvider(shome, "abo")
    with pytest.raises(ProviderError, match="needs a dispatcher"):
        await collect(p.turn(request()))


# ---------------------------------------------------------------- turn: failures
async def test_not_logged_in_raises_the_clear_error(prov, monkeypatch):
    patch_query(monkeypatch, [init(), assistant("Not logged in · Please run /login", error="authentication_failed"),
                              result(is_error=True, text="Not logged in · Please run /login", terminal="api_error"),
                              ResultError("Claude Code returned an error result", {"result": "Not logged in"}, 1)])
    with pytest.raises(C.NotLoggedIn) as e:
        await collect(prov.turn(request()))
    assert str(e.value) == LOGIN_MSG and e.value.kind == "auth" and isinstance(e.value, ProviderError)


async def test_not_logged_in_from_a_bare_result_error(prov, monkeypatch):
    patch_query(monkeypatch, [ResultError("x", {"result": "Not logged in · Please run /login"}, 1)])
    with pytest.raises(C.NotLoggedIn, match="lm login abo"):
        await collect(prov.turn(request()))


async def test_expired_token_gets_the_same_instruction(prov, monkeypatch):
    patch_query(monkeypatch, [init(), result(is_error=True, text="OAuth token has expired", status=401)])
    with pytest.raises(C.NotLoggedIn):
        await collect(prov.turn(request()))


async def test_rate_limit_is_an_error_event_not_an_exception(prov, monkeypatch):
    patch_query(monkeypatch, [init(), assistant("API Error: 429", error="rate_limit"),
                              result(is_error=True, text="API Error: 429 rate limit", status=429,
                                     usage=usage_dict(0, 0)),
                              ResultError("err", {"result": "API Error: 429 rate limit"}, 1)])
    evs = await collect(prov.turn(request()))
    errs = [e for e in evs if isinstance(e, Error)]
    assert len(errs) == 1 and errs[0].kind == "rate_limit" and "429" in errs[0].message
    assert not any(isinstance(e, Text) for e in evs)             # the error text is not model output


@pytest.mark.parametrize("status,text,sdk,kind", [
    (402, "credit balance is too low", None, "billing_error"),
    (529, "overloaded", None, "overloaded"),
    (None, "boom", "server_error", "overloaded"),
    (None, "boom", None, "other"),
])
async def test_error_kinds(prov, monkeypatch, status, text, sdk, kind):
    msgs = [init()] + ([assistant(text, error=sdk)] if sdk else []) + [result(is_error=True, text=text, status=status)]
    patch_query(monkeypatch, msgs)
    errs = [e for e in await collect(prov.turn(request())) if isinstance(e, Error)]
    assert [e.kind for e in errs] == [kind]


async def test_max_turns_is_its_own_kind(prov, monkeypatch):
    patch_query(monkeypatch, [init(), result(is_error=True, subtype="error_max_turns", text=None,
                                             terminal="max_turns")])
    errs = [e for e in await collect(prov.turn(request())) if isinstance(e, Error)]
    assert [e.kind for e in errs] == ["max_turns"]


async def test_lost_conversation_is_replayed_once(prov, monkeypatch):
    from lightmind.model import Message
    prov.session_id = "gone"
    q = patch_query(monkeypatch,
                    [init("gone"), result("gone", is_error=True, text="No conversation found with session ID: gone"),
                     ResultError("x", {"result": "No conversation found"}, 1)],
                    [init("fresh"), assistant("recovered"), result("fresh")])
    hist = [Message("user", "earlier question"), Message("assistant", "earlier answer")]
    evs = await collect(prov.turn(request("go on", history=hist)))
    assert q.calls[0]["options"].resume == "gone" and q.calls[1]["options"].resume is None
    assert "[user] earlier question" in q.calls[1]["prompt"]
    assert any(isinstance(e, Notice) and e.kind == "warning" for e in evs)
    assert [e.text for e in evs if isinstance(e, Text)] == ["recovered"] and prov.session_id == "fresh"


async def test_a_lost_conversation_reported_as_an_error_is_replayed_too(prov, monkeypatch):
    """The CLI of another account ends with an error and no result at all ("No conversation found"): the same replay (AN-760)."""
    from lightmind.model import Message
    prov.session_id = "of-the-other-account"
    q = patch_query(monkeypatch,
                    [ResultError("x", {"result": "No conversation found with session ID: of-the-other-account"}, 1)],
                    [init("fresh"), assistant("recovered"), result("fresh")])
    evs = await collect(prov.turn(request("go on", history=[Message("user", "earlier question")])))
    assert q.calls[0]["options"].resume == "of-the-other-account" and q.calls[1]["options"].resume is None
    assert [e.text for e in evs if isinstance(e, Text)] == ["recovered"] and prov.session_id == "fresh"
    assert not [e for e in evs if isinstance(e, Error)]


async def test_a_fresh_conversation_that_fails_is_just_an_error(prov, monkeypatch):
    prov.session_id = "gone"
    lost = [init(), result(is_error=True, text="No conversation found with session ID")]
    q = patch_query(monkeypatch, lost, lost)
    errs = [e for e in await collect(prov.turn(request())) if isinstance(e, Error)]
    assert len(q.calls) == 2 and [e.kind for e in errs] == ["other"]       # no endless retry


async def test_process_error_becomes_an_error_event_with_masked_stderr(tree, monkeypatch):
    h = SessionHome(tree, providers=default_providers(abo="system/claude/abo"))
    h._providers.accounts["abo"].secret = "claude-abo/abo"
    write(h.path("secrets") / "claude-abo" / "abo.env", "CLAUDE_CODE_OAUTH_TOKEN=sk-fake-oauth-000111\n")
    d, _ = make_dispatcher(h)
    p = C.ClaudeSdkProvider(h, "abo", d)

    def boom(kw):
        p._stderr.append("crash while using sk-fake-oauth-000111")
        return [ProcessError("failed", exit_code=3)]
    patch_query(monkeypatch, boom)
    errs = [e for e in await collect(p.turn(request())) if isinstance(e, Error)]
    assert len(errs) == 1 and errs[0].kind == "other" and "(3)" in errs[0].message
    assert "sk-fake-oauth-000111" not in errs[0].message and "<secret:" in errs[0].message


async def test_missing_cli_is_a_provider_error(prov, monkeypatch):
    patch_query(monkeypatch, [CLINotFoundError("Claude Code not found")])
    with pytest.raises(ProviderError, match="not found"):
        await collect(prov.turn(request()))


# ------------------------------------------------------------------ MCP + guard
async def test_mcp_handler_goes_through_the_dispatcher(prov, shome):
    write(shome.root / "notes" / "a.txt", "alpha\nbeta\n")
    tools = {t.name: t for t in prov._sdk_tools()}
    ok = await tools["read"].handler({"path": "notes/a.txt"})
    assert ok["is_error"] is False and "1\talpha" in ok["content"][0]["text"]
    write(shome.path("secrets") / "svc" / "main.env", "KEY=abcdef123456\n")
    denied = await tools["read"].handler({"path": "secrets/svc/main.env"})
    assert denied["is_error"] is True and denied["content"][0]["text"].startswith("denied by rule")
    assert "abcdef123456" not in json.dumps(denied)
    # the dispatcher logged both calls in the run
    ev = [json.loads(l) for l in (prov.dispatcher.run.path / "events.jsonl").read_text().splitlines()]
    tool_ev = [e["data"] for e in ev if e["kind"] == "tool"]
    assert [t["ok"] for t in tool_ev] == [True, False] and tool_ev[1]["denied_by"]


async def test_mcp_handler_hands_an_open_interjection_to_the_model_on_the_tool_result(prov, shome):
    """The CLI owns the loop, so the tool result is the transition: his message rides on it, framed, as a block of its own."""
    from lightmind import interject as ij
    write(shome.root / "notes" / "a.txt", "alpha\n")
    d = prov.dispatcher
    d.inbox = ij.Inbox()
    d.inbox.put("look at the desktop")
    out = await {t.name: t for t in prov._sdk_tools()}["read"].handler({"path": "notes/a.txt"})
    texts = [c["text"] for c in out["content"]]
    assert len(texts) == 2 and "1\talpha" in texts[0] and "[Interjection]" in texts[1] and "look at the desktop" in texts[1]
    ev = d.events.get_nowait()                                    # the loop shows it to the UI from the event queue
    assert (ev.id, ev.text, ev.parent) == (1, "look at the desktop", "")
    again = await {t.name: t for t in prov._sdk_tools()}["read"].handler({"path": "notes/a.txt"})
    assert len(again["content"]) == 1                             # read once, not twice


def test_tool_schemas_become_mcp_tools(prov):
    tools = prov._sdk_tools()
    assert {t.name for t in tools} == {s["name"] for s in prov.dispatcher.schemas()}
    bash = next(t for t in tools if t.name == "bash")
    assert bash.input_schema["required"] == ["command", "description"] and bash.description


async def test_guard_denies_foreign_tools(prov):
    out = await prov._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t", None)
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny" and "not a Lightmind tool" in spec["permissionDecisionReason"]


async def test_guard_denies_what_the_rules_deny_and_logs_it(prov, shome):
    write(shome.path("secrets") / "svc" / "main.env", "KEY=abcdef123456\n")
    out = await prov._pre_tool_use({"tool_name": "mcp__lm__read",
                                    "tool_input": {"path": "secrets/svc/main.env"}}, "t", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    log = [json.loads(l) for l in (prov.dispatcher.run.path / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "guard" and e["data"]["decision"] == "deny" for e in log)


async def test_guard_lets_allowed_calls_through(prov):
    out = await prov._pre_tool_use({"tool_name": "mcp__lm__read", "tool_input": {"path": "lightmind.md"}}, "t", None)
    assert out == {} and prov.guard_log[-1] == ("read", "allow")


async def test_guard_does_not_stop_asks(prov):
    # git push is an ask in the template: the dispatcher asks, the guard only stops denies
    out = await prov._pre_tool_use({"tool_name": "mcp__lm__bash", "tool_input": {"command": "git push origin"}}, "t", None)
    assert out == {}


async def test_guard_fails_closed(prov, monkeypatch):
    monkeypatch.setattr(prov, "_guard", lambda *a: (_ for _ in ()).throw(RuntimeError("kaputt")))
    out = await prov._pre_tool_use({"tool_name": "mcp__lm__read", "tool_input": {}}, "t", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "guard error" in out["hookSpecificOutput"]["permissionDecisionReason"]


async def test_guard_rejects_unknown_tool_names(prov):
    out = await prov._pre_tool_use({"tool_name": "mcp__lm__nothing", "tool_input": {}}, "t", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ------------------------------------------------- the CLI process ends with the iterator
def fake_client(monkeypatch, items, *, connect_error=None):
    """Replace ClaudeSDKClient; the returned list records the lifecycle in order."""
    log = []

    class Client:
        def __init__(self, options=None):
            log.append(("init", options))

        async def connect(self, prompt=None):
            log.append(("connect", prompt))
            if connect_error:
                raise connect_error

        async def receive_response(self):
            try:
                for m in items:
                    yield m
            finally:
                log.append(("receive closed", None))

        async def disconnect(self):
            log.append(("disconnect", None))

    monkeypatch.setattr(C, "ClaudeSDKClient", Client)
    return log


async def test_query_runs_connect_stream_disconnect(monkeypatch):
    items = [init(), assistant("one"), result()]
    log = fake_client(monkeypatch, items)
    assert await collect(C._query(prompt="hello", options="OPTS")) == items
    assert log == [("init", "OPTS"), ("connect", "hello"), ("receive closed", None), ("disconnect", None)]


async def test_query_disconnects_before_aclose_returns(monkeypatch):
    log = fake_client(monkeypatch, [init(), assistant("one"), assistant("two"), result()])
    gen = C._query(prompt="hello", options=None)
    assert isinstance(await anext(gen), SystemMessage)
    assert log[-1][0] == "connect"                       # the CLI is running
    await gen.aclose()
    assert [k for k, _ in log] == ["init", "connect", "receive closed", "disconnect"]


async def test_query_disconnects_when_the_consumer_raises(monkeypatch):
    log = fake_client(monkeypatch, [init(), assistant("one"), result()])
    with pytest.raises(RuntimeError):
        async with aclosing(C._query(prompt="x", options=None)) as stream:
            async for _ in stream:
                raise RuntimeError("boom")
    assert log[-1] == ("disconnect", None)


async def test_query_disconnects_when_connect_fails(monkeypatch):
    log = fake_client(monkeypatch, [], connect_error=CLINotFoundError("no cli"))
    with pytest.raises(CLINotFoundError):
        await collect(C._query(prompt="x", options=None))
    assert [k for k, _ in log] == ["init", "connect", "disconnect"]


async def test_not_logged_in_arrives_after_the_cli_was_shut_down(monkeypatch, prov):
    log = fake_client(monkeypatch, [init(), assistant("Not logged in", error="authentication_failed"),
                                    result(is_error=True, text="Not logged in")])
    with pytest.raises(C.NotLoggedIn):
        await collect(prov.turn(request("hi")))
    assert log[-1] == ("disconnect", None)               # no process is left behind the error


async def test_stopping_a_turn_early_shuts_the_cli_down(monkeypatch, prov):
    log = fake_client(monkeypatch, [init(), assistant(tool_use("t1", "mcp__lm__read", {"path": "x"})),
                                    assistant("later"), result()])
    gen = prov.turn(request("hi"))
    async for ev in gen:
        if isinstance(ev, ToolCall):
            break
    assert log[-1][0] != "disconnect"
    await gen.aclose()
    assert log[-1] == ("disconnect", None)


# ------------------------------------------------------------------------ usage
async def test_usage_reports_last_seen_limits_across_instances(prov, shome, monkeypatch):
    empty = await prov.usage()
    assert empty.five_hour is None and empty.est and empty.account == "abo"
    patch_query(monkeypatch, [init(), ratelimit(0.25, 0.5), result()])
    await collect(prov.turn(request()))
    u = await prov.usage()
    assert (u.five_hour, u.seven_day) == (0.25, 0.5) and u.note.startswith("last seen")
    again = C.ClaudeSdkProvider(shome, "abo")                    # a new process later
    u2 = await again.usage()
    assert (u2.five_hour, u2.seven_day) == (0.25, 0.5)
    assert (shome.path("state") / "limits-abo.json").is_file()


async def test_limits_now_reads_the_state_file_and_a_save_keeps_the_other_window(prov, shome, monkeypatch):
    assert prov.limits_now() == {}
    patch_query(monkeypatch, [init(), ratelimit(0.25, 0.5), result()])
    await collect(prov.turn(request()))
    other = C.ClaudeSdkProvider(shome, "abo")                    # a chat of another process, same account
    assert other.limits_now() == {"five_hour": 0.25, "seven_day": 0.5}
    other.limits = {"five_hour": RateLimit("five_hour", 0.6, 1)}
    other._save_limits()                                         # saw only one window: the other one stays in the file
    assert prov.limits_now() == {"five_hour": 0.6, "seven_day": 0.5}
