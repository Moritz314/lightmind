"""anthropic_api and openai_compat against httpx.MockTransport: no network, no real key."""
import json
import shutil

import httpx
import pytest

from lightmind import loop as L
from lightmind.context import Context
from lightmind.files import Home
from lightmind.model import Error, Message, RateLimit, Request, Run, Text, ToolCall, Usage
from lightmind.providers import base
from lightmind.providers.anthropic_api import AnthropicApiProvider, to_api_messages
from lightmind.providers.base import ProviderError
from lightmind.providers.openai_compat import OpenAICompatProvider
from lightmind.runs import RunDir

from test_loop_support import make_dispatcher

KEY = "sk-ant-TESTKEY-0123456789"
ORKEY = "sk-or-TESTKEY-9876543210"
KW = dict(model="claude-sonnet-5-5", effort="medium", context_limit=100_000, handover_at=0.7, switch_at=0.9)


def env_file(key_var: str, key: str) -> str:
    return f"# test account\n# --- END HEADER ---\n{key_var}={key}\n"


@pytest.fixture
def ahome(tree) -> Home:
    """The shipped tree with both examples switched on and a (fake) key each."""
    prov = tree / "providers"
    for src, dst in (("_example-anthropic-api.md", "anthropic-api.md"), ("_example-openrouter.md", "openrouter.md")):
        shutil.copy(prov / src, prov / dst)
    (tree / "secrets" / "anthropic").mkdir(parents=True, exist_ok=True)
    (tree / "secrets" / "anthropic" / "api-1.env").write_text(env_file("ANTHROPIC_API_KEY", KEY), "utf-8")
    (tree / "secrets" / "openrouter").mkdir(parents=True, exist_ok=True)
    (tree / "secrets" / "openrouter" / "main.env").write_text(env_file("OPENROUTER_API_KEY", ORKEY), "utf-8")
    return Home(tree)


def set_fallback(home: Home, name: str, to: str) -> Home:
    p = home.root / "providers" / f"{name}.md"
    p.write_text(p.read_text("utf-8").replace("fallback: []", f"fallback: [{to}]"), "utf-8")
    return Home(home.root)


class Server:
    """Mock endpoint: answers with the scripted responses in order and records the requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def stream(text: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream", **(headers or {})},
                          content=text.encode())


def a_sse(*events: tuple[str, dict]) -> str:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events)


def a_text(text_parts, usage=None, headers=None, stop="end_turn") -> httpx.Response:
    u = {"input_tokens": 25, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10,
         "output_tokens": 1, **(usage or {})}
    ev = [("message_start", {"type": "message_start", "message": {"usage": u}}),
          ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})]
    ev += [("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": t}}) for t in text_parts]
    ev += [("content_block_stop", {"index": 0}),
           ("message_delta", {"delta": {"stop_reason": stop}, "usage": {"output_tokens": 7}}),
           ("message_stop", {})]
    return stream(a_sse(*ev), headers)


def a_tool(id_, name, args: dict, text="Let me look.") -> httpx.Response:
    j = json.dumps(args)
    ev = [("message_start", {"message": {"usage": {"input_tokens": 30, "output_tokens": 1}}}),
          ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
          ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
          ("content_block_stop", {"index": 0}),
          ("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": id_, "name": name,
                                                                   "input": {}}}),
          ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": j[:6]}}),
          ("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": j[6:]}}),
          ("content_block_stop", {"index": 1}),
          ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}),
          ("message_stop", {})]
    return stream(a_sse(*ev))


async def drain(provider, req) -> list:
    return [e async for e in provider.turn(req)]


def req(text="hi", **kw) -> Request:
    kw = {"system": "SYSTEM PREFIX", "state": "STATE NOW", "model": "claude-sonnet-5-5", "effort": "medium", **kw}
    return Request(messages=[Message("user", text)], **kw)


def make(home, name="anthropic-api", server: Server | None = None):
    p = base.make_provider(home, name)
    if server:
        p.transport = server.transport
    return p


# ============================================================ anthropic: one turn
async def test_text_turn_streams_text_and_usage(ahome):
    srv = Server(a_text(["Hel", "lo"]))
    evs = await drain(make(ahome, server=srv), req())
    assert isinstance(make(ahome), AnthropicApiProvider)
    assert [e.text for e in evs if isinstance(e, Text)] == ["Hel", "lo"]
    assert evs[-1] == Usage(input=25, output=7, cache_read=100, cache_write=10)
    r = srv.requests[0]
    assert str(r.url) == "https://api.anthropic.com/v1/messages"
    assert r.headers["x-api-key"] == KEY and r.headers["anthropic-version"]
    b = srv.body()
    assert b["stream"] is True and b["model"] == "claude-sonnet-5-5" and b["max_tokens"] == 32000
    assert b["messages"] == [{"role": "user", "content": "hi"}]
    assert b["output_config"] == {"effort": "medium"} and "thinking" not in b and "tools" not in b


async def test_prefix_has_cache_control_and_state_stays_after_it(ahome):
    srv = Server(a_text(["x"]))
    await drain(make(ahome, server=srv), req())
    system = srv.body()["system"]
    assert system[0] == {"type": "text", "text": "SYSTEM PREFIX", "cache_control": {"type": "ephemeral"}}
    assert system[1] == {"type": "text", "text": "STATE NOW"}


async def test_history_has_a_rolling_cache_breakpoint_in_the_request(ahome):
    srv = Server(a_text(["x"]))
    hist = [Message("user", "first"), Message("assistant", "answer")]
    r = req("next")
    r.messages = hist + r.messages
    await drain(make(ahome, server=srv), r)
    msgs = srv.body()["messages"]
    assert msgs[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[-1]["content"] == "next"


async def test_old_models_get_no_effort(ahome):
    srv = Server(a_text(["x"]))
    await drain(make(ahome, server=srv), req(model="claude-haiku-4-5"))
    assert "output_config" not in srv.body()


async def test_tool_call_turn_then_tool_result_through_the_loop(ahome):
    """owns_loop=False end to end: ToolCall -> dispatcher -> tool message -> second request."""
    srv = Server(a_tool("toolu_1", "read", {"path": "lightmind.md", "limit": 2}), a_text(["Done."]))
    prov = make(ahome, server=srv)
    d, run = make_dispatcher(ahome)
    ctx = Context(blocks=[("lightmind", "PREFIX")], state="STATE", tool_schemas=d.schemas())
    run.run.account, run.run.model = "anthropic-api", "claude-sonnet-5-5"
    seen = []
    res = await L.run_loop(prov, ctx, d, run, [Message("user", "read it")], on_event=seen.append, **KW)
    assert (res.status, res.final_text) == ("done", "Done.")
    first, second = srv.body(0), srv.body(1)
    assert {t["name"] for t in first["tools"]} >= {"read", "bash"}
    assert all(set(t) == {"name", "description", "input_schema"} for t in first["tools"])
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assistant, result = second["messages"][1], second["messages"][2]
    assert assistant["content"][0] == {"type": "text", "text": "Let me look."}
    assert assistant["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "read",
                                       "input": {"path": "lightmind.md", "limit": 2},
                                       "cache_control": {"type": "ephemeral"}}     # rolling history breakpoint
    assert result["content"][0]["type"] == "tool_result" and result["content"][0]["tool_use_id"] == "toolu_1"
    assert "lightmind" in result["content"][0]["content"].lower()
    assert any(isinstance(e, ToolCall) and e.args["path"] == "lightmind.md" for e in seen)


def test_parallel_tool_results_share_one_user_message():
    calls = [ToolCall("a", "read", {}), ToolCall("b", "read", {})]
    out = to_api_messages([Message("user", "go"), Message("assistant", "", tool_calls=calls),
                           Message("tool", "ra", tool_call_id="a"), Message("tool", "rb", tool_call_id="b"),
                           Message("user", "next")])
    assert [m["role"] for m in out] == ["user", "assistant", "user", "user"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]
    assert out[1]["content"] == [{"type": "tool_use", "id": "a", "name": "read", "input": {}},
                                 {"type": "tool_use", "id": "b", "name": "read", "input": {}}]


async def test_cost_comes_from_the_price_table(ahome):
    srv = Server(a_text(["x"], usage={"input_tokens": 1000, "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0}))
    d, run = make_dispatcher(ahome)
    run.run.account, run.run.model = "anthropic-api", "claude-sonnet-5-5"
    ctx = Context(blocks=[("lightmind", "P")], state="S")
    res = await L.run_loop(make(ahome, server=srv), ctx, d, run, [Message("user", "hi")], **KW)
    meta = json.loads((run.path / "meta.json").read_text("utf-8"))
    assert res.tokens.input == 1000 and res.tokens.output == 7
    assert meta["cost_est"] is False
    assert meta["cost_usd"] == pytest.approx((1000 * 2 + 7 * 10) / 1e6)       # sonnet 5.5: $2 in, $10 out


async def test_rate_limit_headers_become_an_event(ahome):
    h = {"anthropic-ratelimit-requests-limit": "100", "anthropic-ratelimit-requests-remaining": "90",
         "anthropic-ratelimit-tokens-limit": "1000", "anthropic-ratelimit-tokens-remaining": "50",
         "anthropic-ratelimit-tokens-reset": "2026-10-20T12:00:00Z"}
    evs = await drain(make(ahome, server=Server(a_text(["x"], headers=h))), req())
    rl = next(e for e in evs if isinstance(e, RateLimit))
    assert (rl.kind, rl.utilization) == ("tokens", pytest.approx(0.95)) and rl.resets_at > 0


@pytest.mark.parametrize("status,body,kind", [
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, "rate_limit"),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, "overloaded"),
    (400, {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "Your credit balance is too low"}}, "billing_error"),
    (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, "auth"),
    (500, {"type": "error", "error": {"type": "api_error", "message": "oops"}}, "other")])
async def test_http_errors_map_to_error_kinds(ahome, status, body, kind):
    evs = await drain(make(ahome, server=Server(httpx.Response(status, json=body))), req())
    assert len(evs) == 1 and isinstance(evs[0], Error) and evs[0].kind == kind


async def test_mid_stream_overload_is_an_error(ahome):
    s = a_sse(("message_start", {"message": {"usage": {}}}),
              ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    evs = await drain(make(ahome, server=Server(stream(s))), req())
    assert [e.kind for e in evs if isinstance(e, Error)] == ["overloaded"]


async def test_connection_failure_is_an_error_event(ahome):
    def boom(request):
        raise httpx.ConnectError("no route")
    p = make(ahome)
    p.transport = httpx.MockTransport(boom)
    evs = await drain(p, req())
    assert evs[0].kind == "other" and "no route" in evs[0].message


async def test_429_pauses_the_run_and_the_loop_names_the_fallback(ahome):
    home = set_fallback(ahome, "anthropic-api", "openrouter")
    srv = Server(httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "limit"}},
                                headers={"retry-after": "30"}))
    d, run = make_dispatcher(home)
    run.run.account, run.run.model = "anthropic-api", "claude-sonnet-5-5"
    ctx = Context(blocks=[("lightmind", "P")], state="S")
    res = await L.run_loop(make(home, server=srv), ctx, d, run, [Message("user", "hi")], **KW)
    assert (res.status, res.exit_reason, res.switch_to) == ("paused", "rate_limit", "openrouter")


async def test_missing_key_names_the_secret_to_create(ahome):
    (ahome.root / "secrets" / "anthropic" / "api-1.env").unlink()
    with pytest.raises(ProviderError) as e:
        await drain(make(ahome), req())
    assert e.value.kind == "auth" and "anthropic/api-1" in str(e.value) and "ANTHROPIC_API_KEY" in str(e.value)


async def test_the_key_stays_out_of_events_logs_and_errors(ahome):
    echo = {"error": {"type": "authentication_error", "message": f"bad key {KEY}"}}
    srv = Server(httpx.Response(401, json=echo))
    d, run = make_dispatcher(ahome)
    run.run.account, run.run.model = "anthropic-api", "claude-sonnet-5-5"
    ctx = Context(blocks=[("lightmind", "P")], state="S")
    seen = []
    await L.run_loop(make(ahome, server=srv), ctx, d, run, [Message("user", "hi")], on_event=seen.append, **KW)
    assert srv.requests[0].headers["x-api-key"] == KEY                       # only the header carries it
    assert KEY.encode() not in srv.requests[0].content                       # not in the body dump
    assert KEY not in repr(seen)
    for f in run.path.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text("utf-8", "replace"), f


def test_base_url_and_max_tokens_come_from_the_account_file(ahome):
    p = ahome.root / "providers" / "anthropic-api.md"
    p.write_text(p.read_text("utf-8").replace('base_url: ""', "base_url: http://proxy.local/").replace(
        "max_tokens: 32000", "max_tokens: 999"), "utf-8")
    h = Home(ahome.root)
    prov = make(h)
    assert prov.max_tokens == 999 and prov.acct.base_url == "http://proxy.local/"


# ============================================================ session on an API account
async def test_a_session_can_be_opened_on_an_api_account(ahome):
    s = L.open_session(ahome, account="anthropic-api", model="medium")
    assert (s.info.account, s.info.model) == ("anthropic-api", "claude-sonnet-5-5")
    assert isinstance(s.provider, AnthropicApiProvider)
    srv = Server(a_text(["Hi ", "there"]))
    s.provider.transport = srv.transport
    evs = [e async for e in s.send("hello")]
    assert "".join(e.text for e in evs if isinstance(e, Text)) == "Hi there"
    assert srv.body()["model"] == "claude-sonnet-5-5"
    assert srv.body()["messages"][-1]["role"] == "user"
    assert s.info.cost_usd > 0 and s.info.cost_est is False
    s.close()


def test_switching_a_session_to_openrouter(ahome):
    s = L.open_session(ahome, account="anthropic-api", model="medium")
    s.set(account="openrouter")
    assert isinstance(s._next_provider, OpenAICompatProvider)   # takes over with the next message
    s.close()


# ============================================================ openai-compatible
def o_sse(*chunks: dict | str) -> str:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)


def o_text(parts, usage=None, headers=None) -> httpx.Response:
    ch = [{"choices": [{"delta": {"role": "assistant", "content": p}}]} for p in parts]
    ch.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    ch.append({"choices": [], "usage": usage or {"prompt_tokens": 135, "completion_tokens": 9,
                                                 "prompt_tokens_details": {"cached_tokens": 100,
                                                                           "cache_write_tokens": 10}}})
    return stream(": OPENROUTER PROCESSING\n\n" + o_sse(*ch, "[DONE]"), headers)


def o_tool(id_, name, args: dict) -> httpx.Response:
    j = json.dumps(args)
    ch = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": id_, "type": "function",
                                                   "function": {"name": name, "arguments": ""}}]}}]},
          {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": j[:5]}}]}}]},
          {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": j[5:]}}]}}]},
          {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
          {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 5}}]
    return stream(o_sse(*ch, "[DONE]"))


OR_MODEL = "anthropic/claude-sonnet-5.5"


def oreq(text="hi", **kw) -> Request:
    return req(text, model=OR_MODEL, **kw)


async def test_openrouter_text_turn_usage_and_headers(ahome):
    p = ahome.root / "providers" / "openrouter.md"
    p.write_text(p.read_text("utf-8").replace('referer: ""', "referer: https://example.org"), "utf-8")
    h = Home(ahome.root)
    srv = Server(o_text(["Hel", "lo"]))
    evs = await drain(make(h, "openrouter", srv), oreq())
    assert [e.text for e in evs if isinstance(e, Text)] == ["Hel", "lo"]
    assert evs[-1] == Usage(input=25, output=9, cache_read=100, cache_write=10)   # prompt minus cached minus written
    r = srv.requests[0]
    assert str(r.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert r.headers["authorization"] == f"Bearer {ORKEY}"
    assert r.headers["http-referer"] == "https://example.org" and r.headers["x-title"] == "Lightmind"
    b = srv.body()
    assert b["stream_options"] == {"include_usage": True} and b["reasoning"] == {"effort": "medium"}
    sys_parts = b["messages"][0]["content"]
    assert sys_parts[0]["cache_control"] == {"type": "ephemeral"} and sys_parts[1]["text"] == "STATE NOW"


async def test_openrouter_tool_turn_through_the_loop(ahome):
    srv = Server(o_tool("call_1", "read", {"path": "lightmind.md"}), o_text(["ok"]))
    prov = make(ahome, "openrouter", srv)
    d, run = make_dispatcher(ahome)
    run.run.account, run.run.model = "openrouter", OR_MODEL
    ctx = Context(blocks=[("lightmind", "PREFIX")], state="STATE", tool_schemas=d.schemas())
    res = await L.run_loop(prov, ctx, d, run, [Message("user", "read it")], **{**KW, "model": OR_MODEL})
    assert (res.status, res.final_text) == ("done", "ok")
    t = srv.body(0)["tools"][0]
    assert t["type"] == "function" and set(t["function"]) == {"name", "description", "parameters"}
    m = srv.body(1)["messages"]
    assert [x["role"] for x in m] == ["system", "user", "assistant", "tool"]
    assert m[2]["tool_calls"][0] == {"id": "call_1", "type": "function",
                                     "function": {"name": "read", "arguments": '{"path": "lightmind.md"}'}}
    assert m[3]["tool_call_id"] == "call_1" and m[3]["content"]


@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (402, "billing_error"), (401, "auth"),
                                         (503, "overloaded"), (400, "other")])
async def test_openrouter_errors(ahome, status, kind):
    srv = Server(httpx.Response(status, json={"error": {"message": f"nope {ORKEY}", "code": status}}))
    evs = await drain(make(ahome, "openrouter", srv), oreq())
    assert evs[0].kind == kind and ORKEY not in evs[0].message


async def test_openrouter_missing_key(ahome):
    (ahome.root / "secrets" / "openrouter" / "main.env").unlink()
    with pytest.raises(ProviderError, match="openrouter/main"):
        await drain(make(ahome, "openrouter"), oreq())


async def test_local_server_needs_no_key(ahome):
    (ahome.root / "providers" / "local.md").write_text(
        "---\ntype: openai-compatible\nbase_url: http://localhost:11434/v1\ndefault_model: qwen3\n---\nlocal\n", "utf-8")
    h = Home(ahome.root)
    srv = Server(o_text(["hey"]))
    evs = await drain(make(h, "local", srv), req(model="qwen3", effort="high"))
    r = srv.requests[0]
    assert str(r.url) == "http://localhost:11434/v1/chat/completions" and "authorization" not in r.headers
    b = srv.body()
    assert isinstance(b["messages"][0]["content"], str) and "reasoning" not in b     # plain system string
    assert isinstance(evs[0], Text)


def test_openai_compat_alias_type_resolves(ahome):
    (ahome.root / "providers" / "lms.md").write_text(
        "---\ntype: openai-compat\nbase_url: http://localhost:1234/v1\n---\n", "utf-8")
    assert isinstance(make(Home(ahome.root), "lms"), OpenAICompatProvider)
