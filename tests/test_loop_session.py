"""Session API: open / send / set / close / resume / list / on_start hooks."""
import json
import sys
from pathlib import Path

import pytest

from lightmind import loop as L
from lightmind.model import Check, Error, Message, RateLimit, Rule, Run, Text, ToolCall, Usage
from lightmind.providers.base import Notice, ToolDone
from lightmind.providers.claude_sdk import NotLoggedIn
from lightmind.runs import RunDir, read_trusted, trusted_file

from test_loop_support import SdkFake, SessionHome, collect, default_settings, write

PY = Path(sys.executable).as_posix()          # hook commands run in bash: forward slashes

PERMISSIONS = """---
fallback: deny
allow: [read(**), "write({run}/**)", skill(*), tool(*), "bash(echo*)"]
ask: []
deny: []
locked:
  deny: ["read({secrets}/**/*.env)"]
modes:
  auto: {label: Auto, text: Work normally.}
  plan: {label: Plan, ask: ["bash(*)"], text: Plan only.}
---
# Rules
- Test rules.
"""


@pytest.fixture
def shome(tree):
    return SessionHome(tree)


@pytest.fixture
def fakes(monkeypatch):
    """Replace make_provider: one SdkFake per account, remembered in a dict."""
    made: dict[str, SdkFake] = {}

    def factory(home, account, dispatcher=None):
        p = made.setdefault(account, SdkFake([]))
        p.dispatcher = dispatcher
        return p
    monkeypatch.setattr(L, "make_provider", factory)
    return made


def script(fake: SdkFake, *turns):
    fake.script = list(turns)
    fake._i = 0
    return fake


def events_of(session):
    return [json.loads(l) for l in (session.run.path / "events.jsonl").read_text("utf-8").splitlines()]


# ----------------------------------------------------------------------- open
def test_open_session_uses_the_settings_defaults(shome, fakes):
    s = L.open_session(shome)
    i = s.info
    assert (i.server, i.model, i.account, i.mode, i.effort, i.ctx_limit) == (
        "laptop", "haiku", "abo", "auto", "low", 200_000)
    assert (i.ctx_used, i.cost_usd, i.status, i.agent, i.project) == (0, 0.0, "running", "", "")
    assert s.id == s.run.run.id and "-chat-" in s.id
    assert (s.run.path / "meta.json").is_file() and (s.run.path / "session.md").is_file()
    first = events_of(s)[0]
    assert first["kind"] == "session" and first["data"]["account"] == "abo"
    assert fakes["abo"].dispatcher is s.dispatcher                       # the adapter got the tools


def test_open_session_arguments_beat_agent_beats_defaults(shome, fakes):
    write(shome.path("agents") / "webdev" / shome.filename("agent"),
          "---\nprovider: abo2\nmodel: strong\neffort: high\nmode: plan\ncontext_limit: 100k\n---\nBody.\n")
    s = L.open_session(shome, agent="webdev")
    i = s.info
    assert (i.account, i.model, i.effort, i.mode, i.ctx_limit, i.agent) == ("abo2", "opus", "high", "plan", 100_000, "webdev")
    assert "-webdev-" in s.id
    s2 = L.open_session(shome, agent="webdev", account="main", model="cheap", effort="max", mode="auto", context="1m")
    j = s2.info
    assert (j.account, j.model, j.effort, j.mode, j.ctx_limit) == ("abo", "haiku", "max", "auto", 1_000_000)


def test_project_provider_is_used_when_nothing_else_sets_one(shome, fakes):
    write(shome.path("projects") / "p1" / shome.filename("project_memory"), "---\nprovider: abo2\n---\nmem\n")
    assert L.open_session(shome, project="p1").info.account == "abo2"
    assert L.open_session(shome, project="p1", account="abo").info.account == "abo"
    assert L.open_session(shome, project="p1").run.run.project == "p1"


def test_remote_servers_wait_for_the_next_step(shome, fakes):
    for server in ("laptop", "local", None):
        assert L.open_session(shome, server=server).info.server in ("laptop", "local")
    with pytest.raises(NotImplementedError, match="remote servers come in the next step"):
        L.open_session(shome, server="buildbox")


def test_bad_input_fails_before_a_run_folder_exists(shome, fakes):
    before = list(shome.path("runs").iterdir()) if shome.path("runs").is_dir() else []
    for kw, exc in (({"agent": "ghost"}, OSError), ({"mode": "wild"}, ValueError),
                    ({"project": "ghost"}, ValueError), ({"account": "ghost"}, ValueError),
                    ({"tools_on": ["nothing-like-this"]}, ValueError)):
        with pytest.raises(exc):
            L.open_session(shome, **kw)
    after = list(shome.path("runs").iterdir()) if shome.path("runs").is_dir() else []
    assert len(after) == len(before)


def test_secrets_become_session_rules(shome, fakes):
    from lightmind import rules
    s = L.open_session(shome, secrets=["stripe/main"])
    assert Rule("secret", "stripe/main", "allow", origin="session:open") in s.state.rules
    layers = rules.build_layers(shome, None, "auto", s.state, None)
    assert rules.decide(layers, Check("secret", "stripe/main"), s.dispatcher.ctx).effect == "allow"
    assert rules.decide(layers, Check("secret", "other/acct"), s.dispatcher.ctx).effect != "allow"


# ----------------------------------------------------------------------- send
async def test_send_streams_events_and_updates_the_status_line(shome, fakes):
    s = L.open_session(shome)
    fakes["abo"].context_tokens = 650
    script(fakes["abo"], [Text("hello"), Usage(100, 20, 500, 50)])
    evs = await collect(s.send("hi there"))
    assert evs == [Text("hello"), Usage(100, 20, 500, 50)]
    i = s.info
    assert i.ctx_used == 650 and i.cost_usd > 0 and i.cost_est and i.ctx_pct == pytest.approx(650 / 200_000)
    assert [(m.role, m.text) for m in s.messages] == [("user", "hi there"), ("assistant", "hello")]
    req = fakes["abo"].requests[0]
    assert req.messages[-1].text == "hi there" and req.model == "haiku" and req.effort == "low"
    assert "Where things are" in req.system or "Lightmind" in req.system        # master prompt is the prefix
    for want in ("mode: auto", "server: laptop", "account: abo", f"runs/{s.id}"):
        assert want in req.state
    assert req.tools == [] and req.max_turns is None                               # the SDK runs the tools
    meta = json.loads((s.run.path / "meta.json").read_text())
    assert meta["tokens"]["input"] == 100 and meta["account"] == "abo" and meta["provider"] == "claude-subscription"


async def test_the_prefix_is_byte_stable_across_turns(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), Usage()], [Text("b"), Usage()])
    await collect(s.send("one"))
    await collect(s.send("two"))
    r1, r2 = fakes["abo"].requests
    assert r1.system == r2.system and [m.text for m in r2.messages if m.role == "user"] == ["one", "two"]


async def test_send_refuses_empty_closed_and_parallel(shome, fakes):
    s = L.open_session(shome)
    with pytest.raises(ValueError, match="empty"):
        await collect(s.send("   "))
    script(fakes["abo"], [Text("a"), Usage()])
    gen = s.send("first")
    await gen.__anext__()                                                         # a turn is running
    with pytest.raises(RuntimeError, match="already running"):
        await collect(s.send("second"))
    await gen.aclose()
    s.close()
    with pytest.raises(RuntimeError, match="closed"):
        await collect(s.send("late"))


class Flaky(SdkFake):
    """Not logged in until `fail` is switched off."""
    fail = True

    async def turn(self, req):
        if self.fail:
            raise NotLoggedIn("abo")
        async for ev in super().turn(req):
            yield ev


async def test_a_provider_error_leaves_the_session_usable(shome, fakes):
    fakes["abo"] = p = Flaky([])
    s = L.open_session(shome)
    with pytest.raises(NotLoggedIn, match="lm login abo"):
        await collect(s.send("hello"))
    assert not s._busy and s.info.status == "running"
    p.fail = False
    script(p, [Text("fine now"), Usage()])
    assert [e for e in await collect(s.send("again")) if isinstance(e, Text)] == [Text("fine now")]
    assert any(e["kind"] == "error" for e in events_of(s))


async def test_error_events_pass_through_without_ending_the_session(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Error("overloaded", "busy"), Usage()])
    evs = await collect(s.send("x"))
    assert Error("overloaded", "busy") in evs and s.last_result.status == "failed"
    assert [m.role for m in s.messages] == ["user"]                               # nothing invented


async def test_provider_warnings_surface_once(shome, fakes):
    s = L.open_session(shome)
    fakes["abo"].warnings = ["account 'abo' has no config_dir: using the system default login"]
    script(fakes["abo"], [Text("a"), Usage()], [Text("b"), Usage()])
    first = await collect(s.send("one"))
    second = await collect(s.send("two"))
    assert [n.kind for n in first if isinstance(n, Notice)] == ["warning"]
    assert not [n for n in second if isinstance(n, Notice)]
    assert any(e["kind"] == "warning" for e in events_of(s))


# ------------------------------------------------------------------------ set
async def test_set_is_recorded_now_and_applied_at_the_next_turn(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), Usage()], [Text("b"), Usage()])
    await collect(s.send("one"))
    s.set(mode="plan", model="strong", effort="high")
    i = s.info
    assert (i.mode, i.model, i.effort) == ("plan", "opus", "high")                # status line follows at once
    assert s.dispatcher.mode == "auto"                                            # ... the dispatcher not yet
    await collect(s.send("two"))
    assert s.dispatcher.mode == "plan"
    req = fakes["abo"].requests[1]
    assert (req.model, req.effort) == ("opus", "high") and "Mode: plan" in req.system
    assert fakes["abo"].requests[0].system != req.system                           # mode text is part of the prefix
    assert json.loads((s.run.path / "meta.json").read_text())["mode"] == "plan"


def test_set_rejects_nonsense_without_changing_anything(shome):
    s = L.open_session(shome)                                   # the real adapter: nothing is spawned
    with pytest.raises(ValueError, match="unknown mode"):
        s.set(mode="wild")
    with pytest.raises(ValueError, match="unknown account"):
        s.set(account="ghost")
    assert (s.info.mode, s.info.account) == ("auto", "abo")


async def test_set_account_switches_provider_and_replays_history(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a1"), Usage()])
    await collect(s.send("q1"))
    s.set(account="abo2")
    assert s.info.account == "abo2" and "abo2" in fakes                          # adapter built now, used next turn
    script(fakes["abo2"], [Text("a2"), Usage()])
    await collect(s.send("q2"))
    sent = fakes["abo2"].requests[0].messages
    assert [(m.role, m.text) for m in sent] == [("user", "q1"), ("assistant", "a1"), ("user", "q2")]
    assert len(fakes["abo"].requests) == 1 and fakes["abo2"].dispatcher is s.dispatcher
    assert json.loads((s.run.path / "meta.json").read_text())["account"] == "abo2"


async def test_a_tier_is_resolved_again_for_the_new_account(shome, fakes):
    s = L.open_session(shome, model="cheap")
    assert s.info.model == "haiku"
    s.set(account="local")                                       # same tier, other family row
    assert (s.info.account, s.info.model) == ("local", "qwen3-32b")
    s.set(account="abo2", model="strong")
    assert (s.info.account, s.info.model) == ("abo2", "opus")


async def test_set_tools_rebuilds_the_dispatcher_and_keeps_counters(shome, fakes):
    s = L.open_session(shome)
    assert "pdf" in {t["name"] for t in s.dispatcher.schemas()}
    s.dispatcher.n = 7
    s.dispatcher.masker.add("svc/main", "KEY", "abcdef123456")
    s.set(tools_off=["pdf"])
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("x"))
    assert "pdf" not in {t["name"] for t in s.dispatcher.schemas()}
    assert s.dispatcher.n == 7 and s.dispatcher.masker.mask("abcdef123456") != "abcdef123456"
    assert fakes["abo"].dispatcher is s.dispatcher
    s.set(tools_on=["pdf"])
    script(fakes["abo"], [Text("b"), Usage()])
    await collect(s.send("y"))
    assert "pdf" in {t["name"] for t in s.dispatcher.schemas()}
    assert [r.name for r in s.state.tools_added] == ["pdf"] and s.state.tools_removed == []
    with pytest.raises(ValueError):
        s.set(tools_on=["no-such-thing"])


async def test_asker_survives_a_dispatcher_rebuild(shome, fakes):
    async def ask(question, check):
        return "yes"
    s = L.open_session(shome, asker=ask)
    assert s.dispatcher.asker is ask
    s.set(tools_off=["pdf"])
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("x"))
    assert s.dispatcher.asker is ask
    s.asker = None
    assert s.dispatcher.asker is None


# ---------------------------------------------------------------- on_start / end
def test_on_start_shows_and_keeps_context_lines(shome, fakes):
    shome.settings = default_settings(on_start=[
        {"run": "echo hello-from-hook", "show": True, "context": True},
        {"run": "echo quiet-note", "show": False, "context": True},
        {"run": "echo only-shown", "show": True},
        "echo plain-string-entry"])
    s = L.open_session(shome)
    assert L.run_on_start(shome, s) == ["hello-from-hook", "only-shown"]
    assert s.start_context == ["hello-from-hook", "quiet-note"]
    assert [e["data"]["cmd"] for e in events_of(s) if e["kind"] == "on_start"] == [
        "echo hello-from-hook", "echo quiet-note", "echo only-shown", "echo plain-string-entry"]


async def test_start_context_reaches_the_volatile_block_only(shome, fakes):
    shome.settings = default_settings(on_start=[{"run": "echo vault: 12 notes", "context": True}])
    s = L.open_session(shome)
    L.run_on_start(shome, s)
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("x"))
    req = fakes["abo"].requests[0]
    assert "start: vault: 12 notes" in req.state and "12 notes" not in req.system


def test_hook_placeholders_come_from_the_paths_map(shome, fakes):
    shome.settings = default_settings(on_start=[
        {"run": "echo {runs} {state} {nothing}", "show": True},
        {"run": "echo {print x}", "show": True}])
    s = L.open_session(shome)
    out = L.run_on_start(shome, s)
    assert out[0] == f"{shome.rel('runs')} {shome.rel('state')} {{nothing}}"        # unknown keys stay as written
    assert out[1] == "{print x}"                                                    # shell braces are not placeholders


def test_hooks_run_in_the_tree_root_with_lm_env(shome, fakes):
    shome.settings = default_settings(on_start=[
        {"run": f'"{PY}" -c "import os;print(os.getcwd());print(os.environ[\'LM_HOME\']);print(os.environ[\'LM_RUN\'])"',
         "show": True}])
    s = L.open_session(shome)
    cwd, home_, run_ = L.run_on_start(shome, s)
    assert cwd == str(shome.root) and home_ == str(shome.root) and run_ == str(s.run.path)


def test_hook_output_is_masked(shome, fakes):
    shome.settings = default_settings(on_start=[{"run": "echo token=abcdef123456", "show": True}])
    s = L.open_session(shome)
    s.dispatcher.masker.add("svc/main", "TOKEN", "abcdef123456")
    L.run_on_start(shome, s)
    logged = [e for e in events_of(s) if e["kind"] == "on_start"][0]["data"]["output"]
    assert "abcdef123456" not in logged and "<secret:svc/main:TOKEN>" in logged


# ------------------------------------------- hooks: Claude Code compatible contract
HOOK = r"""
import json, os, sys
data = json.load(sys.stdin)
cap = os.environ.get("HOOK_CAPTURE")
if cap:
    os.makedirs(os.path.dirname(cap), exist_ok=True)
    with open(cap, "w", encoding="utf-8") as f:
        json.dump({"stdin": data, "vault": os.environ.get("CLAUDE_MEM_VAULT"),
                   "project": os.environ.get("CLAUDE_PROJECT_DIR"), "cwd": os.getcwd(),
                   "lm_run": os.environ.get("LM_RUN"), "lm_home": os.environ.get("LM_HOME")}, f)
mode, event = os.environ.get("HOOK_MODE", "plain"), data["hook_event_name"]
secret = os.environ.get("SECRET_VALUE", "")
if mode == "plain":
    print("plain output for " + event)
elif mode == "json":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": "ctx from json"},
                      "systemMessage": "heads up"}))
elif mode == "jsonobj":
    print('{"decision": "approve"}')
elif mode == "number":
    print(42)
elif mode == "fail":
    print("stderr text", file=sys.stderr)
    sys.exit(2)
elif mode == "big":
    print("x" * 20000)
elif mode == "echo-secret":
    print("the key is " + secret)
elif mode == "json-secret":
    print(json.dumps({"hookSpecificOutput": {"additionalContext": "token " + secret}, "systemMessage": "sys " + secret}))
elif mode == "json-escaped-secret":
    print(json.dumps({"hookSpecificOutput": {"additionalContext": "tricky " + secret}}))
elif mode == "env-dump":
    print("MY_SERVICE_TOKEN=" + os.environ.get("MY_SERVICE_TOKEN", "-"))
elif mode == "fail-secret":
    print("boom " + secret, file=sys.stderr)
    sys.exit(1)
elif mode == "marker":
    open(os.environ["HOOK_MARKER"], "w").write("ran")
"""


@pytest.fixture
def hook(tmp_path):
    """entry(mode, **keys) -> a settings entry that runs the tiny fake hook script above."""
    script = tmp_path / "hook.py"
    script.write_text(HOOK, "utf-8")

    def entry(mode="plain", env=None, **keys):
        return {"run": f'"{PY}" "{script.as_posix()}"', "env": {"HOOK_MODE": mode, **(env or {})}, **keys}
    return entry


def captured(shome, name):
    return json.loads((shome.path("state") / name).read_text("utf-8"))


async def test_every_hook_gets_the_claude_code_json_and_env(shome, fakes, hook):
    shome.settings = default_settings(
        on_start=[hook(env={"HOOK_CAPTURE": "{state}/cap-start.json", "CLAUDE_MEM_VAULT": "{vault}"})],
        on_prompt=[hook(env={"HOOK_CAPTURE": "{state}/cap-prompt.json"})],
        on_end=[hook(env={"HOOK_CAPTURE": "{state}/cap-end.json"})])
    s = L.open_session(shome)
    L.run_on_start(shome, s)
    start = captured(shome, "cap-start.json")
    assert start["stdin"] == {"hook_event_name": "SessionStart", "session_id": s.id, "cwd": str(shome.root),
                              "transcript_path": str(s.run.path / "events.jsonl"), "source": "startup"}
    assert start["vault"] == str(shome.path("vault"))                      # env values get absolute paths
    assert start["project"] == str(shome.root) and start["cwd"] == str(shome.root)
    assert start["lm_run"] == str(s.run.path) and start["lm_home"] == str(shome.root)
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("my question"))
    prompt = captured(shome, "cap-prompt.json")["stdin"]
    assert prompt["hook_event_name"] == "UserPromptSubmit" and prompt["prompt"] == "my question"
    assert prompt["session_id"] == s.id and "source" not in prompt
    write(s.run.path / "log.md", "notes\n")
    s.close()
    end = captured(shome, "cap-end.json")["stdin"]
    assert end["hook_event_name"] == "SessionEnd" and end["transcript_path"] == str(s.run.path / "log.md")
    assert end["reason"] == "other"
    fakes.clear()
    r = L.resume_session(shome, s.id)
    L.run_on_start(shome, r)
    assert captured(shome, "cap-start.json")["stdin"]["source"] == "resume"


async def test_on_prompt_context_goes_into_that_message_only(shome, fakes, hook):
    shome.settings = default_settings(on_prompt=[hook(context=True, show=True)])
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), Usage()], [Text("b"), Usage()])
    evs = await collect(s.send("question"))
    block = '\n\n<context source="on_prompt">\nplain output for UserPromptSubmit\n</context>'
    req = fakes["abo"].requests[0]
    assert req.messages[-1].text == "question" + block                     # appended to the user message ...
    assert "plain output" not in req.system and "plain output" not in req.state   # ... never to the prefix
    assert Notice("hook", "plain output for UserPromptSubmit") in evs
    await collect(s.send("again"))
    assert fakes["abo"].requests[1].messages[-1].text == "again" + block   # runs before EVERY message
    assert [e["data"]["rc"] for e in events_of(s) if e["kind"] == "on_prompt"] == [0, 0]
    logged = [e["data"]["text"] for e in events_of(s) if e["kind"] == "message" and e["data"]["role"] == "user"]
    assert logged == ["question" + block, "again" + block]                 # the record is what the model saw


async def test_hooks_without_context_leave_the_message_alone(shome, fakes, hook):
    shome.settings = default_settings(on_prompt=[hook(show=True), hook("jsonobj", context=True)])
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("question"))
    assert fakes["abo"].requests[0].messages[-1].text == "question"


def test_json_output_gives_additional_context_and_system_message(shome, fakes, hook):
    shome.settings = default_settings(on_start=[hook("json", context=True)])
    s = L.open_session(shome)
    assert L.run_on_start(shome, s) == ["heads up"]                        # systemMessage is shown even without `show`
    assert s.start_context == ["ctx from json"]                            # the JSON itself is not the context


def test_plain_text_that_parses_as_json_stays_plain(shome, fakes, hook):
    shome.settings = default_settings(on_start=[hook("number", context=True, show=True),
                                                hook("jsonobj", context=True, show=True)])
    s = L.open_session(shome)
    assert L.run_on_start(shome, s) == ["42"]                              # 42 is text; an object without the keys is nothing
    assert s.start_context == ["42"]


def test_a_failing_hook_is_a_warning_even_when_hidden(shome, fakes, hook):
    shome.settings = default_settings(on_start=[hook("fail"), hook("fail", show=True, context=True)])
    s = L.open_session(shome)
    lines = L.run_on_start(shome, s)
    assert len(lines) == 2 and all(l.startswith("warning: hook `") and "exited 2: stderr text" in l for l in lines)
    assert s.start_context == []


async def test_a_failing_prompt_hook_never_stops_the_message(shome, fakes, hook):
    shome.settings = default_settings(on_prompt=[hook("fail"), hook(context=True)])
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), Usage()])
    evs = await collect(s.send("question"))
    warn = [e for e in evs if isinstance(e, Notice) and e.kind == "warning"]
    assert len(warn) == 1 and "exited 2" in warn[0].text
    assert "plain output for UserPromptSubmit" in fakes["abo"].requests[0].messages[-1].text   # the good hook still ran
    assert [e for e in evs if isinstance(e, Text)] == [Text("a")]


def test_a_missing_hook_program_is_a_warning(shome, fakes):
    shome.settings = default_settings(on_start=["definitely-not-a-program-xyz"])
    s = L.open_session(shome)
    lines = L.run_on_start(shome, s)
    assert len(lines) == 1 and lines[0].startswith("warning: hook `definitely-not-a-program-xyz` exited")


def test_hooks_use_the_configured_shell_and_a_missing_one_is_a_warning(shome, fakes):
    shome.settings = default_settings(on_start=[{"run": "echo hi", "show": True}], shell="no-such-shell-xyz")
    s = L.open_session(shome)
    lines = L.run_on_start(shome, s)
    assert len(lines) == 1 and lines[0].startswith("warning: hook `echo hi` could not start:")
    assert "no-such-shell-xyz" in lines[0]                                 # no fallback to cmd.exe
    shome.settings = default_settings(on_start=[{"run": "echo hi", "show": True}])
    assert L.run_on_start(shome, s) == ["hi"]


def test_hook_timeout_defaults_to_15_seconds_and_can_be_set_per_entry(shome, fakes):
    assert L.HOOK_TIMEOUT == 15
    slow = f'"{PY}" -c "import time; time.sleep(30)"'
    shome.settings = default_settings(on_start=[{"run": slow, "timeout": 0.5}])
    s = L.open_session(shome)
    lines = L.run_on_start(shome, s)
    assert len(lines) == 1 and "timed out after 0.5s" in lines[0]
    assert [e["data"]["rc"] for e in events_of(s) if e["kind"] == "on_start"] == [-1]


def test_hook_context_is_cut_at_a_cap(shome, fakes, hook):
    shome.settings = default_settings(on_start=[hook("big", context=True), hook("big", context=True, max=100)])
    s = L.open_session(shome)
    L.run_on_start(shome, s)
    assert len(s.start_context[0]) == L.HOOK_CONTEXT_MAX + len(" [...cut]")
    assert len(s.start_context[1]) == 100 + len(" [...cut]") and s.start_context[1].endswith("[...cut]")


async def test_a_secret_never_reaches_the_model_through_a_hook(shome, fakes, hook):
    """The secret was never injected into any tool in this session: the dispatcher's masker has
    not seen it.  The hook output is masked against every secret file of the tree anyway."""
    value, token = "abcdef123456", "sk-fake-oauth-000111"
    write(shome.path("secrets") / "svc" / "main.env", f"# --- END HEADER ---\nKEY={value}\n")
    write(shome.path("secrets") / "claude-abo" / "abo.env", f"CLAUDE_CODE_OAUTH_TOKEN={token}\n")
    shome.settings = default_settings(
        on_start=[hook("echo-secret", context=True, show=True, env={"SECRET_VALUE": value}),
                  hook("json-secret", context=True, env={"SECRET_VALUE": token}),
                  hook("fail-secret", env={"SECRET_VALUE": value})],
        on_prompt=[hook("echo-secret", context=True, show=True, env={"SECRET_VALUE": value})])
    s = L.open_session(shome)
    assert value not in s.dispatcher.masker.mask(value)                  # the session knows every secret of the tree from the start
    lines = L.run_on_start(shome, s)
    assert lines[:2] == ["the key is <secret:svc/main:KEY>", "sys <secret:claude-abo/abo:CLAUDE_CODE_OAUTH_TOKEN>"]
    assert len(lines) == 3 and lines[2].startswith("warning: hook `") and "boom <secret:svc/main:KEY>" in lines[2]
    assert s.start_context == ["the key is <secret:svc/main:KEY>",
                               "token <secret:claude-abo/abo:CLAUDE_CODE_OAUTH_TOKEN>"]
    script(fakes["abo"], [Text("a"), Usage()])
    evs = await collect(s.send("question"))
    assert Notice("hook", "the key is <secret:svc/main:KEY>") in evs
    req = fakes["abo"].requests[0]
    sent = json.dumps([req.system, req.state, [m.text for m in req.messages]])
    everything = sent + (s.run.path / "events.jsonl").read_text("utf-8") + json.dumps([str(l) for l in lines])
    assert value not in everything and token not in everything
    assert "<secret:svc/main:KEY>" in req.messages[-1].text


async def test_masking_reads_json_output_after_parsing(shome, fakes, hook):
    """A value with a quote and a backslash is written escaped in JSON; the plain value is what
    the masker knows, so the output must be masked after parsing, not before."""
    value = 'ab"cd\\ef123'
    write(shome.path("secrets") / "svc" / "main.env", "KEY=" + value + "\n")
    shome.settings = default_settings(on_prompt=[hook("json-escaped-secret", context=True, env={"SECRET_VALUE": value})])
    s = L.open_session(shome)
    assert value not in s.dispatcher.masker.mask(value)
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("question"))
    sent = fakes["abo"].requests[0].messages[-1].text
    assert "tricky <secret:svc/main:KEY>" in sent and "ab" + chr(34) + "cd" not in sent


def test_masking_cuts_after_masking_not_before(shome, fakes, hook):
    value = "SECRET-" + "x" * 40
    write(shome.path("secrets") / "svc" / "main.env", "KEY=" + value + "\n")
    shome.settings = default_settings(on_start=[hook("echo-secret", context=True, max=20, env={"SECRET_VALUE": value})])
    s = L.open_session(shome)
    L.run_on_start(shome, s)
    assert s.start_context == ["the key is <secret:s [...cut]"]
    assert "SECRET-" not in s.start_context[0]


def test_credentials_of_the_process_environment_are_masked_too(shome, fakes, hook, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-1234567890")
    shome.settings = default_settings(on_start=[hook("env-dump", context=True, show=True)])
    s = L.open_session(shome)
    assert L.run_on_start(shome, s) == ["MY_SERVICE_TOKEN=<secret:env:MY_SERVICE_TOKEN>"]
    assert s.start_context == ["MY_SERVICE_TOKEN=<secret:env:MY_SERVICE_TOKEN>"]


def test_a_secret_in_the_command_line_or_the_veto_reason_is_masked(shome, fakes):
    value = "cmd-secret-9876"
    write(shome.path("secrets") / "svc" / "main.env", "KEY=" + value + "\n")
    shome.settings = default_settings(on_start=["echo " + value])
    s = L.open_session(shome, hook_gate=lambda event, command: "no: " + value)
    lines = L.run_on_start(shome, s)
    assert len(lines) == 1 and value not in lines[0] and "<secret:svc/main:KEY>" in lines[0]
    assert value not in (s.run.path / "events.jsonl").read_text("utf-8")


def test_bad_numbers_in_a_hook_entry_fall_back_to_the_defaults(shome, fakes, hook):
    shome.settings = default_settings(on_start=[hook(context=True, timeout="soon", max="lots"),
                                                {**hook(context=True), "env": ["not", "a", "map"]}])
    s = L.open_session(shome)
    assert L.run_on_start(shome, s) == []
    assert s.start_context == ["plain output for SessionStart"] * 2


def test_hook_gate_vetoes_and_fails_closed(shome, fakes, hook, tmp_path):
    marker = tmp_path / "ran.txt"
    shome.settings = default_settings(on_start=[hook("marker", env={"HOOK_MARKER": str(marker)})])
    seen = []

    def deny(event, command):
        seen.append((event, command))
        return "policy says no"
    s = L.open_session(shome, hook_gate=deny)
    lines = L.run_on_start(shome, s)
    assert len(lines) == 1 and lines[0].endswith("skipped: policy says no") and not marker.exists()
    assert seen[0][0] == "on_start" and "hook.py" in seen[0][1]
    assert [e["data"].get("skipped") for e in events_of(s) if e["kind"] == "on_start"] == ["policy says no"]

    def broken(event, command):
        raise RuntimeError("gate down")
    s.hook_gate = broken
    assert "skipped: gate error: RuntimeError: gate down" in L.run_on_start(shome, s)[0] and not marker.exists()

    s.hook_gate = lambda event, command: None                              # no reason: the hook runs
    assert L.run_on_start(shome, s) == [] and marker.read_text() == "ran"
    s.hook_gate = None
    marker.unlink()
    L.run_on_start(shome, s)
    assert marker.exists()                                                 # no gate: trusted configuration runs


async def test_a_vetoed_prompt_hook_adds_no_context_but_the_message_goes_out(shome, fakes, hook):
    shome.settings = default_settings(on_prompt=[hook(context=True)])
    s = L.open_session(shome, hook_gate=lambda event, command: "blocked")
    script(fakes["abo"], [Text("a"), Usage()])
    evs = await collect(s.send("question"))
    assert fakes["abo"].requests[0].messages[-1].text == "question"
    assert any(isinstance(e, Notice) and e.kind == "warning" and "skipped: blocked" in e.text for e in evs)


def test_resume_passes_the_gate_on(shome, fakes, hook):
    shome.settings = default_settings(on_start=[hook()])
    s = L.open_session(shome)
    s.close()
    gate = lambda event, command: "no"                                     # noqa: E731
    assert L.resume_session(shome, s.id, hook_gate=gate).hook_gate is gate


def test_close_runs_on_end_and_finishes_the_run(shome, fakes):
    shome.settings = default_settings(on_end=[{"run": "echo bye", "show": True}, {"run": "echo silent"}])
    s = L.open_session(shome)
    assert s.close() == ["bye"]
    assert s.close() == []                                                         # idempotent
    meta = json.loads((s.run.path / "meta.json").read_text())
    assert meta["status"] == "done" and meta["end"] and meta["exit_reason"] == "closed"
    assert "<!-- lm:end status=done -->" in (s.run.path / "log.md").read_text()
    s2 = L.open_session(shome)
    s2.close("paused", "detached")
    assert json.loads((s2.run.path / "meta.json").read_text())["status"] == "paused"


# --------------------------------------------------------------- limits, handover
async def test_a_limit_warning_switches_the_account_for_the_next_turn(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a"), RateLimit("five_hour", 0.95, 1), Usage()])
    evs = await collect(s.send("x"))
    note = next(e for e in evs if isinstance(e, Notice) and e.kind == "switch")
    assert "abo -> abo2" in note.text and s.info.account == "abo2"
    assert s.info.limits == {"five_hour": 0.95}
    script(fakes["abo2"], [Text("b"), Usage()])
    await collect(s.send("y"))
    assert len(fakes["abo2"].requests) == 1


async def test_a_hard_limit_error_also_names_the_next_account(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Error("rate_limit", "429"), Usage()])
    evs = await collect(s.send("x"))
    assert Error("rate_limit", "429") in evs and s.info.account == "abo2"
    assert s.last_result.status == "paused" and s.last_result.exit_reason == "rate_limit"


def handover_turns(session, fake, answer="answer", handover="## Handover\ndone: a\nopen: b\nnext: c"):
    """First turn answers, second (the handover turn) writes log.md like the model would."""
    def writes_log(req):
        write(session.run.path / "log.md", handover + "\n")
        return [Text("handover written"), Usage()]
    return script(fake, [Text(answer), Usage()], writes_log)


async def test_a_nearly_full_context_ends_in_a_fresh_conversation_with_the_handover(shome, fakes):
    s = L.open_session(shome, context=1000)
    fake = fakes["abo"]
    fake.context_tokens, fake.session_id = 900, "sdk-old"
    handover_turns(s, fake)
    evs = await collect(s.send("long task"))
    assert [n.kind for n in evs if isinstance(n, Notice)] == ["handover", "handover"]   # asked, then restarted
    assert len(fake.requests) == 2 and "## Handover" in fake.requests[1].messages[-1].text
    assert s.messages == [] and fake.session_id == "" and s.info.ctx_used == 0
    assert any(e["kind"] == "reset" for e in events_of(s))
    fake.context_tokens = 100
    script(fake, [Text("on we go"), Usage()])
    await collect(s.send("continue please"))
    first = fake.requests[-1].messages
    assert len(first) == 1 and "Continue a previous run. Its handover:" in first[0].text
    assert "done: a" in first[0].text and "next: c" in first[0].text and first[0].text.endswith("continue please")
    assert "long task" not in first[0].text                                        # the old conversation is gone


async def test_without_a_written_log_the_last_answer_is_the_handover(shome, fakes):
    s = L.open_session(shome, context=1000)
    fake = fakes["abo"]
    fake.context_tokens = 900
    script(fake, [Text("answer"), Usage()], [Text("done: x\nnext: y"), Usage()])
    await collect(s.send("t"))
    fake.context_tokens = 10
    script(fake, [Text("k"), Usage()])
    await collect(s.send("go"))
    assert "next: y" in fake.requests[-1].messages[0].text


async def test_the_pending_handover_survives_close_and_resume(shome, fakes):
    s = L.open_session(shome, context=1000)
    fakes["abo"].context_tokens = 900
    handover_turns(s, fakes["abo"])
    await collect(s.send("t"))
    s.close()
    fakes.clear()
    r = L.resume_session(shome, s.id)
    script(fakes["abo"], [Text("hi"), Usage()])
    await collect(r.send("welcome back"))
    assert "next: c" in fakes["abo"].requests[0].messages[0].text and len(fakes["abo"].requests[0].messages) == 1


# ------------------------------------------------------------- resume and list
async def test_resume_restores_settings_conversation_and_sdk_session(shome, fakes):
    s = L.open_session(shome, model="strong", effort="high", tools_off=["pdf"])
    fakes["abo"].session_id = "sdk-9"
    script(fakes["abo"], [Text("a1"), Usage(10, 2)])
    await collect(s.send("hello world"))
    s.set(mode="plan")
    write(s.run.path / "tool-out" / "3-bash.txt", "x")
    s.close()
    assert json.loads((s.run.path / "meta.json").read_text())["status"] == "done"
    fakes.clear()
    r = L.resume_session(shome, s.id[:24])                                          # a prefix is enough
    i = r.info
    assert (i.id, i.model, i.effort, i.mode, i.account, i.status) == (s.id, "opus", "high", "plan", "abo", "running")
    assert r.provider.session_id == "sdk-9"                                         # the SDK conversation continues
    assert [(m.role, m.text) for m in r.messages] == [("user", "hello world"), ("assistant", "a1")]
    assert r.state.tools_removed == ["pdf"] and "pdf" not in {t["name"] for t in r.dispatcher.schemas()}
    assert r.dispatcher.n == 3                                                      # tool-out numbering goes on
    meta = json.loads((r.run.path / "meta.json").read_text())
    assert meta["status"] == "running" and meta["end"] == "" and meta["tokens"]["input"] == 10
    script(fakes["abo"], [Text("a2"), Usage()])
    await collect(r.send("and now?"))
    assert [m.text for m in fakes["abo"].requests[0].messages if m.role == "user"] == ["hello world", "and now?"]
    assert "Mode: plan" in fakes["abo"].requests[0].system


async def test_resume_keeps_always_this_session_grants(shome, fakes):
    write(shome.path("permissions"), PERMISSIONS)
    s = L.open_session(shome, mode="plan")
    s.state.rules.append(Rule("bash", "echo hi", "allow", origin="session:always"))
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("x"))
    s.close()
    fakes.clear()
    r = L.resume_session(shome, s.id)
    assert [x.text() for x in r.state.rules] == ["bash(echo hi)"]


def test_resume_errors(shome, fakes):
    with pytest.raises(FileNotFoundError):
        L.resume_session(shome, "no-such-run")
    one_shot = RunDir(shome, Run(id="20260101-000000-plain-aaaa", agent="plain"))
    one_shot.event("text", Text("hi"))
    with pytest.raises(ValueError, match="not a chat session"):
        L.resume_session(shome, one_shot.run.id)


def test_list_sessions_finds_only_chats_newest_first(shome, fakes, monkeypatch):
    ids = iter(["20260101-100000-chat-aaaa", "20260101-110000-chat-bbbb"])
    monkeypatch.setattr(L, "new_run_id", lambda agent, now=None: next(ids))
    a = L.open_session(shome)
    a.messages.append(Message("user", "Please   build\nthe landing page for the new customer site, with a long tail " * 2))
    L._log_message(a.run, a.messages[0])
    b = L.open_session(shome, model="strong")
    RunDir(shome, Run(id="20260101-120000-plain-cccc", agent="plain")).event("text", Text("not a chat"))
    rows = L.list_sessions(shome)
    assert [r.id for r in rows] == [b.id, a.id]
    assert rows[1].title.startswith("Please build the landing page") and len(rows[1].title) <= 60
    assert (rows[0].model, rows[0].account, rows[0].status, rows[0].server) == ("opus", "abo", "running", "laptop")
    assert rows[0].last_active and rows[0].cost_usd == 0.0
    assert [r.id for r in L.list_sessions(shome, limit=1)] == [b.id]
    b.close()
    assert L.list_sessions(shome)[0].status == "done"


# ------------------------------------------- end to end through the real dispatcher
class ToolCallingFake(SdkFake):
    """Does what the SDK does: for each requested call, run it through the dispatcher (the MCP
    handler's job) and report call + result."""

    def __init__(self, calls, **kw) -> None:
        super().__init__([], **kw)
        self.calls = calls
        self.results = []

    async def turn(self, req):
        self.requests.append(req)
        for i, (name, args) in enumerate(self.calls):
            yield ToolCall(f"t{i}", name, args)
            r = await self.dispatcher.call(name, args)
            self.results.append(r)
            yield ToolDone(f"t{i}", name, r.ok, r.text[:200])
        yield Text("finished")
        yield Usage(10, 5)


async def test_tool_calls_pass_the_real_dispatcher_and_rules(shome, monkeypatch):
    write(shome.path("permissions"), PERMISSIONS)
    write(shome.root / "notes.txt", "alpha\nbeta\n")
    write(shome.path("secrets") / "svc" / "main.env", "KEY=abcdef123456\n")
    fake = ToolCallingFake([("read", {"path": "notes.txt"}), ("read", {"path": "secrets/svc/main.env"}),
                            ("write", {"path": "elsewhere.txt", "content": "x"})])
    monkeypatch.setattr(L, "make_provider", lambda h, a, d=None: (setattr(fake, "dispatcher", d), fake)[1])
    s = L.open_session(shome)
    evs = await collect(s.send("go"))
    done = [e for e in evs if isinstance(e, ToolDone)]
    assert [d.ok for d in done] == [True, False, False]
    assert "1\talpha" in fake.results[0].text and fake.results[1].denied_by and fake.results[2].denied_by
    tool_events = [e["data"] for e in events_of(s) if e["kind"] == "tool"]
    assert [t["ok"] for t in tool_events] == [True, False, False] and tool_events[1]["denied_by"]
    assert not (shome.root / "elsewhere.txt").exists()                               # denied: nothing written
    assert "abcdef123456" not in json.dumps(events_of(s))


async def test_asks_are_answered_through_the_session_asker(shome, monkeypatch):
    write(shome.path("permissions"), PERMISSIONS)
    questions = []

    async def ask(question, check):
        questions.append(check)
        return "always"
    fake = ToolCallingFake([("bash", {"command": "echo one"}), ("bash", {"command": "echo one"})])
    monkeypatch.setattr(L, "make_provider", lambda h, a, d=None: (setattr(fake, "dispatcher", d), fake)[1])
    s = L.open_session(shome, mode="plan", asker=ask)                                 # plan: bash asks
    await collect(s.send("run it"))
    assert questions == [Check("bash", "echo one")]                                  # asked once, then "always"
    assert [r.ok for r in fake.results] == [True, True] and "one" in fake.results[0].text
    assert [x.text() for x in s.state.rules] == ["bash(echo one)"]
    rec = read_trusted(shome, s.id)
    assert rec["state"]["rules"][0]["pattern"] == "echo one" and events_of(s)[-1]["kind"] == "session"   # persisted


async def test_no_asker_means_ask_becomes_deny(shome, monkeypatch):
    write(shome.path("permissions"), PERMISSIONS)
    fake = ToolCallingFake([("bash", {"command": "echo one"})])
    monkeypatch.setattr(L, "make_provider", lambda h, a, d=None: (setattr(fake, "dispatcher", d), fake)[1])
    s = L.open_session(shome, mode="plan")
    await collect(s.send("run it"))
    assert fake.results[0].ok is False and "nobody to ask" in fake.results[0].denied_by.reason


# ------------------------------------------- resume does not trust the run folder
def forge(session, **data):
    """What an agent that may write the run folder can do: append a newer `session` event of its own."""
    with (session.run.path / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-10-20T12:00:00+02:00", "kind": "session", "data": data}) + "\n")


async def closed_chat(shome, fakes, **kw):
    s = L.open_session(shome, **kw)
    script(fakes["abo"], [Text("a"), Usage()])
    await collect(s.send("hello"))
    s.close()
    fakes.clear()
    return s


async def test_resume_reads_grants_and_settings_from_the_trusted_record_not_the_run_folder(shome, fakes):
    for name in ("webdev", "boss"):
        write(shome.path("agents") / name / shome.filename("agent"), f"---\nname: {name}\n---\nBody.\n")
    s = await closed_chat(shome, fakes, mode="plan", agent="webdev", secrets=["svc/main"])
    forge(s, sdk_session_id="other", server="laptop", agent="boss", project="", account="abo2", model="opus",
          model_req="opus", effort="max", context=1, mode="bypass", ctx_used=0, handover="",
          state={"mode": "bypass", "rules": [{"tool": "secret", "pattern": "*", "effect": "allow", "origin": "session:open"}]})
    meta = json.loads((s.run.path / "meta.json").read_text())
    (s.run.path / "meta.json").write_text(json.dumps({**meta, "id": "20200101-000000-chat-ffff", "mode": "bypass"}))
    r = L.resume_session(shome, s.id)
    assert (r.id, r.mode, r.account, r.agent.name, r.effort) == (s.id, "plan", "abo", "webdev", "medium")
    assert [x.text() for x in r.state.rules] == ["secret(svc/main)"] and r.state.mode == "plan"
    assert read_trusted(shome, s.id)["mode"] == "plan" and trusted_file(shome, s.id).parent.name == "sessions"


async def test_resume_needs_the_trusted_record(shome, fakes):
    s = await closed_chat(shome, fakes)
    trusted_file(shome, s.id).unlink()
    with pytest.raises(ValueError, match="cannot be resumed"):
        L.resume_session(shome, s.id)


async def test_resume_checks_agent_project_mode_and_account_again(shome, fakes):
    write(shome.path("agents") / "webdev" / shome.filename("agent"), "---\nname: webdev\n---\nBody.\n")
    (shome.path("projects") / "p1").mkdir(parents=True, exist_ok=True)
    s = await closed_chat(shome, fakes, agent="webdev", project="p1")
    good = read_trusted(shome, s.id)
    for key, bad, msg in (("agent", "ghost", "agent"), ("project", "../x", "project"), ("mode", "wild", "unknown mode"),
                          ("account", "ghost", "unknown account")):
        trusted_file(shome, s.id).write_text(json.dumps({**good, key: bad}), "utf-8")
        with pytest.raises(ValueError, match=msg):
            L.resume_session(shome, s.id)
    trusted_file(shome, s.id).write_text(json.dumps(good), "utf-8")
    assert L.resume_session(shome, s.id).project == "p1"


async def test_resume_drops_session_rules_of_unknown_origin(shome, fakes):
    s = await closed_chat(shome, fakes)
    rec = read_trusted(shome, s.id)
    rule = lambda pat, origin: {"tool": "bash", "pattern": pat, "effect": "allow", "until": None, "origin": origin}
    rec["state"]["rules"] = [rule("a", "session:open"), rule("b", "session:always"), rule("c", "session:cli"),
                             rule("d", ""), rule("e", "permissions.md:3"), {"tool": "bash", "pattern": "f"}]
    trusted_file(shome, s.id).write_text(json.dumps(rec), "utf-8")
    assert [x.pattern for x in L.resume_session(shome, s.id).state.rules] == ["a", "b", "c"]


async def test_rules_given_to_open_session_survive_a_resume(shome, fakes):
    from lightmind.rules import parse_rule
    s = L.open_session(shome, rules=[parse_rule("bash(git status*)", "allow", origin="session:cli"),
                                     parse_rule("web(*)", "deny", origin="session:cli")])
    assert [(x.text(), x.effect) for x in s.state.rules] == [("bash(git status*)", "allow"), ("web(*)", "deny")]
    s.close()
    assert [x.text() for x in L.resume_session(shome, s.id).state.rules] == ["bash(git status*)", "web(*)"]


def test_the_state_block_has_the_time_once(shome, fakes):
    s = L.open_session(shome)
    s._sync()
    assert "time: " in s.ctx.state and "date:" not in s.ctx.state


def test_hooks_start_from_the_runtime_env_and_log_parsed_masked_text(shome, fakes, hook):
    import os
    value = 'ab"cd\\ef123'
    write(shome.path("secrets") / "svc" / "main.env", "KEY=" + value + "\n")
    shome.settings = default_settings(on_start=[
        {"run": f'"{PY}" -c "import os;print(os.environ[\'UV_CACHE_DIR\']);print(os.environ[\'PATH\'].split(os.pathsep)[-1])"',
         "show": True},
        hook("json-escaped-secret", context=True, env={"SECRET_VALUE": value})])
    s = L.open_session(shome)
    lines = L.run_on_start(shome, s)
    assert lines == [str(shome.path("cache")), str(shome.path("bin"))]
    log = (s.run.path / "events.jsonl").read_text("utf-8")
    hook_event = [json.loads(l)["data"] for l in log.splitlines() if json.loads(l)["kind"] == "on_start"][1]
    assert hook_event["output"] == "tricky <secret:svc/main:KEY>" and "hookSpecificOutput" not in log
    assert "ab\\\"cd" not in log and "ab\"cd" not in log


# ------------------------------------------- loadout switches
def test_a_tool_with_only_a_readme_can_be_switched_off_and_on(shome, fakes):
    s = L.open_session(shome, tools_off=["vault"])                   # the shipped tool has a README and no tool.md
    assert s.state.tools_removed == ["vault"]
    s.set(tools_on=["vault"])
    assert [r.name for r in s.state.tools_added] == ["vault"] and s.state.tools_removed == []
    with pytest.raises(ValueError):
        s.set(tools_on=["no-such-thing"])


def test_tools_off_never_hides_a_skill(shome, fakes):
    s = L.open_session(shome, tools_off=["example-skill"])
    s._sync()
    assert "- example-skill:" in s.ctx.prefix and "- run-tracking:" in s.ctx.prefix   # skills are always on
    s.set(tools_on=["vault"])
    s._sync()
    assert "- run-tracking:" in s.ctx.prefix and "- example-skill:" in s.ctx.prefix


# ------------------------------------------------------------------ resume after a cancelled tool call
async def test_resume_closes_a_trailing_tool_use_without_result(shome, monkeypatch):
    import asyncio
    from lightmind.providers.fake import call
    from test_loop_support import ApiFake
    api = ApiFake([call("read", {"path": "a"}, id="c9")])
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: api)
    s = L.open_session(shome)

    async def cancelled(name, args):
        raise asyncio.CancelledError
    s.dispatcher.call = cancelled
    with pytest.raises(asyncio.CancelledError):
        await collect(s.send("go"))
    # simulate a crash before the loop could answer: drop the tool message from the log
    ev = s.run.path / "events.jsonl"
    lines = [l for l in ev.read_text("utf-8").splitlines()
             if not ('"kind": "message"' in l and '"role": "tool"' in l)]
    ev.write_text("\n".join(lines) + "\n", "utf-8")
    s.close()
    r = L.resume_session(shome, s.id)
    assert [m.role for m in r.messages][-2:] == ["assistant", "tool"]
    assert (r.messages[-1].tool_call_id, r.messages[-1].text) == ("c9", L.CANCELLED_TEXT)
    r.close()
    again = L.resume_session(shome, s.id)                                           # the fill-in was logged
    assert [m.role for m in again.messages].count("tool") == 1
