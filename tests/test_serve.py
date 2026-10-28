"""`lm serve --stdio` (protocol v1, plan/PROTOCOL.md): the real backend as a child process, JSON lines in and out, a scripted
provider (tests/serve_runner.py) instead of a model.  No network, no CLI, no VS Code."""
import asyncio
import json
import os
import queue
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from lightmind import loop, runs, serve
from lightmind import settings as S
from lightmind.files import Home
from lightmind.providers.fake import FakeProvider

HERE = Path(__file__).parent
QUIET_HOOK = """on_start:
  - run: uv run {tools}/vault/status.py "{vault}"
    show: true
    context: true
    timeout: 30
"""


@pytest.fixture
def quiet_tree(tree):
    """The shipped on_start hook runs uv and git: these tests use none."""
    p = tree / "lightmind.md"
    text = p.read_text("utf-8")
    assert QUIET_HOOK in text
    p.write_text(text.replace(QUIET_HOOK, "on_start: []\n", 1), "utf-8")
    return tree


class Client:
    """One `lm serve --stdio` child."""

    def __init__(self, proc, tmp):
        self.proc, self.tmp, self.log, self.n, self.err = proc, tmp, [], 0, []
        self.drain = asyncio.create_task(self._drain())

    @classmethod
    async def start(cls, tree, tmp, script=(), workdir=None, env=None):
        sp = tmp / f"script-{uuid.uuid4().hex[:8]}.json"
        sp.write_text(json.dumps(script if isinstance(script, dict) else list(script)), "utf-8")
        argv = [sys.executable, str(HERE / "serve_runner.py"), str(sp), "--home", str(tree), "serve", "--stdio"]
        if workdir:
            argv += ["--workdir", str(workdir)]
        e = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **(env or {})}
        for var in ("LM_RUN", "LM_DEPTH", "LM_PARENT"):
            e.pop(var, None)
        e.update(env or {})
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, cwd=str(tree), env=e)
        return cls(proc, tmp)

    async def _drain(self):
        while line := await self.proc.stderr.readline():
            self.err.append(line.decode("utf-8", "replace"))

    async def recv(self, timeout=30):
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        if not line:
            raise AssertionError("the backend closed stdout; stderr:\n" + "".join(self.err)[-3000:])
        msg = json.loads(line)                              # every line of stdout is JSON, nothing else
        self.log.append(msg)
        return msg

    async def raw(self, data: bytes):
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def send(self, **obj):
        await self.raw(json.dumps(obj).encode("utf-8") + b"\n")

    async def request(self, type_, **kw):
        """-> (result, events that came before it)"""
        self.n += 1
        rid = f"r{self.n}"
        await self.send(type=type_, id=rid, **kw)
        seen = []
        while True:
            msg = await self.recv()
            if msg["type"] == "result" and msg["id"] == rid:
                return msg, seen
            seen.append(msg)

    async def until(self, type_, **match):
        while True:
            msg = await self.recv()
            if msg["type"] == type_ and all(msg.get(k) == v for k, v in match.items()):
                return msg

    def of(self, type_):
        return [x for x in self.log if x["type"] == type_]

    async def close(self):
        if self.proc.returncode is None:
            with_pipe = self.proc.stdin
            try:
                await self.send(type="shutdown")
                with_pipe.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 30)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        await self.drain

    async def kill(self):
        self.proc.kill()
        await self.proc.wait()
        await self.drain


@pytest.fixture
async def start(quiet_tree, tmp_path):
    clients = []

    async def make(script=(), workdir=None, env=None):
        c = await Client.start(quiet_tree, tmp_path, script, workdir, env)
        clients.append(c)
        return c
    yield make
    for c in clients:
        if c.proc.returncode is None:
            c.proc.kill()
            await c.proc.wait()


async def opened(c, **kw):
    hello = await c.recv()
    assert hello["type"] == "hello"
    res, _ = await c.request("session.new", **kw)
    assert res["ok"], res
    return res["data"]["session"]


# ------------------------------------------------------------------ start and end
async def test_hello_comes_first_and_shutdown_ends_the_process(start, quiet_tree):
    c = await start()
    hello = await c.recv()
    assert hello["type"] == "hello" and hello["version"] == 1 and hello["session"] is None
    assert hello["tree"] == str(quiet_tree) and hello["workdir"] is None and hello["project"] is None
    assert hello["lm"] and hello["codename"]
    await c.close()
    assert c.proc.returncode == 0


async def test_a_run_started_by_an_agent_is_refused(quiet_tree, tmp_path):
    c = await Client.start(quiet_tree, tmp_path, env={"LM_RUN": "1"})
    out = await c.proc.stdout.read()
    assert await c.proc.wait() == 2 and out == b""
    await c.drain
    assert "lm serve" in "".join(c.err)


# ------------------------------------------------------------------ a chat
async def test_new_session_send_streams_text_and_slash_commands_run(start, quiet_tree):
    c = await start([[["deltas", ["Hel", "lo"]]]])
    sess = await opened(c)
    assert sess["origin"] == "vscode" and sess["status"] == "running" and sess["readonly"] is False
    sid = sess["id"]
    res, _ = await c.request("send", text="/effort high hi there")
    assert res == {"type": "result", "id": res["id"], "ok": True, "data": {}}
    end = await c.until("turn.end")
    assert end["status"] == "done" and end["session"] == sid and 0 <= end["context_used"] <= 1 and end["cost_usd"] >= 0
    texts = [x for x in c.of("text") if x["session"] == sid]
    assert [x["text"] for x in texts] == ["Hel", "lo"] and all(x["delta"] is True for x in texts)
    assert c.of("turn.start")[0]["turn"] == end["turn"]
    assert any(n["text"] == "effort: high" for n in c.of("notice"))
    status = await c.until("status")
    assert status["effort"] == "high" and status["session"] == sid
    assert all(x["session"] in (sid, None) for x in c.log if x["type"] not in ("result", "hello"))
    await c.close()


async def test_a_permission_question_is_answered_and_closed(start, quiet_tree):
    target = str(quiet_tree / "providers" / "extra.md")
    c = await start([[["call", "write", {"path": "providers/extra.md", "content": "x\n"}, "c1"]], [["say", "done"]]])
    await opened(c)
    await c.request("send", text="write it")
    q = await c.until("question")
    assert q["kind"] == "permission" and q["choices"] == ["yes", "always", "no"] and "write" in q["text"]
    start_ev = c.of("tool.start")[0]
    assert start_ev["name"] == "write" and start_ev["files"] == [os.path.normpath(target)]
    bad, _ = await c.request("answer", question=q["question"], choice="maybe")
    assert bad["ok"] is False
    ok, _ = await c.request("answer", question=q["question"], choice="yes")
    assert ok["ok"] is True
    closed = await c.until("question.closed")
    assert closed["question"] == q["question"] and closed["choice"] == "yes"
    end_ev = await c.until("tool.end")
    assert end_ev["call"] == "c1" and end_ev["ok"] is True and end_ev["files"] == [os.path.normpath(target)]
    assert (await c.until("turn.end"))["status"] == "done"
    again, _ = await c.request("answer", question=q["question"], choice="yes")
    assert again["ok"] is False                              # closed: nothing to answer
    await c.close()


async def test_the_ask_tool_goes_through_a_question(start):
    c = await start([[["call", "ask", {"question": "Which colour?"}, "a1"]], [["say", "thanks"]]])
    await opened(c)
    await c.request("send", text="ask me")
    q = await c.until("question")
    assert q["kind"] == "ask" and q["text"] == "Which colour?" and q["choices"] == []
    await c.request("answer", question=q["question"], choice="blue")
    assert (await c.until("question.closed"))["choice"] == "blue"
    assert (await c.until("tool.end"))["summary"] == "blue"
    assert (await c.until("turn.end"))["status"] == "done"
    await c.close()


async def test_cancel_with_an_open_question_closes_it_before_the_turn_ends(start):
    c = await start([[["call", "ask", {"question": "Still there?"}, "a1"]]])
    await opened(c)
    await c.request("send", text="ask me")
    q = await c.until("question")
    res, _ = await c.request("cancel")
    assert res["ok"]
    end = await c.until("turn.end")
    assert end["status"] == "cancelled"
    kinds = [x["type"] for x in c.log]
    closed = c.of("question.closed")[0]
    assert closed["question"] == q["question"] and closed["choice"] == "cancelled"
    assert kinds.index("question.closed") < kinds.index("turn.end")
    nxt, _ = await c.request("send", text="and again")       # the chat is usable afterwards
    assert nxt["ok"] and (await c.until("turn.end"))["status"] == "done"
    await c.close()


async def test_cancel_stops_a_running_turn_and_gives_back_what_was_not_read(start):
    c = await start([[["sleep", 60]]])
    await opened(c)
    await c.request("send", text="slow")
    await c.until("turn.start")
    second, seen = await c.request("send", text="too soon")        # a message while the agent works is an interjection
    assert second["ok"] is True and second["data"] == {"interject": "i1", "state": "pending"}
    assert [(x["id"], x["state"]) for x in seen if x["type"] == "interject"] == [("i1", "pending")]
    await c.request("cancel")
    assert (await c.until("turn.end"))["status"] == "cancelled"
    dropped = await c.until("interject", id="i1", state="dropped")  # never read: handed back with its words
    assert dropped["text"] == "too soon"
    idle, _ = await c.request("cancel")
    assert idle["ok"]                                        # nothing running: still fine
    await c.close()


async def test_an_interjection_is_read_at_the_next_tool_result_of_the_same_turn(start):
    c = await start([[["sleep", 1.0], ["call", "todo", {"action": "list"}, "c1"]], [["say", "got it"]]])
    await opened(c)
    await c.request("send", text="go")
    await c.until("turn.start")
    res, _ = await c.request("send", text="look at the desktop")
    assert res["data"]["state"] == "pending"
    delivered = await c.until("interject", id="i1", state="delivered")
    assert delivered["text"] == "look at the desktop"
    end = await c.until("turn.end")
    assert end["turn"] == "t1" and [x["text"] for x in c.of("text") if not x.get("parent")] == ["got it"]
    assert len(c.of("turn.start")) == 1                      # one turn: nothing restarted
    await c.close()


async def test_more_than_three_open_interjections_queue_and_go_out_after_the_turn(start):
    c = await start([[["sleep", 1.0], ["say", "one"]], [["say", "read"]], [["say", "queued answer"]]])
    await opened(c)
    await c.request("send", text="go")
    await c.until("turn.start")
    states = []
    for i in range(4):
        res, _ = await c.request("send", text=f"note {i + 1}")
        states.append(res["data"]["state"])
    assert states == ["pending", "pending", "pending", "queued"]
    await c.until("interject", id="q4", state="sent")
    await c.until("turn.end", turn="t2")
    assert len(c.of("turn.start")) == 2 and [x["state"] for x in c.of("interject") if x["state"] == "delivered"] == ["delivered"] * 3
    assert [x["text"] for x in c.of("text") if not x.get("parent")][-1] == "queued answer"
    await c.close()


# ------------------------------------------------------------------ crash, lock, history
async def test_a_crashed_backend_leaves_a_lock_the_next_one_takes_over(start):
    a = await start([[["say", "one"]]])
    sid = (await opened(a))["id"]
    b = await start()
    await b.recv()
    held, _ = await b.request("session.open", session=sid)    # a is alive: the chat can only be read
    assert held["ok"] and held["data"]["session"]["readonly"] is True
    refused, _ = await b.request("send", text="hello")
    assert refused["ok"] is False and "read only" in refused["error"]
    await b.close()
    await a.kill()
    c = await start([[["say", "two"]]])
    await c.recv()
    taken, _ = await c.request("session.open", session=sid)
    assert taken["ok"] and taken["data"]["session"]["readonly"] is False
    ok, _ = await c.request("send", text="go on")
    assert ok["ok"] and (await c.until("turn.end"))["status"] == "done"
    await c.close()


async def test_session_open_shows_the_history_and_session_list_the_origin(start, quiet_tree):
    a = await start([[["say", "Hi there"]]])
    sid = (await opened(a))["id"]
    await a.request("send", text="hello")
    await a.until("turn.end")
    await a.close()
    b = await start()
    await b.recv()
    listing, _ = await b.request("session.list", limit=10)
    row = next(r for r in listing["data"]["sessions"] if r["id"] == sid)
    assert row["origin"] == "vscode" and row["status"] == "done" and row["name"] == "hello"
    opened_, _ = await b.request("session.open", session=sid)
    hist = opened_["data"]["history"]
    assert [(h["role"] if "role" in h else "assistant", h["text"]) for h in hist if h["type"] == "text"] == [
        ("user", "hello"), ("assistant", "Hi there")]
    assert all(h["session"] == sid for h in hist)
    again, _ = await b.request("session.open", session=sid)   # the same chat again: no second lock, no close
    assert again["ok"] and again["data"]["session"]["id"] == sid
    gone, _ = await b.request("session.open", session="nope")
    assert gone["ok"] is False
    await b.close()
    assert next(r for r in runs.list_sessions(Home(quiet_tree), 10) if r.id == sid).origin == "vscode"   # the TUI's resume list


# ------------------------------------------------------------------ settings, reload, defaults
async def test_settings_get_and_set(start, quiet_tree):
    c = await start()
    await c.recv()
    res, _ = await c.request("settings.get")
    fields = {f["key"]: f for f in res["data"]["fields"]}
    assert fields["defaults.effort"]["scope"] == "config" and "high" in fields["defaults.effort"]["choices"]
    assert {"vscode.agent", "vscode.model", "vscode.effort", "vscode.mode"} <= set(fields)
    saved, _ = await c.request("settings.set", changes={"defaults.effort": "high", "vscode.effort": "low"})
    assert saved["ok"] and saved["data"] == {"saved": 2}
    res, _ = await c.request("settings.get")
    fields = {f["key"]: f for f in res["data"]["fields"]}
    assert fields["defaults.effort"]["value"] == "high" and fields["vscode.effort"]["value"] == "low"
    assert Home(quiet_tree).settings["vscode"]["effort"] == "low"
    for changes in ({"defaults.effort": "bogus"}, {"defaults.nope": "x"}):
        bad, _ = await c.request("settings.set", changes=changes)
        assert bad["ok"] is False and bad["error"]
    await c.close()


async def test_settings_get_carries_the_reason_when_no_mode_can_be_listed(start, quiet_tree):
    Home(quiet_tree).path("permissions").write_text("---\nfallback: allow\n---\n", encoding="utf-8")
    shutil.rmtree(quiet_tree / "modes")          # no mode file either: only then is there no mode at all
    (quiet_tree / "rechte.md").unlink(missing_ok=True)
    c = await start()
    await c.recv()
    res, _ = await c.request("settings.get")
    assert "no `modes:` mapping" in res["data"]["modes_error"]
    res, _ = await c.request("settings.get")
    await c.close()


async def test_settings_get_has_no_error_for_healthy_modes(start, quiet_tree):
    c = await start()
    await c.recv()
    res, _ = await c.request("settings.get")
    assert "modes_error" not in res["data"]
    fields = {f["key"]: f for f in res["data"]["fields"]}
    assert {"auto", "plan", "bypass"} <= set(fields["defaults.mode"]["choices"])
    await c.close()


async def test_settings_get_lists_the_agents(start, quiet_tree):
    c = await start()
    await c.recv()
    res, _ = await c.request("settings.get")
    agents = {a["name"]: a for a in res["data"]["agents"]}
    assert "example-agent" in agents
    a = agents["example-agent"]
    assert set(a) >= {"name", "project", "provider", "model", "effort", "mode", "effective"}
    assert a["effective"]["model"]                          # the default applies when the agent has none of its own
    await c.close()


async def test_session_set_changes_the_open_chat_and_refuses_otherwise(start, quiet_tree):
    c = await start()
    await c.recv()
    none, _ = await c.request("session.set", effort="high")
    assert none["ok"] is False and "no session" in none["error"]
    new, _ = await c.request("session.new")
    assert new["ok"]
    res, _ = await c.request("session.set", effort="high")
    assert res["ok"] and res["data"]["session"]["id"]
    assert c.of("status")[-1]["effort"] == "high"
    bad, _ = await c.request("session.set", effort="bogus")
    assert bad["ok"] is False
    mode, _ = await c.request("session.set", mode="no-such-mode")
    assert mode["ok"] is False
    empty, _ = await c.request("session.set")
    assert empty["ok"] is False
    await c.close()


async def test_session_list_marks_a_chat_open_in_another_process_and_set_refuses_it(start, quiet_tree):
    a = await start()
    sid = (await opened(a))["id"]
    b = await start()
    await b.recv()
    listing, _ = await b.request("session.list", limit=10)
    assert next(r for r in listing["data"]["sessions"] if r["id"] == sid)["readonly"] is True
    got, _ = await b.request("session.open", session=sid)
    assert got["data"]["session"]["readonly"] is True
    refused, _ = await b.request("session.set", effort="low")
    assert refused["ok"] is False and "read only" in refused["error"]
    listing, _ = await a.request("session.list", limit=10)
    assert next(r for r in listing["data"]["sessions"] if r["id"] == sid)["readonly"] is False   # ours is not "elsewhere"
    await b.close()
    await a.close()


async def test_a_new_chat_takes_its_defaults_from_the_vscode_keys(start, quiet_tree):
    home = Home(quiet_tree)
    S.save_page(home, S.read_general(home), {("vscode", "agent"): "example-agent", ("vscode", "effort"): "high"})
    c = await start()
    await c.recv()
    res, _ = await c.request("session.new")
    assert res["data"]["session"]["agent"] == "example-agent"
    assert c.of("status")[-1]["effort"] == "high"             # the status comes before the result
    res, _ = await c.request("session.new", effort="low", agent="example-agent")    # the request wins
    assert c.of("status")[-1]["effort"] == "low"
    await c.close()


async def test_reload_reports_what_changed(start, quiet_tree):
    c = await start()
    await opened(c)
    res, _ = await c.request("reload")
    assert res["ok"] and res["data"] == {"changed": []}
    skill = quiet_tree / "skills" / "cells" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: cells\ndescription: cells\n---\nBody\n", "utf-8")
    res, seen = await c.request("reload")
    assert res["data"]["changed"] == ["skill cells"]
    assert any(x["type"] == "notice" and "reloaded: skill cells" in x["text"] for x in c.of("notice"))
    await c.close()


# ------------------------------------------------------------------ requests are checked
async def test_bad_requests_are_answered_and_the_backend_goes_on(start):
    c = await start()
    await c.recv()
    await c.raw(b'{"type":"nope","id":"x1"}\n')
    err = await c.until("error")
    assert "unknown type" in err["text"] and err["id"] == "x1"
    await c.raw(b"not json\n")
    assert "not valid" in (await c.until("error"))["text"]
    await c.raw(b"[]\n")
    assert "object" in (await c.until("error"))["text"]
    await c.raw(b'{"type":"cancel"}\n')                      # no id
    assert "`id`" in (await c.until("error"))["text"]
    for msg, why in (({"type": "send", "id": "a1"}, "text"), ({"type": "send", "id": "a2", "text": "  "}, "empty"),
                     ({"type": "session.list", "id": "a3", "limit": 0}, "limit"),
                     ({"type": "session.open", "id": "a4", "session": "../x"}, "letters"),
                     ({"type": "settings.set", "id": "a5", "changes": {"a": [1]}}, "value"),
                     ({"type": "answer", "id": "a6", "question": "q9", "choice": "yes"}, "no open question"),
                     ({"type": "send", "id": "a7", "text": "x", "unknown": 1}, "no chat is open")):
        await c.send(**msg)
        res = await c.until("result", id=msg["id"])
        assert res["ok"] is False and why in res["error"], (msg, res)
    ok, _ = await c.request("session.list")
    assert ok["ok"] and ok["data"]["sessions"] == []
    await c.close()
    assert c.proc.returncode == 0


# ------------------------------------------------------------------ the working folder
async def test_the_project_entry_is_made_by_the_first_message_and_remembered(start, quiet_tree, tmp_path):
    wd = tmp_path / "My Proj"
    wd.mkdir()
    c = await start([[["say", "ok"]]], workdir=wd)
    hello = await c.recv()
    assert hello["workdir"] == str(wd) and hello["project"] is None
    sess = (await c.request("session.new"))[0]["data"]["session"]
    assert sess["project"] == "" and not (quiet_tree / "projects" / "my-proj").exists()      # nothing made by opening
    await c.request("send", text="hi")
    await c.until("turn.end")
    assert any(n["text"] == f"new project my-proj for {wd}" for n in c.of("notice"))
    assert (quiet_tree / "projects" / "my-proj" / "projectMemory.md").is_file()
    assert "path:" not in (quiet_tree / "projects" / "my-proj" / "projectMemory.md").read_text("utf-8")
    assert any(s["project"] == "my-proj" for s in c.of("status"))
    await c.close()
    d = await start(workdir=wd)
    assert (await d.recv())["project"] == "my-proj"
    await d.close()


async def test_a_project_of_the_same_name_is_linked_not_doubled(start, quiet_tree, tmp_path):
    (quiet_tree / "projects" / "other").mkdir()
    wd = tmp_path / "Other"
    wd.mkdir()
    c = await start([[["say", "ok"]]], workdir=wd)
    await opened(c)
    await c.request("send", text="hi")
    await c.until("turn.end")
    assert any(n["text"] == f"linked project other for {wd}" for n in c.of("notice"))
    assert not (quiet_tree / "projects" / "other-2").exists()
    await c.close()


def test_the_folder_reaches_the_context_and_the_shell(quiet_tree, tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "make_provider", lambda *a, **k: FakeProvider([]))
    wd = str(tmp_path / "work")
    s = loop.open_session(Home(quiet_tree), workdir=wd)
    assert s._state_info()["working folder"] == wd
    assert s.dispatcher._env()["LM_WORKDIR"] == wd
    assert os.getcwd() != wd                                  # the tree stays the working directory


def test_tool_start_names_the_files_of_path_tools_only(quiet_tree):
    b = serve.Backend(Home(quiet_tree), None, queue.SimpleQueue())
    assert b._paths("read", {"path": "lightmind.md"}) == [os.path.normpath(quiet_tree / "lightmind.md")]
    assert b._paths("edit", {"path": str(quiet_tree / "a" / ".." / "b.md")}) == [os.path.normpath(quiet_tree / "b.md")]
    assert b._paths("bash", {"command": "cat lightmind.md"}) == [] and b._paths("read", {}) == []


def test_everything_that_leaves_is_masked(quiet_tree):
    out = queue.SimpleQueue()
    b = serve.Backend(Home(quiet_tree), None, out)
    b.base_masker.add("demo/acct", "TOKEN", "s3cr3t-value-123")
    b.emit("notice", level="info", text="the key is s3cr3t-value-123 ok")
    b.result("r1", {"x": ["s3cr3t-value-123"]})
    lines = [json.loads(out.get_nowait()) for _ in range(2)]
    assert "s3cr3t" not in json.dumps(lines) and "<secret:demo/acct:TOKEN>" in lines[0]["text"]


# ------------------------------------------------------------------ background sub-agents (spawn)
def _bg_script(*, sub_sleep=0.0, main_after=("say", "thanks"), extra=()):
    spawn_call = ["call", "spawn", {"task": "look around", "background": True}, "c1"]
    return {"main": [[spawn_call], [["say", "started"]], [list(main_after)], *extra],
            "sub": [[["sleep", sub_sleep], ["say", "found <it> all"]]]}


async def test_a_background_sub_agent_result_comes_as_a_framed_message_and_starts_a_turn(start):
    c = await start(_bg_script())
    await opened(c)
    await c.request("send", text="go")
    ends = [await c.until("turn.end")]
    msg = await c.until("subagent.message")                  # its own event: not a text, not "You"
    assert msg["name"] == "sub" and msg["ok"] is True and "found <it> all" in msg["summary"] and msg["file"].endswith(".md")
    assert msg["turn"] == "t2"                               # the turn the result starts
    ends.append(await c.until("turn.end"))                   # the result started a turn of its own
    assert [e["turn"] for e in ends] == ["t1", "t2"]
    assert not [x for x in c.of("text") if x.get("role") == "user"]
    assert [x["text"] for x in c.of("text") if not x.get("parent")] == ["started", "thanks"]
    start_ev, end_ev = c.of("subagent.start"), c.of("subagent.end")
    assert len(start_ev) == 1 and len(end_ev) == 1 and end_ev[0]["ok"] is True
    # the history keeps it as the same kind of line, still not as a message of the user
    res, _ = await c.request("session.list")
    sid = res["data"]["sessions"][0]["id"]
    opened_, _ = await c.request("session.open", session=sid)
    hist = opened_["data"]["history"]
    assert [h["name"] for h in hist if h["type"] == "subagent.message"] == ["sub"]
    assert not [h for h in hist if h["type"] == "text" and h.get("role") == "user" and "subagent" in h["text"]]
    await c.close()


async def test_a_background_result_waits_for_the_running_turn(start):
    script = _bg_script()
    script["main"][1] = [["sleep", 1.0], ["say", "started"]]        # the main turn goes on after the spawn call
    c = await start(script)
    await opened(c)
    await c.request("send", text="go")
    await c.until("subagent.end")                                   # the sub-agent is done long before the turn
    assert not c.of("subagent.message") and not c.of("turn.end")
    await c.until("turn.end", turn="t1")
    await c.until("subagent.message")                               # only after the turn
    await c.until("turn.end", turn="t2")
    assert [x["text"] for x in c.of("text") if not x.get("parent")] == ["started", "thanks"]
    await c.close()


async def test_auto_turns_zero_keeps_results_until_the_next_message(start, quiet_tree):
    cfg = quiet_tree / "config.md"
    text = cfg.read_text("utf-8")
    assert "auto_turns: 3" in text
    cfg.write_text(text.replace("auto_turns: 3", "auto_turns: 0", 1), "utf-8")
    c = await start(_bg_script())
    await opened(c)
    await c.request("send", text="go")
    await c.until("turn.end", turn="t1")
    note = await c.until("notice")                                  # told once that results wait
    assert "wait" in note["text"] and "auto_turns" in note["text"]
    await asyncio.sleep(0.3)
    assert not c.of("subagent.message") and len(c.of("turn.end")) == 1     # no automatic turn
    await c.request("send", text="and now me")                      # his next message takes them along
    await c.until("turn.end", turn="t2")
    await c.close()


async def test_subagent_cancel_stops_one_and_refuses_unknown(start):
    c = await start(_bg_script(sub_sleep=60))
    await opened(c)
    await c.request("send", text="go")
    await c.until("turn.end", turn="t1")
    started = c.of("subagent.start")[0]
    bad, _ = await c.request("subagent.cancel", sub="nope")
    assert bad["ok"] is False and "no sub-agent" in bad["error"]
    res, _ = await c.request("subagent.cancel", sub=started["id"])
    assert res["ok"], res
    end = next(x for x in c.of("subagent.end") if x["id"] == started["id"])      # came before the result of the request
    assert end["ok"] is False and "cancelled" in end["summary"]
    for _ in range(50):                                       # the cancelled task needs a moment to be gone
        again, _ = await c.request("subagent.cancel", sub=started["id"])
        if not again["ok"]:
            break
        await asyncio.sleep(0.05)
    assert again["ok"] is False
    miss, _ = await c.request("subagent.cancel")
    assert miss["ok"] is False
    await c.close()


async def test_subagent_cancel_without_a_chat_is_refused(start):
    c = await start()
    assert (await c.recv())["type"] == "hello"
    res, _ = await c.request("subagent.cancel", sub="x")
    assert res["ok"] is False and "no chat" in res["error"]
    await c.close()


async def test_session_list_names_the_parent_of_a_run_that_another_run_started(start, quiet_tree):
    a = await start()
    sid = (await opened(a))["id"]
    child = Home(quiet_tree).path("runs") / (sid + "-chat-1")
    child.mkdir()
    (child / "meta.json").write_text(json.dumps({"id": child.name, "parent": sid, "start": "2026-10-23T10:00:00+00:00"}))
    (child / "events.jsonl").write_text(json.dumps({"kind": "session", "data": {}}) + "\n")
    orphan = Home(quiet_tree).path("runs") / "zz-orphan"
    orphan.mkdir()
    (orphan / "meta.json").write_text(json.dumps({"id": orphan.name, "parent": "chat-bot", "start": "2026-10-23T10:00:00+00:00"}))
    (orphan / "events.jsonl").write_text(json.dumps({"kind": "session", "data": {}}) + "\n")
    listing, _ = await a.request("session.list", limit=50)
    by_id = {r["id"]: r for r in listing["data"]["sessions"]}
    assert by_id[sid]["parent"] == ""
    assert by_id[child.name]["parent"] == sid and by_id[child.name]["parent_name"] == ""
    assert by_id[orphan.name]["parent"] == "chat-bot"
    await a.close()


async def test_a_permission_question_is_a_plain_sentence_with_the_command_as_folded_detail(start, quiet_tree):
    c = await start([[["call", "write", {"path": "providers/extra.md", "content": "x\n"}, "c1"]], [["say", "done"]]])
    await opened(c)
    await c.request("send", text="write it")
    q = await c.until("question")
    assert q["text"].split("\n")[0] == "Write file providers/extra.md" and "Allow " not in q["text"]
    assert q["detail"].endswith("extra.md")
    await c.request("answer", question=q["question"], choice="no")
    await c.close()


# ------------------------------------------------------------------ the resume menu: notes, name, end
async def test_notes_get_gives_name_description_and_the_to_do_tree_of_any_chat(start, quiet_tree):
    from lightmind import todos
    a = await start([[["say", "Hi"]]])
    sid = (await opened(a))["id"]
    await a.request("send", text="hello")
    await a.until("turn.end")
    d = Home(quiet_tree).path("runs") / sid
    st = todos.Store(d)
    st.add_folder("Plan")
    st.add_task("first", "go", "Plan")
    st.add_note("idea", "text")
    await a.close()
    b = await start()
    await b.recv()
    res, _ = await b.request("notes.get", session=sid)
    data = res["data"]
    assert res["ok"] and data["session"] == sid and "title" in data and "description" in data
    assert [(x["type"], x["title"]) for x in data["items"]] == [("folder", "Plan"), ("note", "idea")]
    assert data["items"][0]["items"][0]["title"] == "first"
    gone, _ = await b.request("notes.get", session="no-such-chat")
    assert gone["ok"] is False and "no such chat" in gone["error"]
    await b.close()


async def test_notes_name_sets_title_and_description_of_the_open_chat_and_of_a_closed_one(start, quiet_tree):
    a = await start()
    sid = (await opened(a))["id"]
    res, seen = await a.request("notes.name", session=sid, field="title", text="  My   chat ")
    assert res["ok"] and res["data"]["title"] == "My chat"
    assert any(e["type"] == "session.title" and e["title"] == "My chat" for e in seen + a.of("session.title"))
    res, _ = await a.request("notes.name", session=sid, field="description", text="about things")
    assert res["data"]["description"] == "about things"
    await a.close()
    meta = json.loads((Home(quiet_tree).path("runs") / sid / "meta.json").read_text("utf-8"))
    assert meta["title"] == "My chat" and meta["title_manual"] is True and meta["description"] == "about things"
    b = await start()                                       # the chat is not open here: written on disk
    await b.recv()
    res, _ = await b.request("notes.name", session=sid, field="title", text="Renamed")
    assert res["ok"] and res["data"]["title"] == "Renamed"
    assert runs.read_names(Home(quiet_tree), sid) == ("Renamed", "about things")
    res, _ = await b.request("notes.name", session=sid, field="title", text="")     # empty = automatic again
    assert res["ok"] and res["data"]["title"] == ""
    bad, _ = await b.request("notes.name", session=sid, field="agent", text="x")
    assert bad["ok"] is False
    none, _ = await b.request("notes.name", session="no-such-chat", field="title", text="x")
    assert none["ok"] is False
    await b.close()


async def test_session_end_closes_an_orphaned_chat_and_refuses_the_open_one(start, quiet_tree):
    a = await start()
    sid = (await opened(a))["id"]
    own, _ = await a.request("session.end", session=sid)
    assert own["ok"] is False and "open here" in own["error"]
    await a.kill()                                          # a crash: the chat stays "running", nobody holds it
    b = await start()
    await b.recv()
    res, _ = await b.request("session.end", session=sid)
    assert res["ok"] and res["data"]["text"] == "ended"
    meta = json.loads((Home(quiet_tree).path("runs") / sid / "meta.json").read_text("utf-8"))
    assert meta["status"] == "done"
    again, _ = await b.request("session.end", session=sid)
    assert again["data"]["text"] == "already ended"
    gone, _ = await b.request("session.end", session="no-such-chat")
    assert gone["ok"] is False
    await b.close()


async def test_session_end_asks_the_window_that_holds_the_chat_and_that_one_lets_go(start, quiet_tree):
    a = await start()
    sid = (await opened(a))["id"]
    b = await start()
    await b.recv()
    res, _ = await b.request("session.end", session=sid)
    assert res["ok"] and res["data"]["text"] == "ended", res
    ended = await a.until("session.ended")
    assert ended["session"] == sid and "resume menu" in ended["text"]
    sent, _ = await a.request("send", text="still there?")
    assert sent["ok"] is False and "no chat is open" in sent["error"]
    await a.close()
    await b.close()
