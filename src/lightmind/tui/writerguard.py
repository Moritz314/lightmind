"""Guard against Textual's output thread ("textual-output") dying: without it the queue to that thread fills up, the UI
thread blocks in `queue.put` for good and the user sees a black screen with no message (seen on a server).

The guard wraps `WriterThread.run/write/stop` of Textual (the file itself stays untouched): an exception in the thread is
written to a log with its traceback, the queue is emptied, later writes are dropped instead of blocking, and the app is told
to end with exit code 1.  After the terminal is back, `WriterGuard.report` closes the open chat (so `resume` finds it), resets
the terminal and says in German what happened."""
from __future__ import annotations

import functools
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    from textual.drivers._writer_thread import WriterThread
except ImportError:                                  # a Textual without that class has nothing to guard
    WriterThread = None                              # type: ignore[assignment,misc]

LOG_NAME = "tui-writer.log"
WAIT = 0.25                                          # seconds a blocked put waits before it looks whether the thread is still alive
# The state a screen is left in when Textual's own shutdown writes were dropped: main screen, cursor, mouse, paste, focus.
RESET = "\x1b[?1049l\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?2004l\x1b[?1004l\x1b[<u\x1b[0m\r\n"

_original: dict[str, object] = {}                    # the unpatched methods of WriterThread, kept for patch() and for the tests
_active: "WriterGuard | None" = None                 # the guard the patched methods report to


def current() -> "WriterGuard | None":
    """The installed guard (None before `install`)."""
    return _active


def log_path(home) -> Path:
    """`<state>/tui-writer.log` of the tree, else `~/.lightmind/tui-writer.log` when the tree has no usable state folder."""
    try:
        folder = home.path("state")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / LOG_NAME
    except Exception:                                # noqa: BLE001 - the log must not fail because the tree is odd
        folder = Path.home() / ".lightmind"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / LOG_NAME


class WriterGuard:
    """What happened to the output thread of one app: `failure` is the exception that ended it (None while all is well)."""

    def __init__(self, app, path: Path):
        self.app = app                               # the Textual app to end when the output is gone
        self.path = path                             # where the traceback goes
        self.failure: BaseException | None = None
        self.where = ""                              # the thread name (or the place) that noticed
        self.closed_chat = False                     # True when `report` saved an open chat
        self._lock = threading.Lock()

    def fail(self, exc: BaseException, where: str = "textual-output") -> None:
        """Called from any thread, once counts: log the exception with its traceback, then ask the app to end (exit code 1)."""
        with self._lock:
            if self.failure is not None:
                return
            self.failure, self.where = exc, where
        self._log(exc, where)
        self._request_exit()

    def _log(self, exc: BaseException, where: str) -> None:
        """Append the traceback and the terminal facts that usually explain an output error to the log."""
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            import textual
            version = getattr(textual, "__version__", "?")
        except ImportError:
            version = "?"
        head = (f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} output thread '{where}' ended: {type(exc).__name__}: {exc}\n"
                f"pid={os.getpid()} python={sys.version.split()[0]} textual={version} TERM={os.environ.get('TERM', '')} "
                f"LANG={os.environ.get('LANG', '')} LC_ALL={os.environ.get('LC_ALL', '')} "
                f"stdout.encoding={getattr(sys.stdout, 'encoding', '?')}\n")
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(head + text + "\n")
        except OSError:                              # a log that cannot be written must not hide the exit: stderr shows it later
            pass

    def _request_exit(self) -> None:
        """End the app from the output thread: `App.exit` belongs to the UI loop, so it is handed over thread-safely."""
        exit_app = functools.partial(self.app.exit, return_code=1)
        loop = getattr(self.app, "_loop", None)
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(exit_app)
            else:
                exit_app()
        except Exception:                            # noqa: BLE001 - the loop is already closing: nothing is left to end
            pass

    def report(self) -> int:
        """After `app.run()` returned: with a failure, save the open chat, reset the terminal, say what happened in German.
        Returns the exit code to use (1 on a failure, else 0)."""
        if self.failure is None:
            return 0
        chat = getattr(self.app, "chat", None)
        if chat is not None and not chat.s.readonly:
            try:
                chat.s.close("paused", "screen output crashed")      # `paused` + the saved record: `lm resume` continues it
                self.closed_chat = True
            except Exception as e:                   # noqa: BLE001 - the report must still come out
                self._log(e, "closing the chat after the crash")
        real = sys.__stderr__
        try:
            if real is not None and real.isatty():
                real.write(RESET)
                real.flush()
        except (OSError, ValueError):
            pass
        line = f"Bildschirmausgabe abgestürzt ({type(self.failure).__name__}: {self.failure}), Details in {self.path}"
        if self.closed_chat:
            line += "\nDer Chat ist gespeichert und lässt sich über Resume fortsetzen."
        enc = getattr(sys.stderr, "encoding", None) or "utf-8"
        sys.stderr.write(line.encode(enc, "replace").decode(enc) + "\n")
        sys.stderr.flush()
        return 1


def _dead(thread) -> bool:
    """True when the thread has ended or was marked dead by the guard (started threads only: a new one is not dead)."""
    return bool(getattr(thread, "_lm_dead", False)) or (thread.ident is not None and not thread.is_alive())


def _drain(q: queue.Queue) -> None:
    """Throw away what waits in the queue, so a `put` that is blocked on the full queue goes through."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def _run(self) -> None:
    try:
        _original["run"](self)
    except BaseException as exc:                     # noqa: BLE001 - this is the point: nothing may end this thread unseen
        self._lm_dead = True                         # set before the drain: a write that comes now is dropped, not queued
        _drain(self._queue)
        if _active is None:
            raise
        _active.fail(exc, self.name)


def _write(self, text: str) -> None:
    while True:
        if _dead(self):
            if _active is not None and _active.failure is None:      # the thread ended without a trace of ours
                _active.fail(RuntimeError("the output thread ended without an error"), self.name)
            return                                   # the text has nowhere to go
        try:
            self._queue.put(text, timeout=WAIT)
            return
        except queue.Full:                           # a slow terminal is normal: only a dead thread ends the wait
            continue


def _stop(self) -> None:
    if _dead(self):                                  # nobody reads the queue, and `join` of a dead thread is empty anyway
        return
    _original["stop"](self)


def install(app, home) -> WriterGuard:
    """Guard `app`: patch Textual's `WriterThread` (once per process) and return the guard that `app` reports to."""
    global _active
    if WriterThread is not None and "run" not in _original:
        _original.update(run=WriterThread.run, write=WriterThread.write, stop=WriterThread.stop)
        WriterThread.run, WriterThread.write, WriterThread.stop = _run, _write, _stop
    _active = WriterGuard(app, log_path(home))
    return _active
