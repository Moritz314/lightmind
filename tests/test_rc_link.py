"""The Remote Control link (rc.py) with a test client: hello, events with a rising seq, masking, requests, questions, the
files, the token on TCP.  No Textual: the chat is a real session with a scripted provider."""
import asyncio
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

import pytest

from lightmind import loop as L
from lightmind import model as m
from lightmind import rc
from lightmind.providers.base import ToolDone

from test_tui_support import FakeProv, install

SECRET = "abcdef123456XYZ"
UNIX = pytest.mark.skipif(os.name == "nt", reason="unix sockets")


@pytest.fixture
def sess(home, monkeypatch):
    install(monkeypatch, FakeProv())
    s = L.open_session(home)
    s.dispatcher.masker.add("svc/main", "KEY", SECRET)
    yield s
    s.close()


class Client:
    """A node's side of the link: lines in, lines out.  What `until` skips stays for the next `read` or `until`."""

    def __init__(self, reader, writer):
        self.r, self.w, self.raw, self.seen, self.pending = reader, writer, b"", [], []

    async def fetch(self, timeout: float) -> dict:
        line = await asyncio.wait_for(self.r.readline(), timeout)
        assert line, "the link closed the connection"
        self.raw += line
        msg = json.loads(line)
        self.seen.append(msg)
        return msg

    async def read(self, timeout: float = 3.0) -> dict:
        return self.pending.pop(0) if self.pending else await self.fetch(timeout)

    async def until(self, pred, timeout: float = 3.0) -> dict:
        for i, msg in enumerate(self.pending):
            if pred(msg):
                return self.pending.pop(i)
        end = time.monotonic() + timeout
        while True:
            msg = await self.fetch(max(end - time.monotonic(), 0.05))
            if pred(msg):
                return msg
            self.pending.append(msg)

    def send(self, type_: str, **fields) -> None:
        self.w.write(json.dumps({"type": type_, **fields}).encode() + b"\n")

    async def call(self, type_: str, rid: str = "r1", **fields) -> dict:
        self.send(type_, id=rid, **fields)
        return await self.until(lambda x: x.get("type") == "result" and x.get("id") == rid)

    async def closed(self, timeout: float = 3.0) -> bool:
        try:
            while True:
                if not await asyncio.wait_for(self.r.readline(), timeout):
                    return True
        except (ConnectionError, asyncio.TimeoutError):
            return False

    def close(self) -> None:
        self.w.close()


def meta_of(home, sid) -> dict:
    return json.loads((rc.links_dir(home) / f"{sid}.json").read_text("utf-8"))


async def connect(home, sid, token: str | None = "") -> Client:
    """Connect as the node does: through what the link file says."""
    meta = meta_of(home, sid)
    if meta["transport"] == "unix":
        r, w = await asyncio.open_unix_connection(meta["endpoint"])
    else:
        host, port = meta["endpoint"].rsplit(":", 1)
        r, w = await asyncio.open_connection(host, int(port))
        w.write(json.dumps({"type": "auth", "token": meta["token"] if token == "" else token}).encode() + b"\n")
    return Client(r, w)


async def opened(home, s, handler=None, on_close=None, tcp=False, name="Test"):
    link = await rc.Link.start(home, s, name, "tui", handler=handler, on_close=on_close, tcp=tcp)
    return link, await connect(home, s.id)


# ------------------------------------------------------------------------------------------ hello, events, seq
@UNIX
async def test_hello_comes_first_then_events_with_a_rising_seq(home, sess):
    link, c = await opened(home, sess)
    hello = await c.read()
    assert hello["type"] == "link.hello" and hello["version"] == 1 and hello["session"] == sess.id
    assert hello["name"] == "Test" and hello["origin"] == "tui" and hello["pid"] == os.getpid()
    assert set(hello["status"]) == {"model", "effort", "mode", "account", "project"} and hello["busy"] is False
    link.turn_start()
    link.events(m.Text("hello there"))
    link.events(m.ToolCall("c1", "read", {"path": "a.txt"}))
    link.events(ToolDone("c1", "read", True, "line one\nline two"))
    link.turn_end("done", 0.0123, 0.4)
    got = [await c.read() for _ in range(5)]
    assert [g["type"] for g in got] == ["turn.start", "text", "tool.start", "tool.end", "turn.end"]
    seqs = [g["seq"] for g in got]
    assert seqs == sorted(set(seqs)) and seqs[0] == hello["seq"] + 1
    assert got[1]["text"] == "hello there" and got[1]["turn"] == "t1" and got[2]["name"] == "read"
    assert got[4]["status"] == "done" and got[4]["cost_usd"] == 0.0123 and got[4]["context_used"] == 0.4
    c.close()
    await link.close()


@UNIX
async def test_a_secret_never_reaches_the_socket_in_events_results_or_hello(home, sess):
    link, c = await opened(home, sess)
    await c.read()
    unmasked = json.dumps(link.tr.event(m.Text(f"the key is {SECRET}"), "t1"))
    assert SECRET in unmasked                                       # (the translator itself does not hide it ...)
    link.turn_start()
    link.events(m.Text(f"the key is {SECRET}"))
    link.events(m.ToolCall("c1", "bash", {"command": f"echo {SECRET}"}))
    link.events(ToolDone("c1", "bash", True, SECRET))
    link.user_text(f"typed {SECRET}")
    link.notice(f"note {SECRET}")
    res = await c.call("send", text="x")                              # no handler: an error text that echoes the type
    link.emit("title-test", title=SECRET)
    await asyncio.sleep(0.2)
    await c.until(lambda x: x.get("type") == "title-test")
    assert SECRET.encode() not in c.raw                             # ... the link does)
    assert b"the key is " in c.raw and res["ok"] is False
    c.close()
    await link.close()


@UNIX
async def test_masking_follows_a_secret_the_chat_learns_later(home, sess):
    link, c = await opened(home, sess)
    await c.read()
    late = "late-secret-value-123"
    link.emit("notice", level="info", text=late)
    assert (await c.until(lambda x: x["type"] == "notice"))["text"] == late
    sess.dispatcher.masker.add("svc/main", "LATE", late)
    link.emit("notice", level="info", text=late)
    assert late not in (await c.until(lambda x: x["type"] == "notice"))["text"]
    c.close()
    await link.close()


# ------------------------------------------------------------------------------------------ requests
@UNIX
async def test_ping_history_and_unknown_requests(home, sess):
    link, c = await opened(home, sess)
    await c.read()
    assert (await c.call("ping"))["data"] == {"pong": True}
    h = await c.call("history", "h1")
    assert h["ok"] and h["data"]["history"] == [] and h["data"]["seq"] == link.seq
    bad = await c.call("session.destroy", "r9")
    assert bad["ok"] is False and "unknown type" in bad["error"]
    c.w.write(b"this is not json\n")
    err = await c.until(lambda x: x.get("type") == "result" and x.get("ok") is False)
    assert "JSON" in err["error"] or "Expecting" in err["error"] or "Extra" in err["error"]
    c.close()
    await link.close()


@UNIX
async def test_requests_reach_the_handler_one_by_one_in_order_with_a_clean_device_name(home, sess):
    seen, gate = [], asyncio.Event()

    async def handler(msg):
        seen.append((msg["type"], msg.get("text"), msg["from"]))
        if msg.get("text") == "slow":
            await gate.wait()
        if msg.get("text") == "no":
            raise rc.Refused("not now")
        return {"got": msg.get("text")}
    link, c = await opened(home, sess, handler)
    await c.read()
    c.send("send", id="a", text="slow", **{"from": "Pixel\x1b[31m 8\n" + "x" * 100})
    c.send("send", id="b", text="second")
    c.send("send", id="c", text="no")
    await asyncio.sleep(0.3)
    assert [s[1] for s in seen] == ["slow"]                          # b waits behind a: one writer
    gate.set()
    res = {}
    for _ in range(3):
        r = await c.until(lambda x: x.get("type") == "result")
        res[r["id"]] = r
    assert [s[1] for s in seen] == ["slow", "second", "no"]
    assert res["a"]["ok"] and res["a"]["data"] == {"got": "slow"} and res["b"]["data"] == {"got": "second"}
    assert res["c"] == {"type": "result", "id": "c", "ok": False, "error": "not now"}
    who = seen[0][2]
    assert "\x1b" not in who and "\n" not in who and len(who) <= rc.DEVICE_MAX and who.startswith("Pixel")
    c.send("send", id="d", text="ok")
    assert (await c.until(lambda x: x.get("id") == "d"))["data"] == {"got": "ok"}
    assert seen[-1][2] == "phone"                                      # no name: phone
    c.close()
    await link.close()


@UNIX
async def test_a_handler_that_fails_does_not_take_the_link_down(home, sess):
    async def handler(msg):
        raise RuntimeError("boom")
    link, c = await opened(home, sess, handler)
    await c.read()
    r = await c.call("send", text="x")
    assert r["ok"] is False and "RuntimeError: boom" in r["error"]
    assert (await c.call("ping", "p"))["ok"]
    c.close()
    await link.close()


@UNIX
async def test_rc_off_answers_then_closes_the_link_and_calls_on_close(home, sess):
    ended = []
    link, c = await opened(home, sess, on_close=ended.append)
    await c.read()
    r = await c.call("rc.off")
    assert r["ok"]
    last = await c.until(lambda x: x["type"] == "link.closed")
    assert last["reason"] == "off"
    assert await c.closed()
    await asyncio.sleep(0.1)
    assert ended == ["off"] and link.closed
    assert not (rc.links_dir(home) / f"{sess.id}.json").exists()


# ------------------------------------------------------------------------------------------ status, busy
@UNIX
async def test_status_and_busy_are_sent_only_when_they_change(home, sess):
    link, c = await opened(home, sess)
    await c.read()
    link.sync(False, 0)                                              # nothing changed: nothing is sent
    link.sync(True, 0)
    link.sync(True, 0)
    link.sync(True, 2)
    sess.set(effort="high")
    link.sync(True, 2)
    link.sync(False, 0)
    got = []
    while len(got) < 4:
        got.append(await c.read())
    assert [(g["type"], g.get("busy"), g.get("queued"), g.get("effort")) for g in got] == [
        ("busy", True, 0, None), ("busy", True, 2, None), ("status", None, None, "high"), ("busy", False, 0, None)]
    assert await c.call("ping", "z")                                  # and nothing else was in between
    assert [x["type"] for x in c.seen if x["type"] in ("status", "busy")] == ["busy", "busy", "status", "busy"]
    c.close()
    await link.close()


# ------------------------------------------------------------------------------------------ questions
@UNIX
async def test_a_question_is_won_by_the_phone_and_the_keyboard_side_is_cancelled(home, sess):
    local_cancelled = asyncio.Event()

    async def keyboard():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            local_cancelled.set()
            raise
    link, c = await opened(home, sess)
    await c.read()
    task = asyncio.create_task(link.ask("permission", "run rm?", "rm -rf x", ["yes", "always", "no"], "no", keyboard()))
    q = await c.until(lambda x: x["type"] == "question")
    assert q["kind"] == "permission" and q["choices"] == ["yes", "always", "no"] and q["detail"] == "rm -rf x"
    bad = await c.call("answer", "r1", question=q["question"], choice="maybe")
    assert bad["ok"] is False and "yes, always, no" in bad["error"]
    assert (await c.call("answer", "r2", question=q["question"], choice="yes", **{"from": "Pixel"}))["ok"]
    assert await task == "yes"
    closed = await c.until(lambda x: x["type"] == "question.closed")
    assert closed["question"] == q["question"] and closed["choice"] == "yes" and closed["by"] == "Pixel"
    assert local_cancelled.is_set()
    late = await c.call("answer", "r3", question=q["question"], choice="no")
    assert late["ok"] is False and late["error"] == "already answered"
    c.close()
    await link.close()


@UNIX
async def test_a_question_is_won_by_the_keyboard_and_says_so(home, sess):
    answer = asyncio.get_running_loop().create_future()
    link, c = await opened(home, sess)
    await c.read()
    task = asyncio.create_task(link.ask("permission", "run it?", "", ["yes", "always", "no"], "no", answer))
    q = await c.until(lambda x: x["type"] == "question")
    assert "detail" not in q
    answer.set_result("always")
    assert await task == "always"
    closed = await c.until(lambda x: x["type"] == "question.closed")
    assert (closed["choice"], closed["by"]) == ("always", "tui")
    assert (await c.call("answer", "r1", question=q["question"], choice="no"))["error"] == "already answered"
    c.close()
    await link.close()


@UNIX
async def test_a_free_text_answer_and_an_unknown_question(home, sess):
    link, c = await opened(home, sess)
    await c.read()
    task = asyncio.create_task(link.ask("ask", "which color?", "", [], "(none)"))
    q = await c.until(lambda x: x["type"] == "question")
    assert q["kind"] == "ask" and q["choices"] == []
    assert (await c.call("answer", "r1", question="q99", choice="x"))["error"].startswith("no open question")
    assert (await c.call("answer", "r2", question=q["question"], choice="any words at all"))["ok"]
    assert await task == "any words at all"
    c.close()
    await link.close()


@UNIX
async def test_a_question_nobody_answers_gets_its_default(home, sess, monkeypatch):
    monkeypatch.setattr(rc, "ASK_TIMEOUT", 0.2)
    # the template sets run.ask_timeout: 30m, which beats the constant: ignore the setting so the short default applies
    monkeypatch.setattr(rc.core, "duration", lambda value, default: default)
    link, c = await opened(home, sess)
    await c.read()
    assert await link.ask("permission", "x?", "", ["yes", "always", "no"], "no") == "no"
    closed = await c.until(lambda x: x["type"] == "question.closed")
    assert (closed["choice"], closed["by"]) == ("no", "timeout")
    c.close()
    await link.close()


@UNIX
async def test_a_client_that_comes_late_sees_the_open_question_and_a_closed_link_leaves_the_keyboard_alone(home, sess):
    answer = asyncio.get_running_loop().create_future()
    link, c = await opened(home, sess)
    await c.read()
    task = asyncio.create_task(link.ask("permission", "run it?", "", ["yes", "always", "no"], "no", answer))
    q = await c.until(lambda x: x["type"] == "question")
    c2 = await connect(home, sess.id)
    assert (await c2.read())["type"] == "link.hello"
    again = await c2.read()
    assert again["type"] == "question" and again["question"] == q["question"] and again["seq"] == q["seq"]
    await link.close("off")                                          # the link goes, the question stays with the keyboard
    assert not task.done()
    answer.set_result("yes")
    assert await task == "yes"
    c.close()
    c2.close()


@UNIX
async def test_a_question_with_a_closed_link_and_no_keyboard_is_denied(home, sess):
    link, c = await opened(home, sess)
    await c.read()
    task = asyncio.create_task(link.ask("permission", "run it?", "", ["yes", "always", "no"], "no"))
    await c.until(lambda x: x["type"] == "question")
    await link.close("off")
    assert await asyncio.wait_for(task, 2) == "no"
    c.close()


# ------------------------------------------------------------------------------------------ files and transport
@UNIX
async def test_start_writes_socket_and_json_and_close_removes_them(home, sess):
    link = await rc.Link.start(home, sess, "  A  name\x00 with   blanks ", "tui", tcp=False)
    d = rc.links_dir(home)
    meta = meta_of(home, sess.id)
    assert meta["session"] == sess.id and meta["pid"] == os.getpid() and meta["origin"] == "tui" and meta["version"] == 1
    assert meta["name"] == "A name with blanks" and "token" not in meta and meta["transport"] == "unix"
    from lightmind import runs
    assert meta["stamp"] == runs.process_stamp(os.getpid())
    sock = Path(meta["endpoint"])
    assert sock.exists() and stat.S_IMODE(sock.stat().st_mode) == 0o600
    assert stat.S_IMODE((d / f"{sess.id}.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    await link.close("ended")
    assert not sock.exists() and not (d / f"{sess.id}.json").exists()
    await link.close("again")                                         # a second close does nothing


@UNIX
async def test_a_socket_path_too_long_for_the_system_lies_in_a_short_folder_and_is_cleaned(home, sess, monkeypatch):
    # pytest's own tmp_path (with xdist) is itself too long for a socket, so the short folder must come from /tmp
    short = Path(tempfile.mkdtemp(prefix="lmt", dir="/tmp"))
    monkeypatch.setattr(rc, "SOCK_MAX", 10)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(short))
    link, c = await opened(home, sess)
    meta = meta_of(home, sess.id)
    assert meta["endpoint"].startswith(str(short)) and Path(meta["endpoint"]).exists()
    assert (await c.read())["type"] == "link.hello"
    c.close()
    await link.close()
    assert not Path(meta["endpoint"]).exists()
    shutil.rmtree(short, ignore_errors=True)


async def test_tcp_asks_for_the_token_and_the_file_carries_it(home, sess):
    link = await rc.Link.start(home, sess, "Test", "tui", tcp=True)
    meta = meta_of(home, sess.id)
    assert meta["transport"] == "tcp" and meta["endpoint"].startswith("127.0.0.1:") and len(meta["token"]) >= 20
    wrong = await connect(home, sess.id, token="not-the-token")
    assert await wrong.closed()                                       # no hello, closed
    assert wrong.raw == b""
    none = await asyncio.open_connection(*meta["endpoint"].rsplit(":", 1))
    none[1].write(b'{"type":"ping","id":"x"}\n')                      # the first line is not an auth
    assert await asyncio.wait_for(none[0].readline(), 3) == b""
    good = await connect(home, sess.id)
    assert (await good.read())["type"] == "link.hello"
    assert (await good.call("ping"))["data"] == {"pong": True}
    good.close()
    await link.close()
    assert not (rc.links_dir(home) / f"{sess.id}.json").exists()


async def test_a_line_longer_than_the_limit_drops_that_client_only(home, sess, monkeypatch):
    monkeypatch.setattr(rc, "LINE_MAX", 2000)
    link = await rc.Link.start(home, sess, "Test", "tui", tcp=True)
    c, other = await connect(home, sess.id), await connect(home, sess.id)
    await c.read()
    await other.read()
    c.w.write(b"x" * 5000 + b"\n")
    assert await c.closed()
    assert (await other.call("ping"))["ok"]
    other.close()
    await link.close()


async def test_only_a_tui_or_vscode_chat_can_open_a_link_and_a_crashed_links_files_are_replaced(home, sess):
    with pytest.raises(ValueError):
        await rc.Link.start(home, sess, "x", "phone", tcp=True)
    d = rc.links_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sess.id}.json").write_text("{}", "utf-8")
    (d / f"{sess.id}.sock").write_text("", "utf-8")
    link = await rc.Link.start(home, sess, "x", "tui", tcp=True)
    assert meta_of(home, sess.id)["name"] == "x"
    await link.close()
    assert not (d / f"{sess.id}.sock").exists()


def test_enabled_reads_remote_enabled_of_config_md(home):
    from lightmind import settings
    from lightmind.files import Home
    assert rc.enabled(home) is False                                  # shipped: the group is commented out
    cfg = home.root / "config.md"
    for value in (True, False):
        text = cfg.read_bytes().decode("utf-8")
        cfg.write_bytes(settings.edit_text(text, {("remote", "enabled"): value}).encode("utf-8"))
        assert rc.enabled(Home(home.root)) is value


def test_names_and_devices_are_one_clean_line():
    assert rc.clean_text("a\tb\x1b[0m  c\n", 80) == "a b [0m c"
    assert rc.clean_text("x" * 200, 10) == "x" * 10
    assert rc.clean_device(None) == "phone" and rc.clean_device("  ") == "phone" and rc.clean_device("Pixel 8") == "Pixel 8"
