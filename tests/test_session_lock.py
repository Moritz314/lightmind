"""One writer per chat: the session lock in the state folder, the read-only second open, the hand-over to a new tab.
The "other process" is a real Python process (it holds the lock and ends when its stdin closes: nothing is killed)."""
import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

from lightmind import loop as L
from lightmind import runs
from lightmind.model import Text, Usage

from test_loop_session import fakes, script, shome          # noqa: F401  (fixtures)
from test_loop_support import collect

CHILD = """
import os, sys
from lightmind import runs
from lightmind.files import Home
h = Home(sys.argv[1])
print(runs.acquire_lock(h, sys.argv[2]), os.getpid(), flush=True)
sys.stdin.readline()
"""
SLEEPER = "import os, sys; print('up', os.getpid(), flush=True); sys.stdin.readline()"


class Other:
    """A live process: `pid` is the one it reports itself (on Windows `Popen.pid` is the venv launcher's, not the interpreter's)."""
    def __init__(self, proc, line):
        self.proc = proc
        self.first, self.pid = line.split()[0], int(line.split()[1])


@contextmanager
def other_process(code, *args):
    """A live process (its own pid and start time) that ends by itself when we close its stdin."""
    p = subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        other = Other(p, p.stdout.readline().strip())
        yield other, other.first
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        p.wait(timeout=60)


def finished_pid() -> int:
    """The pid of a process that has ended."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=60)
    return p.pid


def snapshot(*roots):
    """Every file under the folders: bytes and modification time (a read must change neither)."""
    out = {}
    for root in roots:
        for f in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if f.is_file():
                out[str(f)] = (f.read_bytes(), f.stat().st_mtime_ns)
    return out


def locks(home):
    return sorted(p.name for p in (home.path("state") / "sessions").glob("*.lock"))


# ------------------------------------------------------------------ the lock itself
def test_a_lock_names_this_process_and_its_start(shome):
    assert runs.acquire_lock(shome, "chat-1") == 0
    pid, stamp = runs._holder(runs.lock_file(shome, "chat-1"))
    assert pid == os.getpid() and stamp == runs.process_stamp(os.getpid())
    assert runs.lock_holder(shome, "chat-1") == os.getpid()
    assert runs.acquire_lock(shome, "chat-1") == 0                           # again: it is ours already
    runs.release_lock(shome, "chat-1")
    assert not runs.lock_file(shome, "chat-1").exists() and runs.lock_holder(shome, "chat-1") == 0


def test_the_lock_holds_against_a_second_process_and_is_taken_over_when_that_one_is_gone(shome):
    with other_process(CHILD, str(shome.root), "chat-2") as (child, first):
        assert first == "0"                                                  # the child has the chat
        assert runs.acquire_lock(shome, "chat-2") == child.pid               # we are told who
        assert runs.lock_holder(shome, "chat-2") == child.pid
        assert runs.lock_file(shome, "chat-2").read_text().split()[0] == str(child.pid)    # and nothing was overwritten
        runs.release_lock(shome, "chat-2")                                   # a lock that is not ours is never removed
        assert runs.lock_holder(shome, "chat-2") == child.pid
    assert runs.lock_holder(shome, "chat-2") == 0                            # the child ended without releasing: an orphan
    assert runs.acquire_lock(shome, "chat-2") == 0
    assert runs.lock_file(shome, "chat-2").read_text().split()[0] == str(os.getpid())


def test_the_lock_of_a_process_that_is_gone_counts_as_none(shome):
    dead = finished_pid()
    f = runs.lock_file(shome, "chat-3")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{dead} 123456\n")
    assert runs.lock_holder(shome, "chat-3") == 0
    assert runs.acquire_lock(shome, "chat-3") == 0 and runs._holder(f)[0] == os.getpid()


def test_a_pid_that_another_program_has_now_does_not_hold_a_chat(shome):
    with other_process(SLEEPER) as (child, _):
        stamp = runs.process_stamp(child.pid)
        assert stamp, "this platform can tell when a process started"
        f = runs.lock_file(shome, "chat-4")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{child.pid} {stamp}\n")
        assert runs.lock_holder(shome, "chat-4") == child.pid                # the right start time: the same process
        f.write_text(f"{child.pid} 1{stamp}\n")
        assert runs.lock_holder(shome, "chat-4") == 0                        # the pid was reused: not the process that wrote it
        assert runs.acquire_lock(shome, "chat-4") == 0 and runs._holder(f)[0] == os.getpid()


@pytest.mark.parametrize("text", ["", "garbage", "0 0", "-5 x", "notanumber 1"])
def test_a_lock_that_cannot_be_read_holds_nothing(shome, text):
    f = runs.lock_file(shome, "chat-5")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    assert runs.lock_holder(shome, "chat-5") == 0 and runs.acquire_lock(shome, "chat-5") == 0


# ------------------------------------------------------------------ sessions take and give it up
def test_a_session_holds_its_lock_until_it_ends(shome, fakes):
    s = L.open_session(shome)
    assert locks(shome) == [f"{s.id}.lock"] and runs.lock_holder(shome, s.id) == os.getpid() and not s.readonly
    s.close()
    assert locks(shome) == []


def test_a_session_that_cannot_be_built_leaves_no_lock(shome, monkeypatch):
    def boom(home, account, dispatcher=None):
        raise RuntimeError("no provider")
    monkeypatch.setattr(L, "make_provider", boom)
    with pytest.raises(RuntimeError):
        L.open_session(shome)
    assert locks(shome) == []


async def test_resume_takes_the_chat_over_and_writes_the_pid_anew(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("a1"), Usage(10, 2)])
    await collect(s.send("hello"))
    s.close()
    meta = s.run.path / "meta.json"
    old = json.loads(meta.read_text())
    meta.write_text(json.dumps({**old, "pid": finished_pid()}))               # what the process that closed it left behind
    r = L.resume_session(shome, s.id)
    assert not r.readonly and r.readonly_by == 0 and locks(shome) == [f"{s.id}.lock"]
    now = json.loads(meta.read_text())
    assert now["pid"] == os.getpid() and now["status"] == "running" and now["end"] == ""
    r.close()
    assert locks(shome) == [] and json.loads(meta.read_text())["status"] == "done"


async def test_a_resume_that_fails_after_the_lock_gives_it_back(shome, fakes, monkeypatch):
    s = L.open_session(shome)
    s.close()
    def boom(home, account, dispatcher=None):
        raise RuntimeError("no provider")
    monkeypatch.setattr(L, "make_provider", boom)
    with pytest.raises(RuntimeError):
        L.resume_session(shome, s.id)
    assert locks(shome) == []


# ------------------------------------------------------------------ the second open: read only, and nothing is written
async def test_a_chat_that_a_live_process_has_open_is_shown_read_only_and_nothing_is_written(shome, fakes):
    s = L.open_session(shome)
    script(fakes["abo"], [Text("the answer"), Usage(10, 2)])
    await collect(s.send("the question"))
    s.close()
    with other_process(CHILD, str(shome.root), s.id) as (child, first):
        assert first == "0"
        lock_before = runs.lock_file(shome, s.id).read_bytes()
        before = snapshot(shome.path("runs") / s.id, runs.trusted_file(shome, s.id))
        r = L.resume_session(shome, s.id)
        assert r.readonly and r.readonly_by == child.pid and r.provider is None
        assert [(m.role, m.text) for m in r.messages] == [("user", "the question"), ("assistant", "the answer")]
        assert r.info.id == s.id and r.info.status == "done"                  # what the run says, not "running" because of us
        with pytest.raises(L.ReadOnlyChat, match=str(child.pid)):
            async for _ in r.send("a second writer"):
                pass
        with pytest.raises(L.ReadOnlyChat):
            r.set(mode="plan")
        assert r.close() == [] and r.closed                                   # no hooks, no end marker, no lock removed
        r.hand_over()
        assert snapshot(shome.path("runs") / s.id, runs.trusted_file(shome, s.id)) == before
        assert runs.lock_file(shome, s.id).read_bytes() == lock_before        # the other process keeps its chat
    assert fakes["abo"].requests[0].messages[-1].text == "the question"       # the read-only session never asked the model


async def test_a_trailing_tool_call_is_not_closed_by_a_session_that_only_reads(shome, fakes):
    """The other process may be in the middle of that tool call: closing it with a "cancelled" result is its business."""
    s = L.open_session(shome)
    from lightmind.model import Message, ToolCall
    L._log_message(s.run, Message("user", "go"))
    L._log_message(s.run, Message("assistant", "", [ToolCall("c1", "bash", {"command": "sleep 1"})]))
    s.close()
    with other_process(CHILD, str(shome.root), s.id):
        r = L.resume_session(shome, s.id)
        assert r.readonly and [m.role for m in r.messages] == ["user", "assistant"]
        text = (shome.path("runs") / s.id / "events.jsonl").read_text()
        assert "cancelled" not in text
    again = L.resume_session(shome, s.id)                                     # the other process is gone: now it is ours to repair
    assert not again.readonly and [m.role for m in again.messages] == ["user", "assistant", "tool"]
    again.close()


# ------------------------------------------------------------------ handing a chat over
def test_hand_over_releases_the_chat_without_ending_it_and_the_next_process_takes_it(shome, fakes, monkeypatch):
    ended = []
    monkeypatch.setattr(L, "run_on_end", lambda home, s: ended.append(s.id) or [])
    s = L.open_session(shome, mode="plan", effort="high", model="strong")
    s.hand_over()
    meta = json.loads((s.run.path / "meta.json").read_text())
    assert s.closed and ended == [] and locks(shome) == []                    # not ended: no hook, and the lock is free
    assert meta["status"] == "paused" and meta["end"] == "" and meta["pid"] == os.getpid()
    assert not (s.run.path / "log.md").exists() or "lm:end" not in (s.run.path / "log.md").read_text()
    s.hand_over()                                                             # twice is harmless
    r = L.resume_session(shome, s.id)                                         # what `lm open <id>` does in the new tab
    assert not r.readonly and (r.info.mode, r.info.effort, r.info.model) == ("plan", "high", "opus")   # the setup's choices came along
    assert json.loads((r.run.path / "meta.json").read_text())["status"] == "running"
    r.close()
    assert ended == [s.id]                                                    # the hooks run when the chat really ends


def test_a_chat_handed_over_and_taken_by_a_live_process_is_read_only_for_a_third(shome, fakes):
    s = L.open_session(shome)
    s.hand_over()
    with other_process(CHILD, str(shome.root), s.id) as (child, first):
        assert first == "0"
        assert L.resume_session(shome, s.id).readonly_by == child.pid


# ------------------------------------------------------------------ an agent cannot forge or free a lock
@pytest.mark.parametrize("mode", ["auto", "bypass", "plan"])
def test_an_agent_cannot_write_or_edit_a_lock_in_any_mode(shome, mode):
    from lightmind import rules as R
    from lightmind.model import RuleContext
    layers = R.build_layers(shome, None, mode, None)
    ctx = RuleContext(home=str(shome.root), run_dir=str(shome.root / "runs" / "r1"))
    for tool in ("write", "edit"):
        for name in ("20261021-101010-chat-aaaa.lock", ".lock"):
            path = f"{shome.rel('state')}/sessions/{name}"
            call = R.checks_for_call(tool, {"path": path, "old": "a", "new": "b"})
            assert R.decide_all(layers, call, ctx).effect == "deny", (mode, tool, name)
