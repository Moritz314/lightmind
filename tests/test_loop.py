"""run_loop / stream_loop with scripted fake providers."""
import asyncio
import json

import pytest

from lightmind import loop as L
from lightmind.context import Context, handover_instruction
from lightmind.model import (Agent, Error, Message, RateLimit, Run, Text, ToolCall, ToolResult,
                             Usage)
from lightmind.providers.base import Notice, ProviderError, ToolDone
from lightmind.providers.fake import call, say
from lightmind.runs import RunDir

from test_loop_support import ApiFake, SdkFake, SessionHome, ToolBox, collect

KW = dict(model="haiku", effort="low", context_limit=100_000, handover_at=0.7, switch_at=0.9)


@pytest.fixture
def shome(tree):
    return SessionHome(tree)


@pytest.fixture
def run(shome):
    return RunDir(shome, Run(id="loop-1", account="abo", model="haiku"))


@pytest.fixture
def box(shome):
    return ToolBox(shome)


@pytest.fixture
def ctx():
    return Context(blocks=[("lightmind", "PREFIX")], state="STATE",
                   tool_schemas=[{"name": "read", "description": "d",
                                  "input_schema": {"type": "object", "properties": {}}}])


def msgs(text="go"):
    return [Message("user", text)]


def events_of(run):
    return [json.loads(l) for l in (run.path / "events.jsonl").read_text("utf-8").splitlines()]


# ------------------------------------------------------------------ basic turn
async def test_single_turn_records_everything(ctx, box, run):
    p = ApiFake([say("hello", input=1000, output=100)])
    seen = []
    res = await L.run_loop(p, ctx, box, run, msgs(), on_event=seen.append, **KW)
    assert (res.status, res.final_text, res.exit_reason) == ("done", "hello", "")
    assert (res.tokens.input, res.tokens.output) == (1000, 100)
    assert [type(e).__name__ for e in seen] == ["Text", "Usage"]
    assert [m.role for m in res.messages] == ["user", "assistant"] and res.messages[-1].text == "hello"
    meta = json.loads((run.path / "meta.json").read_text())
    assert meta["tokens"]["input"] == 1000 and meta["cost_usd"] > 0           # runs.py priced it
    kinds = [e["kind"] for e in events_of(run)]
    assert kinds.count("text") == 1 and kinds.count("usage") == 1 and kinds.count("message") == 1


async def test_request_carries_prefix_state_tools_model(ctx, box, run):
    p = ApiFake([say("x")])
    await L.run_loop(p, ctx, box, run, msgs("task"), **KW)
    r = p.requests[0]
    assert (r.system, r.state, r.model, r.effort) == ("PREFIX", "STATE", "haiku", "low")
    assert r.tools == ctx.tool_schemas and r.max_turns is None
    assert r.messages[-1].text == "task"


# ------------------------------------------------------------------ tool loop
async def test_tool_calls_are_executed_and_fed_back(ctx, box, run):
    box.results["read"] = ToolResult(True, "FILE TEXT")
    p = ApiFake([call("read", {"path": "a"}, id="c1", input=10, output=2), say("it says FILE TEXT", input=30, output=5)])
    got = []
    res = await L.run_loop(p, ctx, box, run, msgs(), on_event=got.append, **KW)
    assert box.calls == [("read", {"path": "a"})]
    assert [m.role for m in res.messages] == ["user", "assistant", "tool", "assistant"]
    assert res.messages[1].tool_calls == [ToolCall("c1", "read", {"path": "a"})]
    assert res.messages[2].text == "FILE TEXT" and res.messages[2].tool_call_id == "c1"
    assert p.requests[1].messages[-1].role == "tool"                    # the model sees the result
    assert ToolDone("c1", "read", True, "FILE TEXT") in got
    assert res.status == "done" and res.tokens.input == 40 and res.final_text.startswith("it says")


async def test_several_calls_in_one_turn_all_get_results_in_order(ctx, box, run):
    two = [ToolCall("a", "read", {}), ToolCall("b", "bash", {"command": "x"}), Usage(5, 1)]
    p = ApiFake([two, say("ok")])
    res = await L.run_loop(p, ctx, box, run, msgs(), **KW)
    assert [(n) for n, _ in box.calls] == ["read", "bash"]
    assert [m.tool_call_id for m in res.messages if m.role == "tool"] == ["a", "b"]


async def test_failed_tool_result_is_reported_not_raised(ctx, box, run):
    box.results["read"] = ToolResult(False, "denied by rule read(x)")
    p = ApiFake([call("read", {}), say("sorry")])
    got = []
    res = await L.run_loop(p, ctx, box, run, msgs(), on_event=got.append, **KW)
    assert ToolDone("c1", "read", False, "denied by rule read(x)") in got and res.status == "done"


async def test_long_tool_text_is_truncated_in_the_event_only(ctx, box, run):
    box.results["read"] = ToolResult(True, "x" * 5000)
    p = ApiFake([call("read", {}), say("k")])
    got = []
    res = await L.run_loop(p, ctx, box, run, msgs(), on_event=got.append, **KW)
    done = next(e for e in got if isinstance(e, ToolDone))
    assert len(done.text) == L.TOOL_TEXT_LIMIT and len(res.messages[2].text) == 5000


async def test_owning_providers_calls_are_informational(ctx, box, run):
    p = SdkFake([[ToolCall("t1", "read", {"path": "a"}), ToolDone("t1", "read", True, "ok"),
                  Text("answer"), Usage(10, 3)]])
    res = await L.run_loop(p, ctx, box, run, msgs(), **{**KW, "max_turns": 9})
    assert box.calls == [] and res.status == "done"                    # the SDK ran the tool itself
    r = p.requests[0]
    assert r.tools == [] and r.max_turns == 9
    assert res.messages[-1].tool_calls == [] and res.messages[-1].text == "answer"


async def test_max_turns_pauses_the_run(ctx, box, run):
    p = ApiFake([lambda req: call("read", {})] * 10)
    res = await L.run_loop(p, ctx, box, run, msgs(), **{**KW, "max_turns": 3})
    assert (res.status, res.exit_reason) == ("paused", "max_turns") and len(p.requests) == 3


# ---------------------------------------------------------- errors and fallback
@pytest.mark.parametrize("kind", ["rate_limit", "billing_error"])
async def test_limit_errors_pause_and_name_the_fallback(ctx, box, run, kind):
    p = ApiFake([[Error(kind, "429"), Usage()]])
    res = await L.run_loop(p, ctx, box, run, msgs(), **KW)
    assert (res.status, res.exit_reason, res.switch_to) == ("paused", kind, "abo2")


async def test_agents_with_a_fixed_provider_never_fall_back(ctx, shome, run):
    box = ToolBox(shome, agent=Agent(name="img", provider="abo"))
    p = ApiFake([[Error("rate_limit", "429"), Usage()]])
    res = await L.run_loop(p, ctx, box, run, msgs(), **KW)
    assert (res.status, res.switch_to) == ("paused", "")


async def test_account_without_chain_pauses_without_target(ctx, box, run, shome):
    run.run.account = "abo2"
    res = await L.run_loop(ApiFake([[Error("rate_limit", "x"), Usage()]]), ctx, box, run, msgs(), **KW)
    assert (res.status, res.switch_to) == ("paused", "")


async def test_fallback_alias_and_self_are_skipped(ctx, box, run, shome, monkeypatch):
    monkeypatch.setattr(L, "_usable", lambda home, name: True)       # the fake accounts have no login or key files
    shome._providers.fallback["abo"] = ["abo", "main", "ghost", "api"]
    res = await L.run_loop(ApiFake([[Error("rate_limit", "x")]]), ctx, box, run, msgs(), **KW)
    assert res.switch_to == "api"                                      # abo, main(=abo) and ghost skipped


@pytest.mark.parametrize("kind", ["auth", "overloaded", "other"])
async def test_other_errors_fail_with_a_reason(ctx, box, run, kind):
    res = await L.run_loop(ApiFake([[Error(kind, "details here")]]), ctx, box, run, msgs(), **KW)
    assert res.status == "failed" and res.exit_reason == f"{kind}: details here"


async def test_max_turns_error_pauses(ctx, box, run):
    res = await L.run_loop(SdkFake([[Text("half"), Error("max_turns", "x"), Usage(5, 5)]]), ctx, box, run, msgs(), **KW)
    assert (res.status, res.exit_reason) == ("paused", "max_turns") and res.final_text == "half"


async def test_a_failed_turn_adds_no_empty_assistant_message(ctx, box, run):
    res = await L.run_loop(ApiFake([[Error("other", "x")]]), ctx, box, run, msgs(), **KW)
    assert [m.role for m in res.messages] == ["user"]


async def test_provider_exceptions_are_logged_and_raised(ctx, box, run):
    class Boom(ApiFake):
        async def turn(self, req):
            raise ProviderError("not logged in: run `lm login abo`", "auth")
            yield  # pragma: no cover
    with pytest.raises(ProviderError, match="lm login abo"):
        await L.run_loop(Boom([]), ctx, box, run, msgs(), **KW)
    err = [e for e in events_of(run) if e["kind"] == "error"]
    assert err and err[0]["data"]["type"] == "ProviderError" and "lm login" in err[0]["data"]["message"]


# ----------------------------------------------------------------- rate limits
async def test_limit_warning_switches_after_the_turn_with_consistent_messages(ctx, box, run):
    turn1 = [ToolCall("c1", "read", {}), RateLimit("five_hour", 0.95, 1), Usage(5, 1)]
    res = await L.run_loop(ApiFake([turn1, say("never reached")]), ctx, box, run, msgs(), **KW)
    assert (res.status, res.exit_reason, res.switch_to) == ("paused", "rate_limit", "abo2")
    assert [m.role for m in res.messages] == ["user", "assistant", "tool"]     # tool result is in: resumable


async def test_limit_warning_on_a_finished_turn_only_names_the_next_account(ctx, box, run):
    res = await L.run_loop(ApiFake([[Text("fin"), RateLimit("seven_day", 0.93, 1), Usage(5, 1)]]),
                           ctx, box, run, msgs(), **KW)
    assert (res.status, res.switch_to) == ("done", "abo2")


async def test_limit_without_fallback_warns_once_and_keeps_working(ctx, box, run):
    run.run.account = "abo2"
    hot = [ToolCall("c", "read", {}), RateLimit("five_hour", 0.95, 1), Usage(5, 1)]
    p = ApiFake([hot, hot, say("end")])
    got = []
    res = await L.run_loop(p, ctx, box, run, msgs(), on_event=got.append, **KW)
    notes = [e for e in got if isinstance(e, Notice)]
    assert res.status == "done" and len(notes) == 1 and notes[0].kind == "limit" and "95%" in notes[0].text


async def test_utilization_below_switch_at_is_ignored(ctx, box, run):
    res = await L.run_loop(ApiFake([[Text("x"), RateLimit("five_hour", 0.89, 1), Usage()]]), ctx, box, run, msgs(), **KW)
    assert (res.status, res.switch_to) == ("done", "")


async def test_the_highest_utilization_is_the_one_reported(ctx, box, run):
    run.run.account = "abo2"
    got = []
    await L.run_loop(ApiFake([[Text("x"), RateLimit("five_hour", 0.91, 1), RateLimit("seven_day", 0.97, 1), Usage()]]),
                     ctx, box, run, msgs(), on_event=got.append, **KW)
    notes = [e for e in got if isinstance(e, Notice)]
    assert len(notes) == 1 and "seven_day at 97%" in notes[0].text


# ------------------------------------------------------------------- handover
async def test_handover_is_asked_once_between_turns_then_ends(ctx, box, run):
    work = call("read", {}, input=800, output=10)                    # 800 of 1000 tokens: over 70 %
    p = ApiFake([work, call("write", {"path": "log.md"}, input=850, output=10), say("handover written", input=900)])
    got = []
    res = await L.run_loop(p, ctx, box, run, msgs(), on_event=got.append,
                           **{**KW, "context_limit": 1000})
    asked = [m for m in res.messages if m.role == "user" and m.text == handover_instruction()]
    assert len(asked) == 1 and res.status == "handover"
    assert p.requests[1].messages[-1].text == handover_instruction()      # asked at the turn boundary
    assert [n.kind for n in got if isinstance(n, Notice)] == ["handover"]
    assert [n for n, _ in box.calls] == ["read", "write"]                 # the handover turn could still work


async def test_a_finished_run_does_not_need_a_handover(ctx, box, run):
    p = ApiFake([say("all done", input=900)])
    res = await L.run_loop(p, ctx, box, run, msgs(), **{**KW, "context_limit": 1000})
    assert res.status == "done" and len(p.requests) == 1


async def test_a_chat_asks_for_a_handover_after_a_finished_turn(ctx, box, run):
    p = ApiFake([say("answer", input=900), say("## Handover written", input=950)])
    res = L.LoopResult()
    evs = await collect(L.stream_loop(p, ctx, box, run, msgs(), result=res, handover_when_done=True,
                                      **{**KW, "context_limit": 1000}))
    assert res.status == "handover" and len(p.requests) == 2 and res.final_text == "## Handover written"
    assert any(isinstance(e, Notice) and e.kind == "handover" for e in evs)


async def test_below_the_threshold_nothing_happens(ctx, box, run):
    p = ApiFake([call("read", {}, input=690), say("fine", input=100)])
    res = await L.run_loop(p, ctx, box, run, msgs(), **{**KW, "context_limit": 1000})
    assert res.status == "done" and not any(m.text == handover_instruction() for m in res.messages)


async def test_context_size_comes_from_the_provider_when_it_knows_it(ctx, box, run):
    p = SdkFake([[Text("a"), Usage(input=10, output=5)]])
    p.context_tokens = 900                                              # last call's prompt, not the sum of usage
    res = L.LoopResult()
    await collect(L.stream_loop(p, ctx, box, run, msgs(), result=res, handover_when_done=True,
                                **{**KW, "context_limit": 1000}))
    assert len(p.requests) == 2 and res.context_tokens == 900


async def test_context_tokens_are_reported(ctx, box, run):
    res = await L.run_loop(ApiFake([say("x", input=300)]), ctx, box, run, msgs(), **KW)
    assert res.context_tokens == 300


async def test_zero_context_limit_never_hands_over(ctx, box, run):
    res = await L.run_loop(ApiFake([call("read", {}, input=5), say("k")]), ctx, box, run, msgs(),
                           **{**KW, "context_limit": 0})
    assert res.status == "done"


# --------------------------------------------------------------------- budget
async def test_token_budget_stops_between_turns(ctx, box, run):
    p = ApiFake([call("read", {}, input=80, output=30), say("never")])
    res = await L.run_loop(p, ctx, box, run, msgs(), budget_tokens=100, **KW)
    assert (res.status, res.exit_reason) == ("budget", "100 tokens") and len(p.requests) == 1


async def test_budget_does_not_turn_a_finished_run_into_a_failure(ctx, box, run):
    res = await L.run_loop(ApiFake([say("x", input=500, output=500)]), ctx, box, run, msgs(), budget_tokens=10, **KW)
    assert res.status == "done"


async def test_cache_reads_do_not_count_against_the_budget(ctx, box, run):
    turn = [ToolCall("c", "read", {}), Usage(input=10, output=10, cache_read=50_000)]
    res = await L.run_loop(ApiFake([turn, say("ok")]), ctx, box, run, msgs(), budget_tokens=100, **KW)
    assert res.status == "done"


# ---------------------------------------------------------------- the stream
async def test_stream_yields_events_in_order_and_fills_the_result(ctx, box, run):
    p = ApiFake([call("read", {}), say("end")])
    res = L.LoopResult()
    evs = await collect(L.stream_loop(p, ctx, box, run, msgs(), result=res, **KW))
    assert [type(e).__name__ for e in evs] == ["ToolCall", "Usage", "ToolDone", "Text", "Usage"]
    assert res.status == "done" and res.final_text == "end"


async def test_consumer_may_stop_early(ctx, box, run):
    p = ApiFake([say("one"), say("two")])
    gen = L.stream_loop(p, ctx, box, run, msgs(), **KW)
    first = await gen.__anext__()
    await gen.aclose()
    assert isinstance(first, Text) and len(p.requests) == 1


async def test_every_message_is_logged_for_resume(ctx, box, run):
    p = ApiFake([call("read", {}, id="z"), say("end")])
    await L.run_loop(p, ctx, box, run, msgs(), **KW)
    logged = [e["data"] for e in events_of(run) if e["kind"] == "message"]
    assert [m["role"] for m in logged] == ["assistant", "tool", "assistant"]
    assert logged[0]["tool_calls"][0]["id"] == "z" and logged[1]["tool_call_id"] == "z"


async def test_usage_accumulates_in_the_run_meta(ctx, box, run):
    p = ApiFake([call("read", {}, input=100, output=10), say("e", input=200, output=20)])
    await L.run_loop(p, ctx, box, run, msgs(), **KW)
    t = json.loads((run.path / "meta.json").read_text())["tokens"]
    assert (t["input"], t["output"]) == (300, 30)


# ------------------------------------------------------------------ cancelled tool calls
class CancellingBox(ToolBox):
    """The first call finishes, the second is cancelled (Esc in the TUI, Ctrl+C in `lm chat`)."""
    async def call(self, name, args):
        if len(self.calls) == 1:
            self.calls.append((name, args))
            raise asyncio.CancelledError
        return await super().call(name, args)


async def test_a_cancelled_tool_call_still_gets_a_tool_message_for_every_call(ctx, shome, run):
    box = CancellingBox(shome)
    p = ApiFake([[ToolCall("c1", "read", {}), ToolCall("c2", "read", {}), ToolCall("c3", "read", {}), Usage()]])
    m = msgs()
    with pytest.raises(asyncio.CancelledError):
        await collect(L.stream_loop(p, ctx, box, run, m, **KW))
    assert [x.role for x in m] == ["user", "assistant", "tool", "tool", "tool"]
    assert [(x.tool_call_id, x.text) for x in m[2:]] == [
        ("c1", "result of read"), ("c2", L.CANCELLED_TEXT), ("c3", L.CANCELLED_TEXT)]
    logged = [e["data"] for e in events_of(run) if e["kind"] == "message" and e["data"]["role"] == "tool"]
    assert [d["tool_call_id"] for d in logged] == ["c1", "c2", "c3"]


async def test_closing_the_stream_after_a_tool_result_adds_no_duplicate(ctx, box, run):
    p = ApiFake([[ToolCall("c1", "read", {}), ToolCall("c2", "read", {}), Usage()]])
    m = msgs()
    stream = L.stream_loop(p, ctx, box, run, m, **KW)
    async for ev in stream:
        if isinstance(ev, ToolDone):
            break
    await stream.aclose()
    assert [(x.role, x.tool_call_id) for x in m[2:]] == [("tool", "c1"), ("tool", "c2")]
    assert m[3].text == L.CANCELLED_TEXT
