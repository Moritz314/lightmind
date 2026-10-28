"""The mode menu never comes up empty without saying why: a permissions file that cannot be read gives a reason."""
import shutil

import pytest

from lightmind import core, settings as S
from lightmind.files import Home
from lightmind.tui import data
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import SetupScreen

from conftest import legacy_modes
from test_tui_support import FakeProv, install, open_chat, plain, quiet, running, say, texts, until

BROKEN = {
    "yaml": "---\nfallback: allow\nlocked:\n  deny: [read({secrets}/**)]\nmodes:\n  auto: {label: Auto}\n---\n# body\n",
    "no modes": "---\nfallback: allow\n---\n# body\n",
    "empty modes": "---\nmodes: {}\n---\n# body\n",
    "modes is a list": "---\nmodes: [auto, plan]\n---\n",
}


def spoil(home: Home, text: str | None):
    """Break the permissions file; the mode files go too, because with any mode file present the `modes:` block is not read."""
    shutil.rmtree(home.root / "modes", ignore_errors=True)
    (home.root / "rechte.md").unlink(missing_ok=True)
    p = home.path("permissions")
    if text is None:
        p.unlink()
    else:
        p.write_text(text, encoding="utf-8")


def test_a_healthy_file_lists_the_modes(home):
    assert {"auto", "plan", "bypass"} <= set(home.modes())
    assert data.modes_error(home) == "" and S.modes_error(home) == ""
    assert {"auto", "plan", "bypass"} <= set(core.mode_labels(home))


@pytest.mark.parametrize("why", list(BROKEN) + ["missing"])
def test_a_file_without_usable_modes_gives_a_reason_everywhere(home, why):
    spoil(home, BROKEN.get(why))
    if why == "yaml":
        # a flow list holding `{config}` is not YAML: the reason names the file's problem
        spoil(home, "---\nmodes:\n  auto:\n    ask: [write({config})]\n  x: {a: [\n---\n")
    with pytest.raises(ValueError) as e:
        home.modes()
    msg = str(e.value)
    assert msg and ("permissions" in msg)
    assert data.modes_error(home) == msg and S.modes_error(home) == msg
    assert data.modes(home) == {} and S._modes(home) == {}
    r = core.command(home, type("S", (), {})(), "mode", "plan")
    assert r.ok is False and msg in r.text


def test_the_cache_does_not_keep_a_broken_read(home):
    legacy_modes(home)          # the older layout: the modes are in permissions.md, so restoring that file restores them
    good = home.path("permissions").read_text(encoding="utf-8")
    spoil(home, BROKEN["no modes"])
    assert data.modes_error(home)
    spoil(home, good)
    assert "auto" in home.modes() and data.modes_error(home) == ""


def test_a_session_refuses_a_mode_with_the_reason(home):
    from lightmind.loop import _check_mode
    spoil(home, BROKEN["no modes"])
    with pytest.raises(ValueError, match="no `modes:` mapping"):
        _check_mode(home, "auto")


def test_the_cache_does_not_keep_a_missing_modes_folder(home):
    kept = home.root / "modes-kept"
    (home.root / "modes").rename(kept)
    assert data.modes_error(home) and "auto" not in S._modes(home)
    kept.rename(home.root / "modes")
    assert "auto" in home.modes() and data.modes_error(home) == ""


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


async def test_slash_mode_shows_the_reason_instead_of_an_empty_menu(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        spoil(home, BROKEN["no modes"])                                           # spoiled while the chat is open
        await say(pilot, "/mode")
        await until(pilot, lambda: any("no `modes:` mapping" in t for t in texts(app)))
        assert isinstance(app.screen, ChatScreen)


async def test_the_setup_card_names_the_problem_on_the_mode_step(home):
    spoil(home, BROKEN["no modes"])
    async with running(home) as (app, pilot):
        await app.push_screen(SetupScreen())
        await pilot.pause(0.3)
        sc = app.screen
        opts = sc.options("mode")
        assert len(opts) == 1 and opts[0][3] is False
        sc.i = 6                                                                  # the last step is the mode
        sc.draw()
        assert "no `modes:` mapping" in plain(app, "#card")


def test_the_health_check_fails_without_modes(home):
    from lightmind import checks
    spoil(home, BROKEN["no modes"])
    lines = [l for l in checks.check_config(home) if "permissions.md" in l.detail]
    assert lines and lines[0].status == checks.FAIL and "no `modes:` mapping" in lines[0].detail
