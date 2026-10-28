"""Live smoke tests against the real Claude CLI (system login, haiku, effort low, tiny prompts).

Skipped by default.  Run by hand:

    UV_PROJECT_ENVIRONMENT=<venv> uv run --extra dev --with psutil pytest -m live -s tests/test_provider_live.py

`psutil` is optional; with it the test reports the memory of the spawned CLI processes.
"""
import asyncio
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from lightmind import loop as L
from lightmind.model import Text, ToolCall, Usage
from lightmind.providers import claude_sdk as C
from lightmind.providers.base import ToolDone

from test_loop_support import SessionHome, collect, default_providers, make_dispatcher, request, write

pytestmark = pytest.mark.live


class Memory:
    """Samples the child processes of this python process (the spawned Claude CLI and what it
    starts) every 100 ms; keeps the peak and the per-process breakdown at the peak."""

    def __init__(self) -> None:
        try:
            import psutil
        except ImportError:
            psutil = None
        self.psutil = psutil
        self.stop = threading.Event()
        self.peak_ws = self.peak_private = 0
        self.peak_procs: list[tuple[str, int, int]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        ps = self.psutil
        me = ps.Process()
        while not self.stop.is_set():
            rows = []
            try:
                for k in me.children(recursive=True):
                    try:
                        mi = k.memory_info()
                        rows.append((k.name(), mi.wset, mi.private))
                    except ps.Error:
                        pass
            except ps.Error:
                pass
            ws, private = sum(r[1] for r in rows), sum(r[2] for r in rows)
            if ws > self.peak_ws:
                self.peak_ws, self.peak_private, self.peak_procs = ws, private, rows
            self.stop.wait(0.1)

    def __enter__(self) -> "Memory":
        if self.psutil:
            self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.psutil:
            self.stop.set()
            self.thread.join()

    async def children_left(self, wait: float = 6.0) -> int:
        """Child processes still alive `wait` seconds from now (0 = nothing resident).  Polls with
        asyncio.sleep: the SDK closes its transport on the event loop, so a blocking wait would keep
        alive the very process it waits for."""
        if not self.psutil:
            return 0
        me, end = self.psutil.Process(), time.time() + wait
        while time.time() < end and me.children(recursive=True):
            await asyncio.sleep(0.2)
        return len(me.children(recursive=True))

    def describe(self) -> str:
        """The children alive right now: name, pid, age in seconds, start of the command line."""
        if not self.psutil:
            return "n/a"
        rows = []
        for k in self.psutil.Process().children(recursive=True):
            try:
                rows.append(f"{k.name()} pid {k.pid} age {time.time() - k.create_time():.0f}s "
                            f"{' '.join(k.cmdline())[:100]}")
            except self.psutil.Error:
                pass
        return "; ".join(rows) or "none"

    def line(self) -> str:
        if not self.psutil:
            return "memory: n/a (psutil missing)"
        mb = lambda b: f"{b / 1048576:.0f} MB"
        procs = ", ".join(f"{n} {mb(w)}" for n, w, _ in sorted(self.peak_procs, key=lambda r: -r[1]))
        return (f"memory of {len(self.peak_procs)} child processes at peak: working set {mb(self.peak_ws)}, "
                f"private {mb(self.peak_private)} [{procs}]")


def cli_project_dir(root: Path) -> Path:
    """Where the CLI keeps the transcripts of sessions started with cwd = root."""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return cfg / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


@pytest.fixture
def live_home(tree):
    home = SessionHome(tree)
    before = cli_project_dir(home.root).exists()
    yield home
    d = cli_project_dir(home.root)
    if d.exists() and not before:                    # remove only what this test created
        shutil.rmtree(d, ignore_errors=True)


async def timed(session, text):
    t0, first, events = time.time(), None, []
    async for ev in session.send(text):
        first = first if first is not None else time.time() - t0
        events.append(ev)
    return events, first or 0.0, time.time() - t0


def said(events) -> str:
    return "".join(e.text for e in events if isinstance(e, Text))


async def test_live_session_tool_call_and_resume(live_home):
    home = live_home
    write(home.root / "projects" / "smoke.txt", "The magic word is MAGIC-4711.\n")
    report = []
    with Memory() as mem:
        s = L.open_session(home, model="haiku", effort="low")
        try:
            ev1, first1, total1 = await timed(s, "Reply with the single word: pong")
        except C.NotLoggedIn:
            pytest.skip("the system Claude login is not available")
        # --- turn 1: a tiny question
        assert "pong" in said(ev1).lower()
        assert any(isinstance(e, Usage) and e.output > 0 for e in ev1)
        report.append(f"turn 1  first event {first1:.1f}s, total {total1:.1f}s, text {said(ev1).strip()!r}")
        # --- turn 2: a tool call through our MCP server, dispatcher and rule check
        ev2, first2, total2 = await timed(
            s, "Use your read tool on the file projects/smoke.txt and answer with the magic word only.")
        calls = [e for e in ev2 if isinstance(e, ToolCall)]
        done = [e for e in ev2 if isinstance(e, ToolDone)]
        assert calls and calls[0].name == "read" and "smoke.txt" in calls[0].args["path"]
        assert done and done[0].ok and "MAGIC-4711" in done[0].text
        assert "MAGIC-4711" in said(ev2)
        guard = list(s.provider.guard_log)
        assert ("read", "allow") in guard                                   # the PreToolUse hook ran and let it pass
        tool_events = [json.loads(l)["data"] for l in (s.run.path / "events.jsonl").read_text("utf-8").splitlines()
                       if json.loads(l)["kind"] == "tool"]
        assert tool_events and tool_events[0]["name"] == "read" and tool_events[0]["ok"] is True
        assert tool_events[0]["denied_by"] == ""                            # passed the dispatcher's rule check
        report.append(f"turn 2  first event {first2:.1f}s, total {total2:.1f}s, tool read ok, guard {guard}, "
                      f"answer {said(ev2).strip()!r}")
        sdk_id, info = s.provider.session_id, s.info
        report.append(f"session {s.id}  sdk conversation {sdk_id[:8]}...  ctx {info.ctx_used} tokens  "
                      f"cost ${info.cost_usd:.4f} (estimate)  CLI-reported ${s.provider.cost_usd}")
        s.close()
        # --- turn 3: a new process resumes the conversation
        r = L.resume_session(home, s.id)
        assert r.provider.session_id == sdk_id
        ev3, first3, total3 = await timed(
            r, "What was the magic word in the file you read before? Reply with the word only.")
        assert "MAGIC-4711" in said(ev3)                                    # only possible if the conversation continued
        assert r.provider.session_id == sdk_id
        report.append(f"turn 3  (resume_session) first event {first3:.1f}s, total {total3:.1f}s, "
                      f"answer {said(ev3).strip()!r}")
        t = r.run.run.tokens
        report.append(f"run totals: in {t.input} out {t.output} cache_read {t.cache_read} cache_write {t.cache_write}, "
                      f"cost ${r.info.cost_usd:.4f} (prices.md estimate)")
        r.close()
    report.append(mem.line())
    left = await mem.children_left()
    report.append(f"child processes alive after the last turn: {left} (an idle session holds no process)")
    assert left == 0
    limits = await r.provider.usage()
    report.append(f"account usage seen: 5h {limits.five_hour}, 7d {limits.seven_day} ({limits.note})")
    print("\nLIVE REPORT\n  " + "\n  ".join(report))


async def test_live_empty_config_dir_says_not_logged_in(tree):
    """No model call: a fresh config dir inside the tree has no login, the CLI says so at once."""
    home = SessionHome(tree, providers=default_providers(abo="system/claude/abo"))
    d, _ = make_dispatcher(home)
    p = C.ClaudeSdkProvider(home, "abo", d)
    before = cli_project_dir(home.root).exists()
    try:
        with Memory() as mem:
            with pytest.raises(C.NotLoggedIn) as e:
                await collect(p.turn(request("hi")))
            left = await mem.children_left(wait=1.0)
    finally:
        if not before:
            shutil.rmtree(cli_project_dir(home.root), ignore_errors=True)
    assert str(e.value) == "not logged in: run `lm login abo`"
    assert left == 0, f"the CLI outlived the error: {mem.describe()}"
    assert (home.root / "system" / "claude" / "abo").is_dir()                  # the login would go there, inside the tree


async def test_live_stopping_early_ends_the_cli_process(live_home):
    """Esc in the TUI: abandoning the iterator mid-turn must stop the CLI, not let it finish."""
    home = live_home
    write(home.root / "projects" / "smoke.txt", "The magic word is MAGIC-4711.\n")
    with Memory() as mem:
        at_start = mem.describe()
        s = L.open_session(home, model="haiku", effort="low")
        gen = s.send("Use your read tool on projects/smoke.txt, then tell me the magic word.")
        try:
            async for ev in gen:
                if isinstance(ev, ToolCall):                 # the model asked for the tool: stop here
                    break
        except C.NotLoggedIn:
            pytest.skip("the system Claude login is not available")
        assert at_start == "none", f"a process was already running: {at_start}"
        alive = len(mem.describe().split("; "))
        t0 = time.time()
        await gen.aclose()
        closed_in = time.time() - t0
        left = await mem.children_left(wait=1.0)        # a short grace for the process table only
    assert left == 0, f"still alive after the early stop: {mem.describe()}"
    assert not s._busy
    print(f"\nSTOP: {alive} process(es) running at the stop, aclose() took {closed_in:.1f}s, "
          f"children left right after: {left}")
    s.close("paused", "stopped")
