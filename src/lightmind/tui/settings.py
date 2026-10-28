"""The settings screen (`/settings`, alias `/config`, the Settings row of the start menu): config.md and the agent files as
lists of fields, changed one value at a time and saved line by line.

Everything about the fields (which exist, which values each may take, reading a file, saving only what changed) is
`lightmind.settings`: the HTML page later uses the same module.  This file only draws it and takes the keys.

Keys: Up/Down choose a field, Enter changes it (a list of the allowed values), S saves what was changed, R reads the files
again (drops what was not saved), E opens the file in the editor, W is the settings page in the browser (not there yet),
Tab / Left / Right switch between General, Ultracode (the `workflow:` section) and Agents, Esc leaves (a second time when
something is not saved).
"""
from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import events
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Static

from .. import settings as core
from . import acts, data
from .screens import Pick, spaced
from .theme import AMBER, BG, CYAN, DIM, MAG, MINT, TXT

UNSET = "\0unset"                       # the Pick answers None for Esc, so "not set" travels under another name
AREAS = (("general", "General"), ("ultracode", "Ultracode"), ("agents", "Agents"))
KEYS = "↑↓ choose · ⏎ change · S save · R reload · E edit file · W web page · Tab area · Esc back"


def same(a: Any, b: Any) -> bool:
    """Strict: `1` is not `True`, and None is only None."""
    return a is None and b is None or (a is not None and b is not None and type(a) is type(b) and a == b)


class SettingsScreen(Screen):
    def __init__(self):
        super().__init__()
        self.area = "general"
        self.pages: dict[tuple[str, str], core.Page] = {}          # ("general", "") and ("agent", <name>)
        self.pending: dict[tuple[str, str], dict[core.KeyPath, Any]] = {}
        self.cur = {key: 0 for key, _ in AREAS}                    # the cursor of each area: an index into the fields
        self.note, self.warn, self.armed = "", False, False        # `armed`: Esc was pressed once with unsaved changes
        self.stamp: dict[tuple[str, str], Any] = {}                # (mtime, size) of each file when it was read

    def compose(self):
        yield Static(id="sett")

    def on_mount(self):
        self.draw()
        self.run_worker(self.load())                                # off the Mount handler: the first paint does not wait for the files
        self.set_interval(1.0, self.look)

    # ------------------------------------------------------------------ the files
    def read_all(self) -> dict[tuple[str, str], core.Page]:
        h = self.app.home
        out: dict[tuple[str, str], core.Page] = {("general", ""): core.read_general(h)}
        for name in data.safe(lambda: core.agent_names(h), []):
            out[("agent", name)] = core.read_agent(h, name)
        return out

    @staticmethod
    def stat(page: core.Page):
        try:
            st = page.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def stamps(self) -> dict[tuple[str, str], Any]:
        return {k: self.stat(p) for k, p in self.pages.items()}

    async def load(self) -> None:
        """Read config.md and the agent files again (off the UI thread); what was not saved is dropped."""
        self.pages = await asyncio.to_thread(self.read_all)
        self.pending.clear()
        self.armed = False
        self.stamp = await asyncio.to_thread(self.stamps)
        self.draw()

    async def look(self) -> None:
        """A file that changed on disk (the editor, another window) is read again, unless it has unsaved changes here: then
        the screen says so and leaves the choice to R."""
        if self.app.screen is not self or not self.pages:
            return
        now = await asyncio.to_thread(self.stamps)
        stale = [k for k in now if now[k] != self.stamp.get(k)]
        if not stale:
            return
        held = [k for k in stale if k in self.pending]
        if held:
            names = ", ".join(self.pages[k].path.name for k in held)
            return self.say(f"{names} changed on disk: R reads it again (and drops your changes to it)", warn=True)
        fresh = await asyncio.to_thread(self.read_all)
        self.pages = {k: (self.pages[k] if k in self.pending else p) for k, p in fresh.items()}
        self.stamp = await asyncio.to_thread(self.stamps)
        self.draw()

    # ------------------------------------------------------------------ what is on screen
    def entries(self) -> list[dict]:
        """The lines of the current area: group heads (the agents) and fields; only fields can be chosen."""
        out: list[dict] = []
        if self.area in ("general", "ultracode"):
            p = self.pages.get(("general", ""))
            return [{"pk": ("general", ""), "row": r} for r in (p.rows if p else []) if core.area_of(r.field) == self.area]
        for (scope, name), p in self.pages.items():
            if scope == "agent":
                out.append({"head": name, "pk": (scope, name)})
                out += [{"pk": (scope, name), "row": r} for r in p.rows]
        return out

    def current(self, entries: list[dict] | None = None) -> dict | None:
        fs = [e for e in (entries if entries is not None else self.entries()) if "row" in e]
        return fs[max(0, min(self.cur[self.area], len(fs) - 1))] if fs else None

    def value_of(self, e: dict) -> Any:
        """What the field will hold: the unsaved choice, else what the file has (None: not set)."""
        pk, path = e["pk"], e["row"].field.path
        return self.pending[pk][path] if path in self.pending.get(pk, {}) else e["row"].value

    def values_of(self, pk: tuple[str, str]) -> dict[core.KeyPath, Any]:
        return self.pages[pk].values() | self.pending.get(pk, {})

    def label_of(self, e: dict, value: Any) -> str:
        f = e["row"].field
        if value is None:
            return f.unset
        for v, label, _ in core.options(self.app.home, f, self.values_of(e["pk"])):
            if v is not None and same(v, value):
                return label
        return str(value)

    def say(self, text: str, warn: bool = False) -> None:
        self.note, self.warn = text, warn
        self.draw()

    def draw(self) -> None:
        try:
            box = self.query_one("#sett", Static)
        except NoMatches:                                           # not composed yet (or gone)
            return
        t = Text(spaced("settings") + "\n", f"bold {CYAN}")
        for key, label in AREAS:
            t.append(f" {label} ", f"bold {BG} on {CYAN}" if key == self.area else DIM)
            t.append("  ")
        t.append("\n\n")
        gen = self.pages.get(("general", ""))
        if self.area in ("general", "ultracode") and gen is not None:
            if gen.error:
                t.append(f"{gen.error}\nFix the file with E, or run `lm update`.\n\n", MAG)
            elif gen.state == "unguarded":
                t.append("config.md is not used: permissions.md lets an agent write it without asking. Run `lm update`.\n\n", MAG)
        entries = self.entries()
        sel = self.current(entries)
        cap = max(6, self.size.height - 14)
        at = next((i for i, e in enumerate(entries) if e is sel), 0)
        first = max(0, min(at - cap // 2, len(entries) - cap))
        for e in entries[first:first + cap]:
            if "head" in e:
                p = self.pages[e["pk"]]
                t.append(f"  {data.clean(e['head'])}", f"bold {TXT}")
                t.append(f"   {data.clean(p.error)}" if p.error else "", MAG)
                t.append("\n")
            else:
                self.line(t, e, e is sel)
        if not entries and self.area == "agents":
            t.append("  no agents yet: copy agents/_example to agents/<name>\n", DIM)
        if sel is not None:
            f = sel["row"].field
            t.append("\n" + f.help + "\n", DIM)
            if f.danger and self.never_asks(self.value_of(sel)):
                t.append(f.danger + "\n", f"bold {MAG}")
        if self.note:
            t.append("\n" + self.note + "\n", MAG if self.warn else MINT)
        n = sum(len(v) for v in self.pending.values())
        t.append(f"\n{n} unsaved change{'s' * (n != 1)}\n" if n else "\n", AMBER)
        t.append(KEYS, DIM)
        box.update(t)

    def never_asks(self, value) -> bool:
        """Is `value` a mode that never asks (bypass, or `fragt-nie: ja`)?  Only a mode field holds such a value."""
        m = data.modes(self.app.home).get(value) if isinstance(value, str) else None
        return m.never_asks if m else value == "bypass"

    def line(self, t: Text, e: dict, on: bool) -> None:
        f, row = e["row"].field, e["row"]
        value = self.value_of(e)
        changed = f.path in self.pending.get(e["pk"], {})
        danger = bool(f.danger) and self.never_asks(value)
        t.append(" ▌ " if on else "   ", CYAN)
        t.append("  " if e["pk"][0] == "agent" else "")
        t.append(f"{f.label:<18}", f"bold {BG} on {CYAN}" if on else TXT)
        t.append(f" {data.clean(self.label_of(e, value) if value is not None else (f.unset or '-')):<14}",
                 MAG if danger else AMBER if changed else TXT if value is not None else DIM)
        if value is None and row.effective is not None:                 # not set in the file: what applies instead
            t.append(f" ({data.clean(self.label_of(e, row.effective))})", DIM)
        t.append("  ●" if changed else "", AMBER)                                  # not saved yet
        t.append("  !" if danger else "", MAG)                                    # bypass: no questions asked
        t.append("\n")

    # ------------------------------------------------------------------ keys
    async def on_key(self, e: events.Key):
        k = e.key
        self.armed = self.armed and k == "escape"
        if k in ("up", "down"):
            n = len([x for x in self.entries() if "row" in x])
            if n:
                self.cur[self.area] = (self.cur[self.area] + (1 if k == "down" else -1)) % n
            self.say("")
        elif k in ("tab", "left", "right"):
            keys = [key for key, _ in AREAS]
            self.area = keys[(keys.index(self.area) + (-1 if k == "left" else 1)) % len(keys)]
            self.say("")
        elif k == "enter":
            self.change()
        elif k == "s":
            await self.save()
        elif k == "r":
            await self.load()
            self.say("read the files again")
        elif k == "e":
            self.edit()
        elif k == "w":
            self.say("the settings page in the browser is not there yet: it comes soon")
        elif k == "escape":
            if self.pending and not self.armed:
                self.armed = True
                n = sum(len(v) for v in self.pending.values())
                self.say(f"{n} unsaved change{'s' * (n != 1)}: S saves, Esc again leaves without saving", warn=True)
            else:
                self.app.pop_screen()
            return e.stop()
        else:
            return
        e.stop()

    def change(self) -> None:
        sel = self.current()
        if sel is None:
            return self.say("nothing to change here")
        page = self.pages[sel["pk"]]
        if page.error:
            return self.say(page.error, warn=True)
        f = sel["row"].field
        opts = core.options(self.app.home, f, self.values_of(sel["pk"]))
        if not opts:
            return self.say(f"{f.label}: no values to choose from yet", warn=True)
        current = self.value_of(sel)
        pick = [(UNSET if v is None else v, label or str(v), note, True) for v, label, note in opts]
        self.app.push_screen(Pick(f.label, pick, UNSET if current is None else current), lambda v: self.chosen(sel, v))

    def chosen(self, sel: dict, v: Any) -> None:
        if v is None:                                              # Esc in the list
            return
        value = None if v == UNSET else v
        pk, path = sel["pk"], sel["row"].field.path
        if same(sel["row"].value, value):
            self.pending.get(pk, {}).pop(path, None)               # back to what the file says
            if not self.pending.get(pk):
                self.pending.pop(pk, None)
        else:
            self.pending.setdefault(pk, {})[path] = value
        self.say("")

    async def save(self) -> None:
        if not self.pending:
            return self.say("nothing changed")
        h, done, problems = self.app.home, 0, []
        for pk, changes in list(self.pending.items()):
            page = self.pages[pk]
            try:
                self.pages[pk] = await asyncio.to_thread(core.save_page, h, page, changes)
            except core.Conflict:
                problems.append(f"{page.path.name} changed on disk: nothing written. R reads it again (and drops your changes to it)")
            except core.SettingsError as err:
                problems.append(data.clean(str(err)))
            else:
                done += len(changes)
                del self.pending[pk]
                self.stamp[pk] = self.stat(self.pages[pk])
        msg = (f"saved {done} change{'s' * (done != 1)}" if done else "") + ("; " if done and problems else "") + "; ".join(problems)
        self.armed = False
        self.say(msg or "nothing changed", warn=bool(problems))

    def edit(self) -> None:
        """The file of this area (the highlighted agent's file in Agents), in the editor.  A change made there shows up by itself
        (`look`), or with R when there are unsaved changes here."""
        sel = self.current()
        page = self.pages.get(sel["pk"]) if (sel and self.area == "agents") else self.pages.get(("general", ""))
        if page is None:
            return self.say("no file to open here", warn=True)
        ok, msg = acts.open_file(self.app, page.path)
        self.say(data.clean(msg), warn=not ok)
