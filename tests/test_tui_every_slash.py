"""Every slash command the Orbit chat offers (popup, `ui.slash` of the template) really runs: none ends in "unknown command"."""
from contextlib import contextmanager

import pytest

from lightmind import core, files, model as m, upload
from lightmind.tui import chat as chatmod, data
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen, Msg

from test_tui_support import FakeProv, idle, install, open_chat, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


@pytest.fixture(autouse=True)
def no_real_login(monkeypatch):
    """Nothing opens a browser or an ssh session; the headless driver cannot suspend."""
    from lightmind.providers import claude_sdk
    from lightmind import remote
    monkeypatch.setattr(claude_sdk, "login", lambda home, account: 0)
    monkeypatch.setattr(remote, "open_remote", lambda home, name: 0)
    monkeypatch.setattr(upload, "pick", lambda start=None, platform=None: upload.Picked())      # /upload: a dialog that is cancelled

    @contextmanager
    def fake_suspend(self):
        yield
    monkeypatch.setattr(Orbit, "suspend", fake_suspend)


def offered(home) -> list[str]:
    """Names of the commands the popup lists, skills left out."""
    skills = set(data.skill_commands(home))
    return [n for n, _ in data.slash_commands(home) if n not in skills]


def test_every_offered_command_has_a_handler_in_the_chat(home):
    names = offered(home)
    assert {"rename", "describe", "login", "key", "settings", "config", "clear"} <= set(names)
    rows = dict(data.slash_rows(home))
    missing = [n for n in names if n not in rows and not hasattr(ChatScreen, "cmd_" + n.replace("-", "_"))]
    assert missing == []
    assert [n for n in chatmod.BUILTIN if not hasattr(ChatScreen, "cmd_" + n.replace("-", "_"))] == []


def test_the_rows_of_the_template_point_to_known_actions(home):
    assert all(action in data.ACTIONS for _, action in data.slash_rows(home))


@pytest.mark.parametrize("name", [n for n, _ in core.BUILTIN_SLASH])
async def test_each_builtin_runs_and_is_not_unknown(home, prov, name):
    assert name in offered(home)
    said = []
    real = ChatScreen.add

    async def spy(self, kind, text, *a, **kw):
        said.append(str(text))
        return await real(self, kind, text, *a, **kw)
    chatmod.ChatScreen.add = spy
    try:
        async with running(home) as (app, pilot):
            await open_chat(app, pilot)
            await app.chat.slash(f"/{name}")
            await pilot.pause(0.3)
    finally:
        chatmod.ChatScreen.add = real
    assert not any("unknown command" in t for t in said), said


async def test_rename_and_describe_set_the_chat_and_alone_unlock_it(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        s = app.chat.s
        await say(pilot, "/rename Mein Titel")
        await until(pilot, lambda: s.title == "Mein Titel")
        assert any("Mein Titel" in w.plain for w in app.chat.query(Msg))
        assert "Mein Titel" in app.title                             # the window title follows
        await say(pilot, "/describe Eine Beschreibung")
        await until(pilot, lambda: s.run.run.description == "Eine Beschreibung")
        assert s.run.run.title_manual and s.run.run.description_manual
        await say(pilot, "/rename")
        await until(pilot, lambda: not s.run.run.title_manual)
        assert not any("unknown command" in w.plain for w in app.chat.query(Msg))


async def test_rename_in_a_read_only_chat_says_so(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        app.chat.s.readonly_by = 4242
        await app.chat.slash("/rename Nope")
        assert any("read only" in w.plain for w in app.chat.query(Msg))
        assert app.chat.s.title != "Nope"
