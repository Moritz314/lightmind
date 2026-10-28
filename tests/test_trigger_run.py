"""`run:` triggers: a trigger that runs an `lm` subcommand (sync, check) and no agent, so the server can pull and push every
15 minutes with nobody in a session.  The pure parts of tools/triggers/apply.py; no scheduler is touched."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from conftest import templates_dir

pytest.importorskip("yaml")


def _load():
    path = templates_dir() / "tools" / "triggers" / "apply.py"
    spec = importlib.util.spec_from_file_location("apply_run_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply = _load()


def _write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


SYNC = '---\nmachine: homeserver\nschedule: "*/15 * * * *"\nrun: sync\n---\n'


def test_a_run_trigger_needs_no_agent_and_no_task(tmp_path):
    _write(tmp_path, "sync.md", SYNC)
    (t,) = apply.load_triggers(tmp_path)
    assert t["run"] == "sync" and t["enabled"] and not t["error"]
    assert t["machine"] == "homeserver" and t["schedule"] == "*/15 * * * *" and t["agent"] == "" and t["task"] == ""


def test_run_may_only_be_sync_or_check_and_never_a_command_line(tmp_path):
    ok = {"sync", "check"}
    assert set(apply.RUN_ALLOWED) == ok
    for i, run in enumerate(["rm", "sync --push", "run", "secret", "sync && id", "$(id)", "`id`", "sync;id", "../sync", "SYNC", "[sync]"]):
        _write(tmp_path, f"t{i}.md", f'---\nschedule: "*/15 * * * *"\nrun: "{run}"\n---\n')
    _write(tmp_path, "tl.md", '---\nschedule: "*/15 * * * *"\nrun: [sync]\n---\n')
    _write(tmp_path, "empty.md", '---\nschedule: "*/15 * * * *"\nrun: ""\n---\n')
    for t in apply.load_triggers(tmp_path):
        assert t["error"], t["name"]
    for i, run in enumerate(ok):
        _write(tmp_path / "good", f"g{i}.md", f'---\nschedule: "0 7 * * *"\nrun: {run}\n---\n')
    assert not any(t["error"] for t in apply.load_triggers(tmp_path / "good"))


def test_run_replaces_agent_and_task_it_does_not_come_with_them(tmp_path):
    _write(tmp_path, "both.md", '---\nschedule: "0 7 * * *"\nrun: sync\nagent: ag\ntask: t\n---\n')
    _write(tmp_path, "agent.md", '---\nschedule: "0 7 * * *"\nrun: sync\nagent: ag\n---\n')
    _write(tmp_path, "task.md", '---\nschedule: "0 7 * * *"\nrun: sync\ntask: t\n---\n')
    _write(tmp_path, "neither.md", '---\nschedule: "0 7 * * *"\n---\n')
    ts = {t["name"]: t for t in apply.load_triggers(tmp_path)}
    assert all(t["error"] for t in ts.values())
    assert "run" in ts["both"]["error"] and "agent" in ts["neither"]["error"]


def test_a_run_trigger_is_validated_like_the_others_and_cannot_smuggle_crontab_lines(tmp_path):
    _write(tmp_path, "a.md", '---\nname: "x\\n* * * * * rm -rf ~"\nschedule: "0 7 * * *"\nrun: sync\n---\n')
    _write(tmp_path, "b.md", '---\nschedule: "0 7 * * * evil"\nrun: sync\n---\n')
    _write(tmp_path, "c.md", '---\nschedule: "@reboot x y z w"\nrun: sync\n---\n')
    _write(tmp_path, "d.md", '---\nschedule: "0 7 * * *"\nrun: sync\nname: "bad name"\n---\n')
    _write(tmp_path, "e.md", '---\nrun: sync\n---\n')
    ts = apply.load_triggers(tmp_path)
    assert len(ts) == 5 and all(t["error"] for t in ts)
    assert apply.render_block(tmp_path, [t for t in ts if not t["error"]]).count("\n* * * * *") == 0


def test_the_command_is_lm_sync_without_an_agent(tmp_path):
    t = {"name": "sync", "schedule": "*/15 * * * *", "agent": "", "task": "", "run": "sync"}
    assert apply.lm_command(tmp_path, t) == [str(tmp_path / "system" / "lm"), "sync"]
    assert apply.lm_command(tmp_path, {**t, "run": "check"}, True) == [str(tmp_path / "system" / "lm.cmd"), "check"]
    old = {"name": "n", "schedule": "0 7 * * *", "agent": "ag", "task": "t"}                  # triggers without the field are unchanged
    assert apply.lm_command(tmp_path, old)[1:] == ["run", "--detach", "-a", "ag", "t"]


def test_the_crontab_line_and_the_windows_task_run_lm_sync(tmp_path):
    t = {"name": "sync", "schedule": "*/15 * * * *", "agent": "", "task": "", "run": "sync"}
    lines = apply.render_block(tmp_path, [t]).splitlines()
    assert lines[1] == "# sync" and lines[2].startswith("*/15 * * * * ") and lines[2].endswith(" sync") and "--detach" not in lines[2]
    a = apply.schtasks_args(tmp_path, t)
    assert a[:5] == ["schtasks", "/Create", "/F", "/TN", "Lightmind sync"] and a[5] == "/TR"
    assert a[6].endswith("lm.cmd sync") and "--detach" not in a[6] and a[7:] == ["/SC", "MINUTE", "/MO", "15"]


def test_a_sync_trigger_is_installed_where_its_machine_is_named_and_needs_a_named_instance(tmp_path):
    _write(tmp_path, "sync.md", SYNC)
    _write(tmp_path, "check.md", '---\nschedule: "0 6 * * *"\nrun: check\n---\n')
    ts = {t["name"]: t for t in apply.load_triggers(tmp_path)}
    assert apply.here(ts["sync"], "homeserver", False) and apply.here(ts["sync"], "HomeServer", False)
    assert not apply.here(ts["sync"], "laptop", True)                                         # `machine:` decides, not always_on
    assert not apply.here(ts["sync"], "", True)                                               # no instance name: no sync repo, nothing to sync
    assert apply.here(ts["check"], "", True) is True and not apply.here(ts["check"], "laptop", False)


def _stub_scheduler(monkeypatch):
    """No crontab, no schtasks: reading finds nothing, nothing can be written."""
    done = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(apply, "_run", lambda cmd, **kw: done)
    monkeypatch.setattr(apply, "read_crontab", lambda: "")
    monkeypatch.setattr(apply, "installed_windows", lambda: [])
    monkeypatch.setattr(apply, "write_crontab", lambda text: pytest.fail("a dry run wrote the crontab"))


def test_dry_run_on_homeserver_shows_the_sync_line_and_on_the_laptop_nothing(tmp_path, monkeypatch, capsys):
    _stub_scheduler(monkeypatch)
    _write(tmp_path / "triggers", "sync.md", SYNC)
    state = tmp_path / "system" / "state"
    state.mkdir(parents=True)
    monkeypatch.delenv("LM_MACHINE", raising=False)
    (state / "machine").write_text("homeserver\n", encoding="utf-8")
    assert apply.main(["--tree", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "sync" in out and ("MINUTE" in out and "/MO 15" in out if os.name == "nt" else "*/15 * * * * " in out)
    (state / "machine").write_text("laptop\n", encoding="utf-8")
    assert apply.main(["--tree", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "MINUTE" not in out and "*/15" not in out


def test_list_names_the_run_command(tmp_path, monkeypatch, capsys):
    _stub_scheduler(monkeypatch)
    _write(tmp_path / "triggers", "sync.md", SYNC)
    monkeypatch.delenv("LM_MACHINE", raising=False)
    assert apply.main(["--tree", str(tmp_path), "--list"]) == 0
    assert "run=sync" in capsys.readouterr().out
