"""The node of Remote Control (node.py): what it tells the gateway (4.2) and how it uses the links of the chats (4.1).

No gateway and no real chat: `Gate` plays the gateway (it feeds request lines and collects what the node writes), `FakeLink` plays
the link of a chat (a unix socket in the links folder, or localhost TCP with a token).  The tree is a short path on purpose: a unix
socket path is limited to about 100 characters.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import OVERLAY
from lightmind import core, loop, node, runs, settings, upload
from lightmind.files import Home
from lightmind.init import init_tree
from lightmind.model import Run
from lightmind.providers.fake import FakeProvider
from lightmind.runs import RunDir
from test_session_lock import CHILD, finished_pid, other_process

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix sockets and a shell script as tmux")


@pytest.fixture
def tree(tmp_path_factory) -> Path:
    """Like the `tree` of conftest, in a short folder (unix socket paths are short)."""
    dst = tmp_path_factory.mktemp("n") / "lm"
    init_tree(dst)
    shutil.copytree(OVERLAY, dst, dirs_exist_ok=True)
    return dst


@pytest.fixture
def home(tree) -> Home:
    return Home(tree)


def switch_on(home: Home, **more) -> None:
    """`remote.enabled: true` in config.md (and more `remote.<key>` values), as a person would set them."""
    changes = {("remote", "enabled"): True} | {("remote", k): v for k, v in more.items()}
    settings.save(home, home.config_file, changes)


def card(home: Home, name: str, text: str) -> None:
    folder = home.path("servers") / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "info.md").write_text(f"---\n{text}\n---\n\n# {name}\n", "utf-8")


def chat(home: Home, rid: str, status: str = "done", text: str = "hello", parent: str = "") -> RunDir:
    """A chat on disk with one question and one answer."""
    rd = RunDir(home, Run(id=rid, status=status, parent=parent, start="2026-10-23T10:00:00", account="abo", model="haiku", origin="tui"))
    rd.event("session", {"account": "abo"})
    rd.event("message", {"role": "user", "text": text})
    rd.event("message", {"role": "assistant", "text": f"answer to {text}"})
    return rd


class Gate:
    """The gateway's end of one node: request lines in, the node's lines out."""

    def __init__(self, home: Home) -> None:
        self.home, self.sent, self.count = home, [], 0
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.node = node.Node(home, lambda raw: self.sent.append(json.loads(raw)), name="testnode")
        self.task = asyncio.create_task(self.node.run(self.inbox))

    def put(self, **message) -> str:
        self.count += 1
        message.setdefault("id", f"t{self.count}")
        self.inbox.put_nowait(json.dumps(message).encode() + b"\n")
        return message["id"]

    async def wait(self, found, timeout: float = 6.0, since: int = 0) -> dict:
        """The first line the node sent (after the first `since` ones) that `found` accepts."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hit = next((m for m in self.sent[since:] if found(m)), None)
            if hit is not None:
                return hit
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out; the node sent: {[m.get('type') for m in self.sent]}")

    async def ask(self, kind: str, /, timeout: float = 6.0, **fields) -> dict:
        since = len(self.sent)                        # an upload reuses its id for every request: only answers from now on count
        rid = self.put(type=kind, **fields)
        return await self.wait(lambda m: m.get("type") == "result" and m.get("id") == rid, timeout, since)

    async def data(self, kind: str, /, **fields) -> dict:
        answer = await self.ask(kind, **fields)
        assert answer["ok"], answer
        return answer["data"]

    def of_type(self, kind: str, **match) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind and all(m.get(k) == v for k, v in match.items())]

    async def stop(self) -> None:
        self.inbox.put_nowait(None)
        await asyncio.wait_for(self.task, 5)


@pytest.fixture
async def gate(home, monkeypatch):
    monkeypatch.setattr(node, "POLL_S", 0.05)
    switch_on(home)
    g = Gate(home)
    yield g
    if not g.task.done():
        await g.stop()


class FakeLink:
    """The link of a chat as the node sees it: a socket that says `link.hello` and answers requests (`received` keeps them)."""

    def __init__(self, home: Home, sid: str, name: str = "Test", tcp_token: str = "") -> None:
        self.home, self.sid, self.name, self.token = home, sid, name, tcp_token
        self.received: list[dict] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.answers = {"history": {"history": [{"type": "text", "session": sid, "text": "from the link", "role": "user"}], "seq": 7}}
        self.refuse: dict[str, str] = {}
        self.eof = asyncio.Event()                                    # set when the node's end of the link closes
        self.server = None

    async def start(self, pid: int | None = None, host: str = "127.0.0.1") -> "FakeLink":
        folder = node.links_dir(self.home)
        folder.mkdir(parents=True, exist_ok=True)
        pid = pid if pid is not None else os.getpid()
        info = {"session": self.sid, "name": self.name, "pid": pid, "stamp": runs.process_stamp(pid), "origin": "tui"}
        if self.token:
            self.server = await asyncio.start_server(self.client, "127.0.0.1", 0)
            info |= {"endpoint": f"{host}:{self.server.sockets[0].getsockname()[1]}", "token": self.token}
        else:
            self.server = await asyncio.start_unix_server(self.client, path=str(folder / f"{self.sid}.sock"))
            info["endpoint"] = str(folder / f"{self.sid}.sock")
        (folder / f"{self.sid}.json").write_text(json.dumps(info), "utf-8")
        return self

    async def client(self, reader, writer) -> None:
        if self.token:
            auth = json.loads(await reader.readline())
            if auth.get("token") != self.token:
                return writer.close()
        self.writers.append(writer)
        hello = {"type": "link.hello", "version": 1, "session": self.sid, "name": self.name, "origin": "tui", "pid": os.getpid(),
                 "title": "t", "status": {"model": "haiku"}, "busy": False, "seq": 7}
        writer.write(json.dumps(hello).encode() + b"\n")
        while line := await reader.readline():
            msg = json.loads(line)
            self.received.append(msg)
            if msg["type"] in self.refuse:
                answer = {"type": "result", "id": msg["id"], "ok": False, "error": self.refuse[msg["type"]]}
            else:
                answer = {"type": "result", "id": msg["id"], "ok": True, "data": self.answers.get(msg["type"], {})}
            writer.write(json.dumps(answer).encode() + b"\n")
            if msg["type"] == "session.archive":                       # the real chat closes its link after archiving itself
                writer.close()
        self.eof.set()

    def push(self, event: dict) -> None:
        for writer in self.writers:
            writer.write(json.dumps({"session": self.sid, **event}).encode() + b"\n")

    def drop(self) -> None:
        for writer in self.writers:
            writer.close()

    async def close(self) -> None:
        self.drop()
        self.server.close()
        await self.server.wait_closed()

    def requests(self, kind: str) -> list[dict]:
        return [m for m in self.received if m["type"] == kind]


@pytest.fixture
async def links():
    made: list[FakeLink] = []

    async def make(home, sid, **kw) -> FakeLink:
        link = FakeLink(home, sid, **kw)
        made.append(link)
        return await link.start()

    yield make
    for link in made:
        await link.close()


# ------------------------------------------------------------------------------------------------ hello, switch, ping
async def test_the_first_line_is_node_hello_with_the_facts_of_this_machine(gate, home, monkeypatch):
    first = gate.sent[0]
    assert first["type"] == "node.hello" and first["version"] == 1 and first["node"] == "testnode"
    assert first["lm"] and first["platform"] == sys.platform and first["tree"] == str(home.root)
    assert isinstance(first["can_start"], bool) and isinstance(first["tmux"], bool) and first["machine"]
    monkeypatch.setattr(node, "tmux_exe", lambda: "")
    assert node.Node(home, lambda raw: None).hello()["can_start"] is False


async def test_ping_is_answered_with_pong(gate):
    gate.put(type="ping", id="p1")
    assert (await gate.wait(lambda m: m.get("type") == "pong"))["id"] == "p1"


async def test_an_unknown_type_is_an_error_and_a_broken_line_is_ignored(gate):
    answer = await gate.ask("no.such.thing")
    assert answer["ok"] is False and "unknown" in answer["error"]
    gate.inbox.put_nowait(b"this is not json\n")
    await gate.wait(lambda m: m.get("type") == "error")
    assert (await gate.data("sessions.list"))["sessions"] == []                      # still alive


async def test_with_remote_off_the_node_refuses_everything_but_ping_and_settings(home, monkeypatch):
    monkeypatch.setattr(node, "POLL_S", 0.05)
    chat(home, "c1")
    g = Gate(home)
    try:
        refused = await g.ask("sessions.list")
        assert refused["ok"] is False and "remote.enabled" in refused["error"]
        assert (await g.ask("session.open", session="c1"))["ok"] is False
        assert (await g.ask("settings.get"))["ok"] is True                                # so the phone can switch it on
        g.put(type="ping", id="p")
        await g.wait(lambda m: m.get("type") == "pong")
        switched = await g.ask("settings.set", changes={"remote.enabled": True})
        assert switched["ok"] is True
        assert (await g.ask("sessions.list"))["ok"] is True
    finally:
        await g.stop()


# ------------------------------------------------------------------------------------------------ links
@posix_only
async def test_a_link_file_makes_rc_on_and_its_events_go_on_with_node_and_session(gate, home, links):
    link = await links(home, "c1", name="Milk")
    on = await gate.wait(lambda m: m.get("type") == "rc.on")
    assert on == {"type": "rc.on", "session": "c1", "name": "Milk", "origin": "tui"}
    await gate.wait(lambda m: m.get("type") == "sessions.changed")
    link.push({"type": "text", "turn": "t1", "text": "hi", "seq": 8})
    event = await gate.wait(lambda m: m.get("type") == "text")
    assert event["node"] == "testnode" and event["session"] == "c1" and event["seq"] == 8 and event["text"] == "hi"
    assert not gate.of_type("link.hello")                                                # the hello is the node's, not the gateway's


@posix_only
async def test_closing_the_socket_gives_rc_off_and_a_dead_process_loses_its_files(gate, home, links):
    link = await links(home, "c1")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    link.drop()
    off = await gate.wait(lambda m: m.get("type") == "rc.off")
    assert off["session"] == "c1" and off["reason"] == "closed"
    assert len(gate.of_type("rc.off")) == 1
    folder = node.links_dir(home)
    (folder / "ghost.json").write_text(json.dumps({"session": "ghost", "pid": finished_pid(), "stamp": ""}), "utf-8")
    (folder / "ghost.sock").write_text("", "utf-8")
    for _ in range(100):
        if not (folder / "ghost.json").exists():
            break
        await asyncio.sleep(0.05)
    assert not (folder / "ghost.json").exists() and not (folder / "ghost.sock").exists()
    assert not gate.of_type("rc.on", session="ghost")


@posix_only
async def test_a_link_of_a_process_that_died_is_reported_gone_and_cleaned(gate, home, links, monkeypatch):
    await links(home, "c1")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    monkeypatch.setattr(node, "alive", lambda pid, stamp: False)
    off = await gate.wait(lambda m: m.get("type") == "rc.off")
    assert off["reason"] == "gone" and not (node.links_dir(home) / "c1.json").exists()


@posix_only
async def test_when_the_gateway_goes_away_the_links_are_let_go_without_rc_off(gate, home, links):
    link = await links(home, "c1")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    await gate.stop()
    await asyncio.wait_for(link.eof.wait(), 3)                                           # the node's end is closed ...
    assert not gate.of_type("rc.off")                                                    # ... but the chat itself goes on: no rc.off
    assert (node.links_dir(home) / "c1.json").exists()


async def test_a_link_that_names_a_remote_host_is_never_connected(gate, home, links, monkeypatch):
    dialled = []
    real = asyncio.open_connection

    async def spy(host, *args, **kwargs):
        dialled.append(host)
        return await real(host, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", spy)
    await links(home, "c1", tcp_token="s3cret")                                         # a proper localhost link with a token works
    assert (await gate.wait(lambda m: m.get("type") == "rc.on"))["session"] == "c1"
    bad = node.links_dir(home) / "c2.json"
    bad.write_text(json.dumps({"session": "c2", "pid": os.getpid(), "stamp": runs.process_stamp(os.getpid()),
                               "endpoint": "10.1.2.3:4000", "token": "x"}), "utf-8")
    await asyncio.sleep(0.4)
    assert not gate.of_type("rc.on", session="c2") and dialled == ["127.0.0.1"]         # the token never goes to another host


async def test_a_link_that_does_not_say_hello_is_not_announced(gate, home, monkeypatch):
    monkeypatch.setattr(node, "HELLO_WAIT_S", 0.3)
    seen = []

    async def mute(reader, writer):
        seen.append(1)
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_server(mute, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    (node.links_dir(home)).mkdir(parents=True, exist_ok=True)
    (node.links_dir(home) / "c1.json").write_text(json.dumps({"session": "c1", "pid": os.getpid(), "stamp": "", "endpoint": f"127.0.0.1:{port}",
                                                             "token": "t"}), "utf-8")
    await asyncio.sleep(0.8)
    server.close()
    assert seen and not gate.of_type("rc.on")


# ------------------------------------------------------------------------------------------------ sessions.list, session.open
@posix_only
async def test_sessions_list_shows_the_archive_only_when_asked_and_the_status_of_each(gate, home, links):
    chat(home, "c-done")
    chat(home, "c-paused", status="paused")
    chat(home, "c-link", status="running")
    chat(home, "c-gone", status="done")
    runs.archive(home, "c-gone")
    runs.claim_name(home, "milk", "c-done")
    await links(home, "c-link", name="Phone")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    rows = {r["id"]: r for r in (await gate.data("sessions.list"))["sessions"]}
    assert set(rows) == {"c-done", "c-paused", "c-link"}
    assert rows["c-done"]["status"] == "done" and rows["c-done"]["name"] == "milk" and rows["c-done"]["archived"] is False
    assert rows["c-paused"]["status"] == "paused"
    assert rows["c-link"]["status"] == "rc" and rows["c-link"]["rc_name"] == "Phone"
    assert {"id", "name", "title", "description", "agent", "project", "status", "archived", "last_active", "model", "origin", "parent"} <= set(rows["c-done"])
    archived = (await gate.data("sessions.list", archived=True))["sessions"]
    assert [r["id"] for r in archived] == ["c-gone"] and archived[0]["archived"] is True


async def test_a_chat_that_another_process_holds_without_a_link_is_running(gate, home):
    chat(home, "c1", status="running")
    with other_process(CHILD, str(home.root), "c1") as (child, first):
        assert first == "0"
        rows = (await gate.data("sessions.list"))["sessions"]
    assert rows[0]["status"] == "running"
    rows = (await gate.data("sessions.list"))["sessions"]
    assert rows[0]["status"] == "paused"                                                  # open on disk, nobody holds it now


async def test_session_open_without_a_link_gives_the_history_and_writable_false(gate, home):
    chat(home, "c1", text="remember the milk")
    data = await gate.data("session.open", session="c1")
    assert data["writable"] is False and data["seq"] == 0
    texts = [(e["type"], e.get("role"), e.get("text")) for e in data["history"] if e["type"] == "text"]
    assert texts == [("text", "user", "remember the milk"), ("text", None, "answer to remember the milk")]
    assert data["session"]["id"] == "c1" and data["session"]["status"] == "done" and data["session"]["title"] == "remember the milk"
    assert runs.lock_holder(home, "c1") == 0                                              # reading did not take the chat
    assert json.loads((home.path("runs") / "c1" / "meta.json").read_text("utf-8"))["status"] == "done"


async def test_the_history_from_disk_is_masked(gate, home):
    secret = home.root / "secrets" / "svc" / "default.env"
    secret.parent.mkdir(parents=True)
    secret.write_text("SVC_PASSWORD=hunter2-very-secret\n", "utf-8")
    chat(home, "c1", text="the password is hunter2-very-secret")
    opened = await gate.data("session.open", session="c1")
    listed = await gate.data("sessions.list")                                            # the title is the user's first words
    for data in (opened, listed):
        assert "hunter2-very-secret" not in json.dumps(data) and "<secret:svc/default:SVC_PASSWORD>" in json.dumps(data)
    assert "hunter2" not in json.dumps(opened["history"]) and "hunter2" not in opened["session"]["title"]


@posix_only
async def test_session_open_with_a_link_takes_the_history_and_seq_from_the_link(gate, home, links):
    chat(home, "c1")
    await links(home, "c1")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    data = await gate.data("session.open", session="c1")
    assert data["writable"] is True and data["seq"] == 7 and data["history"][0]["text"] == "from the link"
    assert data["session"]["status"] == "rc"


async def test_session_open_rejects_unknown_and_unsafe_ids(gate, home):
    for bad in ("nope", "../../etc", "", None, 7):
        answer = await gate.ask("session.open", session=bad)
        assert answer["ok"] is False and answer["error"] == "unknown session"


# ------------------------------------------------------------------------------------------------ steering through the link
async def test_send_to_a_chat_without_a_link_is_refused(gate, home):
    chat(home, "c1")
    answer = await gate.ask("send", session="c1", text="hi", **{"from": "pixel"})
    assert answer["ok"] is False and "not open with Remote Control" in answer["error"]


@posix_only
async def test_send_answer_cancel_and_set_reach_the_chat_with_only_the_allowed_fields(gate, home, links):
    chat(home, "c1")
    link = await links(home, "c1")
    link.answers["send"] = {"interject": "i1", "state": "pending"}
    link.answers["session.set"] = {"status": {"effort": "high"}}
    await gate.wait(lambda m: m.get("type") == "rc.on")
    assert await gate.data("send", session="c1", text="hello", **{"from": "pixel"}) == {"interject": "i1", "state": "pending"}
    await gate.data("answer", session="c1", question="q1", choice="yes", **{"from": "pixel"}, evil="x")
    await gate.data("cancel", session="c1")
    assert await gate.data("session.set", session="c1", effort="high", other="x") == {"status": {"effort": "high"}}
    sent = link.requests("send")[0]
    assert (sent["text"], sent["from"]) == ("hello", "pixel") and set(sent) == {"type", "id", "text", "from"}
    assert set(link.requests("answer")[0]) == {"type", "id", "question", "choice", "from"}
    assert link.requests("cancel") and set(link.requests("session.set")[0]) == {"type", "id", "effort"}


@posix_only
async def test_bad_requests_never_reach_the_chat(gate, home, links):
    chat(home, "c1")
    link = await links(home, "c1")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    assert (await gate.ask("send", session="c1", text="   "))["ok"] is False
    assert (await gate.ask("send", session="c1", text="x" * (node.TEXT_MAX + 1)))["ok"] is False
    assert (await gate.ask("send", session="c1", text=5))["ok"] is False
    assert (await gate.ask("answer", session="c1", choice="yes"))["ok"] is False
    assert (await gate.ask("session.set", session="c1"))["ok"] is False
    assert not link.received


@posix_only
async def test_the_reason_a_chat_gives_for_a_no_goes_back_to_the_gateway(gate, home, links):
    chat(home, "c1")
    link = await links(home, "c1")
    link.refuse["send"] = "a question is open"
    await gate.wait(lambda m: m.get("type") == "rc.on")
    answer = await gate.ask("send", session="c1", text="hi")
    assert answer["ok"] is False and answer["error"] == "a question is open"


# ------------------------------------------------------------------------------------------------ archive
async def test_archive_without_a_link_sets_the_marker_by_phone_and_unarchive_removes_it(gate, home):
    chat(home, "c1")
    assert (await gate.data("session.archive", session="c1"))["text"] == "archived"
    marker = json.loads((home.path("runs") / "c1" / "archived.json").read_text("utf-8"))
    assert marker["by"] == "phone"
    assert (await gate.data("sessions.list"))["sessions"] == []
    assert [r["id"] for r in (await gate.data("sessions.list", archived=True))["sessions"]] == ["c1"]
    assert (await gate.data("session.unarchive", session="c1"))["text"] == "restored"
    assert [r["id"] for r in (await gate.data("sessions.list"))["sessions"]] == ["c1"]
    assert gate.of_type("sessions.changed")


@posix_only
async def test_archive_of_a_chat_with_a_link_is_done_by_the_chat_itself(gate, home, links):
    chat(home, "c1", status="running")
    link = await links(home, "c1")
    link.answers["session.archive"] = {"text": "archived: Milk"}
    await gate.wait(lambda m: m.get("type") == "rc.on")
    assert (await gate.data("session.archive", session="c1")) == {"text": "archived: Milk"}
    assert link.requests("session.archive") and not (home.path("runs") / "c1" / "archived.json").exists()      # the TUI writes it
    await gate.wait(lambda m: m.get("type") == "rc.off")


async def test_archive_of_a_chat_another_process_holds_without_a_link_is_refused(gate, home, monkeypatch):
    monkeypatch.setattr(runs, "request_stop", lambda *a, **k: "asked")
    chat(home, "c1", status="running")
    with other_process(CHILD, str(home.root), "c1"):
        answer = await gate.ask("session.archive", session="c1")
    assert answer["ok"] is False and "still running in process" in answer["error"]
    assert not (home.path("runs") / "c1" / "archived.json").exists()


# ------------------------------------------------------------------------------------------------ new and resumed chats
FAKE_TMUX = """#!/bin/sh
# a stand-in for tmux: every call is logged, one argument per line, calls separated by "--"
here="$(dirname "$0")"
printf '%s\\n' "$@" >> "$here/tmux.log"
echo -- >> "$here/tmux.log"
case "$1" in
  has-session) [ -f "$here/has-session" ]; exit $? ;;
  new-session) touch "$here/has-session" ;;
esac
exit 0
"""


@pytest.fixture
def tmux(tmp_path_factory, monkeypatch):
    folder = tmp_path_factory.mktemp("tmux")
    script = folder / "tmux"
    script.write_text(FAKE_TMUX, "utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LM_TMUX", str(script))
    monkeypatch.setattr(loop, "make_provider", lambda *a, **k: FakeProvider([]))

    class Calls:
        log = folder / "tmux.log"

        def calls(self) -> list[list[str]]:
            if not self.log.exists():
                return []
            return [c.strip("\n").split("\n") for c in self.log.read_text("utf-8").split("--\n") if c.strip()]

    return Calls()


async def link_when_opened(home, links, tmux, nth: int = 0, name: str = "Test") -> tuple[str, FakeLink]:
    """Plays the TUI: as soon as tmux was asked for the `nth` window, the chat it names shows its link."""
    for _ in range(200):
        windows = [c for c in tmux.calls() if c[0] == "new-window"]
        if len(windows) > nth:
            sid = windows[nth][windows[nth].index("open") + 1]
            return sid, await links(home, sid, name=name)
        await asyncio.sleep(0.05)
    raise AssertionError("tmux was never asked for a window")


@posix_only
async def test_session_new_makes_the_chat_hands_it_over_and_opens_it_in_tmux(gate, home, links, tmux):
    switch_on(home, effort="high")
    task = asyncio.create_task(link_when_opened(home, links, tmux))
    data = await gate.data("session.new", name="milk-run")
    sid, _ = await task
    assert data == {"session": sid}
    lm = str(home.root / "system" / "lm")
    assert tmux.calls() == [["has-session", "-t", "lm"], ["new-session", "-d", "-s", "lm", lm],
                            ["new-window", "-d", "-t", "lm:", "-n", "milk-run", lm, "open", sid, "--rc", "milk-run"]]
    meta = json.loads((home.path("runs") / sid / "meta.json").read_text("utf-8"))
    assert meta["origin"] == "phone" and meta["status"] == "paused" and runs.lock_holder(home, sid) == 0
    assert runs.read_trusted(home, sid)["effort"] == "high"                                # remote.effort of config.md
    assert runs.find_name(home, "milk-run") == sid


@posix_only
async def test_session_new_sends_the_first_message_through_the_new_link(gate, home, links, tmux):
    task = asyncio.create_task(link_when_opened(home, links, tmux))
    await gate.data("session.new", text="start with the shopping list")
    _, link = await task
    sent = link.requests("send")
    assert len(sent) == 1 and sent[0]["text"] == "start with the shopping list" and sent[0]["from"] == "phone"


@posix_only
async def test_a_second_new_chat_reuses_the_tmux_session(gate, home, links, tmux):
    for nth in range(2):
        task = asyncio.create_task(link_when_opened(home, links, tmux, nth))
        await gate.data("session.new")
        await task
    assert [c[0] for c in tmux.calls()] == ["has-session", "new-session", "new-window", "has-session", "new-window"]


@posix_only
async def test_a_name_with_a_space_or_a_taken_name_is_refused_before_anything_is_made(gate, home, tmux):
    before = {p.name for p in home.path("runs").iterdir()} if home.path("runs").exists() else set()
    for bad in ("my chat", "x;rm -rf", "-flag", "a" * 65, 5):
        answer = await gate.ask("session.new", name=bad)
        assert answer["ok"] is False and "name" in answer["error"], bad
    chat(home, "c1")
    runs.claim_name(home, "taken", "c1")
    answer = await gate.ask("session.new", name="taken")
    assert answer["ok"] is False and "exists" in answer["error"]
    assert tmux.calls() == []
    assert {p.name for p in home.path("runs").iterdir()} == before | {"c1"}


@posix_only
async def test_session_new_checks_agent_project_model_effort_and_mode(gate, home, tmux):
    for field, value in (("agent", "no-such-agent"), ("project", "no-such-project"), ("effort", "gigantic"),
                         ("mode", "no-such-mode"), ("model", "bad model; rm")):
        answer = await gate.ask("session.new", **{field: value})
        assert answer["ok"] is False, field
    assert tmux.calls() == []


@posix_only
async def test_session_new_without_a_link_in_time_says_which_chat_and_how_to_find_it(gate, home, tmux, monkeypatch):
    monkeypatch.setattr(node, "NEW_LINK_WAIT_S", 0.4)
    answer = await gate.ask("session.new", name="lost")
    assert answer["ok"] is False and "opened in tmux" in answer["error"] and "lm remote" in answer["error"]


async def test_session_new_where_nothing_can_start_it_is_refused(gate, home, monkeypatch):
    monkeypatch.setattr(node, "tmux_exe", lambda: "")
    answer = await gate.ask("session.new")
    assert answer["ok"] is False and "tmux" in answer["error"]


@posix_only
async def test_a_failing_tmux_is_reported_with_its_own_words(gate, home, tmux):
    Path(os.environ["LM_TMUX"]).write_text("#!/bin/sh\necho no server >&2\nexit 1\n", "utf-8")
    answer = await gate.ask("session.new")
    assert answer["ok"] is False and "tmux new-session failed: no server" in answer["error"]


@posix_only
async def test_session_resume_opens_the_chat_again_in_a_window_and_refuses_what_it_cannot(gate, home, links, tmux):
    chat(home, "c1")
    task = asyncio.create_task(link_when_opened(home, links, tmux))
    assert await gate.data("session.resume", session="c1") == {"session": "c1"}
    assert (await task)[0] == "c1"
    window = [c for c in tmux.calls() if c[0] == "new-window"][0]
    assert window[-4:] == ["open", "c1", "--rc", "phone-c1"]
    assert await gate.data("session.resume", session="c1") == {"session": "c1"}          # it has a link now: nothing more to do
    assert len([c for c in tmux.calls() if c[0] == "new-window"]) == 1
    chat(home, "c2")
    runs.archive(home, "c2")
    assert "archived" in (await gate.ask("session.resume", session="c2"))["error"]
    chat(home, "c3", status="running")
    with other_process(CHILD, str(home.root), "c3"):
        answer = await gate.ask("session.resume", session="c3")
    assert answer["ok"] is False and "/rc" in answer["error"]


# ------------------------------------------------------------------------------------------------ options, commands
async def test_options_lists_models_efforts_modes_agents_and_projects(gate, home):
    data = await gate.data("options")
    assert data["efforts"] == list(core.efforts())
    assert {m["id"] for m in data["modes"]} == set(home.modes())
    assert {a["name"] for a in data["agents"]} == set(home.list_agents()) and all("project" in a for a in data["agents"])
    assert {p["name"] for p in data["projects"]} == set(home.list_projects())
    assert all({"id", "label"} <= set(m) for m in data["models"])


async def test_commands_leave_out_what_needs_a_screen_and_name_the_kind(gate, home):
    chat(home, "c1")
    rows = (await gate.data("commands", session="c1"))["commands"]
    names = {r["name"] for r in rows}
    assert {"model", "effort", "mode", "rename", "reload"} <= names
    assert not names & {"settings", "config", "quit", "exit", "upload", "datei", "login", "key", "accounts", "resume", "server", "clear", "new"}
    assert {r["kind"] for r in rows} <= {"builtin", "ui", "skill"} and all({"name", "help", "kind"} <= set(r) for r in rows)
    assert any(r["kind"] == "skill" for r in rows)                                       # the overlay brings skills


# ------------------------------------------------------------------------------------------------ settings
def lines_of(home: Home) -> list[str]:
    return home.config_file.read_text("utf-8").splitlines()


async def test_settings_get_gives_the_remote_values_servers_and_fields(gate, home):
    card(home, "homeserver", "host: local\nlm_home: ~/lightmind\nremote: true")
    card(home, "buildbox", "host: 10.0.0.2\nssh: buildbox\nlm_home: ~/lightmind")
    settings.save(home, home.config_file, {("remote", "default_node"): "homeserver"})       # no default node in the core
    data = await gate.data("settings.get")
    assert data["remote"]["enabled"] is True and data["remote"]["default_node"] == "homeserver" and data["remote"]["upload_mb"] == 200
    servers = {s["name"]: s["remote"] for s in data["servers"]}
    assert servers["homeserver"] is True and servers["buildbox"] is False and "_example" not in servers
    fields = {f["path"]: f for f in data["fields"]}
    assert "remote.mode" in fields and fields["remote.mode"]["danger"] and "defaults.model" in fields
    assert {o["value"] for o in fields["remote.push"]["options"]} >= {"webpush", "ntfy", "both", "off"}
    assert data["rev"]


async def test_settings_set_changes_one_line_of_config_md_and_nothing_else(gate, home):
    card(home, "buildbox", "host: 10.0.0.2\nssh: buildbox\nlm_home: ~/lightmind")
    before = lines_of(home)
    assert (await gate.data("settings.set", changes={"remote.default_node": "buildbox"})) == {"saved": ["remote.default_node"]}
    after = lines_of(home)
    assert len(after) == len(before) + 1 and [l for l in after if l not in before] == ["  default_node: buildbox"]
    assert all(line in after for line in before)
    assert Home(home.root).settings["remote"]["default_node"] == "buildbox"
    again = list(after)
    await gate.data("settings.set", changes={"remote.effort": "high"})
    await gate.data("settings.set", changes={"remote.effort": "medium"})
    assert [l for l in lines_of(home) if l not in again] == ["  effort: medium"]            # the same line, changed in place


async def test_settings_set_validates_and_writes_nothing_on_a_bad_value(gate, home):
    before = home.config_file.read_bytes()
    for changes in ({"remote.effort": "gigantic"}, {"remote.upload_mb": 100000}, {"remote.nonsense": 1}, {"tools.shell": True},
                    {"remote.default_node": "nowhere"}, {"servers.buildbox.remote": True}, {"servers.laptop.remote": "yes"},
                    {}, "not a dict", {"remote.effort": "high", "remote.mode": "no-such-mode"}):
        answer = await gate.ask("settings.set", changes=changes)
        assert answer["ok"] is False, changes
    assert home.config_file.read_bytes() == before


async def test_settings_set_reports_a_conflict_when_the_file_changed_since_it_was_loaded(gate, home):
    rev = (await gate.data("settings.get"))["rev"]
    text = home.config_file.read_text("utf-8")
    home.config_file.write_text(text + "\n# edited by hand\n", "utf-8")
    answer = await gate.ask("settings.set", changes={"remote.effort": "high"}, rev=rev)
    assert answer["ok"] is False and "changed" in answer["error"]
    assert "effort" not in (Home(home.root).settings.get("remote") or {})
    fresh = (await gate.data("settings.get"))["rev"]
    assert (await gate.ask("settings.set", changes={"remote.effort": "high"}, rev=fresh))["ok"] is True


async def test_settings_set_switches_a_server_in_its_info_md_only(gate, home):
    card(home, "buildbox", "host: 10.0.0.2\nssh: buildbox\nlm_home: ~/lightmind")
    path = home.path("servers") / "buildbox" / "info.md"
    await gate.data("settings.set", changes={"servers.buildbox.remote": True})
    assert path.read_text("utf-8").splitlines()[1:5] == ["host: 10.0.0.2", "ssh: buildbox", "lm_home: ~/lightmind", "remote: true"]
    assert [t["enabled"] for t in node.targets(Home(home.root)) if t["name"] == "buildbox"] == [True]
    await gate.data("settings.set", changes={"servers.buildbox.remote": False})
    assert "remote: false" in path.read_text("utf-8") and path.read_text("utf-8").endswith("# buildbox\n")


async def test_settings_are_only_changed_on_the_machine_of_the_gateway(gate, home):
    card(home, "elsewhere", "host: example.lan\nlm_home: ~/lightmind")
    settings.save(home, home.config_file, {("remote", "gateway"): "elsewhere"})
    for kind in ("settings.get", "settings.set"):
        answer = await gate.ask(kind, changes={"remote.effort": "high"})
        assert answer["ok"] is False and "machine of the gateway" in answer["error"]


# ------------------------------------------------------------------------------------------------ uploads
def chunks(data: bytes, size: int) -> list[str]:
    return [base64.b64encode(data[i:i + size]).decode() for i in range(0, len(data), size)] or [""]


async def push_file(gate, sid: str, name: str, data: bytes, kind: str = "file", mime: str = "application/octet-stream",
                    uid: str = "u0123456789abcdef", size: int = 4) -> dict:
    await gate.data("upload.begin", id=uid, session=sid, name=name, size=len(data), mime=mime, kind=kind)
    for seq, part in enumerate(chunks(data, size)):
        await gate.data("upload.chunk", id=uid, seq=seq, data=part)
    answer = await gate.ask("upload.end", id=uid)
    assert answer["ok"], answer
    return answer["data"]


@pytest.fixture
async def linked_chat(gate, home, links):
    chat(home, "c1")
    link = await links(home, "c1")
    await gate.wait(lambda m: m.get("type") == "rc.on")
    return link


@posix_only
async def test_an_upload_in_three_chunks_lands_in_the_uploads_folder_with_its_marker(gate, home, linked_chat):
    result = await push_file(gate, "c1", "notes.txt", b"abcdefghij", size=4)
    folder = home.path("runs") / "c1" / "uploads"
    assert (folder / "notes.txt").read_bytes() == b"abcdefghij" and [p.name for p in folder.iterdir()] == ["notes.txt"]
    assert result["path"] == "runs/c1/uploads/notes.txt" and result["marker"] == "[Anhang: runs/c1/uploads/notes.txt]"
    assert result["kind"] == "file" and "transcript" not in result
    second = await push_file(gate, "c1", "notes.txt", b"again", uid="u2")
    assert second["path"] == "runs/c1/uploads/notes-1.txt"                              # a taken name gets the next free one


@posix_only
async def test_a_hostile_file_name_is_cut_down_to_a_base_name(gate, home, linked_chat):
    for index, (given, wanted) in enumerate((("../x", "x"), ("..\\..\\evil.sh", "evil.sh"), ("/etc/passwd", "passwd"),
                                              ("a\x00b\nc.txt", "abc.txt"), ("...hidden", "hidden"), ("", "upload"), ("..", "upload-1"),
                                              ("na:me?.png", "na_me_.png"))):
        result = await push_file(gate, "c1", given, b"x", uid=f"u{index}")
        assert Path(result["path"]).name == wanted, given
        assert (home.path("runs") / "c1" / "uploads" / wanted).is_file()
    assert not (home.path("runs") / "c1" / "x").exists() and not (home.root / "x").exists()


def test_clean_filename_keeps_the_ending_of_a_long_name_and_names_a_recording():
    long = node.clean_filename("a" * 300 + ".webm")
    assert len(long) <= 120 and long.endswith(".webm")
    assert node.clean_filename("", "voice") == "voice" and node.clean_filename(None) == "upload"


@posix_only
async def test_an_upload_over_the_limit_is_refused_at_the_start_or_while_streaming(gate, home, linked_chat):
    settings.save(home, home.config_file, {("remote", "upload_mb"): 1})
    answer = await gate.ask("upload.begin", id="ubig", session="c1", name="big.bin", size=2 * 1024 * 1024, mime="x", kind="file")
    assert answer["ok"] is False and "1 MB" in answer["error"]
    await gate.data("upload.begin", id="ustream", session="c1", name="lie.bin", size=None, mime="x", kind="file")
    block = b"\0" * (600 * 1024)
    await gate.data("upload.chunk", id="ustream", seq=0, data=base64.b64encode(block).decode())
    answer = await gate.ask("upload.chunk", id="ustream", seq=1, data=base64.b64encode(block).decode())
    assert answer["ok"] is False and "1 MB" in answer["error"]
    assert not any((home.path("runs") / "c1" / "uploads").iterdir()), "no .part and no empty reserved file may stay"


@posix_only
async def test_upload_to_a_chat_without_a_link_or_with_a_bad_id_is_refused(gate, home):
    chat(home, "c1")
    answer = await gate.ask("upload.begin", id="u1", session="c1", name="a.txt", size=1, mime="x", kind="file")
    assert answer["ok"] is False and "read-only" in answer["error"]
    assert not (home.path("runs") / "c1" / "uploads").exists()


@posix_only
async def test_a_broken_off_or_out_of_order_upload_leaves_no_file_behind(gate, home, linked_chat):
    folder = home.path("runs") / "c1" / "uploads"
    await gate.data("upload.begin", id="ua", session="c1", name="a.bin", size=3, mime="x", kind="file")
    await gate.data("upload.chunk", id="ua", seq=0, data=chunks(b"abc", 4)[0])
    await gate.data("upload.abort", id="ua")
    assert list(folder.iterdir()) == []
    await gate.data("upload.begin", id="ub", session="c1", name="b.bin", size=3, mime="x", kind="file")
    assert (await gate.ask("upload.chunk", id="ub", seq=5, data=chunks(b"abc", 4)[0]))["ok"] is False
    assert (await gate.ask("upload.chunk", id="ub", seq=0, data="!!not base64!!"))["ok"] is False
    assert list(folder.iterdir()) == []
    assert (await gate.ask("upload.end", id="ub"))["ok"] is False
    assert (await gate.ask("upload.abort", id="never-began"))["ok"] is True


@posix_only
async def test_a_voice_upload_gets_a_transcript_and_the_media_call_is_made_off_the_event_loop(gate, home, linked_chat, monkeypatch):
    calls, release = [], threading.Event()

    def media(home_, mode, file, folder):
        calls.append((mode, Path(file).name))
        assert release.wait(5)
        return {"kind": "audio", "report": str(folder / "report.md"), "transcript": {"text": "buy milk and eggs"}}

    monkeypatch.setattr(node, "run_media", media)
    uid = "uvoice"
    await gate.data("upload.begin", id=uid, session="c1", name="rec.webm", size=4, mime="audio/webm;codecs=opus", kind="voice")
    await gate.data("upload.chunk", id=uid, seq=0, data=chunks(b"data", 4)[0])
    since = len(gate.sent)
    rid = gate.put(type="upload.end", id=uid)
    gate.put(type="ping", id="while-busy")
    await gate.wait(lambda m: m.get("type") == "pong" and m.get("id") == "while-busy")      # the node answers while the call runs
    assert not [m for m in gate.sent[since:] if m.get("id") == rid]
    release.set()
    done = await gate.wait(lambda m: m.get("type") == "result" and m.get("id") == rid, since=since)
    assert done["data"]["transcript"] == "buy milk and eggs" and done["data"]["kind"] == "voice"
    assert done["data"]["marker"] == "[Anhang: runs/c1/uploads/rec.webm]" and calls == [("audio", "rec.webm")]


# ------------------------------------------------------------------------------------------------ what media makes of a file
def put(home, name: str, data: bytes = b"x") -> Path:
    folder = home.path("runs") / "c1" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def never(*args, **kwargs):
    raise AssertionError("media must not be called for this file")


def test_a_plain_image_and_an_unknown_file_are_only_marked(home):
    png = put(home, "pic.png")
    assert node.process_upload(home, png, "file", "image/png", never)["marker"] == "[Anhang: runs/c1/uploads/pic.png]"
    zipped = put(home, "data.zip")
    answer = node.process_upload(home, zipped, "file", "application/zip", never)
    assert answer["media"] == "file" and answer["marker"] == "[Anhang: runs/c1/uploads/data.zip]"


def test_a_heic_picture_is_made_a_png_and_the_marker_points_to_it(home):
    heic = put(home, "IMG_1.HEIC")

    def media(home_, mode, file, folder):
        assert mode == "image" and file == heic
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "image.png").write_bytes(b"png")
        return {"kind": "image", "image": str(folder / "image.png")}

    answer = node.process_upload(home, heic, "file", "image/heic", media)
    assert answer["marker"] == "[Anhang: runs/c1/uploads/IMG_1-image/image.png]" and "note" not in answer
    failed = node.process_upload(home, heic, "file", "image/heic", lambda *a: {"error": "no decoder"})
    assert failed["marker"] == "[Anhang: runs/c1/uploads/IMG_1.HEIC]" and "no decoder" in failed["note"]


def test_audio_and_video_get_a_transcript_a_missing_key_is_said_not_invented(home):
    clip = put(home, "talk.m4a")
    ok = node.process_upload(home, clip, "file", "audio/mp4", lambda *a: {"transcript": {"text": " hello there "}})
    assert ok["transcript"] == "hello there" and ok["media"] == "audio" and ok["marker"].endswith("talk.m4a]")
    keyless = node.process_upload(home, clip, "file", "audio/mp4", lambda *a: {"transcript": {"error": "ELEVENLABS_API_KEY missing"}})
    assert "transcript" not in keyless and "ELEVENLABS_API_KEY missing" in keyless["note"]
    broken = node.process_upload(home, clip, "file", "audio/mp4", lambda *a: {"error": "media failed"})
    assert "transcript" not in broken and "media failed" in broken["note"]
    movie = put(home, "clip.mp4")
    report = movie.parent / "clip-video" / "report.md"
    report.parent.mkdir()
    report.write_text("# report", "utf-8")
    video = node.process_upload(home, movie, "file", "video/mp4", lambda *a: {"report": str(report), "transcript": {"text": "words"}})
    assert video["marker"] == "[Anhang: runs/c1/uploads/clip-video/report.md]" and video["transcript"] == "words" and video["media"] == "video"


def test_a_recording_is_audio_whatever_its_container_says_and_a_document_gets_its_text(home):
    assert node.classify("rec.mp4", "video/mp4", "voice") == "audio" and node.classify("rec.webm", "", "voice") == "audio"
    assert node.classify("a.webm", "video/webm", "file") == "video" and node.classify("a.PDF", "", "file") == "doc"
    pdf = put(home, "paper.pdf")
    text = pdf.parent / "paper-doc" / "text.md"
    text.parent.mkdir()
    text.write_text("# paper", "utf-8")
    answer = node.process_upload(home, pdf, "file", "application/pdf", lambda *a: {"report": str(text)})
    assert answer["marker"].splitlines() == ["[Anhang: runs/c1/uploads/paper-doc/text.md]", "[Anhang: runs/c1/uploads/paper.pdf]"]
    assert upload.marker(pdf, home.root) == answer["marker"].splitlines()[1]


def test_the_media_call_gets_the_key_in_its_environment_never_on_the_command_line(home, monkeypatch):
    secret = home.root / "secrets" / "elevenlabs" / "default.env"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("ELEVENLABS_API_KEY=el-key-0123456789\n", "utf-8")
    monkeypatch.setenv("OTHER_API_KEY", "must-not-leak")
    monkeypatch.setenv("LM_DEPTH", "2")
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, '{"kind": "audio", "transcript": {"text": "ok"}}\n', ""

    def fake_run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return Done()

    monkeypatch.setattr(node.subprocess, "run", fake_run)
    clip = put(home, "a.ogg")
    result = node.run_media(home, "audio", clip, clip.parent / "out")
    assert result["transcript"]["text"] == "ok"
    command = seen["command"]
    assert command[1:3] == ["run", "--quiet"] and command[3].endswith(os.path.join("tools", "media", "media.py"))
    assert command[4:] == ["audio", str(clip), "--out", str(clip.parent / "out"), "--json"]
    assert not any("el-key-0123456789" in part for part in command)
    assert seen["env"]["ELEVENLABS_API_KEY"] == "el-key-0123456789"
    assert "OTHER_API_KEY" not in seen["env"] and "LM_DEPTH" not in seen["env"]


def test_a_media_call_that_fails_or_answers_nonsense_is_an_error_entry_not_a_crash(home, monkeypatch):
    clip = put(home, "a.ogg")

    class Failed:
        returncode, stdout, stderr = 1, "", "boom\nsecond line"

    monkeypatch.setattr(node.subprocess, "run", lambda *a, **k: Failed())
    assert "second line" in node.run_media(home, "audio", clip, clip.parent / "o")["error"]

    class Garbage:
        returncode, stdout, stderr = 0, "no json here", ""

    monkeypatch.setattr(node.subprocess, "run", lambda *a, **k: Garbage())
    assert node.run_media(home, "audio", clip, clip.parent / "o")["error"] == "media returned no JSON"


# ------------------------------------------------------------------------------------------------ Windows start path
def as_windows(monkeypatch, home: Home, launcher: bool) -> None:
    """Make node.py believe it runs on Windows (no tmux); the launcher `system/lm.cmd` exists or not."""
    monkeypatch.setattr(node, "on_windows", lambda: True)
    monkeypatch.setattr(node, "tmux_exe", lambda: "")
    monkeypatch.setattr(node.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    lm = home.root / "system" / "lm.cmd"
    lm.parent.mkdir(exist_ok=True)
    if launcher:
        lm.write_text("@echo off", "utf-8")
    else:
        lm.unlink(missing_ok=True)


def test_windows_hello_can_start_when_the_launcher_exists(home, monkeypatch):
    as_windows(monkeypatch, home, launcher=True)
    assert node.Node(home, lambda raw: None).hello()["can_start"] is True


def test_windows_hello_cannot_start_without_the_launcher_and_need_start_refuses(home, monkeypatch):
    as_windows(monkeypatch, home, launcher=False)
    n = node.Node(home, lambda raw: None)
    assert n.hello()["can_start"] is False
    with pytest.raises(node.Refuse, match="lm.cmd"):
        n.need_start()


def test_windows_need_start_accepts_with_the_launcher(home, monkeypatch):
    as_windows(monkeypatch, home, launcher=True)
    node.Node(home, lambda raw: None).need_start()


def test_windows_start_runs_lm_cmd_open_in_a_new_console(home, monkeypatch):
    as_windows(monkeypatch, home, launcher=True)
    seen = {}
    monkeypatch.setattr(node.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    node.start_chat(home, "c1", "phone-c1")
    assert seen["cmd"] == [str(home.root / "system" / "lm.cmd"), "open", "c1", "--rc", "phone-c1"]
    assert seen["kw"]["creationflags"] == 16 and seen["kw"]["cwd"] == str(home.root)


def test_windows_start_without_the_launcher_is_a_runtime_error(home, monkeypatch):
    as_windows(monkeypatch, home, launcher=False)
    with pytest.raises(RuntimeError, match="lm.cmd"):
        node.start_chat(home, "c1", "phone-c1")
