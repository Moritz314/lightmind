"""Orbit, the terminal UI of Lightmind: `lightmind.tui.app.main(home)`.

Needs the optional extra: `pip install lightmind[tui]` (Textual)."""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from typing import TYPE_CHECKING

from rich.theme import Theme
from textual.app import App
from textual.binding import Binding

from .. import checks, core, launch, sync                # noqa: F401 - `sync` stays reachable as app.sync (tests patch it)
from ..files import Home
from . import data, profile, writerguard
from .boot import BootScreen
from .acts import KeyModal
from .keys import KeysScreen, collect
from .screens import AskModal, MapScreen, ResumeScreen
from .theme import AMBER, BG, CYAN, NEUTRAL, PANEL, TXT

if TYPE_CHECKING:
    from .chat import ChatScreen


class Orbit(App):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("f1,ctrl+underscore,ctrl+slash,ctrl+g", "keys", "all keys", priority=True),   # Ctrl+/ arrives as ctrl+underscore or ctrl+slash; F1 is often a laptop media key, so Ctrl+G too
                Binding("ctrl+q", "archive_quit", "archive the chat and quit", priority=True)]      # replaces Textual's own Ctrl+Q (quit)
    CSS = f"""
    Screen {{ background: {BG}; color: {TXT}; align: center middle; }}
    MapScreen {{ align: left top; layout: vertical; }}
    Banner {{ height: 7; width: 100%; }}
    #map {{ height: 1fr; width: 100%; }}
    #menu {{ height: 1; width: 100%; text-align: center; }}
    #foot {{ height: 2; width: 100%; }}
    #card, #acc {{ width: 96; height: auto; max-height: 100%; padding: 1 2; }}
    #list, #dash, #sett {{ width: 116; height: auto; max-height: 100%; padding: 1 2; }}
    BootScreen {{ align: left top; }}
    #boot {{ width: 100%; height: auto; padding: 1 2; }}
    ChatScreen {{ align: left top; }}
    #body {{ width: 100%; height: 100%; }}
    #box {{ width: 1fr; height: 100%; }}
    NotesPane {{ display: none; width: 46; height: 100%; padding: 0 1; background: {BG}; border-left: heavy {NEUTRAL}; }}
    #np-head, #np-help {{ height: 1; background: {BG}; }}
    #np-todos {{ height: 1fr; background: {BG}; }}
    #np-in {{ background: #000000 40%; border: none; height: 1; padding: 0 1; }}
    #np-in:focus {{ border: none; }}
    #np-edit {{ height: 6; background: #000000 40%; border: none; padding: 0 1; }}
    #np-edit:focus {{ border: none; }}
    #head {{ height: 3; padding: 0 1; }}
    #msgs {{ height: 1fr; padding: 0 2; scrollbar-size-vertical: 1; scrollbar-color: #2a2f4d; scrollbar-background: {BG}; }}
    .m {{ margin-bottom: 0; }}
    #hint {{ height: 1; padding: 0 2; }}
    #pop {{ display: none; height: auto; max-height: 12; padding: 0 2; background: {BG}; }}
    #inp {{ background: {BG}; background-tint: transparent; border: none; height: auto; max-height: 8; padding: 0 2; scrollbar-size-vertical: 1; scrollbar-color: #2a2f4d; scrollbar-background: {BG}; }}
    #inp:focus {{ background: {BG}; background-tint: transparent; border: none; }}
    #status {{ height: 2; padding: 0 0; background: {BG}; }}
    #hint {{ background: {BG}; }}
    #subs {{ display: none; height: 1; padding: 0 2; background: {BG}; }}
    SubScreen {{ align: left top; }}
    #smsgs {{ height: 1fr; padding: 0 2; scrollbar-size-vertical: 1; scrollbar-color: #2a2f4d; scrollbar-background: {BG}; }}
    #shint {{ height: 1; padding: 0 2; background: {BG}; }}
    KeysScreen {{ background: #000000 60%; align: center middle; }}
    #keys {{ width: 72; max-width: 100%; height: auto; max-height: 90%; background: {PANEL}; border: heavy {CYAN}; padding: 1 2; }}
    ToolHead:focus {{ background: #1a1f3a; }}
    StatusBar:focus {{ background: {BG}; }}
    Pick, AskModal, KeyModal {{ background: #000000 60%; align: center middle; }}
    AskModal {{ align: center bottom; }}                      /* a request sits at the bottom, where the input is, not in the middle */
    #keybox {{ width: 64; height: auto; background: {PANEL}; border: heavy {AMBER}; padding: 1 2; }}
    #keyin {{ background: #000000 40%; border: none; height: 1; padding: 0 1; }}
    #askbody {{ height: auto; max-height: 12; }}
    #pick, #ask {{ width: 64; max-width: 100%; height: auto; background: {PANEL}; border: heavy {CYAN}; padding: 1 2; }}
    #ask {{ width: 90; max-width: 100%; max-height: 90%; }}
    """

    def __init__(self, home: Home, anim: bool = True, boot=None, direct: dict | None = None):
        """`boot`: None = as `ui.boot` says (and `LM_NO_BOOT=1` switches it off), False = no checklist, or the list of
        `checks.Pending` the caller started early (so the checks run while Textual is still being imported).
        `direct` (`lm open`, `lm q`, `lm resume`): `{"screen": "resume"}` (`lm resume` and `lmr` without a name) shows the resume menu of
        the start menu, the same `ResumeScreen`; a chat picked there opens in this window.  `{"id": <chat id>}` resumes that chat (with `"rc": <name>` it also turns Remote Control
        on, `""` = the default name), `{"new": {"agent": ..., "project": ..., and for `lm q` account, model, effort, context, mode, name, workdir}}` starts a new
        one, both straight away: no checklist, no start menu, and so no menu below the chat (Esc ends the app)."""
        super().__init__()
        self.home, self.anim, self.calm = home, anim, not anim
        self.boot, self.boot_note = boot, ""
        self.direct = direct
        self.console.push_theme(Theme({f"markdown.h{i}": f"bold {CYAN}" for i in (1, 2, 3, 4)} | {"markdown.code": f"bold {AMBER}"}))
        self.prof = profile.start(home)               # LM_PROFILE=1: frame times into <state>/profile.log, else None
        self.chat: ChatScreen | None = None          # the open chat, if any
        self.exit_lines: list[str] = []              # what the on_end hooks printed

    def on_mount(self):
        core.sync_start(self.home)                   # a sync repo: pull in the background; the checklist line "sync" shows the result
        self.set_interval(core.TICK, self.sync_tick)
        if self.direct is not None:
            self.run_worker(self.open_direct())
            return
        pending = self.boot
        mode = checks.boot_mode(self.home)
        if pending is None and mode != "false" and not os.environ.get("LM_NO_BOOT"):
            pending = checks.start(self.home)
        if pending and mode != "false":
            self.push_screen(BootScreen(pending, brief=mode == "brief"))
        else:
            self.push_screen(MapScreen())

    async def sync_tick(self):
        await core.sync_tick(self.home)                  # it looks at the settings and starts a thread: not on the UI thread

    def open_chat(self, session, resumed: bool = False):
        from .chat import ChatScreen                 # imported here: it pulls in rich.markdown, which the landing never needs
        self.switch_screen(ChatScreen(session, resumed))

    async def open_direct(self):
        """`lm open`: the chat the command named, as the only screen.  Why it cannot be had (an unknown id, an agent that
        is gone ...) is what the command prints, with exit code 1."""
        from .chat import ChatScreen
        spec = self.direct or {}
        if spec.get("screen") == "resume":           # the menu entry "Resume chat", without the start menu below it
            return self.push_screen(ResumeScreen(archived=bool(spec.get("archived"))))
        loop = importlib.import_module("lightmind.loop")
        try:
            if "id" in spec:
                session = await asyncio.to_thread(lambda: loop.resume_session(self.home, spec["id"], asker=self.ask))
            else:
                new = spec.get("new") or {}
                session = await asyncio.to_thread(lambda: loop.open_session(
                    self.home, agent=new.get("agent") or None, project=new.get("project") or None, asker=self.ask,
                    account=new.get("account") or None, model=new.get("model") or None, effort=new.get("effort") or None,
                    context=new.get("context") or None, mode=new.get("mode") or None, workdir=new.get("workdir") or ""))
                if new.get("name"):
                    runs = importlib.import_module("lightmind.runs")
                    try:
                        runs.claim_name(self.home, new["name"], session.id)
                    except ValueError:               # another chat took the name since the command looked
                        session.close("failed", f"session name {new['name']!r} is taken")
                        raise
                    if new.get("name_fixed"):
                        session.run.set_name("title", new["name"])       # chosen with -n: the title, locked
        except Exception as e:                       # noqa: BLE001 - anything that stops the chat from opening is said, not shown as a trace
            self.exit_lines.append(f"{spec.get('label') or 'lm open'}: {str(e) or type(e).__name__}")
            return self.exit(return_code=1)
        chat = ChatScreen(session, resumed="id" in spec)
        if spec.get("rc") is not None:
            chat.rc_wanted = str(spec["rc"])         # `--rc NAME` opens the link, not `remote.auto` (which would race it)
        self.push_screen(chat)
        if spec.get("rc") is not None:
            self.run_worker(self.start_rc(chat, str(spec["rc"])))

    async def start_rc(self, chat, name: str):
        """`lm open --rc <name>`: Remote Control on as soon as the chat is on the screen."""
        await asyncio.wait_for(chat.shown.wait(), 30)
        await chat.cmd_rc(name)

    async def enter_chat(self, session, resumed: bool = False):
        """A chat that is ready: show it here, or give it to a new tab or window (`sessions.open`, see launch.py).  In the
        new place `lm open <id>` resumes it (the setup choices are in its record); the screen that asked stays, so the
        start menu is still there.  A chat that another process has open is only shown, here.  When the new place cannot
        be started the chat comes back to this window, and the reason is said."""
        plan = launch.plan_open(self.home, session.id)
        if plan.note:
            self.notify(plan.note, severity="warning")
        if not plan.separate or session.readonly or (self.direct or {}).get("screen"):     # `lmr`: the window that asked is the one that goes on
            return self.open_chat(session, resumed)
        await asyncio.to_thread(session.hand_over)
        ok, why = await asyncio.to_thread(launch.spawn, plan, self.home.root)
        if ok:
            self.notify(f"opened in a new {plan.what}")
            return await self.back()
        self.notify(f"could not open a new {plan.what} ({why}): the chat opens here", severity="warning")
        loop = importlib.import_module("lightmind.loop")
        try:
            again = await asyncio.to_thread(lambda: loop.resume_session(self.home, session.id, asker=self.ask))
        except Exception as e:                       # noqa: BLE001 - the card that asked shows why and stays
            return self.notify(f"{session.id}: {str(e) or type(e).__name__}", severity="error")
        self.open_chat(again, resumed=True)

    async def back(self):
        """Esc on a screen that sits right above the start menu: back to it.  In a tab that `lm open` started there is no
        menu below, so the last screen ends the app."""
        if self.direct is not None and len(self.screen_stack) <= 2:
            return await self.action_quit()
        self.pop_screen()

    async def ask(self, question, check=None) -> str:
        """The asker of a session (rules.Asker): a modal; y / a / n answer yes / always / no.  No answer within
        `run.ask_timeout` counts as no, and a turn that is stopped takes its question down with it."""
        done = asyncio.get_running_loop().create_future()
        modal = AskModal(str(question), str(getattr(check, "arg", "") or ""))
        self.push_screen(modal, lambda r: done.done() or done.set_result(r or "no"))
        try:
            return await asyncio.wait_for(done, data.duration((data.settings(self.home).get("run") or {}).get("ask_timeout"), 1800))
        finally:
            if self.screen is modal:
                modal.dismiss(None)

    def action_keys(self):
        """F1: every key that works on this screen and everywhere, from the live bindings."""
        if isinstance(self.screen, (AskModal, KeyModal)):         # a question waits for its answer: nothing goes on top of it
            return
        if isinstance(self.screen, KeysScreen):
            return self.pop_screen()
        self.push_screen(KeysScreen(collect(self)))

    async def action_archive_quit(self):
        """Ctrl+Q: an open chat that can be written is archived (`ChatScreen.archive`), then the app ends; in the start menu, or
        with a chat that is only read, it just ends (`action_quit`)."""
        chat = self.chat
        if chat is None or chat.s.readonly:
            return await self.action_quit()
        try:
            await chat.archive(leave=False)
            await core.sync_end(self.home, 15)                            # commit + push, never longer than that
        finally:
            self.exit()

    def action_help_quit(self) -> None:
        """Ctrl+C: Textual's own hint names the key bound to `quit`; Ctrl+Q is bound to `archive_quit` now, so say it here."""
        self.notify("Press [b]ctrl+q[/b] to quit the app (an open chat is archived)", title="Do you want to quit?")

    async def action_quit(self):
        try:
            if self.chat is not None:
                await self.chat.finish()
            await core.sync_end(self.home, 15)                            # commit + push, never longer than that
        finally:
            self.exit()


def anim_allowed() -> bool:
    return not (os.environ.get("NO_COLOR") or os.environ.get("LM_NO_ANIM") == "1" or not sys.stdout.isatty())


def main(home: Home, boot=None, open: dict | None = None) -> int:
    """Run Orbit.  `open` = `lm open`: see `Orbit(direct=...)`."""
    app = Orbit(home, anim=anim_allowed(), boot=boot, direct=open)
    guard = writerguard.install(app, home)           # a dead output thread ends the app with a message, it must not hang
    app.run()
    code = guard.report()                            # after the terminal is back: 1 + the German message when the output died
    for line in app.exit_lines:                      # what the on_end hooks said, now that the screen is gone
        sys.stdout.write(line.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8") + "\n")
    return code or app.return_code or 0
