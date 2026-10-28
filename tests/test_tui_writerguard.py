"""The output thread of Textual dies: the app must say so and end, never hang on a black screen (writerguard.py)."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from lightmind.tui import app as orbit
from lightmind.tui import writerguard as wg
from textual.drivers._writer_thread import WriterThread

import lightmind.loop as L
from lightmind import runs
from test_tui_support import FakeProv, idle, install, open_chat, quiet, running, until


class BrokenFile:
    """A terminal whose write fails: the way the output thread of homeserver died (the real cause is not known)."""

    def write(self, text):
        raise UnicodeEncodeError("ascii", text, 0, 1, "boom: cannot encode")

    def flush(self):
        pass


class FakeApp:
    """What the guard needs of an app: `exit`, `_loop`, `chat`."""

    def __init__(self):
        self._loop = None
        self.chat = None
        self.exits: list[int] = []

    def exit(self, result=None, return_code=0, message=None):
        self.exits.append(return_code)


@pytest.fixture
def guard(home, monkeypatch):
    """A guard on a fake app; the patched WriterThread is restored after the test so no other test sees the guard."""
    saved = {k: getattr(WriterThread, k) for k in ("run", "write", "stop")}
    saved_original, saved_active = dict(wg._original), wg._active
    wg._original.clear()
    g = wg.install(FakeApp(), home)
    yield g
    for k, v in saved.items():
        setattr(WriterThread, k, v)
    wg._original.clear()
    wg._original.update(saved_original)
    wg._active = saved_active


def dead_thread() -> WriterThread:
    t = WriterThread(BrokenFile())
    t.start()
    t.write("first screen")
    t.join(5)
    assert not t.is_alive()
    return t


def within(seconds: float, fn) -> bool:
    """True when `fn` came back in time; a call that hangs is left to a daemon thread."""
    done = threading.Event()
    threading.Thread(target=lambda: (fn(), done.set()), daemon=True).start()
    return done.wait(seconds)


def test_exception_in_the_thread_is_logged_with_its_traceback(guard):
    dead_thread()
    assert isinstance(guard.failure, UnicodeEncodeError)
    log = guard.path.read_text("utf-8")
    assert "boom: cannot encode" in log and "Traceback" in log and "test_tui_writerguard.py" in log and "textual-output" in log
    assert guard.app.exits == [1]                    # the app was told to end with a code that is not 0


def test_write_after_the_death_does_not_block(guard):
    t = dead_thread()
    assert within(5, lambda: [t.write("x" * 100) for _ in range(200)])


def test_without_the_guard_the_same_thing_hangs(guard, monkeypatch):
    """The counter-check: the unpatched `write` blocks once the queue is full - what the user saw as a black screen."""
    t = dead_thread()
    monkeypatch.setattr(WriterThread, "write", wg._original["write"])
    assert not within(1.5, lambda: [t.write("x") for _ in range(200)])


def test_stop_after_the_death_returns(guard):
    t = dead_thread()
    t.write("x")
    assert within(5, t.stop)


def test_a_healthy_thread_still_writes(guard):
    class Out:
        text = ""

        def write(self, text):
            self.text += text

        def flush(self):
            pass

    out = Out()
    t = WriterThread(out)
    t.start()
    t.write("hello ")
    t.write("world")
    t.stop()
    assert out.text == "hello world" and guard.failure is None and guard.app.exits == []


def test_report_is_silent_without_a_failure(guard, capsys):
    assert guard.report() == 0
    assert capsys.readouterr().err == ""


def test_report_says_it_in_german_and_names_the_log(guard, capsys):
    dead_thread()
    assert guard.report() == 1
    err = capsys.readouterr().err
    assert "Bildschirmausgabe abgestürzt" in err and str(guard.path) in err


def test_main_returns_1_when_the_output_died(home, monkeypatch, capsys, guard):
    """`main`: the app ended (here: replaced by a run that only kills the output), exit code 1 and the message on stderr."""
    monkeypatch.setattr(orbit, "anim_allowed", lambda: False)
    monkeypatch.setattr(wg, "install", lambda app, home: guard)

    def run(self, **kw):
        guard.app = self
        dead_thread()
    monkeypatch.setattr(orbit.Orbit, "run", run)
    assert orbit.main(home) == 1
    assert "Bildschirmausgabe abgestürzt" in capsys.readouterr().err


async def test_the_running_app_ends_by_itself(home, monkeypatch, guard):
    quiet(home)
    async with running(home) as (app, pilot):
        guard.app = app
        await asyncio.to_thread(dead_thread)
        await until(pilot, lambda: app.return_code == 1)


async def test_the_open_chat_is_saved_and_can_be_resumed(home, monkeypatch, capsys, guard):
    quiet(home)
    install(monkeypatch, FakeProv())
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await idle(pilot, app)
        guard.app = app
        sid = app.chat.s.id
        await asyncio.to_thread(dead_thread)
        await until(pilot, lambda: app.return_code == 1)
    assert guard.report() == 1                       # after the app: stderr is the real one again, as in `main`
    assert guard.closed_chat and "lässt sich über Resume fortsetzen" in capsys.readouterr().err
    meta = runs.show_run(home, sid)["run"]
    assert meta["status"] == "paused" and meta["exit_reason"] == "screen output crashed"
    again = L.resume_session(home, sid)
    assert not again.readonly
    again.close()
