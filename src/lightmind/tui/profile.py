"""`LM_PROFILE=1`: a frame-time logger for the start screen.  A measuring tool: without the variable nothing here runs
(`start` returns None and every caller skips its measuring).

It writes `<state>/profile.log` (`system/state/profile.log` in a standard tree), one tab-separated line per event:

    <seconds since the start>  <kind>  <milliseconds>  <detail>

`tick`    start screen: the time since the previous tick (the frame interval), the detail is the work of the tick itself
`render`  one `MapView.render` (the map), the detail is the size of the map in cells
`lag`     the event loop answered a 20 ms heartbeat this late (only above 15 ms): something held the loop up, an import in
          another thread (the GIL), a slow call on the UI thread, the terminal output
`reload`  the snapshot of the tree, detail `thread` or `ui`: where it ran
`take`    taking the snapshot over on the UI thread
`warm`    the background import of one module (a thread), the detail is its name
`compose` building the start screen before its first paint
`gc`      a garbage collection that took more than 3 ms (the detail names the generation)
`beats`   at the end: the number of heartbeats, the detail is `p50 p95 p99 max` of their lateness in ms

The file stops growing at 2 MB.  Reading it: `scripts/profile_report.py`-style code is five lines of `statistics`; the numbers
in `INTERFACES.md` (tui) were made that way."""
from __future__ import annotations

import atexit
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ENV = "LM_PROFILE"
MAX_BYTES = 2_000_000
BEAT = 0.02                    # seconds between heartbeats
BEAT_LOG = 0.015               # a heartbeat later than this is logged as `lag`


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


class Log:
    def __init__(self, path: Path):
        self.t0 = time.perf_counter()
        self.path, self.size = path, 0
        self.mu = threading.Lock()
        self.last: dict[str, float] = {}           # kind -> time of its previous event (for intervals)
        self.beats: list[float] = []
        self.gc_hook = None
        self.f = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.f = open(path, "w", encoding="utf-8", newline="\n", buffering=1 << 16)
        except OSError:
            self.f = None
        self.put("start", 0.0, f"pid {os.getpid()}")

    def put(self, kind: str, ms: float, detail: str = "") -> None:
        if self.f is None:
            return
        line = f"{time.perf_counter() - self.t0:9.3f}\t{kind}\t{ms:8.2f}\t{detail}\n"
        with self.mu:
            if self.f is None:
                return
            if self.size + len(line) > MAX_BYTES:
                self.f.write("log full\n")
                self.f.close()
                self.f = None
                return
            self.size += len(line)
            try:
                self.f.write(line)
            except (OSError, ValueError):
                self.f = None

    def tick(self, kind: str, t0: float, detail: str = "") -> None:
        """An event that repeats: its interval since the previous one of the same kind, and the work since `t0`."""
        now = time.perf_counter()
        prev = self.last.get(kind)
        self.last[kind] = t0
        self.put(kind, (t0 - prev) * 1000 if prev is not None else 0.0, f"work {(now - t0) * 1000:.2f} ms {detail}".strip())

    def beat(self, late: float) -> None:
        self.beats.append(late * 1000)
        if late > BEAT_LOG:
            self.put("lag", late * 1000)

    def close(self) -> None:
        if self.gc_hook is not None:
            import gc
            if self.gc_hook in gc.callbacks:
                gc.callbacks.remove(self.gc_hook)
            self.gc_hook = None
        if self.beats:
            b = sorted(self.beats)
            at = lambda q: b[min(len(b) - 1, int(q * len(b)))]
            self.put("beats", float(len(b)), f"p50 {at(.5):.1f} p95 {at(.95):.1f} p99 {at(.99):.1f} max {b[-1]:.1f}")
            self.beats = []
        with self.mu:
            if self.f is not None:
                try:
                    self.f.close()
                except OSError:
                    pass
                self.f = None


class Heartbeat:
    """A timer callback that measures how late it is called: the lateness is what the loop was busy with."""

    def __init__(self, log: Log):
        self.log, self.due = log, time.perf_counter() + BEAT

    def __call__(self) -> None:
        now = time.perf_counter()
        self.log.beat(max(0.0, now - self.due))
        self.due = now + BEAT                       # the next one is due one interval after this one ran


GC_LOG = 3.0                   # a garbage collection longer than this many ms is logged as `gc`


def watch_gc(log: Log) -> None:
    """Log every garbage collection that takes longer than GC_LOG ms: a full collection walks every object of the process
    and stops the loop for as long as that takes.  `Log.close` takes the hook down again."""
    import gc
    began = [0.0]

    def callback(phase: str, info: dict) -> None:
        if phase == "start":
            began[0] = time.perf_counter()
        else:
            ms = (time.perf_counter() - began[0]) * 1000
            if ms > GC_LOG:
                log.put("gc", ms, f"generation {info.get('generation')} collected {info.get('collected')}")
    gc.callbacks.append(callback)
    log.gc_hook = callback


_log: Log | None = None


def start(home) -> Log | None:
    """The log of this process when `LM_PROFILE=1`, else None.  Made once; written out when the process ends."""
    global _log
    if not enabled():
        return None
    if _log is None:
        _log = Log(home.path("state") / "profile.log")
        watch_gc(_log)
        atexit.register(_log.close)
    return _log


@contextmanager
def timed(log: Log | None, kind: str, detail: str = ""):
    """`with timed(log, "reload", "thread"): ...` logs how long the block took; nothing when `log` is None."""
    if log is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log.put(kind, (time.perf_counter() - t0) * 1000, detail)
