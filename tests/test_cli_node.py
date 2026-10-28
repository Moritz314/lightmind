"""`lm node` (targets, stdio and its one-node-per-tree lock), `lm archive` and `lm open --rc` on the command line."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lightmind import cli, node, remote, runs, settings
from lightmind.files import Home
from test_node import card, chat, home, switch_on, tree      # noqa: F401 - the short tree: a unix socket path is limited
from test_session_lock import CHILD, finished_pid, other_process


def lm(tree: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """`lm --home <tree> <args>` as its own process; its stdin is `stdin` (closed at once when None)."""
    return subprocess.run([sys.executable, "-m", "lightmind.cli", "--home", str(tree), *args], input=stdin or b"",
                          capture_output=True, timeout=120)


def run_main(tree: Path, capsys, *args: str) -> tuple[int, str, str]:
    code = cli.main(["--home", str(tree), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


# ------------------------------------------------------------------------------------------------ lm node targets
def by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def test_targets_for_the_servers_of_a_tree(tree, monkeypatch):
    monkeypatch.setenv("LM_MACHINE", "testbox")                       # a card whose `machine` is this instance is local
    home = Home(tree)
    card(home, "homeserver", "machine: testbox\nhost: 10.0.0.1\nlm_home: ~/lightmind\nrole: [always-on]")
    card(home, "buildbox", "machine: buildbox\nhost: 10.0.0.2\nssh: buildbox\nlm_home: ~/lightmind\nremote: true")
    card(home, "gpubox", "machine: gpubox\nuser: user\nhost: gpubox.lan\nport: 2222\nlm_home: /srv/lm")
    card(home, "laptop2", "machine: laptop2\nlm_home: C:/Users/user/lightmind\nremote: true")
    card(home, "plain", "machine: plain\nhost: plain.lan\nuser: user")
    settings.save(home, home.config_file, {("remote", "default_node"): "buildbox"})
    rows = by_name(node.targets(Home(tree)))
    assert "plain" not in rows and "_example" not in rows                       # no lm_home, no tree: no node
    homeserver, buildbox, gpubox, uplinker = rows["homeserver"], rows["buildbox"], rows["gpubox"], rows["laptop2"]
    assert homeserver["transport"] == "local" and homeserver["enabled"] is False and homeserver["default"] is False
    assert homeserver["argv"] == [str(tree / "system" / "lm"), "node", "--stdio"] and homeserver["role"] == ["always-on"]
    assert buildbox["transport"] == "ssh" and buildbox["enabled"] is True and buildbox["default"] is True
    assert buildbox["argv"] == ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20", "buildbox",
                                  "~/lightmind/system/lm node --stdio"]
    assert gpubox["enabled"] is False and gpubox["argv"][-4:] == ["-p", "2222", "user@gpubox.lan", "/srv/lm/system/lm node --stdio"]
    assert uplinker["transport"] == "uplink" and uplinker["argv"] == [] and uplinker["enabled"] is True
    assert all(set(r) >= {"name", "machine", "enabled", "default", "transport", "argv", "role"} for r in rows.values())


def test_targets_json_on_the_command_line_is_one_list(tree, capsys):
    card(Home(tree), "buildbox", "host: 10.0.0.2\nssh: buildbox\nlm_home: ~/lightmind\nremote: true")
    code, out, _ = run_main(tree, capsys, "node", "targets", "--json")
    rows = json.loads(out)
    assert code == 0 and isinstance(rows, list) and by_name(rows)["buildbox"]["enabled"] is True
    code, out, _ = run_main(tree, capsys, "node", "targets")
    assert code == 0 and "buildbox" in out and "ssh" in out and "on" in out


def test_a_server_with_several_aliases_is_probed_only_when_it_is_switched_on(tree, monkeypatch):
    home = Home(tree)
    card(home, "multi", "host: 10.0.0.3\nssh: [first, second]\nlm_home: ~/lightmind\nremote: true")
    card(home, "multioff", "host: 10.0.0.4\nssh: [first, second]\nlm_home: ~/lightmind")
    probed = []
    monkeypatch.setattr(remote, "reachable", lambda dest: probed.append(dest) or dest == ["second"])
    rows = by_name(node.targets(Home(tree)))
    assert rows["multi"]["argv"][-2:] == ["second", "~/lightmind/system/lm node --stdio"] and probed == [["first"], ["second"]]
    assert rows["multioff"]["argv"][-2:] == ["first", "~/lightmind/system/lm node --stdio"]      # off: no network call for it
    monkeypatch.setattr(remote, "reachable", lambda dest: False)
    rows = by_name(node.targets(Home(tree)))
    assert rows["multi"]["argv"] == [] and "reachable" in rows["multi"]["note"]


def test_an_unsafe_ssh_alias_gives_no_command_line_and_says_why(tree):
    card(Home(tree), "evil", "host: 10.0.0.5\nssh: \"x; rm -rf /\"\nlm_home: ~/lightmind\nremote: true")
    row = by_name(node.targets(Home(tree)))["evil"]
    assert row["argv"] == [] and "plain ssh alias" in row["note"]


# ------------------------------------------------------------------------------------------------ lm node --stdio
def test_the_node_says_hello_first_answers_a_ping_and_prints_nothing_else_on_stdout(tree):
    switch_on(Home(tree))
    done = lm(tree, "node", "--stdio", stdin=b'{"type":"ping","id":"p1"}\n{"type":"sessions.list","id":"l1"}\n')
    assert done.returncode == 0, done.stderr.decode()
    lines = [json.loads(line) for line in done.stdout.decode().splitlines()]
    assert lines[0]["type"] == "node.hello" and lines[0]["version"] == 1 and lines[0]["tree"] == str(tree)
    assert {"node", "machine", "lm", "platform", "can_start"} <= set(lines[0])
    assert {"type": "pong", "id": "p1"} in lines
    assert any(l.get("type") == "result" and l["id"] == "l1" and l["ok"] is True and l["data"] == {"sessions": []} for l in lines)
    assert not (tree / "system" / "state" / "remote" / "node.lock").exists()               # gone with the process


def test_without_remote_enabled_the_stdio_node_answers_a_request_with_the_reason(tree):
    done = lm(tree, "node", "--stdio", stdin=b'{"type":"sessions.list","id":"l1"}\n')
    answer = [json.loads(l) for l in done.stdout.decode().splitlines()][-1]
    assert answer["ok"] is False and "remote.enabled" in answer["error"]


def wait_for(found, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if found():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_second_node_of_the_same_tree_exits_3_and_the_first_goes_on(tree):
    lock = tree / "system" / "state" / "remote" / "node.lock"
    first = subprocess.Popen([sys.executable, "-m", "lightmind.cli", "--home", str(tree), "node", "--stdio"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert json.loads(first.stdout.readline())["type"] == "node.hello"
        wait_for(lock.exists)
        second = lm(tree, "node", "--stdio")
        assert second.returncode == 3 and b"another node is running" in second.stderr and second.stdout == b""
        assert first.poll() is None and lock.exists()
    finally:
        first.stdin.close()
        assert first.wait(30) == 0
        first.stdout.close()
        first.stderr.close()
    assert not lock.exists()


def test_the_lock_of_a_dead_node_is_taken_over(tree):
    lock = tree / "system" / "state" / "remote" / "node.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{finished_pid()} 123\n", "utf-8")
    done = lm(tree, "node", "--stdio")
    assert done.returncode == 0 and json.loads(done.stdout.splitlines()[0])["type"] == "node.hello"
    with other_process(CHILD, str(tree), "x") as (child, _):
        lock.write_text(f"{child.pid} {runs.process_stamp(child.pid)}\n", "utf-8")
        assert lm(tree, "node", "--stdio").returncode == 3                                  # alive: not taken over
        assert lock.exists()


def test_lm_node_needs_exactly_one_mode(tree, capsys):
    for args in (("node",), ("node", "--stdio", "--uplink"), ("node", "targets", "--stdio")):
        code, _, err = run_main(tree, capsys, *args)
        assert code == 2 and "exactly one" in err


def test_the_relay_without_a_gateway_socket_says_so_and_exits_1(tree):
    done = lm(tree, "node", "--relay", "laptop")
    assert done.returncode == 1 and b"gateway is not listening" in done.stderr


# ------------------------------------------------------------------------------------------------ lm archive
def test_archive_list_and_restore_on_the_command_line(tree, capsys):
    home = Home(tree)
    chat(home, "20261023-100000-chat-aaaa", text="the milk")
    chat(home, "20261023-110000-chat-bbbb", text="the eggs")
    runs.claim_name(home, "eggs", "20261023-110000-chat-bbbb")
    code, out, _ = run_main(tree, capsys, "archive", "20261023-1000")                      # a unique prefix is enough
    assert code == 0 and "20261023-100000-chat-aaaa: archived" in out
    code, out, _ = run_main(tree, capsys, "archive", "eggs")                                # a session name too
    assert code == 0 and "archived" in out
    code, out, _ = run_main(tree, capsys, "archive", "list", "--json")
    rows = json.loads(out)
    assert code == 0 and {r["id"] for r in rows} == {"20261023-100000-chat-aaaa", "20261023-110000-chat-bbbb"}
    assert {r["archived_by"] for r in rows} == {"cli"} and all(r["archived_at"] for r in rows)
    assert {r["name"]: r for r in rows}["eggs"]["title"] == "the eggs"
    assert [r.id for r in runs.list_sessions(home)] == []
    code, out, _ = run_main(tree, capsys, "archive", "restore", "eggs")
    assert code == 0 and "restored" in out
    assert [r.id for r in runs.list_sessions(home)] == ["20261023-110000-chat-bbbb"]
    code, out, _ = run_main(tree, capsys, "archive", "restore", "eggs")
    assert code == 0 and "not archived" in out
    code, out, _ = run_main(tree, capsys, "archive", "list")
    assert code == 0 and "20261023-100000-chat-aaaa" in out and "bbbb" not in out


def test_archive_says_what_is_wrong_with_the_reference(tree, capsys):
    home = Home(tree)
    chat(home, "20261023-100000-chat-aaaa")
    chat(home, "20261023-100500-chat-aaab")
    for args, why in ((("archive", "nothing-like-this"), "no chat or session name"), (("archive", "20261023-10"), "matches 2 runs"),
                      (("archive", "list", "extra"), "lm archive <chat>"), (("archive", "restore"), "lm archive <chat>"),
                      (("archive", "restore", "a", "b"), "lm archive <chat>")):
        code, _, err = run_main(tree, capsys, *args)
        assert code == 2 and why in err, args
    assert runs.list_sessions(home, archived=True) == []


def test_archive_of_a_chat_another_process_holds_fails_and_marks_nothing(tree, capsys, monkeypatch):
    monkeypatch.setattr(runs, "request_stop", lambda *a, **k: "asked")
    home = Home(tree)
    chat(home, "c1", status="running")
    with other_process(CHILD, str(tree), "c1"):
        code, _, err = run_main(tree, capsys, "archive", "c1")
    assert code == 2 and "still running in process" in err and not runs.is_archived(tree / "runs" / "c1")


# ------------------------------------------------------------------------------------------------ lm open --rc
def spec_for(tree, monkeypatch, *args: str) -> dict:
    monkeypatch.setattr(cli, "_person_only", lambda what: None)
    return cli._open_spec(Home(tree), cli.build_parser().parse_args(["--home", str(tree), "open", *args]))


def test_open_rc_puts_the_name_into_the_spec_for_an_id_and_for_new(tree, monkeypatch):
    chat(Home(tree), "20261023-100000-chat-aaaa")
    assert spec_for(tree, monkeypatch, "20261023-1000", "--rc", "phone-1") == {"id": "20261023-100000-chat-aaaa", "rc": "phone-1"}
    assert spec_for(tree, monkeypatch, "20261023-1000", "--rc") == {"id": "20261023-100000-chat-aaaa", "rc": ""}      # no name: the title
    assert spec_for(tree, monkeypatch, "20261023-1000") == {"id": "20261023-100000-chat-aaaa"}                       # no --rc: no key
    assert spec_for(tree, monkeypatch, "new", "--rc", "phone-2", "-n", "milk") == {"new": {"agent": "", "project": "", "name": "milk", "name_fixed": True}, "rc": "phone-2"}


def test_open_rc_refuses_a_name_that_could_be_taken_for_an_option_or_a_command(tree, monkeypatch):
    chat(Home(tree), "c1")
    for bad in ("two words", "-x", "a;b", "$(x)", "../x", ".dot"):
        with pytest.raises(cli.Usage, match="--rc"):
            spec_for(tree, monkeypatch, "c1", f"--rc={bad}")


# ------------------------------------------------------------------------------------------------ lm node --uplink through --relay
@pytest.mark.skipif(sys.platform == "win32", reason="unix socket and a shell script as ssh")
def test_the_uplink_talks_to_the_gateway_socket_through_the_relay_and_ends_with_it(tree, tmp_path_factory):
    home = Home(tree)
    card(home, "homeserver", "host: 10.0.0.1\nssh: homeserver\nlm_home: ~/lightmind")
    switch_on(home, gateway="homeserver")
    bin_dir = tmp_path_factory.mktemp("bin")
    fake_ssh = bin_dir / "ssh"                                    # what ssh would start on the gateway machine: the relay
    fake_ssh.write_text('#!/bin/sh\nexec "$LM_TEST_PY" -m lightmind.cli --home "$LM_TEST_TREE" node --relay laptop\n', "utf-8")
    fake_ssh.chmod(fake_ssh.stat().st_mode | stat.S_IEXEC)
    sock_path = tree / "system" / "state" / "remote" / "gateway.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    server.settimeout(60)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "LM_TEST_PY": sys.executable, "LM_TEST_TREE": str(tree)}
    uplink = subprocess.Popen([sys.executable, "-m", "lightmind.cli", "--home", str(tree), "node", "--uplink"], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        connection, _ = server.accept()
        connection.settimeout(30)                                 # a hang fails the test in half a minute, not when the node gives up
        lines = connection.makefile("rwb")
        hello = json.loads(lines.readline())
        assert hello["type"] == "node.hello" and hello["version"] == 1                       # the node's line crossed the relay
        lines.write(b'{"type":"ping","id":"p9"}\n')
        lines.flush()
        assert json.loads(lines.readline()) == {"type": "pong", "id": "p9"}                  # and the gateway's request came back
        lines.close()
        connection.close()
        assert uplink.wait(60) == 0, uplink.stderr.read().decode()
    finally:
        if uplink.poll() is None:
            uplink.kill()
        uplink.stdout.close()
        uplink.stderr.close()
        server.close()
    assert not (tree / "system" / "state" / "remote" / "node.lock").exists()


def test_the_uplink_without_a_gateway_card_says_so_and_leaves_no_lock(tree):
    done = lm(tree, "node", "--uplink")
    assert done.returncode == 1 and b"lm node --uplink" in done.stderr
    assert not (tree / "system" / "state" / "remote" / "node.lock").exists()
