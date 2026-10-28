"""A server card may list several ssh aliases (`ssh: [homeserver, homeserver-ts]`): the first one that answers is used.  ssh is always mocked."""
import subprocess

import pytest

from lightmind import cli
from lightmind import remote as R
from lightmind.model import Server

from test_remote import add_server, server


@pytest.fixture
def net(monkeypatch):
    """ssh replaced: `probes` lists what was probed, `up` the aliases that answer, `calls` what was run for real."""
    class Net:
        probes: list = []
        calls: list = []
        up: set = set()
    n = Net()
    n.probes, n.calls, n.up = [], [], set()

    def run(cmd, **kw):
        n.probes.append(cmd)
        return subprocess.CompletedProcess(cmd, 0 if cmd[-2] in n.up else 255)
    monkeypatch.setattr(R.subprocess, "run", run)
    monkeypatch.setattr(R.subprocess, "call", lambda cmd, *a, **k: n.calls.append(cmd) or 0)
    return n


def test_the_loader_reads_a_list_and_a_single_alias(home):
    add_server(home, "a", "---\nhost: h\nssh: [homeserver, homeserver-ts]\n---\n# a\n")
    add_server(home, "b", "---\nhost: h\nssh: homeserver\n---\n# b\n")
    assert home.load_server("a").ssh == ["homeserver", "homeserver-ts"]
    assert home.load_server("b").ssh == "homeserver"


def test_the_first_reachable_alias_is_used_and_probed_with_batchmode(net, tree, home, capsys):
    add_server(home, "homeserver", "---\nhost: h\nssh: [homeserver, homeserver-ts]\n---\n# h\n")
    net.up = {"homeserver-ts"}
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 0
    assert [c[-2] for c in net.probes] == ["homeserver", "homeserver-ts"]
    assert net.probes[0] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "homeserver", "true"]
    assert net.calls == [["ssh", "-t", "homeserver-ts", "tmux new -A -s lm ~/lightmind/system/lm"]]
    assert "via homeserver-ts" in capsys.readouterr().out


def test_the_first_alias_wins_when_both_answer_and_the_second_is_not_probed(net, tree, home):
    add_server(home, "homeserver", "---\nhost: h\nssh: [homeserver, homeserver-ts]\n---\n# h\n")
    net.up = {"homeserver", "homeserver-ts"}
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 0
    assert [c[-2] for c in net.probes] == ["homeserver"] and net.calls[0][2] == "homeserver"


def test_none_reachable_is_a_usage_error_and_ssh_is_not_started(net, tree, home, capsys):
    add_server(home, "homeserver", "---\nhost: h\nssh: [homeserver, homeserver-ts]\n---\n# h\n")
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 2
    assert "none of homeserver, homeserver-ts is reachable" in capsys.readouterr().err and net.calls == []


def test_a_single_alias_is_not_probed(net, tree, home):
    add_server(home, "homeserver", "---\nhost: h\nssh: homeserver\n---\n# h\n")
    assert cli.main(["--home", str(tree), "remote", "homeserver"]) == 0
    assert net.probes == [] and net.calls[0][2] == "homeserver"


def test_a_probe_that_times_out_counts_as_unreachable(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)
    monkeypatch.setattr(R.subprocess, "run", slow)
    assert R.reachable(["homeserver"]) is False


@pytest.mark.parametrize("ssh", [["ok", "-oProxyCommand=x"], ["ok", "a b"], ["a;b"]])
def test_every_entry_of_the_list_must_be_a_plain_alias(ssh):
    with pytest.raises(R.RemoteError, match="not a plain ssh alias"):
        R.targets(server(ssh=ssh))


def test_json_shows_the_command_of_the_first_alias_without_probing(net, tree, home, capsys):
    add_server(home, "homeserver", "---\nhost: h\nssh: [homeserver, homeserver-ts]\n---\n# h\n")
    assert cli.main(["--home", str(tree), "--json", "remote", "homeserver"]) == 0
    assert '"homeserver"' in capsys.readouterr().out and net.probes == [] and net.calls == []


def test_the_message_comes_out_once_in_the_terminal_ui(monkeypatch, net, home):
    from lightmind.tui import acts
    from contextlib import contextmanager
    add_server(home, "homeserver", "---\nhost: h\nssh: homeserver\n---\n# h\n")
    lines = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a))))

    class App:
        pass
    app = App()
    app.home = home

    @contextmanager
    def suspend():
        yield
    app.suspend = suspend
    ok, _ = acts.remote(app, "homeserver")
    assert ok and sum("opening the permanent session on homeserver" in l for l in lines) == 1


def test_check_server_accepts_any_alias_of_the_list(home, monkeypatch):
    from lightmind import checks
    add_server(home, "homeserver", "---\nhost: h\nssh: [homeserver, homeserver-ts]\n---\n# h\n")
    monkeypatch.setattr(checks, "_run", lambda cmd, t=0: (0, "") if cmd[-2] == "homeserver-ts" else (255, "refused"))
    line = checks.check_server(home, "homeserver")[0]
    assert line.status == checks.OK and "homeserver-ts" in line.detail
    monkeypatch.setattr(checks, "_run", lambda cmd, t=0: (255, "refused"))
    assert checks.check_server(home, "homeserver")[0].status == checks.WARN
