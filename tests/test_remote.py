"""The permanent session on another server: `ssh -t <alias> 'tmux new -A -s lm <tree>/system/lm'`, ssh always mocked."""
from contextlib import contextmanager

import pytest

from lightmind import cli
from lightmind import remote as R
from lightmind.model import Server
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import SetupScreen

from test_tui_support import FakeProv, install, open_chat, plain, quiet, running, say, until


def server(name="buildbox", **kw):
    return Server(name=name, host=kw.pop("host", "10.0.0.5"), local=kw.pop("local", False), **kw)


def add_server(home, name, text):
    (home.root / "servers" / name).mkdir(exist_ok=True)
    (home.root / "servers" / name / "info.md").write_text(text, "utf-8")


@pytest.fixture
def ssh(monkeypatch):
    """subprocess.call inside lightmind.remote replaced: the calls are collected, nothing connects."""
    calls = []
    monkeypatch.setattr(R.subprocess, "call", lambda cmd, *a, **k: calls.append(cmd) or 0)
    return calls


# ------------------------------------------------------------------ the command
def test_the_command_uses_the_ssh_alias_and_the_tree_of_the_server():
    s = server(ssh="homeserver", lm_home="~/lightmind")
    assert R.remote_command(s) == ["ssh", "-t", "homeserver", "tmux new -A -s lm ~/lightmind/system/lm"]


def test_the_tree_defaults_to_lightmind_in_the_home_folder_and_an_absolute_path_works():
    assert R.remote_command(server(ssh="k"))[-1] == "tmux new -A -s lm ~/lightmind/system/lm"
    assert R.remote_command(server(ssh="k", lm_home="/srv/lm/"))[-1] == "tmux new -A -s lm /srv/lm/system/lm"


def test_without_an_alias_user_host_and_port_are_used():
    assert R.remote_command(server(user="user", port=2222))[:5] == ["ssh", "-t", "-p", "2222", "user@10.0.0.5"]
    assert R.remote_command(server())[:3] == ["ssh", "-t", "10.0.0.5"]


@pytest.mark.parametrize("kw", [
    {"ssh": "-oProxyCommand=x"}, {"ssh": "a b"}, {"ssh": "a;rm -rf x"}, {"ssh": "k", "lm_home": "~/lm; rm -rf ~"},
    {"ssh": "k", "lm_home": "../x"}, {"ssh": "k", "lm_home": "~/a b"}, {"ssh": "k", "lm_home": "$(id)"},
    {"host": "-oX=y"}])
def test_an_unsafe_value_in_info_md_is_refused(kw):
    with pytest.raises(R.RemoteError):
        R.remote_command(server(**kw))


def test_this_machine_has_nothing_to_connect_to():
    with pytest.raises(R.RemoteError, match="this machine"):
        R.remote_command(Server(name="laptop", host="local", local=True))


def test_info_md_gives_ssh_and_lm_home_to_the_loader(home):
    add_server(home, "homeserver", "---\nhost: 192.0.2.10\nssh: homeserver\nlm_home: ~/lightmind\n---\n# h\n")
    s = home.load_server("homeserver")
    assert (s.ssh, s.lm_home, s.local) == ("homeserver", "~/lightmind", False)


# ------------------------------------------------------------------ lm remote
def test_lm_remote_runs_ssh_with_tmux_for_the_server(tree, home, ssh):
    add_server(home, "homeserver", "---\nhost: 192.0.2.10\nssh: homeserver\nlm_home: ~/lightmind\n---\n# h\n")
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 0
    assert ssh == [["ssh", "-t", "homeserver", "tmux new -A -s lm ~/lightmind/system/lm"]]


def test_lm_remote_passes_the_exit_code_of_ssh_on(tree, home, monkeypatch):
    add_server(home, "homeserver", "---\nhost: h\nssh: homeserver\n---\n# h\n")
    monkeypatch.setattr(R.subprocess, "call", lambda cmd, *a, **k: 255)
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 255


def test_lm_remote_json_only_shows_the_command(tree, home, ssh, capsys):
    add_server(home, "homeserver", "---\nhost: h\nssh: homeserver\n---\n# h\n")
    assert cli.main(["--home", str(tree), "--json", "remote", "homeserver"]) == 0
    assert '"tmux new -A -s lm ~/lightmind/system/lm"' in capsys.readouterr().out and ssh == []


def test_lm_remote_refuses_the_local_and_the_unknown_server_without_calling_ssh(tree, ssh, capsys):
    assert cli.main(["--home", str(tree), "remote", "laptop"]) == 2
    assert "this machine" in capsys.readouterr().err
    assert cli.main(["--home", str(tree), "remote", "nowhere"]) == 2
    assert "no server 'nowhere'" in capsys.readouterr().err and ssh == []


def test_lm_remote_without_ssh_installed_is_a_usage_error(tree, home, monkeypatch, capsys):
    add_server(home, "homeserver", "---\nhost: h\nssh: homeserver\n---\n# h\n")

    def missing(*a, **k):
        raise FileNotFoundError(2, "no ssh", "ssh")
    monkeypatch.setattr(R.subprocess, "call", missing)
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 2
    assert "ssh is not installed" in capsys.readouterr().err


# ------------------------------------------------------------------ the UI
@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


@pytest.fixture
def suspended(monkeypatch):
    """suspend() a no-op (the headless driver cannot suspend); counts how often the UI was suspended."""
    n = {"suspend": 0}

    @contextmanager
    def fake(self):
        n["suspend"] += 1
        yield
    monkeypatch.setattr(Orbit, "suspend", fake)
    return n


async def test_picking_a_remote_server_opens_its_permanent_session(home, prov, ssh, suspended):
    add_server(home, "buildbox", "---\nhost: buildbox.lan\nssh: buildbox\nlm_home: ~/lightmind\n---\n# p\n")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        assert "buildbox" in plain(app, "#card")
        await pilot.press("down")
        assert "permanent session over ssh" in plain(app, "#card")
        await pilot.press("enter")                                   # buildbox is remote: ssh instead of the next card
        await until(pilot, lambda: len(ssh) == 1)
        assert ssh[0] == ["ssh", "-t", "buildbox", "tmux new -A -s lm ~/lightmind/system/lm"]
        assert suspended["suspend"] == 1
        assert isinstance(app.screen, SetupScreen) and app.screen.i == 0       # back on the card afterwards


async def test_a_local_server_still_goes_on_to_the_next_card(home, prov, ssh, suspended):
    add_server(home, "buildbox", "---\nhost: buildbox.lan\nssh: buildbox\n---\n# p\n")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("enter")                                   # laptop: the account card follows
        assert app.screen.i == 1 and ssh == []


async def test_a_failing_ssh_is_reported_and_the_ui_comes_back(home, prov, suspended, monkeypatch):
    add_server(home, "buildbox", "---\nhost: buildbox.lan\nssh: buildbox\n---\n# p\n")
    monkeypatch.setattr(R.subprocess, "call", lambda cmd, *a, **k: 255)
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("down", "enter")
        await until(pilot, lambda: "code 255" in plain(app, "#card"))
        assert isinstance(app.screen, SetupScreen)


async def test_slash_server_opens_the_session_of_a_remote_server_and_never_reaches_ssh_for_an_unknown_one(
        home, prov, ssh, suspended):
    add_server(home, "buildbox", "---\nhost: buildbox.lan\nssh: buildbox\n---\n# p\n")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/server buildbox")
        await until(pilot, lambda: len(ssh) == 1)
        assert ssh[0][:3] == ["ssh", "-t", "buildbox"] and isinstance(app.screen, ChatScreen)
        await say(pilot, "/server ghost")
        await pilot.pause(0.3)
        assert len(ssh) == 1
