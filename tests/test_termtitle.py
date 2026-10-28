"""The terminal tab title: its text, the Windows console title (faked), the OSC 0 fallback, keeping it, the chat title in the
Orbit tab, a name given with -n, the 5h / 7d figures of the status line."""
import sys

import pytest

from lightmind import cli, loop as L, termtitle

from test_loop_session import shome  # noqa: F401 - fixture
from test_quick import lm, tty  # noqa: F401 - fixtures
from test_tui_support import FakeProv, install, open_chat, quiet, running, say, until


class FakeKernel:
    def __init__(self, title="claude"):
        self.title, self.sets = title, []

    def SetConsoleTitleW(self, text):
        self.title = text
        self.sets.append(text)
        return 1

    def GetConsoleTitleW(self, buf, n):
        buf.value = self.title
        return len(self.title)


@pytest.fixture
def win(monkeypatch):
    k = FakeKernel()
    monkeypatch.setattr(termtitle, "_kernel32", lambda: k)
    monkeypatch.setattr(termtitle.sys, "platform", "win32")
    return k


def test_tab_text_title_then_name_then_program():
    assert termtitle.tab_text("Neue Seite") == "Neue Seite · Lightmind"
    assert termtitle.tab_text("", "terminal-one") == "terminal-one · Lightmind"
    assert termtitle.tab_text("Titel", "name") == "Titel · Lightmind"
    assert termtitle.tab_text() == "Lightmind"
    assert termtitle.tab_text("a\x1bb\x07") == "ab · Lightmind"


def test_windows_sets_the_console_title_and_reads_it_back(win):
    termtitle.set_title("Neue Seite · Lightmind", write=lambda s: None)
    assert win.sets == ["Neue Seite · Lightmind"] and termtitle.current() == "Neue Seite · Lightmind"


def test_keep_sets_again_only_when_claude_exe_changed_it(win):
    termtitle.keep("T · Lightmind", write=lambda s: None)          # the console says "claude": overwritten
    assert win.sets == ["T · Lightmind"]
    termtitle.keep("T · Lightmind", write=lambda s: None)          # still ours: nothing written
    assert win.sets == ["T · Lightmind"]


def test_osc_goes_to_the_writer_or_a_terminal_stdout(monkeypatch, capsys):
    monkeypatch.setattr(termtitle, "_kernel32", lambda: None)
    got = []
    termtitle.set_title("A", write=got.append)
    assert got == ["\033]0;A\007"]
    termtitle.set_title("B")                                       # stdout is no terminal under pytest: nothing printed
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys, "stdout", type("T", (), {"isatty": lambda s: True, "write": got.append, "flush": lambda s: None})())
    termtitle.set_title("C")
    assert got[-1] == "\033]0;C\007"


def test_a_writer_that_fails_does_not_break_the_chat(monkeypatch):
    monkeypatch.setattr(termtitle, "_kernel32", lambda: None)

    def boom(s):
        raise OSError("gone")
    termtitle.set_title("A", write=boom)


def test_line_mode_title_falls_back_to_the_name(monkeypatch, win):
    monkeypatch.setattr(cli, "_TAB_NAME", "terminal-one")
    cli._window_title("")
    assert win.title == "terminal-one · Lightmind"
    cli._window_title("Neue Seite")
    assert win.title == "Neue Seite · Lightmind"


# ------------------------------------------------------------------ the Orbit tab
async def test_orbit_tab_follows_the_chat_title_and_wins_back_against_claude_exe(home, monkeypatch, win):
    quiet(home)
    install(monkeypatch, FakeProv())
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/rename Mein Titel")
        await until(pilot, lambda: win.title == "Mein Titel · Lightmind")
        assert app.title == "Mein Titel · Lightmind"
        win.title = "claude"                                       # the SDK process renames the shared console
        app.chat.keep_tab_title()
        assert win.title == "Mein Titel · Lightmind"


# ------------------------------------------------------------------ a name given with -n
def test_name_flag_becomes_the_locked_title(shome):  # noqa: F811
    s = L.open_session(shome)
    cli._name_session(shome, s, "terminal-one")
    r = s.run.run
    assert r.title == "terminal-one" and r.title_manual
    s.close()


def test_quick_default_name_is_no_chosen_title(lm, tty):  # noqa: F811
    assert lm("q")[0] == 0
    assert "name" not in tty.asked[0]["open"]["new"] and "name_fixed" not in tty.asked[0]["open"]["new"]   # no -n: no name at all
    assert lm("q", "-n", "mine")[0] == 0
    assert tty.asked[1]["open"]["new"]["name_fixed"] is True


async def test_orbit_applies_a_fixed_name_as_title_but_not_the_default(home, monkeypatch, win):
    quiet(home)
    install(monkeypatch, FakeProv())
    from lightmind.tui.app import Orbit
    for name, fixed, title in (("mein-name", True, "mein-name"), ("quick-0101", False, "")):
        app = Orbit(home, anim=False, direct={"new": {"name": name, "name_fixed": fixed}, "label": "lm q"})
        async with app.run_test(size=(130, 42)) as pilot:
            await until(pilot, lambda: app.chat is not None)
            s = app.chat.s
            assert s.title == title and s.run.run.title_manual is fixed
            assert app.title == (f"{name} · Lightmind" if fixed else "Lightmind")
            await pilot.press("escape")


# ------------------------------------------------------------------ 5h / 7d
def test_default_statusline_has_the_windows():
    from lightmind.tui import data
    assert "5h {five_hour} · 7d {seven_day}" in data.DEFAULT_STATUS
    assert data.DEFAULT_STATUS.index("{ctx}") < data.DEFAULT_STATUS.index("{five_hour}") < data.DEFAULT_STATUS.index("{cost}")


async def test_session_starts_with_the_limits_of_its_account(shome, monkeypatch):  # noqa: F811
    class Prov(FakeProv):
        def limits_now(self):
            return {"five_hour": 0.42, "seven_day": 0.8}
    monkeypatch.setattr(L, "make_provider", lambda home, account, dispatcher=None: Prov())
    s = L.open_session(shome)
    assert s.info.limits == {"five_hour": 0.42, "seven_day": 0.8}
    s.close()


async def test_status_line_colours_and_dashes(home, monkeypatch):
    quiet(home)
    install(monkeypatch, FakeProv())
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        c = app.chat
        assert "5h - · 7d -" in c.status_text().plain
        c.s.limits.update(five_hour=0.5, seven_day=0.9)
        t = c.status_text()
        assert "5h 50% · 7d 90%" in t.plain
        styles = {t.plain[s.start:s.end]: str(s.style) for s in t.spans}
        from lightmind.tui.theme import AMBER, MAG, MINT
        assert styles["50%"] == MINT and styles["90%"] == MAG
        c.s.limits["five_hour"] = 0.7
        assert {t2.plain[s.start:s.end]: str(s.style) for t2 in [c.status_text()] for s in t2.spans}["70%"] == AMBER


async def test_orbit_refreshes_the_windows_from_the_state_file(home, monkeypatch):
    quiet(home)
    prov = install(monkeypatch, FakeProv())
    box = {"five_hour": 0.1}
    prov.limits_now = lambda: dict(box)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        app.chat.refresh_limits()
        assert app.chat.s.limits["five_hour"] == 0.1
        box["five_hour"] = 0.9                                     # another chat of the account ran
        app.chat.refresh_limits()
        assert "5h 90%" in app.chat.status_text().plain
