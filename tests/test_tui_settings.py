"""The settings screen of Orbit: opened from the start menu and by /settings (alias /config), the fields of config.md and
of the agent files, changing and saving only what was changed.  The fields and the writer are tested in
test_settings_page.py; here it is the screen and its keys."""
import os

import pytest

from lightmind import settings as core
from lightmind.tui import acts
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import MapScreen, Pick
from lightmind.tui.settings import UNSET, SettingsScreen

from test_tui_support import FakeProv, install, open_chat, plain, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def text_of(p) -> str:
    return p.read_bytes().decode("utf-8")


def put(p, text: str) -> None:
    old = p.stat().st_mtime if p.exists() else 0
    p.write_bytes(text.encode("utf-8"))
    os.utime(p, (old + 10, old + 10))                      # a time the screen's cache cannot miss


def make_agent(home, name, front="description: x\nmode: auto\n"):
    p = core.agent_file(home, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}---\nbody\n", "utf-8")
    return p


async def open_settings(app, pilot):
    await pilot.press("s")
    await until(pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen.pages)
    await pilot.pause(0.1)


async def choose(app, pilot, value):
    """In the Pick list that is open: go to `value` and take it."""
    await until(pilot, lambda: isinstance(app.screen, Pick))
    pick = app.screen
    target = next(i for i, o in enumerate(pick.opts) if o[0] == value)
    for _ in range((target - pick.c) % len(pick.opts)):
        await pilot.press("down")
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, SettingsScreen))


async def down(pilot, n):
    for _ in range(n):
        await pilot.press("down")


async def goto(app, pilot, label, agent=""):
    """Move the cursor to the field `label` (of `agent` in the Agents area): the tests do not count rows."""
    for _ in range(80):
        sel = app.screen.current()
        if sel is not None and sel["row"].field.label == label and sel["pk"][1] == agent:
            return
        await pilot.press("down")
    raise AssertionError(f"no field {label!r} of {agent!r}")


def screen_text(app) -> str:
    return plain(app, "#sett")



# ------------------------------------------------------------------ opening it
async def test_the_start_menu_opens_the_settings_and_esc_goes_back(home):
    async with running(home) as (app, pilot):
        assert "Settings" in plain(app, "#menu")
        await open_settings(app, pilot)
        text = screen_text(app)
        for want in ("S E T T I N G S", "General", "Agents", "Account", "Model", "Effort", "Context", "Mode", "Open chats in",
                     "Start checklist", "Wait for Enter", "Parallel runs", "Run depth"):
            assert want in text, want
        assert "replace" in text and "Auto" in text            # what config.md of the shared test tree says
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))


@pytest.mark.parametrize("command", ["/settings", "/config"])
async def test_the_slash_commands_open_it_over_the_chat(home, prov, command):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        chat = app.screen
        await say(pilot, command)
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen.pages)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is chat and isinstance(app.screen, ChatScreen))   # the chat waited below it


async def test_the_slash_popup_lists_both_commands(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        app.screen.query_one("#inp").value = "/"
        await pilot.pause(0.2)
        names = [n for n, _ in app.chat.pop]
        assert "settings" in names and "config" in names


# ------------------------------------------------------------------ changing and saving
async def test_a_change_is_kept_on_screen_until_it_is_saved_and_then_only_its_line_changes(home):
    cfg = home.config_file
    before = text_of(cfg)
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await goto(app, pilot, "Mode")
        await pilot.press("enter")
        await choose(app, pilot, "plan")
        text = screen_text(app)
        assert "Plan" in text and "●" in text and "1 unsaved change" in text
        assert text_of(cfg) == before                           # nothing is written before S
        await pilot.press("s")
        await until(pilot, lambda: "saved 1 change" in screen_text(app))
        assert "unsaved" not in screen_text(app)
    old, new = before.splitlines(), text_of(cfg).splitlines()
    assert len(old) == len(new) and [b for a, b in zip(old, new) if a != b] == [next(b for a, b in zip(old, new) if a != b)]
    assert any(b.strip().startswith("mode: plan") for a, b in zip(old, new) if a != b)
    assert home.settings["defaults"]["mode"] == "plan"          # the core sees it at once


async def test_choosing_what_the_file_has_is_no_change_and_esc_in_the_list_changes_nothing(home):
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await goto(app, pilot, "Mode")
        await pilot.press("enter")
        await choose(app, pilot, "plan")
        assert "1 unsaved change" in screen_text(app)
        await pilot.press("enter")
        await choose(app, pilot, "auto")                        # back to the value of the file
        assert "unsaved" not in screen_text(app)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        assert "unsaved" not in screen_text(app)
        await pilot.press("s")
        await until(pilot, lambda: "nothing changed" in screen_text(app))


async def test_bypass_is_marked_and_says_what_it_does(home):
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await goto(app, pilot, "Mode")
        await pilot.press("enter")
        await choose(app, pilot, "bypass")
        text = screen_text(app)
        assert "Bypass asks no questions" in text and "  !" in text and "Bypass" in text
        await down(pilot, 1)
        assert "Bypass asks no questions" not in screen_text(app)           # only beside the field it is about


async def test_several_changes_are_saved_together_and_nothing_else_moves(home):
    cfg = home.config_file
    before = text_of(cfg)
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await goto(app, pilot, "Open chats in")
        await pilot.press("enter")
        await choose(app, pilot, "window")
        await goto(app, pilot, "Parallel runs")
        await pilot.press("enter")
        await choose(app, pilot, 12)                             # (the default is 6)
        assert "2 unsaved changes" in screen_text(app)
        await pilot.press("s")
        await until(pilot, lambda: "saved 2 changes" in screen_text(app))
    after = text_of(cfg)
    assert sum(a != b for a, b in zip(before.splitlines(), after.splitlines())) == 2
    assert home.settings["sessions"]["open"] == "window" and home.settings["run"]["max_parallel"] == 12


async def test_esc_with_unsaved_changes_asks_once(home):
    cfg = home.config_file
    before = text_of(cfg)
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await goto(app, pilot, "Mode")
        await pilot.press("enter")
        await choose(app, pilot, "plan")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen) and "Esc again leaves without saving" in screen_text(app)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))
    assert text_of(cfg) == before


# ------------------------------------------------------------------ the file changes underneath
async def test_a_file_that_changed_on_disk_is_read_again_by_itself_when_nothing_is_pending(home):
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        put(home.config_file, text_of(home.config_file).replace("  effort: medium", "  effort: high", 1))
        await until(pilot, lambda: "high" in screen_text(app), timeout=5)


async def test_with_unsaved_changes_a_changed_file_is_a_conflict_and_nothing_is_overwritten(home):
    cfg = home.config_file
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await goto(app, pilot, "Mode")
        await pilot.press("enter")
        await choose(app, pilot, "plan")
        put(cfg, text_of(cfg).replace("  effort: medium", "  effort: high", 1))
        external = cfg.read_bytes()
        await until(pilot, lambda: "changed on disk" in screen_text(app), timeout=5)          # said, but not read over the change
        await pilot.press("s")
        await until(pilot, lambda: "nothing written" in screen_text(app))
        assert cfg.read_bytes() == external
        await pilot.press("r")
        await until(pilot, lambda: "read the files again" in screen_text(app))
        assert "unsaved" not in screen_text(app) and "high" in screen_text(app)
        await pilot.press("s")
        await until(pilot, lambda: "nothing changed" in screen_text(app))


# ------------------------------------------------------------------ files the core cannot use
async def test_a_broken_empty_or_unguarded_config_md_is_said_on_the_screen(home):
    cfg = home.config_file
    good = text_of(cfg)
    put(cfg, good.replace("  model: sonnet", "  model: [sonnet", 1))
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        assert "config.md: line" in screen_text(app) and "Fix the file with E" in screen_text(app)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, Pick) and "config.md: line" in screen_text(app)
        put(cfg, "")
        await until(pilot, lambda: "is empty" in screen_text(app) and "lm update" in screen_text(app), timeout=5)
        put(cfg, good)
        bypass = home.root / "modes" / "bypass.md"          # the guard is the grant-file right (13) in every mode file
        put(bypass, text_of(bypass).replace("rechte: [13]", "rechte: []"))
        await until(pilot, lambda: "config.md is not used" in screen_text(app), timeout=5)
        assert "Run `lm update`" in screen_text(app)


# ------------------------------------------------------------------ agents
async def test_the_agents_area_lists_the_agent_files_and_saves_a_project(home):
    make_agent(home, "webdev", "description: builds sites\nmode: auto\nmay_spawn: []\n")
    other = make_agent(home, "docs")
    (home.path("projects") / "site").mkdir(parents=True, exist_ok=True)
    before_other = other.read_bytes()
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await pilot.press("tab", "tab")             # General, Ultracode, Agents
        await pilot.pause(0.2)
        text = screen_text(app)
        assert text.index("docs") < text.index("webdev") and "Project" in text and "default" in text
        await goto(app, pilot, "Project", "webdev")
        await pilot.press("enter")
        await choose(app, pilot, "site")
        await pilot.press("s")
        await until(pilot, lambda: "saved 1 change" in screen_text(app))
        assert "site" in screen_text(app)
        await pilot.press("enter")                               # and back to "not set"
        await choose(app, pilot, UNSET)
        await pilot.press("s")
        await until(pilot, lambda: screen_text(app).count("saved 1 change") >= 1 and "unsaved" not in screen_text(app))
    lines = text_of(core.agent_file(home, "webdev")).splitlines()
    assert lines[:5] == ["---", "description: builds sites", "mode: auto", "project: ''", "may_spawn: []"]      # behind `mode`, kept empty
    assert other.read_bytes() == before_other                    # the other agent's file was not touched


async def test_an_agent_project_can_only_be_one_that_exists(home):
    make_agent(home, "webdev")
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await pilot.press("tab", "tab")             # General, Ultracode, Agents
        await goto(app, pilot, "Project", "webdev")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        assert [o[0] for o in app.screen.opts] == [UNSET]        # no project folder yet: only "none"
        await pilot.press("escape")


async def test_no_agents_says_how_to_make_one(home):
    import shutil
    shutil.rmtree(home.path("agents") / "example-agent")                # the shared test tree ships one
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await pilot.press("tab", "tab")             # General, Ultracode, Agents
        await pilot.pause(0.2)
        assert "no agents yet" in screen_text(app)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "nothing to change here" in screen_text(app)


# ------------------------------------------------------------------ E and W
async def test_e_opens_the_file_of_the_area_and_w_is_a_placeholder(home, monkeypatch):
    opened = []
    monkeypatch.setattr(acts, "open_file", lambda app, path: (opened.append(path), (True, f"{path.name} is open"))[1])
    make_agent(home, "webdev")
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await pilot.press("e")
        await until(pilot, lambda: "config.md is open" in screen_text(app))
        await pilot.press("tab", "tab")             # General, Ultracode, Agents
        await goto(app, pilot, "Account", "webdev")
        await pilot.press("e")
        await until(pilot, lambda: len(opened) == 2)
        assert opened == [home.config_file, core.agent_file(home, "webdev")]
        await pilot.press("w")
        await until(pilot, lambda: "comes soon" in screen_text(app))


async def test_e_says_why_when_no_editor_can_be_opened(home, monkeypatch):
    monkeypatch.setattr(acts, "open_file", lambda app, path: (False, "config.md: no editor could be opened"))
    async with running(home) as (app, pilot):
        await open_settings(app, pilot)
        await pilot.press("e")
        await until(pilot, lambda: "no editor could be opened" in screen_text(app))
        assert isinstance(app.screen, SettingsScreen)


def test_open_file_runs_the_editor_of_the_environment_in_this_terminal(tmp_path, monkeypatch):
    calls = {}

    class App:
        def suspend(self):
            from contextlib import contextmanager

            @contextmanager
            def cm():
                calls["suspended"] = True
                yield
            return cm()
    monkeypatch.setenv("EDITOR", "myedit --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(acts.subprocess, "call", lambda argv: calls.setdefault("argv", argv) and 0)
    ok, msg = acts.open_file(App(), tmp_path / "config.md")
    assert ok and calls["suspended"] and calls["argv"] == ["myedit", "--wait", str(tmp_path / "config.md")] and "closed" in msg


def test_open_file_without_an_editor_variable_uses_the_system_opener(tmp_path, monkeypatch):
    seen = []
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(acts.os, "startfile", lambda p: seen.append(("startfile", p)), raising=False)
    monkeypatch.setattr(acts.subprocess, "Popen", lambda argv, **kw: seen.append(("popen", argv)))
    monkeypatch.setattr(acts.sys, "platform", "win32")
    assert acts.open_file(None, tmp_path / "x.md")[0] and seen == [("startfile", str(tmp_path / "x.md"))]
    seen.clear()
    monkeypatch.setattr(acts.sys, "platform", "linux")
    assert acts.open_file(None, tmp_path / "x.md")[0] and seen[0][0] == "popen" and seen[0][1][0] == "xdg-open"
    monkeypatch.setattr(acts.sys, "platform", "darwin")
    acts.open_file(None, tmp_path / "x.md")
    assert seen[-1][1][0] == "open"


def test_open_file_reports_a_failure_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(acts.sys, "platform", "linux")
    def boom(*a, **k):
        raise FileNotFoundError("xdg-open")
    monkeypatch.setattr(acts.subprocess, "Popen", boom)
    ok, msg = acts.open_file(None, tmp_path / "x.md")
    assert not ok and "no editor could be opened" in msg and "x.md" in msg
