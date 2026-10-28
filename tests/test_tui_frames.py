"""The frames of the start screen: what they cost and when they come.  The animation used to saturate the UI thread (it was
80 % of a core for a screen nobody touches), so frames came late and unevenly; these tests hold the way it is made now:

* the picture goes to the terminal through the line API,
* a frame touches no file and waits for nothing (the tree is read in threads, the result is taken over on the UI thread),
* ten frames a second, evenly, none while the screen is covered or the terminal is in the background,
* the chat modules come in when the animation rests, never while it runs,
* `LM_PROFILE=1` writes the frame times into the state folder, and nothing is written without it."""
import builtins
import gc
import os
import threading
import time

import pytest
from rich.style import Style

from lightmind.tui import data, orbit, profile, screens
from lightmind.tui.orbit import Banner, MapView
from lightmind.tui.screens import MapScreen, SetupScreen

from test_tui_support import FakeProv, earlier_chat, edit, install, plain, quiet, running, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def servers(home, *names):
    for name in names:
        (home.root / "servers" / name).mkdir(exist_ok=True)
        (home.root / "servers" / name / "info.md").write_text(f"---\nhost: {name}.lan\n---\n# {name}\n", "utf-8")


@pytest.fixture
def fast_frames(monkeypatch):
    """A frame every 20 ms instead of every 100 ms, for the tests that only count frames (relative, not the rate itself)."""
    monkeypatch.setattr(screens, "FRAME", 0.02)


def cells(strip):
    """Every cell of a strip as (character, foreground, background, flags): what the terminal gets, whatever the segments.
    A space has no foreground to speak of."""
    out = []
    for seg in strip:
        st = seg.style or Style.null()
        fg = st.color.get_truecolor() if st.color else None
        bg = st.bgcolor.get_truecolor() if st.bgcolor else None
        flags = (bool(st.bold), bool(st.dim), bool(st.italic), bool(st.underline), bool(st.reverse))
        out.extend((ch, None if ch == " " else fg, bg, flags) for ch in seg.text)
    return out


# ------------------------------------------------------------------ the picture
async def test_the_banner_fades_in_with_the_reveal(home):
    async with running(home, anim=True) as (app, pilot):
        banner = app.screen.query_one("#banner", Banner)
        await pilot.press("x")
        app.screen.stop()
        def lit(reveal):
            banner.show(1.0, reveal)
            return sum(1 for ch, fg, bg, fl in cells(banner.render_line(3)) if ch == "█" and fg != bg)
        assert lit(1.0) > lit(0.1) >= 0 and lit(0.0) <= lit(0.1)


# ------------------------------------------------------------------ a frame touches no file
def _spy(monkeypatch, calls):
    main = threading.get_ident()
    def watch(name, real):
        def spied(*a, **kw):
            if threading.get_ident() == main:
                calls.append((name, str(a[0]) if a else ""))
            return real(*a, **kw)
        return spied
    for mod, name in ((os, "stat"), (os, "lstat"), (os, "scandir"), (os, "listdir"), (builtins, "open")):
        monkeypatch.setattr(mod, name, watch(name, getattr(mod, name)))


class Tripwire:
    def __getattr__(self, name):
        raise AssertionError(f"a frame asked the tree for home.{name}")


async def test_a_frame_touches_no_file_and_asks_nothing_of_the_tree(home, monkeypatch):
    async with running(home, anim=True) as (app, pilot):
        scr = app.screen
        assert isinstance(scr, MapScreen)
        calls, real = [], app.home
        _spy(monkeypatch, calls)
        app.home = Tripwire()
        try:
            for _ in range(5):
                scr.tick()                                           # the frames, on the UI thread, nothing else going on
        finally:
            app.home = real
        assert calls == []


async def test_a_key_press_redraws_the_menu_without_asking_the_tree_either(home, monkeypatch):
    async with running(home, anim=True) as (app, pilot):
        scr = app.screen
        before = plain(app, "#menu")
        real = app.home
        app.home = Tripwire()
        try:
            await pilot.press("right")
            await pilot.pause(0.05)
        finally:
            app.home = real
        assert scr.idx == 1 and plain(app, "#menu") == before        # (same words, the highlight moved)


async def test_the_menu_line_is_drawn_again_only_when_it_changed(home, monkeypatch):
    async with running(home, anim=True) as (app, pilot):
        scr = app.screen
        updates = []
        real = scr.bar.update
        monkeypatch.setattr(scr.bar, "update", lambda *a, **kw: (updates.append(1), real(*a, **kw))[1])
        for _ in range(6):
            scr.tick()
        assert updates == []                                         # six frames, the same menu: no redraw of it
        await pilot.press("right")
        assert len(updates) == 1


# ------------------------------------------------------------------ the tree is read in threads
async def test_the_snapshot_is_read_in_a_thread_and_only_taken_over_on_the_ui_thread(home, monkeypatch):
    main = threading.get_ident()
    seen: dict[str, list[int]] = {"snapshot": [], "take": []}
    real_snapshot, real_take = data.snapshot, MapScreen.take
    monkeypatch.setattr(data, "snapshot", lambda h, limit=12: (seen["snapshot"].append(threading.get_ident()), real_snapshot(h, limit))[1])
    monkeypatch.setattr(MapScreen, "take", lambda self, snap: (seen["take"].append(threading.get_ident()), real_take(self, snap))[1])
    async with running(home, anim=True) as (app, pilot):
        await pilot.pause(0.3)
        assert seen["snapshot"] == [main]                            # one, for the first paint, before any frame: none after
        seen["snapshot"].clear()
        await app.screen.load()
        assert seen["snapshot"] and all(t != main for t in seen["snapshot"])
        assert seen["take"] == [main]


async def test_an_edit_of_the_menu_is_found_in_a_thread_and_applied_on_the_ui_thread(home, monkeypatch):
    main = threading.get_ident()
    where: dict[str, list[int]] = {"view": [], "apply": []}
    real_view, real_apply = data.view, MapScreen.take_view
    monkeypatch.setattr(data, "view", lambda h: (where["view"].append(threading.get_ident()), real_view(h))[1])
    monkeypatch.setattr(MapScreen, "take_view", lambda self, m, l: (where["apply"].append(threading.get_ident()), real_apply(self, m, l))[1])
    async with running(home, anim=True) as (app, pilot):
        where["apply"].clear()
        edit(home, '"Dashboard | d | dash"', '"Dashboard | d | dash"\n    - "Journal | j | resume"')
        await until(pilot, lambda: "Journal" in plain(app, "#menu"), timeout=5)
        assert where["view"] and all(t != main for t in where["view"])
        assert where["apply"] and set(where["apply"]) == {main}


def test_a_snapshot_carries_the_menu_and_the_layout(home):
    snap = data.snapshot(home)
    assert snap.menu == data.menu(home) and snap.layout == "orbit"
    assert data.view(home) == (snap.menu, snap.layout)
    assert data.snapshot(home, 0).menu == snap.menu                  # also the light one the cards use


# ------------------------------------------------------------------ ten frames a second, evenly
async def test_ten_frames_a_second_evenly_and_a_calm_landing_one(home, monkeypatch):
    stamps = []
    real = MapScreen.tick
    monkeypatch.setattr(MapScreen, "tick", lambda self: (stamps.append(time.perf_counter()), real(self))[1])
    async with running(home, anim=True) as (app, pilot):
        await pilot.pause(1.2)
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
    assert 8 <= len(stamps) <= 16, len(stamps)
    assert 0.085 <= gaps[len(gaps) // 2] <= 0.125, gaps[len(gaps) // 2]          # the middle gap is a tenth of a second
    assert gaps[-1] < 0.5
    stamps.clear()
    async with running(home, anim=False) as (app, pilot):
        await pilot.pause(1.2)
    assert 1 <= len(stamps) <= 3, len(stamps)


# ------------------------------------------------------------------ it rests
async def test_no_frames_while_the_terminal_is_in_the_background(home, monkeypatch, fast_frames):
    ticks = []
    real = MapScreen.tick
    monkeypatch.setattr(MapScreen, "tick", lambda self: (ticks.append(time.perf_counter()), real(self))[1])
    async with running(home, anim=True) as (app, pilot):
        await pilot.pause(0.17)
        app.app_focus = False
        await pilot.pause(0.1)
        n = len(ticks)
        await pilot.pause(0.3)
        assert len(ticks) == n                                       # nothing is drawn for nobody
        app.app_focus = True
        await pilot.pause(0.2)
        assert len(ticks) >= n + 4                                   # and it goes on, with a frame at once


async def test_no_frames_while_another_screen_covers_the_landing(home, monkeypatch, fast_frames):
    ticks = []
    real = MapScreen.tick
    monkeypatch.setattr(MapScreen, "tick", lambda self: (ticks.append(time.perf_counter()), real(self))[1])
    async with running(home, anim=True) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))
        await pilot.pause(0.1)
        n = len(ticks)
        await pilot.pause(0.27)
        assert len(ticks) == n
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))
        await pilot.pause(0.2)
        assert len(ticks) >= n + 3


async def test_blurred_while_covered_does_not_wake_the_animation_when_focus_returns(home, monkeypatch, fast_frames):
    ticks = []
    real = MapScreen.tick
    monkeypatch.setattr(MapScreen, "tick", lambda self: (ticks.append(time.perf_counter()), real(self))[1])
    async with running(home, anim=True) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))
        app.app_focus = False
        await pilot.pause(0.07)
        app.app_focus = True                                         # back in the terminal, the card still on top
        await pilot.pause(0.07)
        n = len(ticks)
        await pilot.pause(0.2)
        assert len(ticks) == n


# ------------------------------------------------------------------ the chat modules come when it rests
async def test_the_chat_modules_are_imported_when_the_animation_rests_not_while_it_runs(home, monkeypatch):
    calls = []

    async def fake_warm(modules=screens.WARM_MODULES, prof=None):
        calls.append(tuple(modules))
    monkeypatch.setattr(screens, "warm", fake_warm)
    async with running(home, anim=True) as (app, pilot):
        await pilot.pause(0.6)                                       # the intro and some animation: nothing imported
        assert calls == []
        await pilot.press("n")                                       # a card opens over the landing: it rests
        await until(pilot, lambda: screens.LANDING_WARM in calls)


async def test_a_terminal_in_the_background_brings_the_chat_modules_in_once(home, monkeypatch):
    calls = []

    async def fake_warm(modules=screens.WARM_MODULES, prof=None):
        calls.append(tuple(modules))
    monkeypatch.setattr(screens, "warm", fake_warm)
    async with running(home, anim=True) as (app, pilot):
        app.app_focus = False
        await until(pilot, lambda: screens.LANDING_WARM in calls)
        app.app_focus = True
        await pilot.pause(0.2)
        app.app_focus = False
        await pilot.pause(0.3)
        assert calls.count(screens.LANDING_WARM) == 1                # asked once, not at every blur


async def test_the_resume_list_brings_the_chat_modules_in_too(home, monkeypatch):
    calls = []

    async def fake_warm(modules=screens.WARM_MODULES, prof=None):
        calls.append(tuple(modules))
    monkeypatch.setattr(screens, "warm", fake_warm)
    async with running(home, anim=False) as (app, pilot):
        calls.clear()
        await pilot.press("r")
        await until(pilot, lambda: calls)
        assert screens.LANDING_WARM in calls


# ------------------------------------------------------------------ the heap is frozen twice, never more
def test_settle_freezes_once_per_stage_and_the_tests_leave_it_off(monkeypatch):
    base = gc.get_freeze_count()
    screens.settle("test-off")                                       # LM_NO_FREEZE is set by conftest
    assert gc.get_freeze_count() == base and "test-off" not in screens._SETTLED
    monkeypatch.delenv("LM_NO_FREEZE")
    try:
        screens.settle("test-on")
        after = gc.get_freeze_count()
        assert after >= base and "test-on" in screens._SETTLED
        screens.settle("test-on")                                    # the same stage again: nothing
        assert gc.get_freeze_count() == after
    finally:
        gc.unfreeze()
        screens._SETTLED.discard("test-on")


async def test_the_landing_settles_the_heap_before_its_first_frame(home, monkeypatch):
    calls = []
    monkeypatch.setattr(screens, "settle", lambda stage, collect=False: calls.append((stage, collect)))
    async with running(home, anim=True) as (app, pilot):
        assert calls == [("start", True)]


async def test_the_chat_modules_settle_the_heap_after_the_sdk_came_in(home, monkeypatch):
    calls = []
    monkeypatch.setattr(screens, "settle", lambda stage, collect=False: calls.append((stage, collect)))
    imported = []
    monkeypatch.setattr(screens, "_import", lambda name, prof=None: imported.append(name))
    await screens.warm(screens.LANDING_WARM)
    assert calls == [] and imported == list(screens.LANDING_WARM)    # the two modules alone: not worth a freeze
    await screens.warm()
    assert calls == [("modules", False)]


# ------------------------------------------------------------------ LM_PROFILE
async def test_lm_profile_writes_the_frame_times_and_nothing_is_written_without_it(home, monkeypatch):
    log = home.path("state") / "profile.log"
    monkeypatch.delenv("LM_PROFILE", raising=False)
    monkeypatch.setattr(profile, "_log", None)
    async with running(home, anim=True) as (app, pilot):
        assert app.prof is None
        await pilot.pause(0.4)
    assert not log.exists()
    monkeypatch.setenv("LM_PROFILE", "1")
    monkeypatch.setattr(profile, "_log", None)
    hooks = len(gc.callbacks)
    async with running(home, anim=True) as (app, pilot):
        assert app.prof is not None
        await pilot.pause(1.2)
    app.prof.close()
    assert len(gc.callbacks) == hooks                                # its collector hook is taken down again
    lines = [l.split("\t") for l in log.read_text("utf-8").splitlines() if l.count("\t") >= 3]
    kinds = {l[1] for l in lines}
    assert {"start", "compose", "tick", "render", "banner", "beats"} <= kinds
    ticks = [l for l in lines if l[1] == "tick"]
    assert len(ticks) >= 8 and all(l[3].startswith("work ") for l in ticks)
    assert 60 <= sorted(float(l[2]) for l in ticks[1:])[len(ticks) // 2] <= 140       # the interval column, in ms


def test_the_profile_log_stops_growing_at_two_megabytes(tmp_path):
    log = profile.Log(tmp_path / "p.log")
    for _ in range(40000):
        log.put("tick", 1.0, "x" * 50)
    log.close()
    size = (tmp_path / "p.log").stat().st_size
    assert size <= profile.MAX_BYTES + 100 and (tmp_path / "p.log").read_text("utf-8").endswith("log full\n")


def test_the_timed_block_logs_only_when_there_is_a_log(tmp_path):
    with profile.timed(None, "x"):
        pass
    log = profile.Log(tmp_path / "p.log")
    with profile.timed(log, "reload", "thread"):
        pass
    log.close()
    assert "\treload\t" in (tmp_path / "p.log").read_text("utf-8")
