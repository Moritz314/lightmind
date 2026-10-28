"""`/reload`: the files the chat is built from are read again, the history stays, a running turn finishes first."""
import asyncio

import pytest
from lightmind import core
from lightmind import model as m
from lightmind.tui.chat import Msg

from test_tui_support import FakeProv, idle, install, open_chat, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    return install(monkeypatch, FakeProv())


def has(app, needle: str) -> bool:
    return any(needle in w.plain for w in app.screen.query(Msg))


def add_skill(home, name="cells"):
    f = home.root / "skills" / name / "SKILL.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\nname: {name}\ndescription: {name} things\n---\nBody\n", "utf-8")


async def test_reload_is_in_the_slash_suggestions_and_the_help(home, prov):
    assert "reload" in dict(core.BUILTIN_SLASH)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("slash", "r", "e", "l")
        box = app.screen.query_one("#pop")
        assert "/reload" in box.content.plain


async def test_reload_says_what_changed_and_keeps_the_history(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "remember the milk")
        await idle(pilot, app)
        add_skill(home)
        (home.root / "permissions.md").write_text((home.root / "permissions.md").read_text("utf-8") + "\n", "utf-8")
        await say(pilot, "/reload")
        await until(pilot, lambda: has(app, "reloaded:"))
        assert has(app, "skill cells") and has(app, "permissions.md") and has(app, "history stays")
        await say(pilot, "and now?")
        await idle(pilot, app)
        assert "cells" in core.skill_commands(home)
        assert len(prov.requests) == 2 and "remember the milk" in str(prov.requests[0])


async def test_reload_without_a_change_says_so(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/reload")
        await until(pilot, lambda: has(app, "nothing changed on disk"))


async def test_reload_during_a_turn_waits_for_its_end(home, prov):
    gate = asyncio.Event()

    async def turn(self, req):
        yield m.Text("first answer")
        await gate.wait()
        yield m.Usage()
    prov.turns.append(turn)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "go")
        await until(pilot, lambda: app.chat.busy)
        add_skill(home)
        await say(pilot, "/reload")
        await until(pilot, lambda: has(app, "waits for the end"))
        assert not has(app, "reloaded:")
        gate.set()
        await until(pilot, lambda: has(app, "reloaded:"))
        assert has(app, "skill cells")


async def test_reload_after_an_update_asks_for_a_restart(home, prov, monkeypatch):
    monkeypatch.setattr(core, "installed_version", lambda: "99.0.0")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/reload")
        await until(pilot, lambda: has(app, "restart the chat"))
        assert has(app, "99.0.0")


def test_reload_keeps_the_old_agent_when_its_file_is_broken(home, prov):
    from lightmind.loop import open_session
    s = open_session(home)
    sig = core.signature(home)
    s.agent = type("A", (), {"name": "ghost", "budget_tokens": None, "budget_usd": None})()
    res, _ = core.reload(home, s, sig)
    assert not res.ok and "agent ghost" in res.text and "kept the old one" in res.text
    s.close()
