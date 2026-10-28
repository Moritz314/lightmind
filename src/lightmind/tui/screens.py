"""The screens of Orbit except the chat: start, new chat, resume list, accounts, dashboard, and the two
modals (a picker, the permission question)."""
from __future__ import annotations

import asyncio
import gc
import importlib
import os
import re
import time

from rich.text import Text
from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from .. import title as release_title
from ..accounts import hint as account_hint
from ..files import parse_size
from ..providers.base import make_provider
from ..runs import (archive as archive_chat, cost_summary, group_sessions, list_runs, list_session_groups, lock_holder,
                    read_names, request_stop, session_names, set_name_on_disk, unarchive as unarchive_chat)
from .. import todos
from . import acts, data, profile
from .notes import NAME, Names, NotesPane
from .orbit import Banner, MapView
from .theme import AMBER, BG, CYAN, DIM, MAG, MINT, OFF, TXT, VIOLET, bar, blend, clamp

SDK_MODULE = "lightmind.providers.claude_sdk"
LANDING_WARM = ("lightmind.loop", "lightmind.tui.chat")      # imported in the background once the landing is on screen
WARM_MODULES = (*LANDING_WARM, SDK_MODULE)                  # ... and the SDK (seconds) once a new-chat card is open


def _loop():
    """The session API, imported on first use: `loop` pulls in hooks, tools and the provider base, which the landing never needs."""
    return importlib.import_module("lightmind.loop")


def _import(name: str, prof=None):
    with profile.timed(prof, "warm", name):
        return importlib.import_module(name)


_SETTLED: set[str] = set()


def settle(stage: str, collect: bool = False):
    """Take everything the process has built so far out of the garbage collector's way (`gc.freeze`), once per `stage` and
    process.  Textual leaves reference cycles behind every strip it draws (a strip caches the one-element list that holds
    itself), about 1500 objects per frame, so the collector has to free thousands of objects every couple of seconds, and a
    full collection also walks every live object of the process: with Textual, Rich and this program loaded that stopped
    the animation for 45 to 85 ms every 2.4 s, a hitch in every 24th frame.  Frozen objects are not walked, so a
    collection only has the frames' own garbage to deal with.  What was frozen is never collected, which for what lives as
    long as the process (the modules, the app) is no loss; that is why it is done only twice: before the first frame, and
    when the chat modules and the SDK (hundreds of thousands of objects) have come in.  `LM_NO_FREEZE=1` switches it off (the
    tests do: a frozen object is never freed, and every Orbit test builds a whole app)."""
    if os.environ.get("LM_NO_FREEZE") or stage in _SETTLED:
        return
    _SETTLED.add(stage)
    if collect:
        gc.collect()                                       # first the garbage that is there, so it is not frozen with the rest
    gc.freeze()


async def warm(modules=WARM_MODULES, prof=None):
    """Import the chat modules and the Claude SDK (seconds) off the UI thread while the landing or a card is on screen,
    so the first chat does not wait.  Nothing here is needed to paint the landing."""
    for name in modules:
        try:
            await asyncio.to_thread(_import, name, prof)
        except Exception:                                  # noqa: BLE001 - the chat reports a real problem itself
            pass
    if SDK_MODULE in modules:
        settle("modules")                                  # the modules just imported are as long-lived as the rest


def spaced(s: str) -> str:
    return " ".join(s.upper())


# ---------------------------------------------------------------- start screen
FRAME = 0.1               # seconds between two frames of the landing: 10 a second, evenly spaced
LOOK = 1.0                # seconds between two looks at lightmind.md and config.md (made in a thread)


class MapScreen(Screen):
    """The start screen.  A frame touches no file and waits for nothing: the tree is read in a thread (`load`, `look`) and only
    the result is taken over here (`take`).  The frames come 10 times a second and rest when the screen is covered or the
    terminal is in the background."""

    def __init__(self):
        super().__init__()
        self.idx, self.lay = 0, "orbit"
        self.shown = None                 # (selected entry, menu) as last drawn
        self.warmed = False
        self.first = True                 # the first time the screen is shown its snapshot is a few milliseconds old

    def compose(self):
        self.prof = getattr(self.app, "prof", None)
        with profile.timed(self.prof, "compose"):
            self.snap = data.snapshot(self.app.home)
        self.menu = self.snap.menu
        yield Banner(self.app.calm, id="banner")
        yield MapView(self.snap, id="map")
        yield Static(id="menu")
        yield Static(Text("←→ ⏎ or hotkey · Tab: choose server to dive into · Ctrl+Q: quit\n" + release_title(), style=DIM, justify="center"), id="foot")

    def on_mount(self):
        self.calm = self.app.calm
        self.banner, self.mv, self.bar = self.query_one("#banner", Banner), self.query_one("#map", MapView), self.query_one("#menu", Static)
        settle("start", collect=True)                               # before the first frame: nobody sees the pause
        self.t_start = time.monotonic()
        self.intro = 0.0 if self.calm else 1.3                      # any key skips it
        self.timer = self.set_interval(1.0 if self.calm else FRAME, self.tick)
        self.set_timer(self.snap.refresh, self.reload)
        self.set_interval(LOOK, self.look)
        self.watch(self.app, "app_focus", self.focus_changed, init=False)
        self.take_view(self.snap.menu, self.snap.layout)
        self.tick()
        if getattr(self.app, "boot_note", ""):
            self.mv.say(self.app.boot_note)                          # the checklist found something: say so once
        if self.prof:
            self.set_interval(profile.BEAT, profile.Heartbeat(self.prof))

    # ------------------------------------------------------------------ when the animation rests
    def on_screen_suspend(self):
        self.stop()                                                 # nothing to animate under another screen
        self.rest()

    def on_screen_resume(self):
        if hasattr(self, "timer"):
            if self.app.app_focus:
                self.go()
            if self.first:
                self.first = False
            else:
                self.call_later(self.load)                          # back from another screen: what it did may show here

    def focus_changed(self, focused: bool):
        """The terminal went to the background or came back: a screen nobody looks at is not drawn."""
        if not hasattr(self, "timer"):
            return
        if not focused:
            self.stop()
            self.rest()
        elif self.app.screen is self:
            self.go()
            self.tick()

    def go(self):
        self.timer.resume()

    def stop(self):
        self.timer.pause()

    def rest(self):
        """The animation rests, so this is the time to bring in the chat modules: imported while it runs they take the
        processor from it (one of them, `tui.chat`, took 6 s that way and the animation stuttered all along)."""
        if self.warmed:
            return
        self.warmed = True
        try:
            self.run_worker(warm(LANDING_WARM, self.prof))
        except Exception:                                           # noqa: BLE001 - the app is closing
            pass

    # ------------------------------------------------------------------ the tree, read in threads
    async def load(self):
        """Read the tree in a thread and take the result over here."""
        def work():
            with profile.timed(self.prof, "reload", "thread"):
                return data.snapshot(self.app.home)
        snap = await asyncio.to_thread(work)
        with profile.timed(self.prof, "take", "ui"):
            self.take(snap)

    async def reload(self):
        if self.app.screen is self:
            try:
                await self.load()
            except Exception:                                       # noqa: BLE001 - e.g. the screen went away meanwhile
                pass
        self.set_timer(self.snap.refresh, self.reload)

    async def look(self):
        """An edit of lightmind.md or config.md shows within a second: the files are looked at in a thread, never in a frame."""
        if self.app.screen is not self:
            return
        try:
            menu, layout = await asyncio.to_thread(data.view, self.app.home)
        except Exception:                                           # noqa: BLE001 - the screen went away meanwhile
            return
        if (menu, layout) != (self.menu, self.lay):
            self.take_view(menu, layout)

    def take(self, snap):
        """A snapshot made in a thread: assign it.  Nothing here touches a file."""
        self.snap = snap
        self.mv.set_snap(snap)
        self.take_view(snap.menu, snap.layout)

    def take_view(self, menu, layout):
        if menu != self.menu:
            self.menu = menu
            self.idx = min(self.idx, len(menu) - 1)
        if layout != self.lay:
            self.lay = layout
            if layout != "orbit":
                self.mv.say(f'ui.layout "{layout}" is not built yet: showing orbit')
        self.draw_menu()

    # ------------------------------------------------------------------ one frame
    def tick(self):
        """One frame: the time moves on, the banner and the map are drawn again.  No file, no thread, nothing that waits."""
        t0 = time.perf_counter() if self.prof else 0.0
        t = time.monotonic()
        p = 1.0
        if self.intro > 0:
            p = (t - self.t_start) / self.intro
            if p >= 1:
                self.intro, p = 0.0, 1.0
        self.banner.show(t, p)
        self.mv.reveal = clamp((p - 0.12) / 0.88)
        self.mv.refresh()
        self.draw_menu()
        if self.prof:
            self.prof.tick("tick", t0)

    def draw_menu(self):
        """The menu line, only when the selection or the rows changed."""
        key = (self.idx, tuple(self.menu))
        if key == self.shown:
            return
        self.shown = key
        m = Text(justify="center")
        for i, (label, k, _) in enumerate(self.menu):
            m.append(f" {k.upper()} ", f"bold {BG} on {AMBER}")
            m.append(f" {label} ", f"bold {BG} on {CYAN}" if i == self.idx else TXT)
            m.append("   ")
        self.bar.update(m)

    async def on_key(self, e: events.Key):
        k, mv = e.key, self.query_one("#map", MapView)
        self.intro = 0.0
        if k in ("left", "right"):
            self.idx = (self.idx + (1 if k == "right" else -1)) % len(self.menu)
        elif k == "tab":
            names = [s.name for s in self.snap.servers]
            mv.sel = names[(names.index(mv.sel) + 1) % len(names)] if mv.sel in names else names[0]
        elif k == "enter":
            await self.act(self.menu[self.idx][2])
        else:
            hit = next((i for i, row in enumerate(self.menu) if row[1] == k), None)
            if hit is None:
                return
            self.idx = hit
            await self.act(self.menu[hit][2])
        e.stop()
        self.tick()

    async def act(self, action: str):
        app = self.app
        if action == "new_chat":
            app.push_screen(SetupScreen(self.query_one("#map", MapView).sel))
        elif action in ("resume", "archive"):
            app.push_screen(ResumeScreen(archived=action == "archive"))
        elif action == "accounts":
            app.push_screen(AccountsScreen())
        elif action == "dash":
            app.push_screen(DashScreen())
        elif action == "settings":
            from .settings import SettingsScreen              # not needed to paint the landing
            app.push_screen(SettingsScreen())
        elif action == "quit":
            await app.action_quit()
        else:
            self.query_one("#map", MapView).say(f'ui.start_menu: no action "{action}" (use {", ".join(data.ACTIONS)})')


# ------------------------------------------------------------------- new chat
STEPS = ["server", "account", "model", "effort", "context", "tools", "mode"]
SUB = {"server": "where the chat runs", "account": "who pays", "model": "which brain", "effort": "how hard it thinks",
       "context": "how much it can hold", "tools": "the tools it may touch (space toggles; skills are always on)", "mode": "how free it is"}
KEY = {"server": "server", "account": "account", "model": "model", "effort": "effort", "context": "ctx", "mode": "mode"}


class SetupScreen(Screen):
    """The new-chat cards.  Besides the seven cards there is a side card for an agent (key A): the chat then starts as the
    agent `agents/<name>/` says (`open_session(agent=...)`), and only a card the person changed overrides the agent."""

    def __init__(self, server: str | None = None):
        super().__init__()
        self.server, self.i, self.cur, self.note, self.busy = server, 0, {}, "", False
        self.picking, self.touched = False, set()       # the agent card is open; the S keys the person changed by hand

    def compose(self):
        yield Static(id="card")

    def on_mount(self):
        h = self.app.home
        self.snap = data.snapshot(h, 0)
        self.accounts = data.safe(lambda: h.load_providers().accounts, {})
        self.modes, self.items = data.modes(h), data.items(h)
        d = data.settings(h).get("defaults") or {}
        acc = str(d.get("provider") or "")
        acc = acc if acc in self.accounts else next(iter(self.accounts), "")
        mode = str(d.get("mode") or "")
        self.S = dict(server=self.server or self.snap.hub, account=acc, model=data.default_model(h, acc, str(d.get("model") or "")),
                      effort=str(d.get("effort") or "medium"), ctx=str(d.get("context") or "200k"), off=set(),
                      mode=mode if mode in self.modes else next(iter(self.modes), mode), agent="")
        self.base = dict(self.S)                            # the values without an agent
        self.agents = data.safe(lambda: self.read_agents(h), [])
        self.run_worker(warm())
        self.draw()

    @staticmethod
    def read_agents(h) -> list[tuple[str, str, bool]]:
        """(name, description, usable) of every agent of the tree; one whose file cannot be read is listed but cannot be taken."""
        out = []
        for name in h.list_agents():
            try:
                out.append((name, h.load_agent(name).description, True))
            except Exception as e:                           # noqa: BLE001 - the card says why and the rest stays usable
                out.append((name, data.clean(str(e))[:50] or "cannot be read", False))
        return out

    def take_agent(self, name: str) -> None:
        """Choose the agent `name` ("" = none): the cards show what the agent sets, unless the person changed them."""
        S = self.S
        S["agent"] = name
        ag = data.safe(lambda: self.app.home.load_agent(name)) if name else None
        for key in ("account", "model", "effort", "mode"):
            if key not in self.touched:
                S[key] = self.base[key]
        if ag is not None:
            if "account" not in self.touched and ag.provider in self.accounts:
                S["account"] = ag.provider
            if "model" not in self.touched and ag.model:
                S["model"] = ag.model
            if "effort" not in self.touched and ag.effort in data.efforts():
                S["effort"] = ag.effort
            if "mode" not in self.touched and ag.mode in self.modes:
                S["mode"] = ag.mode
        for step in STEPS:
            self.cur.pop(step, None)                         # the cursors follow the new values

    def options(self, step):
        """(value, label, description, available) of one card."""
        S, h = self.S, self.app.home
        if step == "server":
            return [(s.name, s.name, f"{s.host}  ·  " + ("default" if s.name == self.snap.hub else "permanent session over ssh" if not s.local else "this machine"), True)
                    for s in self.snap.servers]
        if step == "account":
            return [(n, n, data.account_note(h, n, a), True) for n, a in self.accounts.items()]
        if step == "model":
            return [(n, n, note, True) for n, note in data.model_options(h, S["account"])]
        if step == "effort":
            return [(e, e, data.EFFORT_TEXT.get(e, ""), True) for e in data.efforts()]
        if step == "context":
            return [(c, c, "tokens" + ("  ·  1m window" if data.safe(lambda: parse_size(c), 0) > data.WINDOW else ""), True)
                    for c in dict.fromkeys((*data.context_sizes(h), S["ctx"]))]
        if step == "tools":
            return [(i.name, i.name, f"{i.kind}  ·  {i.desc}" if i.desc else i.kind, True) for i in self.items]
        self.modes = data.modes(h)                           # read again: the file may have been mended since the card opened
        if not self.modes:
            return [("", "no modes", "", False)]
        return [(m.key, m.label, "", True) for m in self.modes.values()]

    def cursor(self, step):
        o = self.options(step)
        if step not in self.cur:
            v = self.S.get(KEY.get(step, ""))
            self.cur[step] = next((i for i, x in enumerate(o) if x[0] == v), 0)
        self.cur[step] = min(self.cur[step], max(0, len(o) - 1))
        return self.cur[step]

    def draw_agents(self):
        """The side card: no agent, then every agent of the tree with its description."""
        t = Text(spaced("agent") + "\n", f"bold {CYAN}")
        t.append("start the chat as one of the agents of this tree (agents/)\n\n", DIM)
        rows = [("", "(no agent)", "a plain chat", True), *((n, n, d, ok) for n, d, ok in self.agents)]
        c = self.cur["agent"] = min(self.cur.get("agent", 0), len(rows) - 1)
        for j, (val, label, desc, ok) in enumerate(rows):
            sel = j == c
            t.append(" ▌ " if sel else "   ", CYAN)
            t.append(f"{label:<24}", OFF if not ok else f"bold {BG} on {CYAN}" if sel else TXT if self.S["agent"] == val else DIM)
            t.append(f"  {data.clean(desc)[:60]}\n", OFF if not ok else CYAN if sel else DIM)
        if not self.agents:
            t.append("\n   no agents in this tree yet\n", DIM)
        if self.note:
            t.append("\n" + self.note + "\n", MAG)
        t.append("\n↑↓ choose · ⏎ take · Esc/A back to the cards", DIM)
        self.query_one("#card").styles.background = BG
        self.query_one("#card", Static).update(t)

    def draw(self):
        if self.picking:
            return self.draw_agents()
        S = self.S
        step = STEPS[self.i]
        opts, c = self.options(step), self.cursor(step)
        mode = self.modes.get(opts[c][0] if step == "mode" and opts else S["mode"])
        accent = mode.color if step == "mode" and mode else CYAN
        self.query_one("#card").styles.background = blend(BG, accent, 0.12) if step == "mode" else BG
        t = Text()
        for j, s in enumerate(STEPS):
            t.append("● " if j < self.i else "◉ " if j == self.i else "○ ", accent if j == self.i else (MINT if j < self.i else DIM))
            t.append(s.upper() + "   ", f"bold {TXT}" if j == self.i else DIM)
        t.append(f"\n\n{self.i + 1:02d}/{len(STEPS):02d}  ", DIM)
        t.append(spaced(step) + "\n", f"bold {accent}")
        t.append(SUB[step] + "\n\n", DIM)
        for j, (val, label, desc, ok) in enumerate(opts):
            sel = j == c
            if step == "tools":
                on = val not in S["off"]
                t.append(" ● " if on else " ○ ", MINT if on else DIM)
                t.append(f" {label:<16}", f"bold {BG} on {accent}" if sel else TXT)
                t.append(f"  {desc[:60]}\n", DIM)
                continue
            if not ok:
                t.append(f" ▌ {label:<22}" if sel else f"   {label:<22}", OFF)
                t.append(f"  {desc[:50]}\n", OFF)
            elif sel:
                t.append(f" ▌ {label:<22}", f"bold {BG} on {accent}")
                t.append(f"  {desc[:60]}\n", accent)
            else:
                t.append(f"   {label:<22}", TXT if S.get(KEY.get(step, "")) == val else DIM)
                t.append(f"  {desc[:40]}\n", OFF)
            if len(opts) <= 5:
                t.append("\n")
        if step == "mode" and not self.modes:
            t.append("\n" + data.clean(data.modes_error(self.app.home)) + "\n", MAG)
        if step == "tools" and not opts:
            t.append("   no tools in this tree yet\n", DIM)
        if step == "tools" and (sk := data.skills(self.app.home)):
            t.append("\nskills, always on: " + ", ".join(i.name for i in sk)[:200] + "\n", DIM)
        if step == "mode" and mode:
            t.append(f"\n{mode.text}\nasks: {', '.join(mode.asks) or 'nothing'}\nblocks: {', '.join(mode.denies) or 'nothing'}\n", mode.color)
            if mode.error:
                t.append(f"cannot be used: {data.clean(mode.error)}\n", MAG)
        on_n = sum(1 for i in self.items if i.name not in S["off"])
        t.append(f"\n{'agent ' + S['agent'] + ' · ' if S['agent'] else ''}{S['server']} · {S['account']} · {S['model']} · {S['effort']} · {S['ctx']} · {on_n} tools · {S['mode']}\n", DIM)
        if self.note:
            t.append(self.note + "\n", MAG)
        t.append("↑↓ choose · ⏎ next · ← back · S start now · A agent · Esc cancel" + (" · Space toggle" if step == "tools" else "")
                 + (" · L log in · K enter key" if step == "account" and any(acts.needs(self.app.home, o[0]) for o in opts) else ""), DIM)
        self.query_one("#card", Static).update(t)

    async def start(self):
        if self.busy:
            return
        if self.is_remote(self.S["server"]):
            return await self.go_remote(self.S["server"])
        S = self.S
        on, off = data.loadout(self.items, S["off"])
        if S["agent"]:                                      # the agent decides what the person did not change; its own tools stay
            ask = {k: S[k] if k in self.touched else None for k in ("account", "model", "effort", "mode")}
            ask.update(context=S["ctx"] if "ctx" in self.touched else None, agent=S["agent"], tools_on=None, tools_off=off or None)
        else:
            ask = dict(account=S["account"], model=S["model"], effort=S["effort"], context=S["ctx"], mode=S["mode"],
                       tools_on=on or None, tools_off=off or None)
        self.busy, self.note = True, "opening the chat …"
        self.draw()
        try:
            session = await asyncio.to_thread(lambda: _loop().open_session(self.app.home, server=S["server"], asker=self.app.ask, **ask))
        except Exception as e:                                      # noqa: BLE001 - the card shows why and stays
            self.busy, self.note = False, str(e) or type(e).__name__
            return self.draw()
        self.busy = False
        await self.app.enter_chat(session)

    def is_remote(self, name: str) -> bool:
        return any(s.name == name and not s.local for s in self.snap.servers)

    async def go_remote(self, name: str):
        """A remote server opens its permanent session (ssh + tmux) instead of a chat here."""
        ok, msg = acts.remote(self.app, name)
        self.note = "" if ok else msg
        self.app.notify(msg, severity="information" if ok else "error")
        self.draw()

    async def pick_agent(self, e: events.Key):
        """A key on the agent card: up / down move, Enter takes the highlighted agent, Esc or A go back to the cards."""
        k, rows = e.key, [("", True), *((n, ok) for n, _d, ok in self.agents)]
        c = self.cur.get("agent", 0)
        self.note = ""
        if k in ("up", "down"):
            self.cur["agent"] = (c + (-1 if k == "up" else 1)) % len(rows)
        elif k == "enter":
            if not rows[c][1]:
                self.note = "not available"
            else:
                self.take_agent(rows[c][0])
                self.picking = False
        elif k in ("escape", "a"):
            self.picking = False
        else:
            return
        e.stop()
        self.draw()

    async def on_key(self, e: events.Key):
        if self.picking:
            return await self.pick_agent(e)
        step, k = STEPS[self.i], e.key
        opts, c = self.options(step), self.cursor(step)
        self.note = ""
        if k == "a":
            self.picking, self.cur["agent"] = True, next((j + 1 for j, a in enumerate(self.agents) if a[0] == self.S["agent"]), 0)
        elif k in ("up", "down") and opts:
            n = self.cur[step] = (c + (-1 if k == "up" else 1)) % len(opts)
            if step != "tools" and opts[n][3]:
                self.S[KEY[step]] = opts[n][0]
                self.touched.add(KEY[step])
                if step == "account":
                    self.touched.add("model")
                    self.S["model"] = data.default_model(self.app.home, opts[n][0], self.S["model"])
                    self.cur.pop("model", None)
        elif k == "space" and step == "tools" and opts:
            self.S["off"] ^= {opts[c][0]}
        elif k == "enter":
            if step == "server" and opts and self.is_remote(opts[c][0]):
                return await self.go_remote(opts[c][0])
            if step != "tools" and opts and not opts[c][3]:
                self.note = "not available"
            elif self.i == len(STEPS) - 1:
                return await self.start()
            else:
                self.i += 1
        elif k in ("left", "backspace"):
            self.i = max(0, self.i - 1)
        elif k == "s":
            return await self.start()
        elif k in ("l", "k") and step == "account" and opts:
            name, need = opts[c][0], acts.needs(self.app.home, opts[c][0])
            if need == ("login" if k == "l" else "key"):
                if k == "l":
                    ok, msg = acts.login(self.app, name)
                    self.app.notify(msg, severity="information" if ok else "error")
                else:
                    acts.ask_key(self.app, name, after=self.draw)
            else:
                self.note = f"{name}: nothing to " + ("log in" if k == "l" else "enter") + " here"
        elif k == "escape":
            await self.app.back()
            return e.stop()
        else:
            return
        e.stop()
        self.draw()


# ----------------------------------------------------------------- resume list
class ResumeScreen(Screen):
    def __init__(self, archived: bool = False):
        super().__init__()
        self.rows, self.c, self.note = None, 0, ""
        self.sessions, self.names, self.unfolded = [], {}, set()    # sessions as read, run names, ids of the groups unfolded
        self.confirm = ""                                           # id of the chat waiting for the "end it?" answer
        self.archived = archived                                    # Tab: the archive instead of the recent chats (True: start there)

    def compose(self):
        yield Static(id="list")

    def regroup(self):
        """Rebuild the lines from the sessions: a run that another run started sits under its parent, closed unless unfolded."""
        keep = self.rows[self.c].id if self.rows and self.c < len(self.rows) else None
        self.rows = group_sessions(self.sessions, self.names, self.unfolded)
        self.c = next((i for i, g in enumerate(self.rows) if g.id == keep), min(self.c, max(0, len(self.rows) - 1)))

    async def on_mount(self):
        self.run_worker(warm(LANDING_WARM))                        # the landing rests under this list: bring the chat in
        self.draw()
        h = self.app.home
        self.names = await asyncio.to_thread(lambda: data.safe(lambda: session_names(h), {}))
        self.sessions = await asyncio.to_thread(lambda: data.safe(lambda: list_session_groups(h, 30, self.names, archived=self.archived), []))
        self.regroup()
        self.draw()

    def draw(self):
        t = Text(spaced("archive" if self.archived else "resume") + "\n", f"bold {CYAN}")
        t.append(("archived chats: they stay out of the other lists" if self.archived else "pick a chat to dive back into") + "\n\n", DIM)
        rows = self.rows or []
        cap = max(5, self.size.height - 10)
        first = max(0, min(self.c - cap // 2, len(rows) - cap))
        for i, g in enumerate(rows[first:first + cap], first):
            sel = i == self.c
            s = g.session
            pad = "  " * g.depth
            fold = ("▾ " if g.id in self.unfolded else "▸ ") if g.kids else "  "
            more = f" ({g.kids})" if g.kids and g.id not in self.unfolded else ""
            if s is None:                                           # the parent is not in the list: a header with its name
                t.append(" ▌ " if sel else "   ", CYAN)
                t.append(f"{pad}{fold}{data.clean(g.label)[:40]}{more}\n", f"bold {BG} on {CYAN}" if sel else DIM)
                continue
            col = {"running": MINT, "paused": AMBER, "done": DIM}[data.state_of(s)]
            t.append(" ▌ " if sel else "   ", CYAN)
            room = max(4, 28 - len(pad + fold + more))
            t.append(f"{(pad + fold + data.clean(s.title or 'new chat')[:room] + more):<29}", f"bold {BG} on {CYAN}" if sel else TXT)
            t.append(f"  {s.account[:12]:<13}{s.model[:10]:<11}{s.mode[:8]:<9}{data.ago(s.last_active):<11}", TXT if sel else DIM)
            t.append(f"${s.cost_usd:.2f}{'~' if s.cost_est else ' '}  ", AMBER if sel else DIM)
            t.append(f"● {s.status}\n", col)
            if sel and s.description:
                t.append(f"     {data.clean(s.description)[:90]}\n", DIM)
        if self.rows is None:
            t.append("loading …\n", DIM)
        elif not rows:
            t.append("the archive is empty\n" if self.archived else "no chats yet: start one with New chat\n", DIM)
        if self.note:
            t.append("\n" + self.note + "\n", MAG)
        t.append("\n↑↓ choose · ⏎ " + ("restore and open" if self.archived else "open") + " · →/← unfold/fold group · n notes · e end · "
                 + ("a restore · Tab recent chats" if self.archived else "a archive · Tab archive") + " · Esc back", DIM)
        self.query_one("#list", Static).update(t)

    async def end_chat(self, s) -> None:
        """`e` then `y`: end the highlighted chat (this machine only; see runs.request_stop)."""
        h = self.app.home
        self.note = f"ending {data.clean(s.title or 'new chat')[:30]} …"
        self.draw()
        try:
            self.note = await asyncio.to_thread(request_stop, h, s.id)
        except Exception as err:                                     # noqa: BLE001 - the list shows why and stays
            self.note = data.clean(str(err) or type(err).__name__)
        await self.reload()

    async def reload(self) -> None:
        """Read the list again (recent chats or archive, as shown) and keep the cursor on its chat."""
        h = self.app.home
        self.sessions = await asyncio.to_thread(lambda: data.safe(lambda: list_session_groups(h, 30, self.names, archived=self.archived), self.sessions))
        self.regroup()
        self.draw()

    async def switch_view(self) -> None:
        """`Tab`: the archive instead of the recent chats, and back."""
        self.archived = not self.archived
        self.rows, self.c, self.unfolded, self.note, self.confirm = None, 0, set(), "", ""
        self.draw()
        await self.reload()

    async def toggle_archive(self, s) -> None:
        """`a`: archive the highlighted chat, or restore it in the archive view.  The chat stays where it is on disk (a marker
        file); one that another process still holds is asked to let go first, which may take a moment."""
        h, title = self.app.home, data.clean(s.title or "new chat")[:30]
        self.note = f"{'restoring' if self.archived else 'archiving'} {title} …"
        self.draw()
        try:
            say = await asyncio.to_thread(unarchive_chat if self.archived else (lambda home, sid: archive_chat(home, sid, "tui")), h, s.id)
        except Exception as err:                                     # noqa: BLE001 - the list shows why and stays
            self.note = data.clean(str(err) or type(err).__name__)
            return self.draw()
        self.note = f"{say}: {title}"
        await self.reload()

    async def on_key(self, e: events.Key):
        n = len(self.rows or [])
        if self.confirm:                                            # the "end?" question: y ends, any other key says no
            sid, self.confirm = self.confirm, ""
            if e.key == "y" and n:
                s = next((r.session for r in self.rows if r.id == sid and r.session), None)
                if s is not None:
                    e.stop()
                    return await self.end_chat(s)
            self.note = ""
            if e.key == "escape":
                e.stop()
                return self.draw()
        if e.key == "tab":
            e.stop()
            e.prevent_default()
            return await self.switch_view()
        if e.key in ("up", "down") and n:
            self.c = (self.c + (-1 if e.key == "up" else 1)) % n
        elif e.key in ("right", "left", "space") and n and self.rows[self.c].kids:
            g = self.rows[self.c]
            if e.key == "right" or (e.key == "space" and g.id not in self.unfolded):
                self.unfolded.add(g.id)
            elif g.id in self.unfolded:
                self.unfolded.discard(g.id)
            self.regroup()
        elif e.key == "enter" and n and self.rows[self.c].session is None:      # a header: unfold or fold it
            self.unfolded.symmetric_difference_update({self.rows[self.c].id})
            self.regroup()
        elif e.key in ("n", "e", "a") and n and self.rows[self.c].session is None:
            self.note = "this line is only a group header: pick a chat inside it"
        elif e.key == "a" and n and self.rows[self.c].depth > 0:
            self.note = "a chat started by another follows it: archive the chat at the top of the group"
        elif e.key == "a" and n:
            e.stop()
            return await self.toggle_archive(self.rows[self.c].session)
        elif e.key == "n" and n:
            s = self.rows[self.c].session
            self.app.push_screen(NotesScreen(s.id, s.title))
        elif e.key == "e" and n:
            s = self.rows[self.c].session
            if data.state_of(s) == "done":
                self.note = "this chat has ended already"
            else:
                self.confirm = s.id
                self.note = f"end '{data.clean(s.title or 'new chat')[:30]}'?  y = yes, any other key = no"
        elif e.key == "enter" and n:
            h, sid, from_archive = self.app.home, self.rows[self.c].id, self.archived
            try:
                session = await asyncio.to_thread(lambda: _loop().resume_session(h, sid, asker=self.app.ask))
                if from_archive:                                    # going on in an archived chat means wanting it back in the list
                    await asyncio.to_thread(unarchive_chat, h, sid)
            except Exception as err:                                # noqa: BLE001 - the list shows why and stays
                self.note = data.clean(str(err) or type(err).__name__)
            else:
                return await self.app.enter_chat(session, resumed=True)
        elif e.key == "escape":
            await self.app.back()
            return e.stop()
        else:
            return
        e.stop()
        self.draw()


# ---------------------------------------------------- notes of a chat from the resume list
class NotesScreen(Screen):
    """Ctrl+N of a chat that is not open here: name and description on top, then its to-dos.  The to-dos are read only
    while another process has the chat open; the name goes to the disk either way (that process adopts it)."""
    BINDINGS = [("escape", "back", "back")]

    def __init__(self, run_id: str, title: str = ""):
        super().__init__()
        self.run_id, self.title_text = run_id, title

    def compose(self):
        h = self.app.home
        holder = lock_holder(h, self.run_id)
        names = Names(lambda: read_names(h, self.run_id), lambda field, text: set_name_on_disk(h, self.run_id, field, text))
        yield NotesPane(todos.Store(h.path("runs") / self.run_id), readonly=bool(holder), names=names, id="notes")

    def on_mount(self):
        pane = self.query_one(NotesPane)
        pane.styles.display, pane.styles.width = "block", "100%"
        view = pane.query_one("#np-todos")
        view.cur = NAME
        view.refresh_rows()
        pane.focus_main()

    async def action_back(self):
        if not self.query_one(NotesPane).escape():
            self.app.pop_screen()


# ------------------------------------------------------------------- accounts
def left(seconds: float) -> str:
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h" if d else f"{h}h {r // 60:02d}m" if h else f"{r // 60}m"


class AccountsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "back"), ("up", "pick(-1)", "up"), ("down", "pick(1)", "down"),
                ("l", "login", "log in"), ("k", "key", "key")]

    def __init__(self):
        super().__init__()
        self.rows = None
        self.sel = None                                  # the account L and K act on
        self._timer = None

    def names(self) -> list[str]:
        return [r[0] for r in self.rows or []]

    def action_pick(self, d: int):
        if self.names():
            self.sel = self.names()[(self.names().index(self.sel) + d) % len(self.names())] if self.sel in self.names() else self.names()[0]
            self.draw()

    def action_login(self):
        if self.sel is None:
            return
        ok, msg = acts.login(self.app, self.sel)
        self.app.notify(msg, severity="information" if ok else "error")
        self.run_worker(self.load())                     # the login state is read again

    def action_key(self):
        if self.sel is not None:
            acts.ask_key(self.app, self.sel, after=lambda: self.run_worker(self.load()))

    def compose(self):
        yield Static(id="acc")

    async def on_mount(self):
        self.draw()
        await self.load()

    async def load(self):
        h, rows = self.app.home, []
        for name, a in data.safe(lambda: h.load_providers().accounts, {}).items():
            try:
                p = await asyncio.to_thread(make_provider, h, name)     # builds the adapter only: no CLI process starts here
                rows.append((name, a, await p.usage(), getattr(p, "limits", None) or {}, ""))
            except NotImplementedError:
                rows.append((name, a, None, {}, "no adapter yet (phase 2)"))
            except Exception as e:                                       # noqa: BLE001
                rows.append((name, a, None, {}, data.clean(str(e) or type(e).__name__)))
        self.rows = rows
        names = self.names()
        if self.sel not in names:                        # start on the first account that is waiting for a login or a key
            self.sel = next((n for n in names if acts.needs(h, n)), names[0] if names else None)
        self.draw()
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(data.refresh_secs(h), self.load)

    def draw(self):
        h = self.app.home
        t = Text(spaced("accounts") + "\n", f"bold {CYAN}")
        t.append("limits as last seen in a chat (kept in the state folder)\n\n", DIM)
        for name, a, u, limits, err in self.rows or []:
            t.append(" ▌" if name == self.sel else "  ", CYAN)
            t.append(f"{name:<16}", f"bold {TXT}")
            t.append(f"{a.type}" + (f"  ·  default {a.default_model}" if a.default_model else "") + "\n", DIM)
            need = acts.needs(h, name)
            if need:
                t.append("    " + data.clean(data.account_note(h, name, a).split("  ·  ")[-1]), AMBER)
                t.append("   press " + ("L to log in" if need == "login" else "K to enter the key") + "\n", CYAN)
            for lab, kind, val in (("5h  ", "five_hour", u and u.five_hour), ("week", "seven_day", u and u.seven_day)):
                if val is None:
                    continue
                t.append(f"    {lab} ")
                t.append_text(bar(val * 100, 30, AMBER if val > 0.75 else MINT))
                t.append(f"  {val * 100:4.0f}%", TXT)
                r = limits.get(kind) and limits[kind].resets_at
                if r:
                    r = r / 1000 if r > 1e11 else r
                    t.append("  " + (f"resets in {left(r - time.time())}" if r > time.time() else "reset since"), DIM)
                t.append("\n")
            if u is not None and u.five_hour is None and u.seven_day is None and u.balance_usd is None:
                t.append(f"    no limits seen yet: {u.note}\n", DIM)
            if u is not None and u.balance_usd is not None:
                t.append(f"    balance  ${u.balance_usd:,.2f}\n", AMBER)
            if err:
                t.append(f"    {err}\n", MAG if "phase 2" not in err else DIM)
            t.append("\n")
        if self.rows is None:
            t.append("loading …\n\n", DIM)
        elif not self.rows:
            t.append("no accounts: add a file to providers/\n\n", DIM)
        t.append("↑↓ choose · L log in · K enter an API key · Esc back", DIM)
        self.query_one("#acc", Static).update(t)


# ------------------------------------------------------------------ dashboard
class DashScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "back")]

    def __init__(self):
        super().__init__()
        self.t0, self.view = time.monotonic(), None

    def compose(self):
        yield Static(id="dash")

    async def on_mount(self):
        self.set_interval(0.5, self.draw)
        await self.load()

    async def load(self):
        h = self.app.home

        def read():
            return (data.snapshot(h, 40), data.safe(lambda: list_runs(h, 200), []),
                    data.safe(lambda: cost_summary(h, since="24h", by="account"), {}))
        self.view = await asyncio.to_thread(read)
        self.set_timer(self.view[0].refresh, self.load)

    def draw(self):
        t = Text(spaced("dashboard") + "\n\n", f"bold {CYAN}")
        if self.view is None:
            t.append("loading …", DIM)
            return self.query_one("#dash", Static).update(t)
        snap, runs, costs = self.view
        day = [r for r in runs if data.age(r.start) < 86400]
        live = [r for r in day if r.status == "running"]
        t.append("  OPEN ", DIM)
        t.append(f"{len(live)}", f"bold {MINT}")
        t.append("    TOKENS 24H ", DIM)
        t.append(f"{sum(r.tokens.input + r.tokens.output for r in day):,}", f"bold {CYAN}")
        t.append("    COST 24H ", DIM)
        t.append(f"${sum(costs.values()):.2f}" + ("~" if any(r.cost_est for r in day) else ""), f"bold {AMBER}")
        t.append("    SERVERS ", DIM)
        t.append(f"{len(snap.servers)}\n\n", f"bold {VIOLET}")
        t.append("chats per server\n", f"bold {TXT}")
        for s in snap.servers:
            t.append(f"  {s.name:<18}", TXT)
            ss = [x for x in snap.sessions if x.server == s.name]
            for x in ss:
                t.append("■ ", {"running": MINT, "paused": AMBER, "done": DIM}[data.state_of(x)])
            t.append(f"{len(ss)}\n", DIM)
        if costs:
            t.append("\ncost last 24 h\n", f"bold {TXT}")
            t.append("".join(f"  {a:<18}${v:.2f}\n" for a, v in sorted(costs.items())), DIM)
        t.append("\nrecent runs\n", f"bold {TXT}")
        names = {s.id: data.clean(s.title) for s in snap.sessions}
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int((time.monotonic() - self.t0) * 4) % 10]
        for r in runs[:12]:
            on = r.status == "running"
            t.append(f"  {spin if on else '●'} ", MINT if on else AMBER if r.status in ("paused", "handover") else DIM)
            t.append(f"{data.ago(r.start):<10}{(names.get(r.id) or r.agent or 'chat')[:24]:<26}{r.model[:10]:<11}{r.server[:12]:<13}", TXT)
            t.append(f"${r.cost_usd:.2f}{'~' if r.cost_est else ' '}  {r.status}\n", DIM)
        if not runs:
            t.append("  nothing has run yet\n", DIM)
        t.append("\nEsc back", DIM)
        self.query_one("#dash", Static).update(t)


# --------------------------------------------------------------------- modals
class Pick(ModalScreen):
    """A list to choose from; `multi` turns it into check boxes (space toggles, enter applies)."""

    def __init__(self, title, options, current=None, multi=False):
        super().__init__()
        self.title_, self.opts, self.multi = title, options, multi
        self.checked = set(current or []) if multi else set()
        self.c = next((i for i, o in enumerate(options) if o[0] == current), 0) if not multi else 0

    def compose(self):
        yield Static(id="pick")

    def on_mount(self):
        self.draw()

    def draw(self):
        t = Text(spaced(self.title_) + "\n\n", f"bold {CYAN}")
        for i, (v, lab, desc, ok) in enumerate(self.opts):
            if self.multi:
                t.append(" ● " if v in self.checked else " ○ ", MINT if v in self.checked else DIM)
            t.append(f" {lab:<18}", f"bold {BG} on {CYAN}" if i == self.c else (TXT if ok else OFF))
            t.append(f" {desc[:34]}\n", DIM)
        t.append("\n↑↓ · " + ("space toggle · ⏎ apply" if self.multi else "⏎ choose") + " · Esc cancel", DIM)
        self.query_one("#pick", Static).update(t)

    def on_key(self, e: events.Key):
        k, n = e.key, len(self.opts)
        if k in ("up", "down") and n:
            self.c = (self.c + (1 if k == "down" else -1)) % n
        elif k == "space" and self.multi and n:
            self.checked ^= {self.opts[self.c][0]}
        elif k == "enter" and (self.multi or (n and self.opts[self.c][3])):
            e.stop()
            self.dismiss(set(self.checked) if self.multi else self.opts[self.c][0])      # (not returned: Textual would await it)
            return
        elif k == "escape":
            e.stop()
            self.dismiss(None)
            return
        e.stop()
        self.draw()


class AskModal(ModalScreen):
    """The core asks before it does something (a rule says ask).  The question is a plain sentence plus the reason
    (escaped, in a scrollable body); the raw command is folded away and opens with d, whole (the body scrolls), so
    the part of a long line that runs is never hidden from the one who says yes.  Yes, always
    and no can always be chosen, however long the text is.
    Keys typed before it opened cannot answer it: it ignores keys for ARM seconds, and an answer needs the
    letter (or the arrows) and then Enter.  Esc is no."""
    # SHOW: the body scrolls, so a long text (a heredoc that writes a file) is shown whole and can be answered yes;
    # only a text beyond this many characters is cut and then answers only no (2026-10-23: 4000 was too small).
    ARM, SHOW = 0.5, 100_000
    DETAIL_SHOW = 100_000                                    # the folded-out command is shown whole up to this many characters
    FIXED_ROWS = 11                                          # border 2 + padding 2 + head 2 + key block up to 5
    OPTIONS = (("yes", "Y", MINT, "yes"), ("always", "A", AMBER, "always in this chat"), ("no", "N", MAG, "no"))

    def __init__(self, question: str, detail: str = ""):
        super().__init__()
        clean = data.clean(question)
        blank = lambda t: re.sub(r"[ \t]{4,}", lambda m: f" ␣×{len(m.group())} ", data.visible(t))   # long blank runs cannot hide a tail
        self.lines = [blank(l) for l in clean.split(chr(10))]
        self.question = " ⏎ ".join(self.lines)
        n = len(clean)
        self.too_long = len(self.question) > self.SHOW       # cut in the middle, the total is said; yes stays possible
        if self.too_long:
            tail = self.SHOW // 4
            self.lines = [(self.question[: self.SHOW - tail] + " … " + self.question[-tail:])]
        self.size_note = f"shortened: {n} characters in all" if self.too_long else ""
        self.shown = chr(10).join(self.lines)
        raw = data.clean(detail)
        self.detail_chars = len(raw)
        self.detail_cut = len(raw) > self.DETAIL_SHOW
        if self.detail_cut:                                  # beyond the limit: start and end, the total is said
            tail = self.DETAIL_SHOW // 4
            raw = raw[: self.DETAIL_SHOW - tail] + " … " + raw[-tail:]
        self.detail_rows = [blank(l) for l in raw.split(chr(10))] if raw.strip() else []
        self.detail_open = False
        self.sel = 2                                         # the safe answer is the default

    def compose(self):
        yield Vertical(Static(id="askhead"), VerticalScroll(Static(id="askq"), id="askbody"), Static(id="askkeys"), id="ask")

    def on_mount(self):
        self.t0 = time.monotonic()
        self.query_one("#askhead", Static).update(Text(spaced("permission") + "\n", f"bold {AMBER}"))
        self.paint_body()
        self.draw()
        self.fit_body()

    def paint_body(self):
        """Sentence first (bold), reason and program after it (dim), then the folded command: a line saying it exists,
        or the whole command when opened with d (the body scrolls)."""
        t = Text()
        for i, l in enumerate(self.lines):
            t.append(l + "\n", f"bold {TXT}" if i == 0 else DIM)
        if self.detail_rows:
            if self.detail_open:
                t.append("\ncommand:\n", DIM)
                t.append("\n".join(self.detail_rows), TXT)
                if self.detail_cut:
                    t.append(f"\n… shortened in the middle, {self.detail_chars} characters in all", DIM)
            else:
                t.append(f"\n▸ d shows the command ({self.detail_chars} characters)", DIM)
        self.query_one("#askq", Static).update(t)

    def on_resize(self, e: events.Resize):
        self.fit_body()

    def fit_body(self):
        """Give the text the room that is left above the fixed part (frame, head, key hints), so the hints
        are never pushed off a small terminal; at least 3 lines, at most 12 or two fifths of the screen."""
        h = self.app.size.height
        keys = self.query_one("#askkeys", Static).outer_size.height or 5       # the hints wrap in a narrow terminal
        room = int(h * 0.9) - (self.FIXED_ROWS - 5 + keys)
        self.query_one("#askbody", VerticalScroll).styles.max_height = max(1, min(max(12, int(h * 0.4)), room))

    @property
    def armed(self) -> bool:
        return time.monotonic() - self.t0 >= self.ARM

    def draw(self):
        t = Text("\n" + (self.size_note + "\n\n" if self.size_note else "\n"))
        t.stylize(DIM)
        for i, (_, letter, col, label) in enumerate(self.OPTIONS):
            t.append(f" {letter} ", f"bold {BG} on {col}")
            t.append(f" {label} ", f"bold {TXT} on #2a2f4d" if i == self.sel else TXT)
            t.append("  ")
        t.append("\n⏎ answers · ←→ or letter chooses" + (" · d command" if self.detail_rows else "") + " · ↑↓ scrolls · Esc no", DIM)
        self.query_one("#askkeys", Static).update(t)

    def on_key(self, e: events.Key):
        e.stop()
        k = e.key
        if k == "escape":
            self.dismiss("no")                               # (not returned: Textual would await it)
            return
        if not self.armed:
            return
        body = self.query_one("#askbody", VerticalScroll)
        if k == "d" and self.detail_rows:
            self.detail_open = not self.detail_open
            self.paint_body()
            body.scroll_home(animate=False)
        elif k in ("left", "right"):
            self.sel = (self.sel + (1 if k == "right" else -1)) % 3
        elif k in ("y", "a", "n"):
            self.sel = "yan".index(k)
        elif k == "up":
            body.scroll_up(animate=False)
        elif k == "down":
            body.scroll_down(animate=False)
        elif k in ("pageup", "pagedown"):
            (body.scroll_page_up if k == "pageup" else body.scroll_page_down)(animate=False)
        elif k == "enter":
            self.dismiss(self.OPTIONS[self.sel][0])
            return
        self.draw()
